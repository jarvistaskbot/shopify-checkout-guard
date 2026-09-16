# CheckoutGuard — Complete Guide

Updated 2026-09-16 (day after App Store approval). All facts verified against the
codebase and production; nothing here is aspirational.

Listing: https://apps.shopify.com/checkoutguard-3 (published, limited visibility)
Site: https://checkoutguardalerts.com | Support: support@checkoutguardalerts.com

---

## 1. What CheckoutGuard is

A Shopify app that tells a merchant, within minutes, when their store is
ACTIVELY BROKEN and losing money — not when sales are merely slow.

Positioning (the one-liner that survived 6 reviews):
"If nobody wants to buy, that's marketing. If people are trying to buy and
failing — that's an incident. Incidents are the only thing we alert on."

Alerts go to Slack (incoming webhook) and optionally email. Each incident
gets an AI likely-cause diagnosis (paid tiers) and a recovery notice with
duration when it ends.

## 2. What it detects (7 detectors)

| Detector | Trigger logic | Example alert |
|---|---|---|
| Checkout funnel collapse | Checkouts started but conversion collapses vs baseline (e.g. 6.4% vs 42%) | "47 started checkout, 3 completed — test your checkout NOW" |
| Order silence | Zero orders during hours the store historically sells (hour-of-day + day-of-week aware baseline) | "Expected ~8 orders/hr Wed evening, none in 90 min" |
| Abandonment spike | Cart abandonment at a multiple of the store's normal rate | |
| Payment failures | Orders piling up in pending/failed financial status | "12 orders stuck — check your gateway" |
| JS error spike | Storefront JS errors via theme app extension ("CheckoutGuard Errors" block) + /events endpoint | Growth+ plans |
| OOS hot product | Best-seller goes out of stock | BUILT but DORMANT — needs read_inventory/read_products scopes (post-approval request pending) |
| Slow bleed (v3) | One-sided CUSUM on hourly checkout-start shortfall vs 28-day same-weekday baseline; catches gradual degradation counters miss | Live but dormant until a store accumulates 28-day baseline |

Recovery: each incident auto-resolves when metrics normalize and sends a
"RESOLVED after N min" notice. Baselines need ~7 days of order history;
until then detection correctly stays silent (a test alert verifies the
pipeline at onboarding).

## 3. Plans and billing (verified from services/plans.py)

All plans: 14-day free trial, monthly recurring via Shopify Billing API
(merchants pay through Shopify; charges auto-detect dev stores → test mode).

| Plan | Price | Order cap/mo | JS errors | AI analysis | Weekly digest | OOS | Fast checks |
|---|---|---|---|---|---|---|---|
| Starter | $29 | 500 | – | – | – | – | – |
| Growth | $79 | 5,000 | yes | yes | yes | – | – |
| Pro | $199 | 20,000 | yes | yes | yes | yes* | yes |
| Scale | $399 | higher | yes | yes | yes | yes* | yes |

*OOS ships after the scope request is granted.

Revenue side: App Store registration is Approved → 0% Shopify revenue share
on the first $1M/yr, 15% above. Payouts: wire transfer (configured), batch
with a payout threshold to amortize SWIFT fees.

## 4. Merchant experience (install → alert)

1. Install from listing URL → Shopify OAuth (scopes: read_orders,
   read_checkouts — read-only, never touches the theme or products).
2. Onboarding page: paste a Slack incoming-webhook URL — or "Skip for now"
   (dashboard then shows a connect-Slack banner; test alerts render in-app).
3. Pick a plan → approve the charge in Shopify (14-day trial starts).
4. Dashboard: Recent Alerts panel (Delivered/Failed badges, full alert text
   expandable — works with zero third-party credentials), "Send test alert"
   button (60s cooldown with countdown).
5. Webhooks registered automatically: orders/create, app/uninstalled,
   checkouts/create, checkouts/delete. Baseline builds ~7 days → real
   detection on.

## 5. Architecture

- Backend: FastAPI (Python) + PostgreSQL 16, Docker Compose on VPS
  76.13.209.1 at /opt/checkoutguard (container maps 9000:8000; trading bot
  owns 8000). nginx + Let's Encrypt in front.
- Domains: checkoutguardalerts.com (primary), sslip.io fallback.
- Auth: OAuth + token exchange to expiring offline tokens
  (services/token_manager.py), auto-refresh loop every 20 min.
- Detection: webhook-driven checks per orders/create + hourly proactive
  sweep (silence, slow-bleed); streak counters persisted in DB (survive
  restarts).
- AI analysis: OpenRouter (claude-haiku), fail-silent, ~$0.001/incident
  (services/ai_analyst.py).
- Alert delivery: Slack webhook POST + optional email; every delivery
  recorded in alert_deliveries with message_preview (in-app verification).
- GDPR: customers/data_request, customers/redact, shop/redact compliance
  webhooks (config-declared, HMAC-verified, 401 on unsigned).
- Repo: github.com/jarvistaskbot/shopify-checkout-guard (main = prod).

## 6. Security / privacy answers (for merchant questions)

- Read-only scopes; no theme edits (JS error tracking is an opt-in theme
  app extension block the merchant adds themselves).
- Data stored: order/checkout timestamps and amounts per store — no
  customer PII beyond what GDPR webhooks can purge; 90-day retention purge.
- Webhook HMAC verified (constant-time); OAuth state nonce; open-redirect
  closed; billing test-mode auto-detection for dev stores.

## 7. Operations runbook

- Health: https://checkoutguardalerts.com/health
- Logs: ssh root@76.13.209.1 → cd /opt/checkoutguard →
  `docker compose logs app --tail 100`
- Deploy: git pull → `docker compose build --no-cache app && docker compose
  up -d` (migrations auto-apply on boot).
- Config deploy (scopes/webhooks): edit checkout-guard/shopify.app.toml →
  `shopify app deploy` from Mac (CLI logged in). Current released version:
  checkoutguard-12 (trimmed scopes).
- Install watcher: Mac mini `checkoutguard_review_watch.sh` → Telegram ping
  on unknown-store activity. Kill when obsolete:
  `pkill -f checkoutguard_review_watch`.
- Slack channels: prod alerts → #checkoutguard-alerts (main workspace);
  review/demo workspace → checkoutguardreview.slack.com #alerts.

## 8. Current status + roadmap

Status 2026-09-16: APPROVED (review #6, Sep 15) after 5 rejections; listing
published, limited visibility; production healthy; billing real; partner
account complete (contacts, wire payouts, registration Approved).

Next (launch playbook, LAUNCH-PLAYBOOK.md):
1. First 3-5 merchants via direct link (Reddit post + DMs — texts final).
2. 2-3 reviews → flip "Make fully visible".
3. App Store ads (niche keywords, $5-10/day probe).
4. Product: request read_inventory/read_products → enable OOS; synthetic
   payment-render probe (v3, analytics-suppression is the launch blocker);
   fix weekday-mismatch bug in legacy silence baseline; rotate review
   workspace Slack secrets.

## 9. Known issues / caveats

- Legacy silence detector has a Python-weekday vs Postgres-DOW mismatch
  (shifts weekday matching by one day). CUSUM detector is unaffected. Fix
  pending Arto's go (changes live behavior).
- "English — Incomplete" flag on the listing: optional fields empty;
  fill before visibility flip.
- OOS + multi_store flags exist in plans but features are gated/unbuilt
  (multi_store) — keep out of listing copy until real.
