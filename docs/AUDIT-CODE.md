# Code Audit — Function-by-Function Findings

---

## main.py

| Function | Verdict | Findings |
|---|---|---|
| `_proactive_monitor_loop()` | WARN | Exception swallowing: catches all exceptions, logs, continues. No escalation. A persistent DB error loops silently every 5 min indefinitely. |
| `_token_refresh_loop()` | WARN | Same exception swallowing pattern. Also: acquires pool and iterates all merchants per-row in one long-held connection — holds connection for O(N_merchants) time. Should release conn between merchants or use `fetchall()` only. |
| `lifespan()` | OK | DB retry (10 attempts, 3s sleep) is good. Background tasks properly cancelled on shutdown. |
| `health()` | NOTE | Returns 200 without verifying DB connectivity. A DB-down scenario returns healthy. Fine for Railway/nginx health, but misleading for monitoring. |

---

## config.py

| Item | Verdict | Findings |
|---|---|---|
| `Settings` | OK | Pydantic settings with `.env` fallback. Clean. |
| No `SECRET_KEY` or `SESSION_SECRET` | BUG | Dashboard session management impossible without one. |
| `OOS_ENABLED` default False | OK | Correct gating for pre-approval feature. |

---

## database.py

| Function | Verdict | Findings |
|---|---|---|
| `create_pool()` | OK | min=2, max=10. Sets global `_pool`. |
| `get_pool()` | OK | Raises on uninitialized pool — good fail-fast. |
| `_apply_schema()` | WARN | Applies `schema.sql` on every startup — idempotent DDL so safe, but wastes ~20ms per boot. Should check migration table. `002_v2.sql` not applied automatically — will break v2 on deploy. |

---

## routes/auth.py

| Function | Verdict | Findings |
|---|---|---|
| `install()` | WARN | Nonce stored in in-memory set — lost on restart, broken under multi-instance. No nonce TTL. |
| `callback()` | BUG | Lines 120-128: `pool2 = await get_pool()` creates redundant reference to same singleton. Minor but confusing. |
| `callback()` | BUG | `asyncio.create_task(_subscribe_webhooks)` fire-and-forget with no error tracking. If webhook registration fails silently, the merchant installs but never receives webhook events. |
| `callback()` | OK | HMAC verification uses `hmac.compare_digest` (constant-time). Correct. |
| `_subscribe_webhooks()` | WARN | Registers topics at runtime but `checkouts/create`, `checkouts/delete` not in shopify.app.toml. Shopify CLI deploy could desync this. |
| `_subscribe_webhooks()` | WARN | No retry on individual webhook registration failures. A transient 5xx from Shopify silently skips the topic. |
| `_fetch_and_store_aov()` | OK | Fetches 50 orders — reasonable sample. Falls back to $50 default on failure. Silent failure is appropriate here. |

---

## routes/webhooks.py

| Function | Verdict | Findings |
|---|---|---|
| `_verify_hmac()` | OK | Body read once, constant-time compare. Correct implementation. |
| `order_created()` | WARN | Inline synchronous DB work before return 200. Risk of Shopify retries if DB is slow. |
| `order_created()` | OK | Per-line-item exceptions caught and logged without aborting handler. |
| `checkout_created()` | OK | Clean. |
| `checkout_deleted()` | OK | Clean. |
| `app_uninstalled()` | OK | Sets active=FALSE — correct soft delete. |
| `customers_data_request()` | OK | Log-only. Correct for no-PII scenario. |
| `customers_redact()` | OK | Log-only. Correct. |
| `shop_redact()` | NOTE | Only deletes if `active = FALSE` — silent no-op if called for active merchant. Should log a warning. |
| `inventory_updated()` | CRITICAL BUG | UPSERT never sets `product_id`. OOS detection always aborts early. |
| `inventory_updated()` | NOTE | fire-and-forget `check_oos_hot_product` — correct pattern, but unrecoverable if it raises. |

---

## routes/billing.py

| Function | Verdict | Findings |
|---|---|---|
| `billing_start()` | CRITICAL | `_TEST_MODE = True` hardcoded. Must be False for production. |
| `billing_start()` | WARN | shpat_ bypass sets billing active without a real charge. Must be removed/gated. |
| `billing_callback()` | BUG | Sets `billing_activated_at` even for pending charges where activation call may have failed. Shows "You're all set" when charge is still technically pending. |
| `_success_html()` | BUG | Renders `{shop}` unescaped in HTML — XSS. |
| `_declined_html()` | BUG | Renders `{shop}` unescaped in HTML — XSS. |
| `_error_html()` | BUG | Renders `{shop}` unescaped in HTML — XSS. |
| Billing enforcement | MISSING | No middleware or route guard checks billing status. Declined merchants get full service. |

---

## routes/onboarding.py

| Function | Verdict | Findings |
|---|---|---|
| `onboarding_page()` | BUG | Renders `{shop}` in HTML unescaped — XSS via `<span>` and form `value=`. |
| `onboarding_save()` | BUG | No CSRF protection. No URL format validation for `slack_webhook_url`. |
| `onboarding_save()` | OK | Correctly handles `alert_email or None` to store NULL vs empty string. |
| `demo_page()` | NOTE | No server-side state change. Correct for a demo. |
| `demo_page()` | NOTE | The demo copy ("volume drops >50%") doesn't precisely match all v2 detection thresholds. |
| `privacy_policy()` | BUG | Claims "stored encrypted" and "30-day retention" — both false. |

---

## routes/events.py

| Function | Verdict | Findings |
|---|---|---|
| `_is_rate_limited()` | BUG | Content-Length bypass: header can be omitted, setting it to 0, bypassing the check. |
| `_is_rate_limited()` | WARN | Memory leak: `_rate_windows` grows unboundedly. |
| `_is_rate_limited()` | WARN | In-memory — breaks at multi-instance. |
| `_sanitize_shop()` | OK | Basic sanity checks. Relies on DB lookup for real validation. |
| `ingest_events()` | PERF | 50 sequential DB queries per batch (one per event for shop validation). Should batch. |
| `ingest_events()` | OK | Correctly handles dict vs list input. |
| `_trigger_js_spike_check()` | OK | fire-and-forget with error logging. Appropriate here. |
| `_ErrorEvent` | NOTE | `lineno` and `colno` parsed but never stored. Schema doesn't include them. |

---

## routes/dashboard.py

| Function | Verdict | Findings |
|---|---|---|
| `dashboard()` | CRITICAL BUG | No auth. Any caller with a shop domain can access incident data and Slack webhook URL. |
| `dashboard()` | BUG | Renders `{shop}` unescaped in HTML. XSS. |
| `dashboard()` | OK | Returns 404 for unknown/inactive shops. |
| `_fmt_dt()` | OK | Clean datetime formatter. |
| `_fmt_impact()` | OK | Type-specific impact formatting. |
| `_render()` | NOTE | `detail` JSON parse defensiveness (str vs dict handling) suggests schema inconsistency in older data. |

---

## services/detector.py

| Function | Verdict | Findings |
|---|---|---|
| `process_event()` | OK | Correct fire-and-forget pattern for webhook handlers. |
| `run_proactive_checks_all_merchants()` | OK | Per-merchant tasks are concurrent (all create_task before any await). |
| `_run_realtime_checks()` | OK | Single SELECT for all merchant state. Efficient. |
| `_check_checkout_funnel()` | WARN | Baseline computed lazily, stored, never refreshed. Stale baseline drifts over time. |
| `_check_checkout_funnel()` | OK | 50% threshold with 5-checkout noise floor. Correct. |
| `_compute_conversion_baseline()` | OK | 7-day lookback, ≥10 checkouts required. Reasonable. |
| `_check_order_silence()` | OK | Streak persistence in DB — survives restart. Correct. |
| `_compute_silence_baseline()` | OK | Day-of-week awareness via DOW EXTRACT. Correct. |
| `_compute_silence_baseline()` | PERF | DOW/HOUR EXTRACT prevents index usage. Full scan for shop events in window. |
| `_check_abandonment_spike()` | BUG | `bl_orders` window upper bound missing — counts all orders since `since` not bounded to `now-35min`. Baseline abandonment rate miscalculated. |
| `_check_payment_failures()` | BUG | No auto-resolve logic. Payment failure incidents never resolve automatically. |
| `_check_payment_failures()` | RACE | Two concurrent callers could both pass the `if active: return` check and both INSERT an incident. |
| `check_js_error_spike()` | OK | New error detection (24h baseline check) is well-designed. |
| `check_js_error_spike()` | BUG | Active incident check only returns first open JS incident. If multiple hashes are open, a new hash may not get its own incident (line 577 checks `detail.get("error_hash") == error_hash` against only one result). |
| `_resolve_stale_js_incidents()` | OK | Quiet window approach (1h, <3 events) is reasonable. |
| `check_oos_hot_product()` | CRITICAL BUG | `product_id` query always returns NULL. OOS detection completely non-functional. |
| `_get_active_incident()` | OK | Simple, correct. LIMIT 1 is appropriate for dedup. |
| `_resolve_incident()` | PERF | Extra DB query to fetch `incident_type` for the recovery alert. Could be passed as parameter. |

---

## services/alerter.py

| Function | Verdict | Findings |
|---|---|---|
| `send_checkout_funnel_alert()` | OK | Revenue estimate formula correct. CTA actionable. |
| `send_silence_alert()` | NOTE | "No orders for 90+ min" message is hardcoded and approximately correct but may mislead. |
| `send_abandonment_alert()` | WARN | Revenue impact = `abandoned × AOV` — overstates because some abandonment is normal even at spike levels. |
| `send_payment_failure_alert()` | OK | Shows order names for direct action. |
| `send_js_error_alert()` | OK | No revenue estimate — correct per spec. |
| `send_oos_alert()` | OK | Formula breakdown shown inline. |
| `send_recovery_alert()` | OK | Generic across all incident types. |
| `_post()` | WARN | No retry on Slack failure. No rate limit handling. |
| `_send_email()` | WARN | No retry. Swallows all exceptions. |
| `_send_email()` | NOTE | No HTML version. No unsubscribe link (CAN-SPAM risk). |

---

## services/token_manager.py

| Function | Verdict | Findings |
|---|---|---|
| `get_valid_token()` | RACE | Two concurrent callers when token is expiring: both call `_call_token_endpoint` — Shopify invalidates refresh token after first use, second call fails. Needs a per-shop lock. |
| `get_valid_token()` | OK | 30-min buffer before expiry is appropriate. |
| `exchange_to_expiring()` | OK | Correct grant type for non-expiring → expiring exchange. WARNING in docstring is appropriate. |
| `_call_token_endpoint()` | OK | Form-encoded body with `Content-Type: application/x-www-form-urlencoded`. Correct. |
| `_parse_token_response()` | OK | Falls back to 86400s if `expires_in` absent. Safe. |

---

## migrations/schema.sql

| Item | Verdict | Findings |
|---|---|---|
| Idempotent DDL | OK | All `IF NOT EXISTS` checks. Safe to rerun. |
| `checkout_events` CHECK constraint | OK | Only allows valid event types. |
| Missing indexes | WARN | No index on `checkout_events.checkout_token` — needed for abandonment join. |
| No data retention DDL | BUG | Tables grow unbounded; privacy policy promises 30-day retention. |
| `billing_status` no CHECK | NOTE | Any string can be stored. |

---

## migrations/002_v2.sql

| Item | Verdict | Findings |
|---|---|---|
| Not auto-applied | CRITICAL | Will break v2 deploy if not run manually first. |
| Idempotent | OK | `IF NOT EXISTS` throughout. |
| `inventory_levels.product_id` nullable | OK | Structurally correct but never populated. |
| No retention DDL | BUG | Same issue as schema.sql. |

---

## checkout-guard/extensions/error-tracker

| Item | Verdict | Findings |
|---|---|---|
| Liquid block inline JS | OK | Functional. Gets shop domain via Liquid interpolation. |
| Asset `error-tracker.js` schema reference | BUG | Loads a second independent script that can't get shop domain — silently exits. Not a double-capture issue (asset is broken), but confusing. |
| `queue.splice(0)` in inline JS | WARN | No MAX_QUEUE cap — unbounded queue on rapid error bursts. |
| `sendBeacon` with Blob | OK | Correct CORS-compatible approach for cross-origin send on unload. |
| No `lineno`/`colno` in payload | NOTE | Spec doesn't require them but useful for debugging. |

---

## Dead Code

- `services/token_manager.py`: `exchange_to_expiring()` is used only by `scripts/exchange_token.py` (one-time migration script). Not dead code per se, but the exchange path is a one-time operation that could be simplified.
- `config.py`: No Slack webhook URL in config — each merchant has their own. Config is minimal and clean.
- `routes/billing.py`: `_CSS` constant is defined once and used inline in all 3 HTML functions. Could be a single module-level template.

---

## Architecture Violations vs Spec

| Spec requirement | Implementation | Verdict |
|---|---|---|
| `webhooks_raw` table + async WebhookProcessor | Inline processing | DEVIATION — acceptable for current scale |
| React + Polaris admin dashboard | Server-rendered HTML templates | DEVIATION — significant; breaks App Bridge embed requirement |
| Embedded App Bridge (iframe in Shopify admin) | Non-embedded (`embedded=false`) | DEVIATION — App Store review risk |
| 4 billing tiers ($29/$79/$199/$399) | Single plan at $29 | DEVIATION — manageable, add tiers later |
| Access tokens encrypted at rest | Plaintext in DB | DEVIATION — privacy policy false claim |
| 30-day data retention | No cleanup jobs | DEVIATION — privacy policy false claim |
