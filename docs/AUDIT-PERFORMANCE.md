# Performance Audit

---

## 1. API Response Latency

### Webhook Handlers
- **Critical path:** HMAC verification + INSERT checkout_events + optional INSERT order_line_items + process_event (create_task, no-op latency)
- **Estimated latency:** 20–50ms at current scale (VPS + local Postgres)
- **Shopify requirement:** < 5 seconds
- **Status:** ✅ Well within limit at current scale

### /events Endpoint
- **Critical path:** Content-length check + JSON parse + DB merchant lookup (per event) + INSERT + create_task
- **Issue:** Up to 50 sequential DB queries per batch — one `SELECT 1 FROM merchants` per event. A 50-event batch does 50 separate queries. These are fast point lookups on PRIMARY KEY (`shop_domain`) but still 50 round-trips.
- **Fix:** `SELECT shop_domain FROM merchants WHERE shop_domain = ANY($1::text[]) AND active = TRUE` once per batch — one query for all unique shops.

### /dashboard
- **Queries per request:** 5 (merchant row, active incidents, recent incidents, checkout count, order count)
- **Estimated latency:** 50–150ms
- **Status:** Acceptable for server-rendered dashboard

---

## 2. Database Query Analysis

### checkout_events Table

| Query | Index available? | Notes |
|---|---|---|
| Count by shop+type+time (funnel, silence) | ✅ `idx_checkout_events_type` | Good |
| NOT EXISTS subquery on checkout_token (abandonment) | ❌ No index on checkout_token | Potential full scan at scale |
| DOW+HOUR EXTRACT (silence baseline) | ❌ Function prevents index usage | Full scan of 28-day window |
| Count by shop+type+time (dashboard) | ✅ | Good |

**Most expensive query:** `_compute_silence_baseline` runs a 28-day lookback with `EXTRACT(DOW...)` and `EXTRACT(HOUR...)`. PostgreSQL cannot use `idx_checkout_events_type` for the DOW/HOUR predicates since they're computed from `created_at`. At 100 checkouts/day × 28 days = 2,800 rows per shop — manageable. At 1,000 checkouts/day × 28 days = 28,000 rows — the EXTRACT scan starts being noticeable.

**Missing index:**
```sql
CREATE INDEX idx_checkout_events_token ON checkout_events (shop_domain, checkout_token)
WHERE checkout_token IS NOT NULL;
```
This would accelerate the abandonment spike NOT EXISTS join.

### js_error_events Table

| Query | Index available? | Notes |
|---|---|---|
| Count by shop+hash+time (spike check) | ✅ `idx_js_error_events_shop_hash_time` | Good |
| Count prior window (baseline check) | ✅ Same compound index | Good |
| SELECT sample by shop+hash (alert data) | ✅ Same index | Good |
| Stale incident resolution | ✅ Same index | Good |

**Well-indexed for its access patterns.** ✅

### order_line_items Table

| Query | Index available? | Notes |
|---|---|---|
| SUM(quantity) by shop+product+time (OOS hot check) | ✅ `idx_order_line_items_shop_product_time` | Good (moot while OOS broken) |
| SELECT product_title/price by shop+product | ✅ Same index | Good |

### inventory_levels Table

| Query | Index available? | Notes |
|---|---|---|
| SELECT product_id by shop+inventory_item_id | ✅ UNIQUE index | Good (returns NULL always, but fast) |
| JSONB text cast for OOS incident lookup | ❌ Not indexed | `detail->>'product_id' = $2::text` is a JSONB text search |

**JSONB scan in OOS incident check:** `incidents WHERE detail->>'product_id' = $2::text`. If many OOS incidents accumulate, this scans the entire `incidents` table filtered by shop + type. A GIN index on the `detail` JSONB column would help, but given OOS is broken, moot.

---

## 3. Background Processing Scalability

### Proactive Monitor Loop (every 5 min)

```python
shops = await conn.fetch("SELECT shop_domain, access_token FROM merchants WHERE active = TRUE")
for row in shops:
    asyncio.create_task(_check_payment_failures(row["shop_domain"], row["access_token"]))
asyncio.create_task(_resolve_stale_js_incidents())
```

**Current:** All payment checks fire concurrently (create_task), good.  
**Issue at scale:** Each `_check_payment_failures` makes an HTTP call to Shopify Admin API. At 100 merchants, 100 concurrent HTTP requests every 5 minutes. Shopify rate limits: 40 requests/app/store/minute (leaky bucket) but 100 requests across 100 different stores is fine since it's per-store.

**Real bottleneck at scale:** asyncpg pool max=10. With 100 concurrent payment checks each acquiring a connection, 90 tasks queue up waiting for a pool slot. With 15s httpx timeout per task, the entire payment check round could take (90 / 10) × 15s = 135s, exceeding the 5-min interval. Need to increase pool size or batch the checks.

**Fix for 50+ merchants:** Increase pool to `max_size=20` and implement a semaphore to cap concurrent API calls:
```python
_payment_check_sem = asyncio.Semaphore(20)
```

### Token Refresh Loop (every 20 min)

Holds one connection for the duration of iterating all merchants:
```python
async with pool.acquire() as conn:
    rows = await conn.fetch("SELECT shop_domain, token_expires_at FROM merchants WHERE active = TRUE")
    for row in rows:
        await get_valid_token(conn, ...)  # each refresh is an HTTP call while holding conn
```

**Issue:** If any `get_valid_token` call takes 15s (timeout), the connection is held for 15s, reducing pool availability. With 10 merchants × 15s timeout worst case = 150s holding one connection.

**Fix:** Fetch the list with one connection, release it, then for each merchant acquire a separate connection for the refresh:
```python
rows = await (await get_pool()).fetch("SELECT ...")
for row in rows:
    async with (await get_pool()).acquire() as conn:
        await get_valid_token(conn, ...)
```

---

## 4. Table Growth & Data Retention

| Table | Growth rate (100 merchants avg) | 1-year size | Without cleanup |
|---|---|---|---|
| `checkout_events` | 100 merchants × 200 events/day = 20,000/day | 7.3M rows | Grows forever |
| `js_error_events` | 100 merchants × 50 errors/day = 5,000/day | 1.8M rows | Grows forever |
| `order_line_items` | 100 merchants × 100 items/day = 10,000/day | 3.6M rows | Grows forever |
| `incidents` | Low (bounded by anomalies) | ~50K rows/year | Acceptable |
| `inventory_levels` | Bounded (1 row per inventory item) | ~10K rows | Bounded |
| `merchants` | Low (installs - uninstalls) | <10K rows | Acceptable |

**At 100 merchants after 1 year without cleanup: ~12.7M rows across the hot tables.** checkout_events EXTRACT queries degrade quadratically as the table grows.

**Required:** Nightly cleanup jobs:
```sql
DELETE FROM checkout_events WHERE created_at < NOW() - INTERVAL '30 days';
DELETE FROM js_error_events WHERE occurred_at < NOW() - INTERVAL '30 days';
DELETE FROM order_line_items WHERE created_at < NOW() - INTERVAL '7 days';
```

---

## 5. Caching Opportunities

| What | Current | Improvement |
|---|---|---|
| `checkout_conversion_baseline` | Computed once, stored in merchants col | ✅ Already cached, but never refreshed |
| Merchant settings per request | SELECT on every webhook | Cache in-memory with 5-min TTL (invalidate on settings change) |
| Silence baseline (28-day EXTRACT) | Computed every check | Cache in Redis/memory with 1h TTL |
| Shop domain validation in /events | 1 SELECT per event per batch | Batch query + in-process LRU cache with 60s TTL |

---

## 6. Theme Extension JS

**Payload size:** The inline JS in `error-tracker.liquid` is approximately 800 bytes (before minification). Well under the 5KB target. ✅

**Batching:** 10-second batch window reduces HTTP requests significantly vs per-error sends. ✅

**sendBeacon on unload:** Correct pattern for reliability on page exit. ✅

**Potential issue:** If a page has rapidly firing errors (tight loop), the queue grows indefinitely (no MAX_QUEUE cap in inline version). In extreme cases this could consume significant memory before the 10s flush.

---

## 7. Connection Pool Sizing

- min_size=2, max_size=10
- At 10 concurrent webhook requests, pool is saturated
- Current VPS workload (1 merchant, low traffic): fine
- At 50+ merchants with concurrent webhook floods: increase to max_size=20-30
- PostgreSQL default max_connections=100, so headroom exists
