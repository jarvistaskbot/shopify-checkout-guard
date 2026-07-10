# API Reference — All Endpoints

---

## 1. Health

### GET / and GET /health
**Auth:** None  
**Response:** `{"status": "ok", "service": "CheckoutGuard"}`  
**Rate limit:** None  
**Notes:** Used by Railway healthcheck (`railway.json`). Always returns 200 as long as the process is running (does not verify DB connectivity).

---

## 2. Auth Routes (`/auth`)

### GET /auth/shopify
**Auth:** None  
**Query params:** `shop` (required) — Shopify store domain  
**Response:** 302 redirect to Shopify OAuth authorize URL  
**Side effects:** Generates and stores nonce in `_pending_nonces`  
**Errors:** None documented (silently generates redirect even for invalid shop values)

### GET /auth/callback
**Auth:** None (HMAC-verified from Shopify)  
**Query params:**
- `shop` (required)
- `code` (required) — OAuth authorization code
- `state` (required) — nonce from install redirect
- `hmac` (required) — Shopify HMAC signature
**Response:** 302 redirect to `/onboarding?shop=X` or `/dashboard?shop=X`  
**Side effects:**
- Inserts/updates merchants row with access_token
- Creates tasks: `_subscribe_webhooks`, `_fetch_and_store_aov`
**Errors:**
- `400 Bad Request` — invalid or unknown nonce
- `403 Forbidden` — HMAC verification failed
- `500` — token exchange failure (Shopify API error)

---

## 3. Webhook Routes (`/webhooks`)

All require `X-Shopify-Hmac-Sha256` header. All return `{"ok": True}` on success.

### POST /webhooks/orders/create
**Auth:** HMAC-SHA256 (SHOPIFY_API_SECRET)  
**Request body:** Shopify `orders/create` webhook JSON  
**Side effects:** INSERT checkout_events (order_created), INSERT order_line_items (per line item), trigger detection  
**Errors:** `401` on HMAC failure

### POST /webhooks/checkouts/create
**Auth:** HMAC-SHA256  
**Request body:** Shopify `checkouts/create` webhook JSON  
**Side effects:** INSERT checkout_events (checkout_created), trigger detection  
**Errors:** `401` on HMAC failure

### POST /webhooks/checkouts/delete
**Auth:** HMAC-SHA256  
**Request body:** Shopify `checkouts/delete` webhook JSON  
**Side effects:** INSERT checkout_events (checkout_deleted), trigger detection  
**Errors:** `401` on HMAC failure

### POST /webhooks/app/uninstalled
**Auth:** HMAC-SHA256  
**Request body:** Shopify `app/uninstalled` webhook JSON  
**Side effects:** UPDATE merchants SET active=FALSE  
**Errors:** `401` on HMAC failure

### POST /webhooks/customers/data_request
**Auth:** HMAC-SHA256 (no `X-Shopify-Shop-Domain` required)  
**Request body:** Shopify GDPR data request JSON  
**Side effects:** None (no PII stored)  
**Response:** `{"ok": True}`

### POST /webhooks/customers/redact
**Auth:** HMAC-SHA256  
**Request body:** Shopify GDPR redact JSON  
**Side effects:** None (no PII stored)  
**Response:** `{"ok": True}`

### POST /webhooks/shop/redact
**Auth:** HMAC-SHA256  
**Request body:** Shopify GDPR shop redact JSON  
**Side effects:** DELETE FROM merchants WHERE shop_domain=$1 AND active=FALSE (cascades)  
**Errors:** `401` on HMAC failure

### POST /webhooks/inventory/update
**Auth:** HMAC-SHA256  
**Request body:** Shopify `inventory_levels/update` JSON  
**Side effects:** UPSERT inventory_levels (item + available), fire-and-forget OOS check  
**Notes:** OOS check is currently non-functional (product_id not stored)  
**Errors:** `401` on HMAC failure

---

## 4. Events Route

### POST /events
**Auth:** None (public, unauthenticated)  
**Content-Type:** application/json  
**Request body:** Array of error events OR single event object:
```json
[
  {
    "shop": "store.myshopify.com",    // required
    "message": "TypeError: ...",       // required, truncated to 500 chars
    "url": "https://...",              // required, truncated to 500 chars
    "source": "theme.js",             // optional, truncated to 200 chars
    "ts": 1720000000000,              // optional, unix ms
    "lineno": 42,                     // optional, parsed but NOT stored
    "colno": 10                        // optional, parsed but NOT stored
  }
]
```
**Payload limit:** 8192 bytes (via Content-Length header — bypassable)  
**Rate limit:** 120 events/minute per shop (in-memory, per-process)  
**Max batch size:** 50 events per request  
**Response:** `{"ok": true, "accepted": N}`  
**Behavior for unknown shops:** Silently drops (returns 200, accepted=0)  
**Side effects:** INSERT js_error_events, fire-and-forget JS spike check

---

## 5. Onboarding Routes

### GET /onboarding
**Auth:** None (!) — should require Shopify session token  
**Query params:** `shop` (required)  
**Response:** HTML form (200)  
**Notes:** Renders `shop` value unescaped — XSS risk

### POST /onboarding
**Auth:** None  
**Request body:** Form-encoded:
- `shop` (required) — store domain
- `slack_webhook_url` (required) — Slack incoming webhook URL
- `alert_email` (optional) — email for alerts
**Side effects:** UPDATE merchants SET slack_webhook_url, alert_email  
**Response:** 303 redirect to `/billing/start?shop=X`  
**Notes:** No CSRF protection, no URL format validation

### GET /demo
**Auth:** None  
**Query params:** `success` (optional, "1" = show success state)  
**Response:** HTML (200) — demo onboarding flow for App Store review  
**Notes:** Does not store any data

### GET /privacy
**Auth:** None  
**Response:** HTML (200) — privacy policy page

---

## 6. Dashboard Route

### GET /dashboard
**Auth:** None (!) — CRITICAL SECURITY ISSUE  
**Query params:** `shop` (required)  
**Response:** HTML (200) — incidents dashboard with active incidents, 7-day history, stats  
**Errors:** `404` if shop not found or inactive  
**Exposure:** Returns `slack_webhook_url` rendered in page source, incident details, checkout/order counts

---

## 7. Billing Routes

### GET /billing/start
**Auth:** None (relies on shop being in DB)  
**Query params:** `shop` (required)  
**Response:** 302 redirect to Shopify billing confirmation URL  
**Side effects:** Creates Shopify recurring_application_charge, sets billing_status='pending'  
**Errors:** Redirect to `/billing/error` if Shopify API fails

### GET /billing/callback
**Auth:** None (no HMAC — Shopify redirects browser here)  
**Query params:** `charge_id` (required), `shop` (required)  
**Response:** HTML (200) — success, declined, or error page  
**Side effects:** Activates charge, updates billing_status, trial_ends_at

### GET /billing/activated
**Auth:** None  
**Query params:** `shop` (required)  
**Response:** HTML (200) — success page (partner bypass path)

### GET /billing/error
**Auth:** None  
**Query params:** `shop` (required)  
**Response:** HTML (200) — error page with retry link

---

## 8. Authentication Coverage Summary

| Route | Shopify HMAC | Session auth | Notes |
|---|---|---|---|
| POST /webhooks/* | ✅ | N/A | Correct |
| POST /events | ❌ | N/A | By design (browser JS) |
| GET /auth/* | Callback only | N/A | Correct |
| GET /onboarding | ❌ | ❌ | Should have session |
| POST /onboarding | ❌ | ❌ | CSRF + session missing |
| GET /dashboard | ❌ | ❌ | CRITICAL — auth missing |
| GET /billing/* | ❌ | ❌ | Relies on shop param |
| GET /demo | N/A | N/A | Public by design |
| GET /privacy | N/A | N/A | Public by design |

---

## 9. Response Time SLA

Shopify requires webhook endpoints to respond within 5 seconds. The detection logic is async (create_task) so webhook handlers return immediately after DB writes. DB writes on a local network (VPS + local Postgres) are typically <20ms. The critical path is:

```
POST /webhooks/orders/create:
  _verify_hmac:          ~5ms  (HMAC computation)
  INSERT checkout_events: ~10ms
  INSERT order_line_items: ~N×10ms (N=line items)
  process_event():        ~0ms (create_task only)
  Total:                 <50ms typical
```

Well within 5 second limit. ✅

---

## 10. Missing API Endpoints

| Missing | Priority | Notes |
|---|---|---|
| GET /api/incidents | HIGH | Webhook-based apps need programmatic access to incident history |
| GET/PUT /api/settings | HIGH | Allow merchants to update alert settings programmatically |
| POST /api/test-alert | MEDIUM | Allow merchants to verify their Slack/email config works |
| GET /api/metrics | MEDIUM | Current conversion rate, baseline, last alert time |
| GET /api/status | LOW | App health for Shopify status page integration |
