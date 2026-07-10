# Module: Webhooks — Shopify Webhook Handlers

**File:** `routes/webhooks.py`  
**Purpose:** Receive, verify, and process Shopify webhook events.

---

## 1. Business Value

Webhooks are the primary data feed. Every checkout event, order, inventory change, and GDPR request arrives here. Correct HMAC verification is a Shopify App Store requirement; missing it would allow spoofed webhook injection.

---

## 2. Registered Topics

| Topic | Handler | Stores to | Triggers |
|---|---|---|---|
| `orders/create` | `order_created` | `checkout_events` + `order_line_items` | `process_event(shop, "order_created")` |
| `checkouts/create` | `checkout_created` | `checkout_events` | `process_event(shop, "checkout_created")` |
| `checkouts/delete` | `checkout_deleted` | `checkout_events` | `process_event(shop, "checkout_deleted")` |
| `app/uninstalled` | `app_uninstalled` | merchants.active=FALSE | none |
| `customers/data_request` | `customers_data_request` | none (GDPR, log only) | none |
| `customers/redact` | `customers_redact` | none (GDPR, log only) | none |
| `shop/redact` | `shop_redact` | DELETE merchants (cascade) | none |
| `inventory_levels/update` | `inventory_updated` | `inventory_levels` | `check_oos_hot_product` (fire-and-forget) |

---

## 3. Function Walk-Through

### `_verify_hmac(request, x_shopify_hmac_sha256)` — shared helper

- Reads entire request body with `await request.body()`.
- Computes `HMAC-SHA256(SHOPIFY_API_SECRET, body)`, base64-encodes.
- Compares with provided header using `hmac.compare_digest` (constant-time, prevents timing attacks).
- Raises `HTTPException(401)` on mismatch.
- Returns raw bytes body for caller to decode.

**Correct implementation.** HMAC is computed over the raw body bytes before JSON decoding — this matches Shopify's signing behavior.

### `order_created` — POST /webhooks/orders/create

1. Verify HMAC.
2. Parse JSON — extracts `id` (order_id), `checkout_token`.
3. INSERT `checkout_events` row with `event_type='order_created'`.
4. Parse `line_items` array — for each item: INSERT `order_line_items` with product_id, title, variant_id, quantity, price.
   - Individual line item failures are caught and logged without aborting the handler.
5. `await process_event(shop, "order_created")` — calls detector synchronously (but detector immediately fires a create_task).

**Design issue:** The handler does synchronous DB work before returning 200. Shopify expects a 200 response within 5 seconds; if DB is slow or locked, Shopify may retry the webhook (with exponential backoff, up to 19 retries). This creates duplicate processing. The spec called for a `webhooks_raw` table pattern (store body, return 200 immediately, process asynchronously) which was not implemented.

### `checkout_created` — POST /webhooks/checkouts/create

- Verify HMAC, parse `token` field, INSERT checkout_events.
- Calls `process_event(shop, "checkout_created")`.

### `checkout_deleted` — POST /webhooks/checkouts/delete

- Verify HMAC, parse `token` field, INSERT checkout_events with `event_type='checkout_deleted'`.
- Calls `process_event(shop, "checkout_deleted")`.

**Note:** `checkout_deleted` events are stored but the schema CHECK constraint allows this event_type. Detection logic calls `_check_abandonment_spike` when event_type is `checkout_created` OR `checkout_deleted` (detector.py:88-89).

### `app_uninstalled` — POST /webhooks/app/uninstalled

- Verify HMAC.
- `UPDATE merchants SET active = FALSE WHERE shop_domain = $1`.
- Does NOT cancel background tasks — the proactive loop will still query this merchant but will return early when `merchant["active"] == FALSE`. This is correct.
- Does NOT delete data immediately — 48h cleanup is deferred to `shop/redact`.

### `customers_data_request` / `customers_redact` — GDPR

- Verify HMAC.
- Log and return 200 — no PII is stored so no action needed.
- **Correctness note:** privacy policy says "Slack webhook URL stored encrypted" but it's stored plaintext. However, Slack webhook URLs are not PII (they don't identify individuals) so GDPR compliance here is likely still correct.

### `shop_redact` — POST /webhooks/shop/redact

- Verify HMAC.
- `DELETE FROM merchants WHERE shop_domain = $1 AND active = FALSE` — cascades to all child tables.
- Only deletes if `active = FALSE` — merchant must have been uninstalled first. This is correct behavior but could silently no-op if called before `app/uninstalled`.

### `inventory_updated` — POST /webhooks/inventory/update

- Verify HMAC.
- Parses `inventory_item_id` and `available`.
- UPSERT `inventory_levels` — updates `available` and `updated_at`.
- **CRITICAL BUG:** The UPSERT does NOT set `product_id`. The schema has a nullable `product_id BIGINT` column but the webhook payload does not include product_id directly. The detector then looks up `product_id FROM inventory_levels WHERE inventory_item_id=$2` (detector.py:726-730) — always returns NULL — and aborts OOS detection. OOS detection is completely non-functional as a result.
- Fire-and-forget `check_oos_hot_product` — the task runs but always returns early due to the NULL product_id.

---

## 4. DB Usage

All handlers acquire a pool connection, do a single INSERT/UPDATE, then release. Short-lived operations. No transactions needed for single-row operations.

`shop_redact` uses CASCADE delete via FK constraints — safe, all related rows removed atomically.

---

## 5. Spec Deviation: Inline vs Async Processing

The design spec (section 6, Architecture) specified:
```
/webhooks/{topic} → store raw to webhooks_raw, return 200 immediately
Background worker: WebhookProcessor — polls webhooks_raw every 2s, routes by topic
```

**Actual implementation:** Processes inline. The webhook handler:
1. Does DB writes (synchronous but awaited)
2. Calls detector (which fires a create_task)
3. Returns 200

**Impact assessment:** For the current scale (single merchant, low volume), inline processing is fine. The risk at scale: if detection queries are slow, the webhook handler takes >5s and Shopify retries. Duplicate events in `checkout_events` could fire duplicate incidents. A deduplication mechanism exists in the detector (`_get_active_incident` prevents duplicate incident creation) but not for the events themselves.

**Verdict:** Deviation is acceptable for v1/v2. Document it, monitor webhook response latency, revisit at 50+ merchants.

---

## 6. Missing shopify.app.toml Declarations

`shopify.app.toml` declares these topics via `[[webhooks.subscriptions]]`:
- orders/create ✅
- app/uninstalled ✅
- customers/data_request ✅ (compliance)
- customers/redact ✅ (compliance)
- shop/redact ✅ (compliance)
- inventory_levels/update ✅

**Missing from toml:**
- `checkouts/create` ❌
- `checkouts/delete` ❌

These are registered at runtime via `_subscribe_webhooks` in auth.py but not declared in the toml. If `shopify app deploy` is ever run, it will sync the toml to the Partner Dashboard — potentially deregistering these webhooks. Since the install flow calls `_subscribe_webhooks` on every reinstall, this would only affect merchants who don't reinstall. For existing merchants on VPS, it doesn't matter as long as webhooks are already registered.

---

## 7. Security

- HMAC verification: implemented correctly on all routes.
- `x_shopify_shop_domain` header: trusted from Shopify (signed with HMAC), not user-controlled input.
- Body is read once in `_verify_hmac` and returned — no double-read issues.

---

## 8. Improvement Recommendations

1. **Fix OOS product_id**: Resolve `inventory_item_id → product_id` mapping via Shopify Admin API call (`GET /admin/api/2024-10/inventory_items/{id}.json`) on first encounter, cache in `inventory_levels`.
2. **Add checkouts topics to shopify.app.toml** to prevent CLI deploy stripping them.
3. **Idempotency key**: Log raw body hash for each webhook to detect and suppress duplicates.
4. **shop_redact null-check**: Add warning log when shop_redact fires for a still-active merchant.
