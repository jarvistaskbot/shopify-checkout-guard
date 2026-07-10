# Module: Out-of-Stock Detection

**Files:** `routes/webhooks.py` (inventory_updated handler), `services/detector.py` (check_oos_hot_product), `migrations/002_v2.sql` (inventory_levels, order_line_items tables)  
**Purpose:** Alert merchants the instant a high-demand product hits zero inventory.

---

## 1. Business Value

Traditional OOS recovery apps (Back in Stock, Klaviyo flows) are reactive — they capture demand after a product goes OOS. CheckoutGuard is proactive: it fires the moment a hot product hits zero, giving the merchant a chance to restock, redirect traffic, or hide the product before significant revenue is lost.

**Target metric:** A product selling 5 units/7 days represents ~$250–500/week for an average Shopify store. Even a 12-hour OOS window costs real money.

---

## 2. Current State: BROKEN

**The OOS detector is completely non-functional due to a bug in the inventory webhook handler.**

### Root Cause

`inventory_levels` table has a `product_id BIGINT` column, but the inventory webhook handler in `webhooks.py:209-242` never populates it:

```python
# webhooks.py:225-234 — WHAT'S THERE
await conn.execute(
    """INSERT INTO inventory_levels (shop_domain, inventory_item_id, available, updated_at)
       VALUES ($1, $2, $3, NOW())
       ON CONFLICT (shop_domain, inventory_item_id)
       DO UPDATE SET available = EXCLUDED.available, updated_at = NOW()""",
    x_shopify_shop_domain, int(inventory_item_id), int(available),
)
```

```python
# detector.py:726-730 — WHAT THE DETECTOR EXPECTS
product_id = await conn.fetchval(
    "SELECT product_id FROM inventory_levels WHERE shop_domain=$1 AND inventory_item_id=$2",
    shop_domain, inventory_item_id,
)
if product_id is None:
    return  # Can't identify product — aborts every time
```

Every OOS check exits at line 730. No incidents are ever created. No alerts are ever sent.

---

## 3. Why product_id Is Missing

Shopify's `inventory_levels/update` webhook payload contains:
- `inventory_item_id` — Shopify's internal inventory item identifier
- `available` — current available quantity
- `location_id` — which fulfillment location

It does NOT contain `product_id` directly. To resolve `inventory_item_id → product_id`, you must call:
```
GET /admin/api/2024-10/inventory_items/{inventory_item_id}.json
```
This returns the inventory item including its associated variant and product IDs.

The webhook handler never makes this API call. `product_id` is always NULL in `inventory_levels`.

---

## 4. Data Flow (As Designed, Not As Working)

```
Shopify fires inventory_levels/update webhook
→ POST /webhooks/inventory/update
→ UPSERT inventory_levels (item + available)
→ check_oos_hot_product(shop, inventory_item_id, available)
   ├─ Look up product_id from inventory_levels  ← ALWAYS NULL
   ├─ Count order_line_items for product in last 7 days
   ├─ If < 5: skip (not hot)
   └─ If available == 0 AND hot: create oos_hot_product incident
      └─ send_oos_alert (Slack + email)
```

---

## 5. Fix Required

**Option A (minimal, correct for v2):** Resolve product_id on demand via Shopify API.

In `inventory_updated` webhook handler, after the UPSERT, if `product_id` is not yet known:
```python
async with httpx.AsyncClient(timeout=10) as client:
    item_resp = await client.get(
        f"https://{shop_domain}/admin/api/2024-10/inventory_items/{inventory_item_id}.json",
        headers={"X-Shopify-Access-Token": access_token},
    )
    if item_resp.status_code == 200:
        variant_id = item_resp.json()["inventory_item"]["variant_id"]
        # Then GET /variants/{variant_id}.json to get product_id
        ...
        await conn.execute(
            "UPDATE inventory_levels SET product_id=$1 WHERE shop_domain=$2 AND inventory_item_id=$3",
            product_id, shop_domain, inventory_item_id,
        )
```

This requires an additional API call per new inventory item. Cache the mapping in DB — once discovered, no further API calls needed for that item.

**Option B (use order history):** The `order_line_items` table already stores product_id. When OOS fires, join through order_line_items to find the product_id for the given variant/item. This doesn't require an API call but requires that the product was previously ordered.

**Recommendation:** Option A for immediate correctness; Option B as fallback for new products with no order history.

---

## 6. Hot Product Logic

A "hot product" is one with `≥ 5 orders (SUM of quantity) in last 7 days` in `order_line_items`. This is a reasonable proxy for demand — it's based on actual sales, not pageviews or wishlist adds.

**Threshold discussion:** 5 units/7 days = ~0.7/day. For a $50 product this represents ~$250/week. For a $200 product, ~$1,000/week. The threshold should arguably be tunable per-merchant or based on absolute revenue, not just unit count.

---

## 7. Revenue Estimate Calculation

```python
units_per_hour = order_count / (_OOS_LOOKBACK_DAYS * 24)
revenue_per_hour = round(units_per_hour * float(unit_price), 2)
```

This is the correct formula: historical run rate extrapolated hourly. Stored in incident detail. Alert shows `~$X/hr estimated`.

The formula is labeled "Medium-High confidence" in the alert — appropriate, since it assumes demand is constant (ignores time-of-day variation, seasonal trends).

---

## 8. Auto-Resolve

When `available > 0` arrives for a product with an open OOS incident: `resolved_at = NOW()`. This is correct and immediate.

---

## 9. Feature Flag

`OOS_ENABLED` env var (default `False`) gates the entire feature. When False:
- `check_oos_hot_product` returns immediately
- `_subscribe_webhooks` does not register `inventory_levels/update`

This is appropriate for v1 (pre-approval) since `read_inventory` scope is not yet granted.

---

## 10. Improvement Roadmap

1. **Fix product_id mapping** (CRITICAL — blocks the entire feature).
2. **Add inventory_item → product_id resolution** via Shopify API on first encounter.
3. **Add `product_id` NOT NULL constraint** once the fix is deployed — prevents the silent failure mode.
4. **Make hot threshold configurable**: per-merchant setting in DB.
5. **Multi-location support**: current implementation doesn't distinguish locations — a product at zero in one location but available elsewhere might fire incorrectly if Shopify ships from the other location.
6. **Backfill product_id**: After fix deploys, run a one-time script to populate product_id for all existing inventory_levels rows.
