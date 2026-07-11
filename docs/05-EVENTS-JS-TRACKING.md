# Module: JS Error Event Intake

**File:** `routes/events.py`  
**Purpose:** Public HTTP endpoint that receives batched JS error events from the Theme App Extension running on merchant storefronts.

---

## 1. Business Value

JS error tracking catches silent client-side failures that break the checkout experience without triggering any Shopify webhook. A TypeError in a theme's cart JS could prevent the "Add to Cart" button from working — causing 100% abandonment with no server-side signal. `/events` is the only way to detect this class of problem.

---

## 2. User Flow

```
Merchant installs Theme App Extension
→ Liquid block renders inline JS on cart/product pages
→ window.onerror / unhandledrejection captures browser errors
→ Queued for 10s, then flushed via sendBeacon or fetch
→ POST https://checkoutguardalerts.com/events  (JSON array)
→ Rate-limited, deduplicated by hash, stored in js_error_events
→ spike check triggers if ≥10 of same hash in 10 min
```

---

## 3. Constants

```python
_MAX_PAYLOAD_BYTES = 8192          # 8KB total payload limit
_MAX_MESSAGE_LEN = 500             # error message truncation
_MAX_SOURCE_LEN = 200              # filename/source truncation
_MAX_URL_LEN = 500                 # page URL truncation
_MAX_BATCH = 50                    # max events per batch
_RATE_LIMIT_EVENTS_PER_MIN = 120   # per shop per minute
```

---

## 4. Function Walk-Through

### `_is_rate_limited(shop)` — sliding window rate limiter

- Maintains `_rate_windows: dict[shop → list[timestamps]]` in process memory.
- On each call: prunes timestamps older than 60s.
- If `len(window) >= 120`: returns True (rate limited).
- Otherwise: appends current timestamp, returns False.

**Issue 1: Memory leak.** `_rate_windows` grows unboundedly. Shops that stop sending events keep their entry in the dict forever. A shop that floods once and then goes quiet leaves a (likely empty) list in the dict. At scale with many shops this is a minor leak — entries are tiny — but it never shrinks.

**Issue 2: Content-Length bypass.** The payload size check at lines 63-65:
```python
content_length = int(request.headers.get("content-length", 0))
if content_length > _MAX_PAYLOAD_BYTES:
    raise HTTPException(status_code=413, detail="Payload too large")
```
If an attacker omits the `Content-Length` header, `content_length = 0` and the check passes. The body is then read via `await request.json()` — which reads the full body regardless of size. A large request without Content-Length header bypasses this check. Should read body with a size limit: `body = await request.body()` with a `starlette` request size limit, or check `len(body)` after reading.

**Issue 3: In-memory rate limiter breaks at multi-instance.** Each process has its own `_rate_windows`. Under horizontal scaling, a shop can flood at `120 × N_instances` events/min.

### `_sanitize_shop(shop)` — shop domain validation

- Strips whitespace, lowercases.
- Rejects if empty, >100 chars, contains spaces, or double dots.
- Does NOT verify it's a `.myshopify.com` domain or any known pattern.
- A spoofed `shop` like `evil.com` passes sanitization. But the next check (`exists = await conn.fetchval("SELECT 1 FROM merchants WHERE ...")`) prevents insertion for unknown shops.

### `ingest_events(request)` — POST /events

1. Check Content-Length header (bypassable — see above).
2. Parse JSON body (dict or list accepted).
3. Acquire DB connection.
4. For each event (up to `_MAX_BATCH=50`):
   a. Parse into `_ErrorEvent` Pydantic model (validates required fields).
   b. Sanitize shop domain.
   c. Check rate limit.
   d. **DB lookup:** `SELECT 1 FROM merchants WHERE shop_domain=$1 AND active=TRUE` — one query per event in the batch. For a 50-event batch, this is 50 queries. Should fetch shop list once per batch and do a set lookup.
   e. Truncate message, source, URL.
   f. Compute `error_hash = sha256(message|source)[:32]`.
   g. INSERT into `js_error_events`.
   h. Fire `asyncio.create_task(_trigger_js_spike_check(shop, error_hash))`.
5. Return `{"ok": True, "accepted": count}`.

**Performance concern:** Up to 50 DB lookups per batch (one per event for shop validation). This should be a single `SELECT shop_domain FROM merchants WHERE shop_domain = ANY($1) AND active=TRUE` with the unique shops in the batch.

### `_trigger_js_spike_check(shop, error_hash)` — fire-and-forget

- Calls `check_js_error_spike` from detector.
- Wrapped in try/except with logger.error.
- This is where the spike detection actually runs per event.

### `_ErrorEvent` — Pydantic model

```python
class _ErrorEvent(BaseModel):
    shop: str          # required
    message: str       # required
    source: Optional[str] = ""
    url: str           # required
    ts: Optional[float] = None
    lineno: Optional[int] = None
    colno: Optional[int] = None
```

`lineno` and `colno` are parsed but never stored — the INSERT doesn't include them. This is wasted parsing but not a bug.

---

## 5. Security

| Issue | Severity | Detail |
|---|---|---|
| Public unauthenticated endpoint | By design | No Shopify HMAC here — browser JS can't sign requests |
| Shop spoofing | Low risk | Mitigated by merchants table lookup |
| Flood attack | MEDIUM | Content-Length bypass allows large payloads; rate limiter bypassable by omitting header |
| Rate limiter in-memory | MEDIUM | Breaks at multi-instance; flooder could target multiple instances |
| XSS via stored error messages | Low | error_message is stored and displayed in dashboard table. Dashboard renders it as `{label}` in a `<td>`. dashboard.py:207 uses `label = _INCIDENT_LABELS.get(...)` not the raw message, but the `detail` dict is rendered via `_fmt_impact`. If `message` contains HTML: it could be injected via the dashboard's detail display — verify escaping. |

---

## 6. Missing Functionality

- `lineno` and `colno` are accepted but not stored — useful for developer debugging.
- No deduplication at ingestion: the same error from one browser session fires multiple events per batch. The hash-based dedup happens at the detection layer (baseline check), not at ingestion.
- No `source` (filename) stored in the DB — the hash includes source but the column doesn't exist in `js_error_events`. The `page_url` is stored but not the script filename.

---

## 7. Improvement Recommendations

1. Fix Content-Length bypass: use `body = await request.body()` then `if len(body) > _MAX_PAYLOAD_BYTES: raise 413`.
2. Batch the shop validation query: one `ANY($1)` query per batch instead of N queries.
3. Add `source`, `lineno`, `colno` columns to `js_error_events` for richer debugging context.
4. Move rate limiter to Redis for multi-instance correctness.
5. Prune `_rate_windows` periodically (or use `functools.lru_cache` with TTL).
