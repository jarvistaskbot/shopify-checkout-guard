# Module: Billing — Shopify Recurring Charges

**File:** `routes/billing.py`  
**Purpose:** Create, confirm, and activate Shopify recurring application charges for merchants.

---

## 1. Business Value

Without billing, the app generates no revenue. Shopify's billing API handles payment collection, trial management, and plan enforcement. The billing module bridges merchant approval (on Shopify's UI) to CheckoutGuard's internal billing state.

---

## 2. Flow

```
GET /billing/start?shop=X
  ↓ (create recurring charge via Shopify API)
  → Redirect to charge["confirmation_url"]  (Shopify's billing page)
  
Merchant approves (or declines) on Shopify
  ↓
GET /billing/callback?charge_id=X&shop=Y
  ↓ (verify + activate charge)
  ├─→ Success: render _success_html
  └─→ Declined/error: render _declined_html or _error_html
```

---

## 3. Current Plan

```python
_PLAN_NAME = "CheckoutGuard Pro"
_PLAN_PRICE = 29.0
_TRIAL_DAYS = 14
_TEST_MODE = True  # ← CRITICAL: must be False before going live
```

Only one plan exists. The spec defined 4 tiers ($29/$79/$199/$399). Implementation has only the $29 plan.

---

## 4. Function Walk-Through

### `billing_start(shop)` — GET /billing/start

1. Fetches token via `get_valid_token`.
2. **Shortcut for partner tokens:** If `token.startswith("shpat_")`, marks billing as 'active' and redirects to `/billing/activated` without creating a real charge. This was added for testing with Partner Dashboard development stores that use non-expiring tokens. Must be removed or gated before live launch.
3. Creates `recurring_application_charge` via Shopify API.
4. Stores `billing_charge_id` and `billing_status='pending'` in DB.
5. Redirects to Shopify's `confirmation_url`.

**Issue:** If `get_valid_token` fails (token expired, network error), `token` is set in the `with` block but then used in the httpx request OUTSIDE the block. Python scoping means `token` is still accessible — this is fine syntactically. But if the `with` block raises, execution never reaches the httpx POST. Exception propagates as 500. Acceptable behavior but could be a more explicit error page.

**TEST_MODE=True** is hardcoded. This means all charges are Shopify test charges. Test charges don't bill real money. If this app goes live with TEST_MODE=True, merchants will "approve" a plan but never be charged. **Must set to False before production launch.**

### `billing_callback(charge_id, shop)` — GET /billing/callback

1. Fetches token.
2. Fetches charge status from Shopify API.
3. If `status == 'pending'`: activates charge (POST to `/activate`).
4. Computes `trial_ends_at = now + 14 days` if trial_days present.
5. **Bug:** `billing_activated_at = now if charge["status"] in ("active", "pending")` — sets activated_at for pending charges that weren't actually activated. The `if charge["status"] == "pending"` branch above activates the charge first, but if activation fails, the status remains "pending" and `billing_activated_at` is still set to now. The callback returns a success page (`_success_html`) for a charge that's still pending. A merchant could see "You're all set" even if activation failed.
6. Updates merchants table with charge_id, status, activated_at, trial_ends_at.
7. Returns appropriate HTML response.

**Issue:** `billing_activated_at` is set to `now` even when charge status is "pending" (before the activation call). After activation, the charge returns status "accepted" not "active". The condition `charge["status"] in ("active", "pending")` for showing success page is correct (both indicate the flow succeeded), but the naming is confusing.

### `billing_activated(shop)` — GET /billing/activated

Shortcut landing page for shpat_ token bypassed billing. Shows success HTML without trial date.

### `billing_error(shop)` / `billing_declined(shop)` — HTML-only

Simple error/declined pages. The `shop` value is rendered into HTML:
```python
def _success_html(shop: str, trial_ends_at) -> str:
    ...
    f"CheckoutGuard is now monitoring <strong>{shop}</strong>"
```

**XSS:** `shop` is rendered unescaped. Same issue as dashboard and onboarding. Use `html.escape(shop)`.

---

## 5. DB Usage

merchants table columns managed by billing:
- `billing_charge_id TEXT` — Shopify charge ID
- `billing_status TEXT` — 'pending' | 'active' | 'trialing' | 'declined' | 'expired'
- `billing_activated_at TIMESTAMPTZ`
- `trial_ends_at TIMESTAMPTZ`

**Missing:** No enforcement of billing status on dashboard access. A merchant who declined billing can still access the dashboard and the app monitors their store indefinitely. There's no "billing_required" check on any route.

---

## 6. Missing Functionality

| Missing | Impact |
|---|---|
| `_TEST_MODE = False` switch for live | CRITICAL — can't take real payments |
| Billing enforcement on app routes | HIGH — merchants who decline still get full service |
| Multiple plan tiers ($29/$79/$199/$399) | HIGH — spec defined 4 tiers; only $29 exists |
| Billing status webhook handler | MEDIUM — Shopify fires `app/subscriptions/approaching_capped_amount` and `recurring_application_charges/updated` — not handled |
| Trial expiry enforcement | MEDIUM — `trial_ends_at` stored but never checked; trial never "expires" |
| Plan upgrade/downgrade flow | LOW — only one plan |
| Invoice/receipt emails | LOW — Shopify handles these |

---

## 7. Improvement Recommendations

1. **Set `_TEST_MODE = False`** via env var (`TEST_MODE = os.getenv("BILLING_TEST_MODE", "false").lower() == "true"`) — never hardcode.
2. Add billing status middleware: redirect to `/billing/start` if `billing_status NOT IN ('active', 'trialing')`.
3. Remove shpat_ bypass shortcut or gate it on an env var (`PARTNER_BILLING_BYPASS=true`).
4. Subscribe to `recurring_application_charges/updated` webhook to catch merchant-initiated cancellations.
5. Add cron job to check trial_ends_at and mark expired trials.
