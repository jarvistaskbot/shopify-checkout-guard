# Module: Detection Engine

**File:** `services/detector.py` (~870 lines)  
**Purpose:** All anomaly detection logic — 6 detectors, incident lifecycle, baseline computation.

---

## 1. Detectors Overview

| # | Name | Signal | Window | Baseline | Alert condition |
|---|---|---|---|---|---|
| 1 | checkout_funnel_collapse | checkouts→orders conversion rate | 30 min | 7-day rolling | current_rate < baseline × 0.50 |
| 2 | volume_drop (order silence) | order count | 30 min | 28-day same-weekday/hour | drop ≥ alert_threshold_pct for 3 consecutive checks |
| 3 | abandonment_spike | unmatched checkouts 35–65 min old | 30 min | 7-day abandonment rate | rate > baseline×3 AND rate>60% |
| 4 | payment_failure | pending orders via Shopify API | on-demand | N/A (absolute: ≥3 pending) | ≥3 orders pending >15 min |
| 5 | js_error_spike (v2) | js_error_events hash count | 10 min | 24h lookback | ≥10 new (not baseline) occurrences |
| 6 | oos_hot_product (v2) | inventory_levels.available | real-time | 7d order history | hot product (≥5 orders/7d) hits 0 |

---

## 2. Constants

```python
_WINDOW_MINUTES = 30
_BASELINE_DAYS = 7
_MIN_BASELINE_VOLUME = 1.0

# Funnel
_MIN_CHECKOUTS_FOR_FUNNEL = 5
_FUNNEL_ALERT_THRESHOLD = 0.50   # alert if current < 50% of baseline

# Silence
_CONSECUTIVE_DROPS_REQUIRED = 3
_RECOVERY_CHECKS_REQUIRED = 2

# Abandonment
_ABANDONMENT_SPIKE_MULTIPLIER = 3.0
_MIN_ABANDONMENTS_FOR_SPIKE = 5

# Payment
_PAYMENT_PENDING_MINUTES = 15
_MIN_PENDING_FOR_ALERT = 3

# JS spike
_JS_SPIKE_MIN_COUNT = 10
_JS_SPIKE_WINDOW_MIN = 10
_JS_RESOLVE_QUIET_MIN = 60
_JS_RESOLVE_MAX_COUNT = 3
_JS_BASELINE_LOOKBACK_H = 24

# OOS
_OOS_HOT_THRESHOLD = 5
_OOS_LOOKBACK_DAYS = 7
```

---

## 3. Function Walk-Through

### `process_event(shop_domain, event_type)` — public entry point

Called by every webhook handler. Immediately fires `asyncio.create_task(_run_realtime_checks)` and returns. Double fire-and-forget pattern: the webhook handler itself was called from `await process_event()`, which internally creates another task. This is correct — it allows the webhook to return 200 quickly.

### `run_proactive_checks_all_merchants()` — called every 5 min

- Fetches all active merchants with their tokens.
- For each: `asyncio.create_task(_check_payment_failures(shop, token))`.
- Also: `asyncio.create_task(_resolve_stale_js_incidents())`.
- The per-merchant tasks run concurrently (all `create_task` calls before any await). At N merchants, all N payment checks run concurrently — not sequential.

### `_run_realtime_checks(shop_domain, event_type)` — private

- Fetches merchant record (all detection state in one SELECT).
- Runs: `_check_checkout_funnel`, `_check_order_silence`, and conditionally `_check_abandonment_spike`.
- All three share the same merchant record and `conn` — efficient.

---

## 4. Detector 1: Checkout Funnel Collapse

**Function:** `_check_checkout_funnel(conn, shop_domain, merchant, now)`

1. Count `checkout_created` events in last 30 min.
2. If < 5 checkouts: return (noise floor).
3. Count `order_created` events in last 30 min.
4. `current_rate = orders / checkouts`.
5. Fetch or compute `checkout_conversion_baseline` (lazy — stored on first computation).
6. If baseline < 5% (near-zero): skip.
7. `is_broken = current_rate < baseline * 0.50` — fires if rate halved.
8. If broken and no active incident: INSERT incident, send alert.
9. If active incident and rate recovered: `_resolve_incident`.

**Baseline computation** (`_compute_conversion_baseline`):
- 7-day lookback — all checkouts and orders.
- Requires ≥10 checkouts to produce a baseline (otherwise returns None).
- Baseline is stored in `merchants.checkout_conversion_baseline` after first computation.
- **Stale baseline risk:** The baseline is computed once and cached. If a merchant's baseline conversion rate changes significantly over time (seasonal, new product), the cached value drifts. No mechanism to periodically recompute it.

**Alert content:** Shows checkout count, order count, current vs baseline rate, estimated revenue at risk (missed_orders × AOV).

---

## 5. Detector 2: Order Silence (Volume Drop)

**Function:** `_check_order_silence(conn, shop_domain, merchant, now)`

1. Count orders in last 30 min (`current_volume`).
2. Compute `_compute_silence_baseline` (day-of-week + hour aware).
3. If baseline < 1.0: skip.
4. Compute `drop_pct = (baseline - current_volume) / baseline * 100`.
5. Update `drop_streak` and `recovery_streak` in merchants table on every check.
6. Alert if `drop_streak >= 3` and no active incident.
7. Resolve if `recovery_streak >= 2` and active incident.

**Silence baseline computation** (`_compute_silence_baseline`):
- Primary: last 28 days, same weekday, ±1 hour. Normalizes to per-30-min slot.
- Fallback: last 7 days, any weekday, same ±1 hour.
- **Correct day-of-week awareness** — avoids false alerts on expected slow weekdays.

**Persistence:** Streaks are persisted in DB with every check — correct. Process restart doesn't lose state.

**Issue:** `alert_threshold_pct` is pulled from `merchants.alert_threshold_pct` (default 20). This means "alert if drop_pct ≥ 20%". The streak of 3 consecutive checks means 90 minutes of sustained 20% drop before alert fires. This is reasonable for avoiding flapping.

---

## 6. Detector 3: Abandonment Spike

**Function:** `_check_abandonment_spike(conn, shop_domain, merchant, now)`

1. Count `checkout_created` events from 35–65 min ago with no matching `order_created` for the same token (`abandoned`).
2. If < 5 abandonments: skip.
3. Count total checkouts in same 35–65 min window (`checkouts_same_window`).
4. `current_abandon_rate = abandoned / max(1, checkouts_same_window)`.
5. Compute baseline abandonment rate from 7-day history.
6. Alert if `current_abandon_rate > baseline * 3.0 AND current_abandon_rate > 0.60`.

**Bug in baseline calculation (lines 378-393):**
```python
since = now - timedelta(days=_BASELINE_DAYS)
bl_checkouts = ... WHERE created_at BETWEEN $2 AND $3  # since → now-35min
bl_orders = ... WHERE created_at >= $2 AND checkout_token IS NOT NULL  # since only
```

`bl_checkouts` is bounded by `now - 35min` (giving a full 7-day window).  
`bl_orders` has `created_at >= since` — uses since but NO upper bound — counts ALL orders in 7 days including the last 35 min.

**Window mismatch:** bl_orders could include more recent orders than bl_checkouts, artificially inflating baseline conversion rate (lowering baseline abandonment rate), making the spike threshold harder to hit. Low practical impact for most stores but technically incorrect.

**Alert:** Fires if 3× spike AND abandon rate >60%. The dual condition is a good noise filter.

---

## 7. Detector 4: Payment Failures

**Function:** `_check_payment_failures(shop_domain, access_token)`

- Called from proactive monitor loop, not from webhook events.
- Checks Shopify Admin API for orders with `financial_status=pending` and `created_at_max=now-15min`.
- If ≥ 3 such orders: creates `payment_failure` incident.
- Skips if existing `payment_failure` incident is already open.
- **No auto-resolve logic** — payment failure incidents never resolve automatically! Once opened, they stay open until manually closed (no such UI exists) or until the shop is deleted. This is a bug: payment issues that self-resolve will show as permanently active incidents.

**Missing auto-resolve path:** The detector never calls `_resolve_incident` for payment_failure type. Other detectors check recovery conditions; this one does not.

---

## 8. Detector 5: JS Error Spike

**Function:** `check_js_error_spike(shop_domain, error_hash)` — called per event

1. Count occurrences of this `error_hash` in last 10 min.
2. If < 10: return.
3. Count same hash in prior 24h window (before current window) — if >0, it's "baseline noise," skip.
4. If no active incident for this hash: create incident, send alert.

**Function:** `_resolve_stale_js_incidents()` — called from proactive loop

- Fetches all open `js_error_spike` incidents.
- For each: count recent occurrences in last 60 min.
- If < 3: resolve incident, send recovery alert.

**Issue with active incident check (lines 573-585):** When checking for an existing active incident, it checks if the error_hash matches the detail field. But `_get_active_incident` only returns one incident (LIMIT 1). If there are multiple JS incidents open for different hashes, only the most recent is returned. A new spike on a different hash could be silently dropped if the hash check fails on the single returned incident.

---

## 9. Detector 6: OOS Hot Product

**Function:** `check_oos_hot_product(shop_domain, inventory_item_id, available)`

1. Check `settings.oos_enabled` flag — return if False.
2. Fetch merchant info.
3. **CRITICAL BUG:** `product_id = await conn.fetchval("SELECT product_id FROM inventory_levels WHERE ...")` — this always returns NULL because inventory_levels.product_id is NEVER populated by the webhook handler (webhooks.py only inserts `inventory_item_id` and `available`).
4. `if product_id is None: return` — exits early every time.
5. **OOS detection is completely non-functional.**

**Fix required:** On the inventory webhook, resolve `inventory_item_id → product_id` via Shopify Admin API:
```
GET /admin/api/2024-10/inventory_items/{inventory_item_id}.json
→ response.inventory_item.product_id (via variant lookup)
```
Or: store product_id in `inventory_levels` when it's first learned from order line items.

---

## 10. Helper Functions

### `_get_active_incident(conn, shop_domain, incident_type)`

- Fetches the most recent unresolved incident for a type.
- Returns None if none exists.
- Clean, correct.

### `_resolve_incident(conn, shop_domain, active, now, merchant)`

- Sets `resolved_at = now`.
- Fires recovery alert via `send_recovery_alert`.
- Requires `incident_type` via a second DB query (SELECT incidents WHERE id=$1) — could be passed as parameter to avoid the extra query.

---

## 11. Performance Concerns

### Missing Index: `checkout_events.checkout_token`

Abandonment spike detector does:
```sql
SELECT COUNT(DISTINCT ce.checkout_token) FROM checkout_events ce
WHERE ... AND NOT EXISTS (
    SELECT 1 FROM checkout_events ord
    WHERE ord.shop_domain=$1 AND ord.event_type='order_created'
      AND ord.checkout_token = ce.checkout_token
)
```

The NOT EXISTS subquery needs to match `checkout_token`. There is no index on `(shop_domain, event_type, checkout_token)`. At scale, this is a sequential scan on checkout_events filtered by shop and time.

### Silence Baseline EXTRACT Queries

```sql
EXTRACT(DOW FROM created_at AT TIME ZONE 'UTC')=$3
AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC') BETWEEN $4 AND $5
```

Index on `(shop_domain, event_type, created_at)` exists — but EXTRACT function prevents index usage on the created_at dimension for filtering DOW/hour. At large volumes, this scans all events for the shop in the 28-day window.

### Baseline Not Refreshed

`checkout_conversion_baseline` is computed once and never updated. A stale baseline could cause persistent false positives or missed true positives.

---

## 12. Race Conditions

### Payment Failure Double-Insert

Two concurrent proactive loops (unlikely but possible at startup) could both check payment failures and both find no active incident, both inserting a new one. The check `if active: return` inside `_check_payment_failures` is NOT transactional — both tasks could pass the check before either inserts.

Fix: Use a unique constraint or INSERT ... ON CONFLICT to prevent duplicate open incidents per shop/type.

### Token Refresh Race

`get_valid_token` is called by multiple concurrent callers. If two concurrent callers both find the token expiring and both call `_call_token_endpoint`, Shopify invalidates the old refresh token after the first use — the second call fails. This is a real race condition under the proactive loop + payment check running simultaneously.
