# Security Audit

---

## CRITICAL Findings

### 1. Dashboard Has No Authentication
**File:** `routes/dashboard.py:98`  
**Severity:** CRITICAL  

```python
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(shop: str = Query(...)) -> HTMLResponse:
    merchant = await conn.fetchrow(
        "SELECT shop_domain, installed_at, slack_webhook_url, alert_email FROM merchants WHERE ..."
    )
```

Anyone who knows a shop's Shopify domain (which is often publicly visible in store URLs) can visit:
```
https://checkoutguardalerts.com/dashboard?shop=victim-store.myshopify.com
```

And receive:
- The merchant's Slack Incoming Webhook URL — allows the attacker to send arbitrary messages to the merchant's Slack workspace
- Full incident history (sensitive business intelligence)
- Checkout and order counts (competitive intelligence)
- Alert email address

**The Slack webhook URL exposure is the most severe consequence.** Slack Incoming Webhook URLs are bearer tokens — anyone with the URL can POST messages to that channel. An attacker could send fraudulent "CheckoutGuard" alerts to panic merchants or send spam.

**Fix options:**
1. Session cookie (set at OAuth callback via `fastapi.responses.Response.set_cookie`).
2. Signed URL token: `HMAC(shop_domain + timestamp, SECRET_KEY)` in the redirect from callback.
3. Full Shopify session token (requires embedded App Bridge migration).

---

## HIGH Findings

### 2. XSS in Server-Rendered HTML — Multiple Pages
**Files:** `routes/onboarding.py:59,64`, `routes/dashboard.py:258`, `routes/billing.py:168,181,193`  
**Severity:** HIGH

The `shop` query/form parameter is rendered directly into HTML output without escaping:

```python
# onboarding.py:59
html = f"""...<span class="shop">{shop}</span>..."""

# onboarding.py:64
html = f"""...<input type="hidden" name="shop" value="{shop}" />"""

# dashboard.py:258
return f"""...<span class="shop">{shop}</span>..."""

# billing.py:168
f"CheckoutGuard is now monitoring <strong>{shop}</strong>"
```

**Exploitability:** The `shop` parameter is set by Shopify during OAuth (legitimate path) and by user input via URL parameters (potential injection path). While Shopify's OAuth redirect uses real `.myshopify.com` domains, the billing and onboarding routes accept arbitrary `?shop=` values from the browser. A crafted URL with `shop="><script>alert(1)</script>` could execute JavaScript in a victim's browser if they click a malicious link.

**Fix:** Apply `html.escape(shop)` on all template insertions:
```python
from html import escape
html = f"""...<span class="shop">{escape(shop)}</span>..."""
```

### 3. In-Memory Nonce Store — Process Restart Breaks OAuth
**File:** `routes/auth.py:32`  
**Severity:** HIGH (Operational)

```python
_pending_nonces: set[str] = set()
```

If uvicorn restarts between the install redirect and the callback (server restart, OOM kill, deploy), the nonce is lost. The callback returns HTTP 400 "Invalid state nonce" and the merchant's install fails. They must restart the install from scratch.

Under multi-process or multi-instance deployments (Railway auto-scaling, Docker Compose with `replicas: 2`), the nonce generated on instance A is not visible to instance B — install callbacks routed to B always fail.

**Fix:** Redis SETNX with 10-minute TTL per nonce.

### 4. TEST_MODE=True Hardcoded in Billing
**File:** `routes/billing.py:27`  
**Severity:** HIGH (Business)

```python
_TEST_MODE = True  # Set to False before going live with real billing
```

This flag is hardcoded True. Test charges are free Shopify test charges — they never result in real money. If the app goes live with this set, all merchants approve the plan but are never billed. Revenue is zero.

**Fix:** `TEST_MODE = os.getenv("BILLING_TEST_MODE", "false").lower() == "true"` — gate on environment variable, default False.

### 5. OOS Detection Completely Broken
**File:** `routes/webhooks.py:225-234`, `services/detector.py:726-730`  
**Severity:** HIGH (Feature)

The inventory webhook handler never populates `product_id` in `inventory_levels`. The OOS detector always gets NULL for product_id and returns early. Feature is marketed/documented as functional but does nothing.

### 6. Payment Failure Incidents Never Auto-Resolve
**File:** `services/detector.py:452-519`  
**Severity:** HIGH (Data Integrity)

`_check_payment_failures` creates incidents but has no code path to resolve them. Once a payment failure incident is opened, it remains in the `active_incidents` list in the dashboard forever (or until the merchant is deleted). The alert fires once and the incident sticks.

---

## MEDIUM Findings

### 7. CSRF on POST /onboarding
**File:** `routes/onboarding.py:90-104`  
**Severity:** MEDIUM

```python
@router.post("/onboarding")
async def onboarding_save(
    shop: str = Form(...),
    slack_webhook_url: str = Form(...),
    ...
)
```

No CSRF token. An attacker can create a page with a form that auto-submits to `/onboarding`:
```html
<form action="https://checkoutguardalerts.com/onboarding" method="POST">
  <input name="shop" value="victim.myshopify.com">
  <input name="slack_webhook_url" value="https://attacker.slack.webhook/...">
</form>
```

If the merchant is tricked into visiting this page (while their browser has any session with the app), their Slack webhook is replaced with the attacker's.

**Practical exploitability:** There's no session cookie currently, so the CSRF changes the DB for any known shop — no session needed. This is straightforward if the attacker knows the shop domain.

**Fix:** CSRF token generated at GET /onboarding, verified at POST /onboarding. Or session-token validation.

### 8. Token Refresh Race Condition
**File:** `services/token_manager.py:23-76`  
**Severity:** MEDIUM

Two concurrent callers (payment check + proactive loop) on the same shop near token expiry both call `get_valid_token`. Both see the token as expiring (check is non-atomic), both call Shopify's token endpoint. Shopify invalidates the old refresh token after the first exchange. The second call fails with an invalid refresh token error, leaving the shop with no valid token.

**Fix:** asyncio.Lock per shop:
```python
_refresh_locks: dict[str, asyncio.Lock] = {}
async def _get_refresh_lock(shop: str) -> asyncio.Lock:
    return _refresh_locks.setdefault(shop, asyncio.Lock())
```

### 9. Content-Length Bypass on /events
**File:** `routes/events.py:63-65`  
**Severity:** MEDIUM

```python
content_length = int(request.headers.get("content-length", 0))
if content_length > _MAX_PAYLOAD_BYTES:
    raise HTTPException(status_code=413, ...)
```

Attacker sends `Content-Length: 0` header with a 10MB body — check passes, full body is read. Creates DoS vector (memory exhaustion on large payloads).

**Fix:**
```python
body = await request.body()
if len(body) > _MAX_PAYLOAD_BYTES:
    raise HTTPException(status_code=413, detail="Payload too large")
```

### 10. Exception Swallowing in Background Loops
**File:** `main.py:24-28`, `main.py:36-48`  
**Severity:** MEDIUM (Operational)

```python
try:
    await run_proactive_checks_all_merchants()
except Exception as exc:
    logger.error("Proactive monitor loop error: %s", exc)
    # loops continue
```

If the DB is down, this logs an error every 5 minutes but never escalates. The app appears "healthy" while silently failing. No Telegram alert, no circuit breaker.

**Fix:** Track consecutive failures; after N failures send a Telegram alert.

---

## LOW Findings

### 11. Secrets Not Encrypted at Rest
**File:** `routes/auth.py`, `migrations/schema.sql`  
**Severity:** LOW (Compliance)

`access_token`, `refresh_token`, `slack_webhook_url` stored as plaintext TEXT. Privacy policy falsely claims "stored encrypted." At DB level, full-disk encryption (handled by VPS provider or managed DB) may cover this, but application-level encryption is stronger.

**Fix:** Use pgcrypto `pgp_sym_encrypt` or store a per-merchant AES-GCM key in a secrets manager.

### 12. No `shop_domain` Format Validation at Webhook Entry
**File:** `routes/webhooks.py`  
**Severity:** LOW

`x_shopify_shop_domain` header is trusted from HMAC-verified Shopify requests. This is correct — if HMAC passes, the request is from Shopify and the domain is legitimate. No additional validation needed.

### 13. Rate Limiter Memory Leak
**File:** `routes/events.py:28`  
**Severity:** LOW

`_rate_windows` dict never shrinks. For current scale (1 active merchant), this is negligible. At 1,000 merchants this could be ~100KB — still trivial.

### 14. Billing Routes: No Auth on Sensitive Actions
**File:** `routes/billing.py`  
**Severity:** LOW

`/billing/start?shop=X` can be triggered for any known shop — creating a billing charge on their behalf. However, this requires Shopify to confirm the charge in a real browser session for the merchant's Shopify account, so the risk of unauthorized charge creation is very low.

---

## .env File Status

`.env` exists locally at `/Users/openclaw/shopify-checkout-guard/.env` and contains real credentials. It is correctly listed in `.gitignore`. No trace of `.env` being committed to git history was found.

**Status: SAFE** — `.env` has never been committed. ✅

---

## Dependency Security

`requirements.txt` versions:
```
fastapi==0.111.0       (2024, not latest — 0.115+ available)
uvicorn[standard]==0.29.0  (current)
asyncpg==0.29.0         (current)
httpx==0.27.0           (current)
cryptography==42.0.8    (not used in app code — dead dependency)
pydantic==2.7.1
pydantic-settings==2.3.0
python-multipart==0.0.9
```

**Notes:**
- `cryptography==42.0.8` is in requirements but never imported in any source file. Dead dependency, adds attack surface. Remove.
- `fastapi==0.111.0` — no known critical CVEs at 0.111. Low urgency to upgrade.
- No `pip-audit` or `safety` check found in CI.

---

## SQL Injection

All DB queries use asyncpg parameterized queries (`$1`, `$2`, etc.). No string interpolation into SQL. **No SQL injection vulnerabilities found.** ✅

---

## Summary Table

| Finding | Severity | File:Line |
|---|---|---|
| Dashboard no auth — exposes Slack URL | CRITICAL | dashboard.py:98 |
| XSS in server-rendered templates | HIGH | onboarding.py:59, dashboard.py:258, billing.py:168+ |
| In-memory nonce store | HIGH | auth.py:32 |
| TEST_MODE=True hardcoded | HIGH | billing.py:27 |
| OOS detection broken (product_id null) | HIGH | webhooks.py:225, detector.py:726 |
| Payment failure incidents never resolve | HIGH | detector.py:452 |
| CSRF on POST /onboarding | MEDIUM | onboarding.py:90 |
| Token refresh race condition | MEDIUM | token_manager.py:46 |
| Content-Length bypass on /events | MEDIUM | events.py:63 |
| Background loop silent failure | MEDIUM | main.py:24,36 |
| Tokens/secrets plaintext in DB | LOW | schema.sql |
| Rate limiter memory leak | LOW | events.py:28 |
| `cryptography` dead dependency | LOW | requirements.txt |
| Privacy policy false claims | MEDIUM | onboarding.py:privacy_policy |
