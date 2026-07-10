# Module: Auth — Shopify OAuth Flow

**File:** `routes/auth.py`  
**Purpose:** Handle the Shopify OAuth 2.0 install flow, store merchant tokens, and trigger post-install setup.

---

## 1. Business Value

OAuth is the entry point for every merchant. A broken or insecure install flow means zero installs or compromised tokens. This module handles:
- Generating and validating OAuth state nonces to prevent CSRF
- Exchanging Shopify authorization codes for access tokens
- Storing tokens (potentially expiring) in the database
- Firing post-install background tasks (webhook registration, AOV seeding)
- Routing new vs returning merchants to the correct next page

---

## 2. User Flow

```
/auth/shopify?shop=X
  ↓ (generate nonce, redirect to Shopify)
https://{shop}/admin/oauth/authorize?...&state={nonce}
  ↓ (Shopify redirects after merchant approves)
/auth/callback?shop=X&code=Y&state={nonce}&hmac=Z
  ↓ (verify HMAC + nonce, exchange code, store token)
  ├─→ /onboarding?shop=X  (first time — no Slack URL configured)
  └─→ /dashboard?shop=X   (returning merchant)
```

---

## 3. Function Walk-Through

### `install(shop)` — GET /auth/shopify

- Generates a 16-byte URL-safe nonce via `secrets.token_urlsafe(16)`.
- Adds nonce to `_pending_nonces` (in-memory `set[str]`).
- Constructs the Shopify OAuth authorize URL with scopes `read_orders,read_checkouts`.
- Returns a `RedirectResponse` to Shopify.

**Scopes declared here differ from shopify.app.toml:** toml requests `read_checkouts,read_orders,read_inventory,read_products` but install URL only requests `read_orders,read_checkouts`. The install URL wins at runtime; scopes in toml are for the Partner Dashboard. This discrepancy means `read_inventory` and `read_products` are NOT granted to existing installs unless merchants reinstall.

### `callback(shop, code, state, hmac_param, request)` — GET /auth/callback

1. **Nonce check:** `state not in _pending_nonces` → 400. Discards the nonce after use (prevents replay).
2. **HMAC verification:** Rebuilds `sorted_params` from all query params except `hmac`, signs with `SHOPIFY_API_SECRET`, compares with `hmac.compare_digest` (constant-time). Rejects with 403 if mismatch.
3. **Token exchange:** POST to `https://{shop}/admin/oauth/access_token` with JSON body containing code + credentials. Parses `access_token`, `refresh_token`, `expires_in`.
4. **DB upsert:** `INSERT ... ON CONFLICT DO UPDATE` — handles both new installs and reinstalls.
5. **Fire-and-forget tasks:**
   - `_subscribe_webhooks(shop, access_token)` — register webhook topics
   - `_fetch_and_store_aov(shop, access_token)` — seed AOV from last 50 paid orders
6. **Routing decision:** Checks `slack_webhook_url` in merchants table; redirects to dashboard if present, onboarding if not.

**Bug:** Uses `pool` for the upsert then immediately creates `pool2 = await get_pool()` for the routing check (lines 96/121). `pool2 is pool` — same singleton. Redundant variable, should reuse `pool` or batch into same `acquire()` block.

### `_subscribe_webhooks(shop, access_token)` — private coroutine

- Fetches existing webhooks from Shopify API to avoid duplicates.
- Registers topics: `orders/create`, `app/uninstalled`, 3 GDPR topics, `checkouts/create`, `checkouts/delete`.
- Conditionally registers `inventory_levels/update` only if `settings.oos_enabled`.
- Missing: `checkouts/create` and `checkouts/delete` are NOT declared in `shopify.app.toml [[webhooks.subscriptions]]` — they're only registered at runtime via this function. This is a discrepancy that may cause Shopify CLI to drop them in future deploys.

### `_fetch_and_store_aov(shop, access_token)` — private coroutine

- Fetches up to 50 paid orders from Shopify Admin API.
- Computes arithmetic mean of `total_price`.
- `UPDATE merchants SET avg_order_value = $1` — only updates if at least one order exists.
- New stores with no orders get the schema default of `$50.00`. This is a reasonable fallback.

---

## 4. DB Usage

- **merchants table:** UPSERT on install (shop_domain PRIMARY KEY), SELECT for routing check.
- **No transactions:** The token exchange and webhook registration are independent — if webhook registration fails, the token is already stored (good).

---

## 5. Error Handling

- HMAC failure: 403 HTTPException (explicit)
- Nonce failure: 400 HTTPException (explicit)
- Token exchange HTTP failure: `resp.raise_for_status()` — bubbles as 500 to caller
- `_subscribe_webhooks`: per-webhook failures logged but not fatal
- `_fetch_and_store_aov`: entire function wrapped in try/except; failure is `logger.warning` and silent skip

---

## 6. Security

| Issue | Severity | Detail |
|---|---|---|
| In-memory nonce store (`_pending_nonces`) | HIGH | Lost on process restart. If uvicorn restarts mid-OAuth flow, the nonce is gone and the callback returns 400. Under multi-instance (Railway, k8s), each process has an independent nonce set — nonce generated on instance A may not be present on instance B that handles the callback. |
| Nonce store unbounded | MEDIUM | `_pending_nonces` is never purged. If a merchant abandons the OAuth flow (never completes callback), the nonce stays forever. At scale this could grow large, but practically harmless for current install volume. |
| fire-and-forget tasks | MEDIUM | Both `asyncio.create_task` calls in callback (lines 116-117) have no error handling. If `_subscribe_webhooks` fails silently, the merchant installs but has no webhooks — the app effectively doesn't work and there's no alert. |
| No token encryption at rest | LOW | `access_token` and `refresh_token` stored as plaintext TEXT in DB. Privacy policy claims "stored encrypted" — this is false. Should use pgcrypto or application-level encryption. |

---

## 7. Performance

- Token exchange is a synchronous HTTP call blocking the response. Typical latency ~200-400ms.
- AOV fetch and webhook registration are fire-and-forget so they don't block the redirect.

---

## 8. Improvement Recommendations

1. Replace `_pending_nonces` with Redis SETNX with 10-min TTL — fixes multi-instance and prevents memory leak.
2. Wrap `_subscribe_webhooks` with retry logic and a Telegram/log alert on total failure.
3. Encrypt tokens at rest (pgcrypto `pgp_sym_encrypt` or application-side AES-GCM).
4. Add `checkouts/create` and `checkouts/delete` to shopify.app.toml webhooks subscriptions.
5. Consolidate the two pool.acquire() calls in `callback` into a single connection.
