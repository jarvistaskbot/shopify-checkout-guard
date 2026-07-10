# Module: Billing — Shopify Recurring Charges

**File:** `routes/billing.py`, `services/billing_guard.py`  
**Purpose:** Create, confirm, and activate Shopify recurring application charges. Enforce billing gating on alerts and digest emails.

---

## 1. Business Value

Without billing, the app generates no revenue. Shopify's billing API handles payment collection, trial management, and plan enforcement. The billing module bridges merchant approval (on Shopify's UI) to CheckoutGuard's internal billing state.

---

## 2. Flow (v2 — enforced)

```
Merchant installs → /auth/callback → session cookie set
  ↓ (first install — no Slack config)
GET /onboarding → enters Slack webhook + email → POST /onboarding
  ↓ (redirect after onboarding save)
GET /billing/start?shop=X
  ↓ (create recurring charge via Shopify API with trial_days=14)
  → Redirect to charge["confirmation_url"]  (Shopify's billing page)
  
Merchant approves on Shopify (FREE for 14 days)
  ↓
GET /billing/callback?charge_id=X&shop=Y
  ↓ (verify + activate charge → billing_status='active', trial_ends_at=now+14d)
  → Redirect to /dashboard

Dashboard shows: "Trial — X days left" banner
Alerts fire for: billing_status IN ('active', 'pending')

Merchant declines charge
  ↓
GET /billing/callback (status='declined')
  → billing_status='declined' stored
  → Dashboard shows subscribe banner (no alerts sent)
  → Merchant can retry via /billing/start

Merchant uninstalls
  → app/uninstalled webhook → active=FALSE, billing_status='cancelled'
  → No further alerts dispatched

shop/redact (48h later) → all merchant data deleted
```

---

## 3. Current Plan

```python
_PLAN_NAME = "CheckoutGuard Pro"
_PLAN_PRICE = 29.0
_TRIAL_DAYS = 14
```

One plan exists. `BILLING_TEST_MODE` env var (default `False`) controls whether charges are Shopify test charges.

---

## 4. Billing Status Values

| Value | Set by | Meaning |
|---|---|---|
| `inactive` | Default (migration 004) | Never visited /billing/start |
| `pending` | /billing/start | Charge created, awaiting Shopify confirmation |
| `active` | /billing/callback | Merchant accepted; trial running or paying |
| `declined` | /billing/callback | Merchant declined charge |
| `cancelled` | app/uninstalled | Merchant uninstalled app |

**Alert dispatch:** Only `active` and `pending` receive Slack/email alerts and weekly digests.

---

## 5. Billing Enforcement (services/billing_guard.py)

`alerts_allowed(billing_status)` — returns True only for 'active' and 'pending'.

Called at every alert dispatch point in `services/detector.py`:
- Checkout funnel collapse alert
- Order silence alert
- Abandonment spike alert
- Payment failure alert + recovery
- JS error spike alert
- OOS hot product alert + recovery

Data collection continues for all merchants regardless of billing status — the 7-day baseline warms up even for inactive merchants, so alerts fire immediately after they subscribe.

`get_billing_banner(billing_status, trial_ends_at, shop)` — returns (css_class, html) for the dashboard billing UI panel:
- `banner-subscribe` for inactive/declined/cancelled merchants
- `banner-trial` for active merchants in trial (shows countdown)
- `None` for fully active merchants past trial

---

## 6. AI Monthly Cost Cap

`services/billing_guard.consume_ai_budget(pool, shop_domain, cap)`:
- Per-merchant monthly counter in `merchants.ai_calls_month` + `ai_calls_reset_at`
- Resets on calendar month boundary
- Default cap: 200 calls/month/merchant (`AI_MONTHLY_CALL_CAP` env var)
- Over cap → skip Anthropic call, return template-only analysis
- Fail-open: DB errors allow the call (never silently drop incidents)

---

## 7. Function Walk-Through

### `billing_start(shop)` — GET /billing/start

1. Requires valid session cookie (redirects to OAuth if missing).
2. **Shortcut for partner tokens:** If `token.startswith("shpat_")`, marks billing as 'active' and redirects to `/billing/activated` without creating a real charge. Gate-keyed by token type only (developer stores get unlimited access without billing).
3. Creates `recurring_application_charge` via Shopify API.
4. Stores `billing_charge_id` and `billing_status='pending'` in DB.
5. Redirects to Shopify's `confirmation_url`.

### `billing_callback(charge_id, shop)` — GET /billing/callback

1. Fetches token and charge status from Shopify.
2. If `status == 'pending'`: activates charge.
3. Computes `trial_ends_at = now + 14 days` if trial_days present.
4. Updates merchants: charge_id, billing_status (from Shopify), trial_ends_at.
5. On 'active'/'pending': renders success page with trial date.
6. On any other status (e.g., 'declined'): renders declined page with retry link.

### `billing_activated(shop)` — GET /billing/activated

Landing page for shpat_ token bypass. Shows success HTML.

---

## 8. DB Schema (billing-related columns)

```sql
billing_charge_id    TEXT                  -- Shopify charge ID
billing_status       TEXT DEFAULT 'inactive' -- see table above
billing_activated_at TIMESTAMPTZ
trial_ends_at        TIMESTAMPTZ
ai_calls_month       INTEGER DEFAULT 0      -- monthly AI counter (004_production.sql)
ai_calls_reset_at    TIMESTAMPTZ            -- when ai_calls_month was last reset
```

---

## 9. Support Contact

Billing error pages link to: `artomnats1996@gmail.com`

---

## 10. Known Remaining Gaps

| Gap | Impact | Notes |
|---|---|---|
| `recurring_application_charges/updated` webhook | MEDIUM | Shopify-initiated cancellations not handled |
| Trial expiry: no active recheck of trial_ends_at | LOW | Dashboard shows correct state; alerts only depend on status |
| Multiple plan tiers | LOW | Only one plan at $29 |
