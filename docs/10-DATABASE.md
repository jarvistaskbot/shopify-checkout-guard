# Module: Database

**Files:** `database.py`, `migrations/schema.sql`, `migrations/002_v2.sql`  
**Purpose:** Connection pool management and schema definition for all persistent state.

---

## 1. Connection Pool

**File:** `database.py`

```python
_pool: asyncpg.Pool | None = None

async def create_pool(dsn: str) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    await _apply_schema(_pool)
    return _pool

async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    return _pool

async def _apply_schema(pool: asyncpg.Pool) -> None:
    schema = (Path(__file__).parent / "migrations" / "schema.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema)
```

**Pool settings:** min=2, max=10. At 10 concurrent requests, all connections are held. Default PostgreSQL max_connections=100 — safe headroom.

**Schema auto-apply:** `schema.sql` is applied on every startup via `_apply_schema`. All DDL uses `CREATE TABLE IF NOT EXISTS` and idempotent `DO $$ BEGIN IF NOT EXISTS...` blocks — safe for repeated application.

**Issue:** `002_v2.sql` is NOT auto-applied. Only `schema.sql` runs on startup. The v2 migration (js_error_events, order_line_items, inventory_levels, alert_email column) must be applied manually before v2 deploys. There is no migration runner. If deployed without running 002_v2.sql, all v2 routes will crash on first DB access.

---

## 2. Full Schema

### Table: `merchants`

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `shop_domain` | TEXT | PRIMARY KEY | Shopify store domain (e.g. store.myshopify.com) |
| `access_token` | TEXT | NOT NULL | Shopify API token (plaintext) |
| `refresh_token` | TEXT | nullable | For token refresh (may be absent for old installs) |
| `token_expires_at` | TIMESTAMPTZ | nullable | NULL = non-expiring token |
| `slack_webhook_url` | TEXT | nullable | Merchant's Slack incoming webhook URL |
| `alert_threshold_pct` | INTEGER | NOT NULL DEFAULT 20 | Volume drop % to trigger silence alert |
| `installed_at` | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | Install timestamp |
| `active` | BOOLEAN | NOT NULL DEFAULT TRUE | FALSE after uninstall |
| `drop_streak` | INTEGER | NOT NULL DEFAULT 0 | Consecutive silence detections (order_silence) |
| `recovery_streak` | INTEGER | NOT NULL DEFAULT 0 | Consecutive recovery detections |
| `avg_order_value` | NUMERIC(10,2) | NOT NULL DEFAULT 50.00 | AOV for revenue impact estimates |
| `billing_charge_id` | TEXT | nullable | Shopify charge ID |
| `billing_status` | TEXT | NOT NULL DEFAULT 'trialing' | pending/active/trialing/declined/expired |
| `billing_activated_at` | TIMESTAMPTZ | nullable | When billing activated |
| `trial_ends_at` | TIMESTAMPTZ | nullable | 14 days from install |
| `checkout_conversion_baseline` | NUMERIC(5,4) | nullable | Cached conversion rate baseline |
| `alert_email` | TEXT | nullable | v2: email for alerts (from 002_v2.sql) |

**Missing index:** No secondary index. Primary key covers `WHERE shop_domain = $1`. Fine for current scale.

**Missing constraint:** `billing_status` has no CHECK constraint — any string can be stored. Should be `CHECK (billing_status IN ('pending', 'active', 'trialing', 'declined', 'expired'))`.

**Missing encryption:** `access_token`, `refresh_token`, `slack_webhook_url` stored plaintext. Privacy policy falsely claims encryption.

---

### Table: `checkout_events`

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | BIGSERIAL | PRIMARY KEY | Auto-increment |
| `shop_domain` | TEXT | NOT NULL, FK → merchants CASCADE | Store association |
| `event_type` | TEXT | NOT NULL, CHECK IN ('checkout_created','checkout_deleted','order_created') | Event classification |
| `checkout_token` | TEXT | nullable | Links checkout→order for funnel tracking |
| `order_id` | TEXT | nullable | Shopify order ID (for order events) |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | Event timestamp |

**Indexes:**
- `idx_checkout_events_shop_created` on `(shop_domain, created_at)` — covers time-range queries
- `idx_checkout_events_type` on `(shop_domain, event_type, created_at)` — covers event-type + time queries

**Missing index:** `(shop_domain, event_type, checkout_token)` — needed for the abandonment spike NOT EXISTS subquery that matches on checkout_token.

**No retention policy:** This table grows unboundedly. For a merchant with 100 checkouts/day, it accumulates 36,500 rows/year. The privacy policy states 30-day retention but no cleanup job exists. At scale (100+ merchants), this becomes a storage and query performance problem.

---

### Table: `incidents`

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | BIGSERIAL | PRIMARY KEY | Auto-increment |
| `shop_domain` | TEXT | NOT NULL, FK → merchants CASCADE | Store association |
| `started_at` | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | Incident start |
| `resolved_at` | TIMESTAMPTZ | nullable | NULL = active; set = resolved |
| `checkout_rate_before` | NUMERIC(6,4) | NOT NULL | Baseline rate at incident start |
| `checkout_rate_during` | NUMERIC(6,4) | NOT NULL | Current rate at detection |
| `estimated_revenue_loss_per_min` | NUMERIC(12,2) | NOT NULL | Revenue impact estimate |
| `avg_order_value` | NUMERIC(10,2) | NOT NULL | AOV snapshot at incident time |
| `notified` | BOOLEAN | NOT NULL DEFAULT FALSE | Whether alert was sent |
| `incident_type` | TEXT | NOT NULL DEFAULT 'volume_drop' | Type of incident (from idempotent ALTER) |
| `detail` | JSONB | nullable | Type-specific metadata |

**Indexes:**
- `idx_incidents_shop_active` PARTIAL on `(shop_domain, resolved_at) WHERE resolved_at IS NULL` — efficiently finds active incidents

**Type misuse:** For JS error and OOS incidents, `checkout_rate_before`, `checkout_rate_during`, `estimated_revenue_loss_per_min`, `avg_order_value` are all set to 0 (meaningless for those types). These columns are a legacy of the original single-detector schema. The `detail` JSONB field carries the actual data for v2 incident types.

**Missing constraint:** `incident_type` has no CHECK constraint.

**Missing deduplication:** Two concurrent detectors could both find no active incident and both INSERT. No UNIQUE constraint on `(shop_domain, incident_type)` for active incidents. This race is unlikely but not prevented.

---

### Table: `js_error_events` (v2, from 002_v2.sql)

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | BIGSERIAL | PRIMARY KEY | Auto-increment |
| `shop_domain` | TEXT | NOT NULL, FK → merchants CASCADE | Store association |
| `error_hash` | VARCHAR(64) | NOT NULL | SHA256[:32] of (message|source) |
| `error_message` | TEXT | NOT NULL | Truncated error message |
| `page_url` | TEXT | NOT NULL | Page where error occurred |
| `occurred_at` | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | Event timestamp |

**Indexes:**
- `idx_js_error_events_shop_hash_time` on `(shop_domain, error_hash, occurred_at)` — covers spike count queries
- `idx_js_error_events_shop_time` on `(shop_domain, occurred_at)` — covers time-range queries

**No retention policy.** At 100 errors/day per merchant, 36,500 rows/year. The 24h baseline lookback only uses recent data; older rows are waste. Add a nightly `DELETE FROM js_error_events WHERE occurred_at < NOW() - INTERVAL '30 days'` cron.

---

### Table: `order_line_items` (v2, from 002_v2.sql)

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | BIGSERIAL | PRIMARY KEY | Auto-increment |
| `shop_domain` | TEXT | NOT NULL, FK → merchants CASCADE | Store association |
| `shopify_order_id` | BIGINT | NOT NULL | Shopify order ID |
| `product_id` | BIGINT | nullable | Shopify product ID |
| `product_title` | TEXT | nullable | Product name (max 500 chars in code) |
| `variant_id` | BIGINT | nullable | Shopify variant ID |
| `quantity` | INTEGER | NOT NULL DEFAULT 1 | Units ordered |
| `price` | NUMERIC(10,2) | nullable | Unit price at time of order |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | Order timestamp |

**Indexes:**
- `idx_order_line_items_shop_product_time` on `(shop_domain, product_id, created_at)` — covers hot product lookups

**No retention policy.** 7-day lookback for hot product detection — rows older than 7 days are waste. Add nightly cleanup.

---

### Table: `inventory_levels` (v2, from 002_v2.sql)

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | BIGSERIAL | PRIMARY KEY | Auto-increment |
| `shop_domain` | TEXT | NOT NULL, FK → merchants CASCADE | Store association |
| `inventory_item_id` | BIGINT | NOT NULL | Shopify inventory item ID |
| `product_id` | BIGINT | nullable | **NEVER POPULATED (bug)** |
| `available` | INTEGER | NOT NULL | Current available quantity |
| `updated_at` | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | Last webhook timestamp |

**Indexes:**
- `idx_inventory_levels_shop_item` UNIQUE on `(shop_domain, inventory_item_id)` — enforces one row per item
- `idx_inventory_levels_shop_product` on `(shop_domain, product_id)` — for product lookups (currently useless since product_id is always NULL)

**Critical:** `product_id` column exists but is never populated. The UNIQUE index on `(shop_domain, inventory_item_id)` enables correct UPSERT behavior.

---

## 3. Migration Strategy

**Current approach:** 
- `schema.sql` — applied automatically on startup (idempotent DDL)
- `002_v2.sql` — must be applied manually before v2 deploy

**Problems:**
1. No migration runner or version tracking (no `schema_migrations` table)
2. `_apply_schema` runs `schema.sql` on every boot — works but inefficient
3. `002_v2.sql` is labeled "Apply ONLY after v2 is deployed" but is in the repo with no automated path
4. If v2 is deployed without running 002_v2.sql, every v2 route fails with "table not found"

**Recommendation:** Adopt Alembic or a simple manual migration table:
```sql
CREATE TABLE IF NOT EXISTS schema_versions (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT NOW());
```
Run migrations sequentially, skip already-applied versions.

---

## 4. Retention Policy (Missing)

| Table | Data age | Privacy policy promise | Current behavior | Fix |
|---|---|---|---|---|
| `checkout_events` | Unbounded | 30 days | Never cleaned | Nightly DELETE WHERE created_at < NOW() - 30d |
| `js_error_events` | Unbounded | (implied) | Never cleaned | Nightly DELETE WHERE occurred_at < NOW() - 30d |
| `order_line_items` | Unbounded | (implied) | Never cleaned | Nightly DELETE WHERE created_at < NOW() - 7d (OOS only needs 7d) |
| `inventory_levels` | 1 row/item | N/A | UPSERT pattern = bounded | OK |
| `incidents` | Unbounded | (implied) | Never cleaned | Keep 90d for analytics |
| `merchants` | Deleted on shop/redact | 48h | Only on GDPR webhook | OK |
