# CheckoutGuard

Shopify app that detects silent revenue bleed on merchant stores — a broken
checkout, a stalled payment gateway, an unusual order silence, or a slow
sustained drop in checkout starts — and alerts the merchant in Slack and in
an in-app dashboard within minutes.

**Live:** https://checkoutguardalerts.com
**Stack:** FastAPI + PostgreSQL 16 + Docker Compose, server-rendered HTML
**Status:** production live; Shopify App Store review in progress (see
`docs/13-REVIEW6-PLAYBOOK.md` for the current review state)

## How it works (short version)

Shopify pushes `orders/create` / `checkouts/create` / `checkouts/delete`
webhooks to the backend. Detection loops compare live activity against
per-store baselines and open incidents when behavior deviates: checkout
funnel collapse, order silence, abandonment spike, payment-gateway stall,
JS error spike, and a CUSUM slow-bleed detector (hour-of-week baseline).
Incidents fire Slack alerts (optional webhook) and are always visible in
the dashboard's Recent Alerts panel with full message content — no external
account is required to see the app work.

## Documentation

Read in this order:

| Doc | Contents |
|---|---|
| `docs/00-OVERVIEW.md` | Architecture, component diagram, data flow |
| `docs/01-AUTH.md` | Shopify OAuth, token exchange, sessions |
| `docs/02-WEBHOOKS.md` | Webhook registration + HMAC verification |
| `docs/03-DETECTION.md` | The 6 anomaly detectors and baselines |
| `docs/04-ALERTING.md` | Slack/email dispatch, delivery history |
| `docs/07-DASHBOARD-ONBOARDING.md` | Merchant-facing pages |
| `docs/08-BILLING.md` | 4-tier plans, Shopify billing flow |
| `docs/10-DATABASE.md` / `docs/11-API.md` | Schema and route reference |
| `docs/12-V3-DIRECTION.md` | Roadmap: synthetic checkout probes |
| `docs/AUDIT-*.md` | Code / security / performance / UX audits |

## Development

- Python venv: `PYTHONPATH='' .venv312/bin/python` (system Python is 3.9; do not use it)
- Run locally: local Postgres + `uvicorn main:app --port 8000`; migrations in
  `migrations/` apply automatically at startup in filename order (idempotent)
- Tests: standalone scripts (`test_*.py` in repo root) against the running
  local server — see each file's header. `test_slow_bleed.py` is pytest-style
  and must be run via pytest or an explicit driver
- Deploy: VPS at `/opt/checkoutguard`, `docker compose build --no-cache app
  && docker compose up -d`

Secrets (API keys, webhook URLs, review credentials) are never committed —
they live in the production `.env` and the Shopify Partner Dashboard.
