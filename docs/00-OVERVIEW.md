# CheckoutGuard — Architecture & System Overview

## 1. What It Is

CheckoutGuard is a Shopify app that detects silent revenue bleed by monitoring real customer behavior — not synthetic tests. It runs 6 anomaly detectors in parallel and alerts merchants via Slack and email the moment their checkout funnel breaks, orders go silent, a payment gateway stalls, or a hot product runs out of stock.

**Live URL:** https://checkoutguardalerts.com  
**App Store status:** v1 under review (2026-07-09)  
**v2 status:** feature branch `v2-feature-branch`, not deployed  

---

## 2. Component Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│  Shopify Storefront                                               │
│  ┌────────────────────────────────────────┐                      │
│  │  Theme App Extension (error-tracker)   │                      │
│  │  Liquid block injects inline JS        │                      │
│  │  window.onerror + unhandledrejection   │                      │
│  │  Batches to POST /events every 10s     │                      │
│  └────────────────┬───────────────────────┘                      │
└───────────────────┼──────────────────────────────────────────────┘
                    │
┌──────────────────────────────────────────────────────────────────┐
│  Shopify Admin (webhooks → HMAC-signed HTTP POST)                │
│  orders/create, checkouts/create, checkouts/delete               │
│  app/uninstalled, inventory_levels/update (v2, gated)            │
│  customers/data_request, customers/redact, shop/redact (GDPR)    │
└───────────────────┼──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│  FastAPI Backend (uvicorn, Docker, port 8000)                    │
│                                                                  │
│  Routes:                                                         │
│  ├─ GET  /auth/shopify          OAuth install redirect           │
│  ├─ GET  /auth/callback         Token exchange + redirect        │
│  ├─ POST /webhooks/*            HMAC-verified webhook handlers   │
│  ├─ POST /events                JS error intake (public)         │
│  ├─ GET  /onboarding            Slack config form                │
│  ├─ POST /onboarding            Save Slack config                │
│  ├─ GET  /dashboard             Server-rendered incidents page   │
│  ├─ GET  /billing/start         Create Shopify charge            │
│  ├─ GET  /billing/callback      Activate charge                  │
│  ├─ GET  /demo                  App Store review demo page       │
│  └─ GET  /privacy               Privacy policy page              │
│                                                                  │
│  Background Tasks (asyncio loops):                               │
│  ├─ _token_refresh_loop()       Every 20 min — refresh expiring  │
│  └─ _proactive_monitor_loop()   Every 5 min — payment + JS stale │
└───────────────────┬──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│  PostgreSQL (asyncpg connection pool, min=2, max=10)             │
│  Tables: merchants, checkout_events, incidents                   │
│  v2 tables: js_error_events, order_line_items, inventory_levels  │
└──────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│  External Alert Services                                         │
│  ├─ Slack Incoming Webhooks (per-merchant, merchant-supplied)    │
│  └─ SendGrid v3 API (from alerts@checkoutguardalerts.com)        │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Deployment Architecture

### Production (ACTUAL): VPS + Docker Compose

```
Internet ──► nginx (port 443/80) ──► localhost:9000
                                          │
                                  docker-compose.yml
                                  ├─ app  (host:9000 → container:8000)
                                  └─ db   (postgres:16-alpine, port 5432)
VPS: root@76.13.209.1
Path: /opt/checkoutguard
```

### Railway (STALE): Config exists but not the live deployment

`railway.json` + `Procfile` both exist in the repo. These were used during early development but the production deployment migrated to the VPS. Railway config is **vestigial** — it is not the current production target.

**Discrepancy to resolve:** `railway.json` declares `builder: NIXPACKS` and a healthcheck path. `docker-compose.yml` maps port 9000:8000. nginx on the VPS proxies 443 → localhost:9000. These are incompatible deploy targets. The Railway config should either be deleted or clearly marked as the disaster-recovery fallback.

---

## 4. Data Flow

### Install Flow
```
Merchant clicks "Add app" in Shopify
→ GET /auth/shopify?shop=X  (generates nonce, redirects to Shopify OAuth)
→ Shopify → GET /auth/callback?code=X&state=Y&hmac=Z
→ Verify HMAC + nonce → exchange code for token
→ INSERT merchants row
→ asyncio.create_task(_subscribe_webhooks)  [fire-and-forget]
→ asyncio.create_task(_fetch_and_store_aov) [fire-and-forget]
→ Redirect to /onboarding (new) or /dashboard (returning)
```

### Webhook Detection Flow
```
Shopify fires webhook → POST /webhooks/{topic}
→ HMAC verification (400ms typical)
→ INSERT checkout_events / order_line_items
→ await process_event(shop, event_type)
   └─ asyncio.create_task(_run_realtime_checks)  [fire-and-forget]
      ├─ _check_checkout_funnel
      ├─ _check_order_silence
      └─ _check_abandonment_spike (only if checkout event)
→ return {"ok": True}
```

### JS Error Flow
```
Browser error → Theme Extension enqueues
→ flush() after 10s (or on unload via sendBeacon)
→ POST /events [no auth, public endpoint]
→ Rate limit check (120 req/min per shop, in-memory)
→ Verify shop is active in merchants table
→ INSERT js_error_events
→ asyncio.create_task(_trigger_js_spike_check)  [fire-and-forget]
```

### Proactive Monitor Loop (every 5 min)
```
_proactive_monitor_loop()
→ run_proactive_checks_all_merchants()
   ├─ For each active merchant:
   │   └─ asyncio.create_task(_check_payment_failures(shop, token))
   └─ asyncio.create_task(_resolve_stale_js_incidents())
```

---

## 5. Third-Party Integrations

| Integration | Purpose | Auth | Notes |
|---|---|---|---|
| Shopify Admin API 2024-10 | Order/webhook queries, billing | Per-merchant access_token | Token may expire (refresh supported) |
| Shopify OAuth | App install/auth | HMAC + client_secret | Non-embedded flow |
| Slack Incoming Webhooks | Incident alerts | Merchant-supplied URL | No auth from our side |
| SendGrid v3 | Email alerts | Bearer API key | From alerts@checkoutguardalerts.com |
| Anthropic/Claude | AI incident analysis | Not yet integrated | Planned (Haiku), not built |

---

## 6. Environment Variables

All settings in `config.py` via `pydantic_settings.BaseSettings`. Source: `.env` file or environment.

| Variable | Required | Default | Purpose | Where consumed |
|---|---|---|---|---|
| `DATABASE_URL` | Yes | `""` | asyncpg DSN (postgres://user:pass@host/db) | `database.py:create_pool`, `main.py:lifespan` |
| `SHOPIFY_API_KEY` | Yes | `""` | OAuth client_id for all API calls | `routes/auth.py`, `main.py:_token_refresh_loop`, `routes/billing.py` |
| `SHOPIFY_API_SECRET` | Yes | `""` | HMAC signing + token exchange | `routes/auth.py`, `routes/webhooks.py:_verify_hmac` |
| `APP_URL` | Yes | `""` | Base URL for redirect_uri + webhook addresses | `routes/auth.py:callback+_subscribe_webhooks` |
| `SENDGRID_API_KEY` | No | `""` | Email via SendGrid v3 | `services/alerter.py:_send_email` |
| `OOS_ENABLED` | No | `False` | Feature flag: enable OOS (inventory) detection | `services/detector.py:check_oos_hot_product`, `routes/auth.py:_subscribe_webhooks` |

> ⚠️ No `SLACK_WEBHOOK_URL` global env — each merchant stores their own webhook URL in the DB.  
> ⚠️ No `SECRET_KEY` for session management — dashboard has NO auth at all (see AUDIT-SECURITY.md).

---

## 7. Feature Implementation Status

| Feature | Status | Notes |
|---|---|---|
| Shopify OAuth install/uninstall | ✅ v1 live | Expiring token + refresh supported |
| Checkout funnel collapse detector | ✅ v1 live | 30-min window, 7-day baseline |
| Order silence detector | ✅ v1 live | Day-of-week aware, 28-day lookback |
| Abandonment spike detector | ✅ v1 live | Baseline query has a window mismatch bug |
| Payment failure detector | ✅ v1 live | Polls Shopify API every 5 min |
| Slack alerts | ✅ v1 live | Per incident type |
| Email alerts (SendGrid) | ✅ v2 built | Requires SENDGRID_API_KEY |
| JS error spike detector | ✅ v2 built | Not deployed yet |
| OOS hot product detector | ⚠️ v2 built, BROKEN | product_id never populated in inventory_levels |
| Theme App Extension (JS) | ✅ v2 built | Liquid block + assets/error-tracker.js |
| Dashboard (server-rendered) | ✅ v2 built | No auth — any request with shop= is served |
| Billing (Shopify charges) | ✅ built | TEST_MODE=True hardcoded — must fix before live |
| GDPR webhooks | ✅ v1 live | 3 required webhooks implemented |
| Token refresh | ✅ v1 live | 20-min background loop + on-demand in `get_valid_token` |
| AI incident analysis (Haiku) | ❌ planned | Not started |
| Weekly digest email | ❌ planned | Not started (approved requirement) |
| Multi-plan billing tiers | ❌ planned | Only one plan ($29) exists |
| Agency / multi-store view | ❌ planned | Not started |
| Embedded App Bridge UI | ❌ never built | embedded=false in toml; dashboard is external page |
| React/Polaris dashboard | ❌ never built | Spec said React; implementation is server-rendered HTML |
| webhooks_raw async pattern | ❌ not built | Spec specified raw-store-then-process; inline processing used instead |

---

## 8. Known Limitations

1. **Single-process**: In-memory nonce store and rate limiter break under horizontal scaling. Multi-instance requires Redis.
2. **No session auth on dashboard**: `/dashboard?shop=X` is accessible to anyone.
3. **OOS detection non-functional**: `product_id` is never stored in `inventory_levels` on webhook receive.
4. **No data retention jobs**: Tables grow unbounded despite privacy policy promising 30-day retention.
5. **Billing in test mode**: `_TEST_MODE = True` in billing.py — charges are test charges, not real.
6. **Non-embedded UI**: Shopify may flag `embedded=false` during App Store review; the UI does not load inside Shopify Admin iframe.
7. **Theme Extension JS double-execution**: Both the Liquid inline script and the external `error-tracker.js` asset fire event listeners — errors will be captured and queued twice.
8. **Sequential proactive loop**: Payment failure checks run sequentially per merchant; at scale (100+ merchants) each 5-min loop takes >N×100ms.
