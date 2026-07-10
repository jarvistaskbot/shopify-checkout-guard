# Audit Summary — CheckoutGuard v2

**Audit date:** 2026-07-10  
**Branch:** v2-feature-branch  
**Auditor:** Claude Sonnet 4.6 (subagent)

---

## Readiness Score: 48 / 100

v2 has solid bones but contains one CRITICAL security vulnerability (unauthenticated dashboard), one CRITICAL broken feature (OOS detection), hardcoded test billing mode, and three high-severity XSS/resolve bugs. None of these are one-liners. Until the dashboard auth, OOS fix, and billing TEST_MODE are addressed, v2 should not be deployed to production.

**Go / No-Go: NO-GO for v2 deploy. Fix the CRITICAL items first.**

---

## Test Suite Results

Both `test_e2e.py` and `test_v2.py` **could not be run** in this audit environment:
- Local Python is 3.14; `.venv` is Python 3.14 where `asyncpg` and `pydantic-core` fail to build (no wheels for 3.14 yet)
- Tests require a running uvicorn server at localhost:8000 and a live PostgreSQL DB — neither was available in this context
- **Test suite status: Unable to execute. Tests should be run on VPS or a Python 3.11 environment.**

Both test files are well-written and cover the right scenarios. The v2 test suite (`test_v2.py`) has 11 tests covering JS spike detection, OOS flow, line item persistence, and dashboard rendering. These tests WILL catch the OOS bug if OOS_ENABLED=True is set.

---

## Top 5 Critical Findings

### 1. Dashboard Has No Authentication (CRITICAL)
**File:** `routes/dashboard.py:98`  
`/dashboard?shop=any-store.myshopify.com` is public. Returns incident history, Slack webhook URL (exploitable for Slack spam), and alert email. Anyone knowing a shop domain can access it.

### 2. OOS Hot Product Detection Is Completely Broken (CRITICAL/Feature)
**File:** `routes/webhooks.py:225`, `services/detector.py:726`  
The inventory webhook never stores `product_id` in `inventory_levels`. The detector always gets NULL and returns early. Zero OOS incidents will ever be created. The feature is marketed but does nothing.

### 3. Billing in TEST_MODE Forever (CRITICAL/Revenue)
**File:** `routes/billing.py:27`  
`_TEST_MODE = True` is hardcoded. All charges are test charges. No real revenue. Must be env-var controlled and defaulted to False.

### 4. XSS in Server-Rendered Templates (HIGH)
**Files:** `routes/onboarding.py:59,64`, `routes/dashboard.py:258`, `routes/billing.py:168,181,193`  
`shop` parameter rendered unescaped in HTML. Practical exploitability is low (shop domains come from Shopify), but must be fixed for defense-in-depth: `html.escape(shop)` on all usages.

### 5. Payment Failure Incidents Never Auto-Resolve (HIGH)
**File:** `services/detector.py:452-519`  
No resolve path in `_check_payment_failures`. Payment failure incidents accumulate as permanently "Active" in the dashboard, destroying dashboard trustworthiness for affected merchants.

---

## Findings by Severity

| Severity | Count | Items |
|---|---|---|
| CRITICAL | 3 | Dashboard auth, OOS product_id bug, billing TEST_MODE |
| HIGH | 6 | XSS (multiple files), payment failure no-resolve, nonce store, OOS scope, token refresh race |
| MEDIUM | 5 | CSRF on onboarding, content-length bypass, exception swallowing, privacy policy false claims, background loop silent failure |
| LOW | 4 | Secrets plaintext, rate limiter leak, dead dependency, SQL injection scan (clean) |
| INFO | 5 | JS double-load (no-op), stale billing Railway config, missing watchdog app.toml topics, abandonment baseline window mismatch, inline vs asset JS confusion |

**Total confirmed findings: 23**

---

## Fix Order: Before v2 Deploy

These must all be fixed before v2 ships:

| Priority | Fix | File | Effort |
|---|---|---|---|
| 1 | Add dashboard auth (signed cookie or HMAC token) | routes/dashboard.py | ~2h |
| 2 | Fix OOS product_id: resolve via Shopify API on inventory webhook | routes/webhooks.py + services/detector.py | ~3h |
| 3 | Set billing TEST_MODE via env var, default False | routes/billing.py | ~15min |
| 4 | `html.escape()` on all {shop} template insertions | All routes | ~30min |
| 5 | Add payment failure auto-resolve logic | services/detector.py | ~1h |
| 6 | Apply 002_v2.sql migration before deploy | migrations/ | run once |
| 7 | Add `checkout_events.checkout_token` index | migrations/ | ~5min |
| 8 | Fix abandonment baseline window mismatch (bl_orders upper bound) | services/detector.py:393 | ~15min |

---

## Fix Order: Post-Deploy Roadmap

These should be addressed within the first 30 days after v2 launch:

| Priority | Fix | Effort |
|---|---|---|
| 1 | Build weekly digest email (CRITICAL for retention/churn) | ~1 day |
| 2 | Integrate Claude Haiku for AI incident analysis | ~1 day |
| 3 | Add data retention jobs (30-day cleanup cron) | ~2h |
| 4 | Fix CSRF on POST /onboarding (CSRF token) | ~1h |
| 5 | Fix Content-Length bypass on /events | ~15min |
| 6 | Add asyncio.Lock per-shop in token refresh | ~1h |
| 7 | Update privacy policy (remove "encrypted", "30-day" until implemented) | ~30min |
| 8 | Add billing enforcement (redirect to /billing/start if billing not active) | ~2h |
| 9 | Remove `cryptography` from requirements.txt (dead dependency) | ~5min |
| 10 | Add `checkouts/create` and `checkouts/delete` to shopify.app.toml | ~10min |
| 11 | Mobile-responsive dashboard table | ~2h |
| 12 | Theme Extension: remove JS double-load (remove schema "javascript" ref) | ~15min |

---

## Technical Debt Assessment

| Category | Level | Notes |
|---|---|---|
| Core detection logic | LOW | Well-structured, readable, testable |
| Authentication system | CRITICAL | Missing entirely on dashboard |
| DB schema | MEDIUM | Missing indexes, no retention, no migration runner |
| Deployment configuration | MEDIUM | Railway config is stale, 002_v2.sql not auto-applied |
| Test coverage | GOOD | 15 well-designed tests across e2e and v2 suites |
| Billing | MEDIUM | Single plan, test mode hardcoded, no enforcement |
| Error handling | MEDIUM | Background loops swallow exceptions silently |
| Alert quality | GOOD | Revenue estimates are well-reasoned, actionable |
| Theme Extension | MEDIUM | Double-load confusion, no MAX_QUEUE cap |

---

## Architecture Assessment

The deviation from the spec (inline processing instead of webhooks_raw pattern, server-rendered HTML instead of React/Polaris, non-embedded) is **acceptable** and arguably better for an MVP:
- Inline processing is simpler and sufficient at current scale
- Server-rendered HTML avoids JS framework complexity and bundle size
- Non-embedded is easier to build and test

The spec's proposed architecture was overengineered for the problem. The actual implementation is pragmatically correct. The main concern is the Shopify App Store review risk from `embedded=false`.

---

## What v2 Does Well

1. Detection logic is multi-signal with proper noise floors and streak-based hysteresis
2. Revenue impact estimates are formula-driven and labeled as estimates
3. GDPR webhooks implemented correctly
4. HMAC verification correct on all webhook routes
5. Token refresh handled properly (background loop + on-demand in `get_valid_token`)
6. Test suites are comprehensive and cover key scenarios
7. Theme Extension JS is lean (<1KB), uses sendBeacon correctly
8. asyncpg usage is clean — all queries parameterized, no SQL injection risk
9. Database schema is clean with appropriate FK constraints and cascade deletes

---

## Doc Files Created

| File | Contents |
|---|---|
| `docs/00-OVERVIEW.md` | Architecture, component diagram, data flow, feature status, env vars |
| `docs/01-AUTH.md` | OAuth flow, nonce security, token exchange |
| `docs/02-WEBHOOKS.md` | All webhook handlers, HMAC, spec deviation analysis |
| `docs/03-DETECTION.md` | All 6 detectors, baselines, race conditions, bugs |
| `docs/04-ALERTING.md` | Slack/email dispatch, message format, missing retry |
| `docs/05-EVENTS-JS-TRACKING.md` | /events intake, rate limiting, validation |
| `docs/06-OOS-DETECTION.md` | OOS broken status, root cause, fix options |
| `docs/07-DASHBOARD-ONBOARDING.md` | Auth gap, XSS, UX, CSRF |
| `docs/08-BILLING.md` | Billing flow, TEST_MODE bug, missing enforcement |
| `docs/09-THEME-EXTENSION.md` | Liquid block, asset JS, double-load analysis |
| `docs/10-DATABASE.md` | Full schema docs, indexes, retention, migration strategy |
| `docs/11-API.md` | Every endpoint with auth, params, errors |
| `docs/AUDIT-CODE.md` | Function-by-function findings table |
| `docs/AUDIT-SECURITY.md` | Security findings with severity and file:line |
| `docs/AUDIT-PERFORMANCE.md` | Query analysis, table growth, pool sizing |
| `docs/AUDIT-UIUX.md` | Every page: purpose, gaps, mobile, accessibility |
| `docs/AUDIT-BUSINESS.md` | Per-feature value, competitive positioning, roadmap |
| `docs/AUDIT-SUMMARY.md` | This file |
