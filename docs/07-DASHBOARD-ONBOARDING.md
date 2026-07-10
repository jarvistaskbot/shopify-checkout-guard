# Module: Dashboard & Onboarding

**Files:** `routes/dashboard.py`, `routes/onboarding.py`  
**Purpose:** Server-rendered HTML pages for merchant onboarding (Slack setup) and incident dashboard.

---

## 1. Architecture Note: Not Embedded

The spec (section 11) requires the admin UI to render inside Shopify admin via App Bridge (iframe). The actual implementation has `embedded = false` in `shopify.app.toml`. These are standalone web pages served at external URLs, not embedded in Shopify admin.

**Implication for App Store review:** Shopify's review guidelines require embedded apps that use App Bridge. A non-embedded app opening external windows is a common rejection reason. This is a HIGH risk for the App Store review process.

---

## 2. Onboarding Page

### GET /onboarding?shop=X

**Purpose:** Collects merchant's Slack webhook URL (and optional email) after install.

Renders a form with:
- `slack_webhook_url` (required text input)
- `alert_email` (optional text input)
- Hidden `shop` field
- Submit → `POST /onboarding`

**Security issue — XSS:** The `shop` query parameter is rendered directly into the HTML output without escaping:
```python
html = f"""...<span class="shop">{shop}</span>...<input type="hidden" name="shop" value="{shop}" />"""
```

If `shop` contains `"` it breaks the `value=` attribute. If it contains `<script>`, it injects into the page. Practical exploitability is low since Shopify signs the `shop` parameter and legitimate values are always `something.myshopify.com`. However, for defense-in-depth, all user-controlled values should be HTML-escaped with `html.escape()`.

### POST /onboarding

- Accepts `shop` (Form), `slack_webhook_url` (Form), `alert_email` (Form, optional).
- `UPDATE merchants SET slack_webhook_url=$1, alert_email=$2 WHERE shop_domain=$3`.
- Redirects 303 to `/billing/start?shop={shop}`.

**Security issue — CSRF:** No CSRF token. A malicious page could submit a form to `/onboarding` and change the Slack webhook for any known shop. Requires attacker to know the shop domain and trick the merchant into loading a malicious page. Low exploitability but technically a CSRF vulnerability.

**No validation on Slack URL format:** Any string is accepted and stored. If a merchant enters an invalid URL, alerts will silently fail (the `_post` function will get an HTTP error). Should validate `slack_webhook_url` starts with `https://hooks.slack.com/`.

---

## 3. Demo Page

### GET /demo?success=0 and GET /demo?success=1

**Purpose:** A fake onboarding flow for App Store review — Shopify reviewers don't install as real merchants, so this simulates the experience.

Two states:
- `success=0` (default): shows the same form as the real onboarding page, prefilled with placeholder shop domain.
- `success=1`: shows a "CheckoutGuard is active" success page with the "What happens next" bullet list.

**Note:** The demo form submits via `GET /demo?success=1` — it doesn't actually save anything. This is intentional for the demo flow.

**Issue:** The demo page says "every 30 minutes" and "7-day rolling baseline" and "drops >50%". The actual detection has:
- 30-min window: ✅ correct
- 7-day baseline: ✅ correct  
- ">50% drop": the detector uses `_FUNNEL_ALERT_THRESHOLD = 0.50` meaning it alerts at <50% of baseline (i.e., >50% drop). The demo text says "if volume drops >50%", which is accurate for the silence detector (`alert_threshold_pct` default 20% — but this is per-check, 30-min window, not 50%). The demo copy doesn't fully match v2's detection logic.

---

## 4. Dashboard

### GET /dashboard?shop=X

**Purpose:** Shows merchant's incident history, active incidents, and summary stats.

**CRITICAL SECURITY ISSUE — No Authentication:**
```python
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(shop: str = Query(...)) -> HTMLResponse:
    pool = await get_pool()
    async with pool.acquire() as conn:
        merchant = await conn.fetchrow(
            """SELECT shop_domain, installed_at, slack_webhook_url, alert_email
               FROM merchants WHERE shop_domain = $1 AND active = TRUE""",
            shop,
        )
        if not merchant:
            raise HTTPException(status_code=404, detail="Shop not found or not active")
```

Anyone who knows a shop's domain can visit `/dashboard?shop=that-store.myshopify.com` and see:
- The Slack webhook URL (allows sending arbitrary messages to the merchant's Slack workspace)
- Incident history (sensitive business intelligence)
- Checkout and order counts (competitive intelligence)
- Alert email address

**This is the most critical finding in the entire codebase.** The Slack webhook URL exposure is particularly severe — it allows direct message injection into the merchant's Slack workspace.

**Fix:** Require a session token or signed URL. Options:
1. Session cookie set at OAuth callback (simplest for non-embedded app).
2. Signed `?token=HMAC(shop_domain, secret)` in the URL (stateless).
3. Shopify session token if migrating to embedded App Bridge.

### Data Displayed

```python
merchant = ... # shop_domain, installed_at, slack_webhook_url, alert_email
active_incidents = ... # all unresolved incidents
recent_incidents = ... # incidents in last 7 days (limit 50)
checkout_count = ... # checkout_created events in last 7 days
order_count = ... # order_created events in last 7 days
```

### `_render(...)` — HTML generation

- Computes conversion rate: `order_count / checkout_count * 100`.
- Status banner: "Calibrating" (if < 7 days old), "Active incidents", or "All clear".
- Stats grid: 4 metrics.
- Incidents table: started, type, status (Active/Resolved), duration, estimated impact.

**XSS:** `shop` value rendered in:
- Line 258: `<p class="sub">Monitoring <span class="shop">{shop}</span></p>`

Same issue as onboarding — unescaped. Low risk since `shop` comes from a DB-verified domain, but defense-in-depth requires escaping.

**`detail` field handling:** Line 214-218 handles `detail` that may arrive as a string (JSON) rather than a parsed dict. In asyncpg, JSONB columns return as Python dicts. The `isinstance(detail, str)` branch suggests there were historical issues with how the column was returned. This defensive check is harmless but signals an earlier schema inconsistency.

### `_fmt_impact(row)` — impact formatter

- For JS error incidents: shows `count_10min` from detail.
- For OOS: shows `estimated_revenue_per_hour` from detail.
- For others: shows `estimated_revenue_loss_per_min * 60` as $/hr.

---

## 5. Privacy Policy

### GET /privacy

Static HTML page. Published at `https://checkoutguardalerts.com/privacy`.

**Correctness check against actual implementation:**

| Privacy policy claim | Reality | Match? |
|---|---|---|
| "Shopify store domain" stored | YES | ✅ |
| "Order creation timestamps and order IDs" stored | YES (checkout_events) | ✅ |
| "Slack Incoming Webhook URL stored encrypted" | STORED PLAINTEXT | ❌ |
| "No customer names, emails, or PII" | Correct — only shop-level data | ✅ |
| "Order event records retained for 30 days" | No retention job exists | ❌ |
| "Data deleted within 48 hours of uninstall" | shop/redact deletes on webhook, not on uninstall | Partial ✅ |

**Two false claims:** encryption at rest (plaintext), 30-day retention (no job). These need to be corrected before scale.

---

## 6. UX Gaps

| Gap | Impact |
|---|---|
| No mobile optimization | Dashboard table breaks on narrow screens |
| No incident detail modal | Merchants can't see full incident detail without raw JSON |
| No "settings" page to update Slack URL | `/onboarding` link at bottom but not prominent |
| No historical trend chart | Merchants can't see conversion rate over time |
| No "mark as resolved" button | Some incidents may auto-resolve slowly or never (payment_failure) |
| Dashboard shows raw shop domain, not store name | Minor UX — could fetch store name from Shopify API |
| Calibrating state shows no data at all | Could show "collecting data..." with last event timestamps |

---

## 7. Improvement Recommendations

1. **CRITICAL:** Add session authentication to /dashboard — signed token at minimum.
2. Use `html.escape(shop)` in all server-rendered templates.
3. Add CSRF token to POST /onboarding form.
4. Validate Slack webhook URL format on save.
5. Add `input[type="url"]` HTML5 validation for Slack URL field.
6. Add a settings route (GET/POST /settings?shop=X) separate from onboarding to update config.
7. Consider migrating to embedded App Bridge to pass Shopify App Store review.
