# CheckoutGuard v2 — Launch Checklist

**Use this when Shopify App Store approval arrives for v1.**  
Steps are in dependency order. Do not skip or reorder.

---

## Phase 1 — Pre-Deploy (before any VPS changes)

### 1. Verify Shopify Partner Dashboard listing
- [ ] **Pricing section** shows "$29/month" recurring + "14-day free trial" — Arto must verify manually in Partners > Apps > CheckoutGuard > Pricing
- [ ] Support email set to `artomnats1996@gmail.com` in Partners Dashboard
- [ ] App listing "Make fully visible" decision: set to public if ready for organic discovery, or leave as "unlisted/invite-only" for initial controlled launch
- [ ] Privacy policy URL points to `https://checkoutguardalerts.com/privacy`
- [ ] App URL points to `https://checkoutguardalerts.com`

### 2. Confirm v2-feature-branch passes all tests locally
```bash
# From /Users/openclaw/shopify-checkout-guard:
export $(cat .env | grep -v '^#' | xargs)
PYTHONPATH=. python test_e2e.py       # 4 tests
PYTHONPATH=. python test_fixes.py     # 20 tests
PYTHONPATH=. python test_v2.py        # 12 tests
PYTHONPATH=. python test_billing_flow.py  # 15 tests
# Expected: 51 total, all PASS
```

---

## Phase 2 — Merge to Main

```bash
git checkout main
git merge v2-feature-branch --no-ff -m "chore: merge v2-feature-branch for production launch"
git push origin main
```

> Only do this after Phase 1 is complete and approved.

---

## Phase 3 — Database Migrations (on VPS)

Run migrations in exact order. Each is idempotent (safe to re-run).

```bash
# SSH to VPS
ssh root@76.13.209.1

# From /opt/checkoutguard:
cd /opt/checkoutguard

psql $DATABASE_URL -f migrations/schema.sql        # 001 — base schema
psql $DATABASE_URL -f migrations/002_v2.sql        # 002 — v2 tables
psql $DATABASE_URL -f migrations/003_fixes.sql     # 003 — audit fixes
psql $DATABASE_URL -f migrations/004_production.sql  # 004 — billing enforcement + AI cap
```

Verify:
```sql
\d merchants  -- should show ai_calls_month, ai_calls_reset_at columns
SELECT billing_status, COUNT(*) FROM merchants GROUP BY billing_status;  -- should have no 'trialing' rows
```

---

## Phase 4 — Environment Variables (on VPS)

Set these in `/opt/checkoutguard/.env` (or Railway env if applicable):

```bash
# REQUIRED — must change from dev values
SECRET_KEY=<32-byte random hex: python3 -c "import secrets; print(secrets.token_hex(32))">
BILLING_TEST_MODE=false

# REQUIRED — must be set for full functionality
SHOPIFY_API_KEY=<from Partners Dashboard>
SHOPIFY_API_SECRET=<from Partners Dashboard>
APP_URL=https://checkoutguardalerts.com

# OPTIONAL but recommended
ANTHROPIC_API_KEY=sk-ant-...   # enable AI incident analysis
AI_ANALYSIS_ENABLED=true
AI_MONTHLY_CALL_CAP=200        # per-merchant monthly AI cap
SENDGRID_API_KEY=SG....        # enable email alerts + weekly digest
OOS_ENABLED=false              # enable after Shopify scopes approved for inventory
```

**Sequencing note:** Set `SECRET_KEY` before restarting — changing it invalidates all existing merchant sessions (they re-OAuth automatically). Set `BILLING_TEST_MODE=false` at the same time as the deploy (not before, to avoid live charges hitting a stale app version).

---

## Phase 5 — Deploy Application

```bash
# On VPS at /opt/checkoutguard:
git pull origin main
docker compose build --no-cache app
docker compose up -d

# Verify
curl https://checkoutguardalerts.com/health
# Expected: {"status":"ok","service":"CheckoutGuard"}
```

---

## Phase 6 — Shopify App Deploy (scopes + extension)

If scopes or the Theme Extension changed from v1:

```bash
# Local machine:
cd /Users/openclaw/shopify-checkout-guard/checkout-guard
shopify app deploy
```

This updates:
- `shopify.app.toml` scopes (including `checkouts/create`, `checkouts/delete`)
- Theme App Extension assets

---

## Phase 7 — Dev Store Verification

Before promoting to public, install on a dev store and verify:

```bash
# Install via partner dashboard or:
https://checkoutguardalerts.com/auth/shopify?shop=YOUR-DEV-STORE.myshopify.com
```

Walk through:
- [ ] Install → OAuth → onboarding page shown ✓
- [ ] Fill Slack webhook + email → saved → redirected to /billing/start ✓
- [ ] Accept trial charge → redirected to /dashboard ✓
- [ ] Dashboard shows "Trial — 14 days left" banner ✓
- [ ] Dashboard shows "Calibrating your store's baseline — anomaly alerts begin after 7 days" ✓
- [ ] Fire test webhook (orders/create) → order appears in detection ✓
- [ ] Uninstall app → billing_status='cancelled', active=FALSE ✓

---

## Phase 8 — First Real Merchant Monitoring

After dev store verification:
- [ ] Monitor VPS logs (`docker compose logs -f app`) for first real install
- [ ] Confirm first Slack alert fires correctly on test incident
- [ ] Confirm billing charge appears in Partners Dashboard (real, not test)
- [ ] Set up VPS alert if container crashes (`systemd` or `docker events`)

---

## Quick Reference — What Code Cannot Do

| Item | Why | Action Required |
|---|---|---|
| Partner Dashboard pricing section | Shopify Partners UI only | Arto verifies $29/mo + 14-day trial shows correctly |
| App listing visibility | Partners UI only | Arto sets "Make fully visible" or "unlisted" |
| `SHOPIFY_API_KEY` / `SECRET_KEY` rotation | Env vars on prod | Arto sets these on VPS before go-live |
| SendGrid "from" domain verification | DNS + SendGrid UI | alerts@checkoutguardalerts.com must have SPF/DKIM |
| Stripe/payment backup | Not applicable | Shopify handles all billing |
