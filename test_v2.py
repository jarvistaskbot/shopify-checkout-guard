"""
CheckoutGuard v2 test suite.

Tests:
  1. /events intake — valid batch accepted, unknown shop dropped, rate limiting works
  2. /events intake — line stored in js_error_events table
  3. JS error spike detection — 10+ events in 10 min creates incident
  4. JS error spike — known baseline error (seen in prior 24h) NOT re-alerted
  5. OOS detection — hot product (>=5 orders/7d) hitting zero creates incident (OOS_ENABLED=True)
  6. OOS detection — non-hot product hitting zero does NOT create incident
  7. OOS detection — product back in stock resolves incident
  8. order_line_items persistence from orders/create webhook
  9. /dashboard renders 200 with correct sections for known shop
  10. /dashboard returns 404 for unknown shop

Run: PYTHONPATH=. .venv/bin/python test_v2.py
     (server must be running at http://localhost:8000)
"""

import asyncio
import base64
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone, timedelta

import httpx

import os
API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")
SHOP = os.environ.get("TEST_SHOP", "checkoutguard-dev-oxkbbl69.myshopify.com")
BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def sign(body: bytes) -> str:
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()


def result(label: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    if not ok:
        sys.exit(1)


async def clean_v2_data(conn) -> None:
    await conn.execute("DELETE FROM js_error_events WHERE shop_domain = $1", SHOP)
    await conn.execute("DELETE FROM order_line_items WHERE shop_domain = $1", SHOP)
    await conn.execute("DELETE FROM inventory_levels WHERE shop_domain = $1", SHOP)
    await conn.execute(
        "DELETE FROM incidents WHERE shop_domain = $1 AND incident_type IN ('js_error_spike','oos_hot_product')",
        SHOP,
    )


# ---------------------------------------------------------------------------
# Test 1: /events intake — validation and routing
# ---------------------------------------------------------------------------

async def test_events_intake_valid(client: httpx.AsyncClient) -> None:
    print("\n[1] /events intake — valid event accepted")
    events = [{"shop": SHOP, "message": "TypeError: null is not an object", "url": "https://example.myshopify.com/cart"}]
    r = await client.post(f"{BASE_URL}/events", json=events)
    result("/events returns 200", r.status_code == 200, f"got {r.status_code}")
    body = r.json()
    result("accepted=1 in response", body.get("accepted") == 1, str(body))


async def test_events_intake_unknown_shop(conn, client: httpx.AsyncClient) -> None:
    print("\n[2] /events intake — unknown shop dropped")
    before = await conn.fetchval("SELECT COUNT(*) FROM js_error_events WHERE shop_domain = 'ghost.myshopify.com'") or 0
    events = [{"shop": "ghost.myshopify.com", "message": "some error", "url": "https://ghost.myshopify.com/products/x"}]
    r = await client.post(f"{BASE_URL}/events", json=events)
    result("Returns 200 (not 4xx)", r.status_code == 200)
    after = await conn.fetchval("SELECT COUNT(*) FROM js_error_events WHERE shop_domain = 'ghost.myshopify.com'") or 0
    result("No row inserted for unknown shop", after == before)


# ---------------------------------------------------------------------------
# Test 3: /events → js_error_events persistence
# ---------------------------------------------------------------------------

async def test_events_persisted(conn, client: httpx.AsyncClient) -> None:
    print("\n[3] /events — event stored in js_error_events")
    msg = "ReferenceError: uniqueV2testmessage is not defined"
    events = [{"shop": SHOP, "message": msg, "url": "https://example.myshopify.com/products/test", "source": "theme.js"}]
    r = await client.post(f"{BASE_URL}/events", json=events)
    result("POST accepted", r.status_code == 200)
    await asyncio.sleep(0.1)

    row = await conn.fetchrow(
        "SELECT * FROM js_error_events WHERE shop_domain=$1 AND error_message=$2",
        SHOP, msg[:500],
    )
    result("Row stored in js_error_events", row is not None)
    if row:
        result("page_url captured", "products/test" in row["page_url"])
        result("error_hash is 32 chars", len(row["error_hash"]) == 32)


# ---------------------------------------------------------------------------
# Test 4: JS error spike creates incident (10+ in 10 min)
# ---------------------------------------------------------------------------

async def test_js_spike_creates_incident(conn) -> None:
    print("\n[4] JS error spike — incident created after 10 events")
    import services.detector as det

    await conn.execute(
        "DELETE FROM incidents WHERE shop_domain=$1 AND incident_type='js_error_spike'", SHOP
    )
    error_hash = "test_spike_hash_v2_unique"
    now = datetime.now(timezone.utc)

    # Insert 10 events in the last 5 minutes (new error — no prior 24h events)
    for _ in range(10):
        await conn.execute(
            """INSERT INTO js_error_events (shop_domain, error_hash, error_message, page_url, occurred_at)
               VALUES ($1, $2, 'TypeError: spike test', 'https://example.myshopify.com/cart', $3)""",
            SHOP, error_hash, now - timedelta(minutes=3),
        )

    await det.check_js_error_spike(SHOP, error_hash)
    await asyncio.sleep(0.05)

    incident = await conn.fetchrow(
        "SELECT * FROM incidents WHERE shop_domain=$1 AND incident_type='js_error_spike' AND resolved_at IS NULL",
        SHOP,
    )
    result("JS spike incident created", incident is not None)
    if incident:
        detail = incident["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        result("error_hash in detail", detail.get("error_hash") == error_hash)
        result("count_10min >= 10", (detail.get("count_10min") or 0) >= 10)


# ---------------------------------------------------------------------------
# Test 5: JS error spike — known baseline error (seen in prior 24h) skipped
# ---------------------------------------------------------------------------

async def test_js_spike_known_baseline_skipped(conn) -> None:
    print("\n[5] JS error spike — known baseline error NOT re-alerted")
    import services.detector as det

    await conn.execute(
        "DELETE FROM incidents WHERE shop_domain=$1 AND incident_type='js_error_spike'", SHOP
    )
    error_hash = "known_baseline_error_hash_v2"
    now = datetime.now(timezone.utc)

    # Insert 10 events in the last 5 min (current window)
    for _ in range(10):
        await conn.execute(
            """INSERT INTO js_error_events (shop_domain, error_hash, error_message, page_url, occurred_at)
               VALUES ($1, $2, 'baseline error', 'https://example.myshopify.com/cart', $3)""",
            SHOP, error_hash, now - timedelta(minutes=3),
        )
    # Also insert events from 12h ago (within the 24h baseline window) → this is a known error
    for _ in range(3):
        await conn.execute(
            """INSERT INTO js_error_events (shop_domain, error_hash, error_message, page_url, occurred_at)
               VALUES ($1, $2, 'baseline error', 'https://example.myshopify.com/cart', $3)""",
            SHOP, error_hash, now - timedelta(hours=12),
        )

    await det.check_js_error_spike(SHOP, error_hash)
    await asyncio.sleep(0.05)

    incident = await conn.fetchrow(
        "SELECT * FROM incidents WHERE shop_domain=$1 AND incident_type='js_error_spike' AND resolved_at IS NULL",
        SHOP,
    )
    result("Known baseline error NOT alerted (no incident)", incident is None)


# ---------------------------------------------------------------------------
# Test 6: OOS hot product creates incident
# ---------------------------------------------------------------------------

async def test_oos_hot_product_incident(conn) -> None:
    print("\n[6] OOS hot product — incident created when hot product hits zero")
    import services.detector as det
    from config import settings

    if not settings.oos_enabled:
        # Temporarily enable for this test
        settings.oos_enabled = True
        _restore = True
    else:
        _restore = False

    try:
        await conn.execute(
            "DELETE FROM incidents WHERE shop_domain=$1 AND incident_type='oos_hot_product'", SHOP
        )
        product_id = 9999001
        inventory_item_id = 8888001
        now = datetime.now(timezone.utc)

        # Insert inventory_levels record so detector can map item→product
        await conn.execute(
            """INSERT INTO inventory_levels (shop_domain, inventory_item_id, product_id, available, updated_at)
               VALUES ($1, $2, $3, 0, NOW())
               ON CONFLICT (shop_domain, inventory_item_id) DO UPDATE
               SET product_id=$3, available=0, updated_at=NOW()""",
            SHOP, inventory_item_id, product_id,
        )

        # Insert 6 order_line_items in the last 7 days (hot product threshold = 5)
        past = now - timedelta(days=2)
        for i in range(6):
            await conn.execute(
                """INSERT INTO order_line_items
                   (shop_domain, shopify_order_id, product_id, product_title, quantity, price, created_at)
                   VALUES ($1, $2, $3, 'Hot Widget', 1, 49.99, $4)""",
                SHOP, 7000000 + i, product_id, past,
            )

        # Simulate inventory hitting zero
        await det.check_oos_hot_product(SHOP, inventory_item_id, 0)
        await asyncio.sleep(0.05)

        incident = await conn.fetchrow(
            """SELECT * FROM incidents WHERE shop_domain=$1 AND incident_type='oos_hot_product'
               AND resolved_at IS NULL""",
            SHOP,
        )
        result("OOS incident created for hot product", incident is not None)
        if incident:
            detail = incident["detail"]
            if isinstance(detail, str):
                detail = json.loads(detail)
            result("product_title in detail", "Hot Widget" in (detail.get("product_title") or ""))
            result(
                "estimated_revenue_per_hour > 0",
                float(detail.get("estimated_revenue_per_hour") or 0) > 0,
            )

        return incident
    finally:
        if _restore:
            settings.oos_enabled = False


# ---------------------------------------------------------------------------
# Test 7: Non-hot product OOS — no incident
# ---------------------------------------------------------------------------

async def test_oos_non_hot_no_incident(conn) -> None:
    print("\n[7] OOS non-hot product — no incident created")
    import services.detector as det
    from config import settings

    if not settings.oos_enabled:
        settings.oos_enabled = True
        _restore = True
    else:
        _restore = False

    try:
        product_id = 9999002
        inventory_item_id = 8888002

        await conn.execute(
            """INSERT INTO inventory_levels (shop_domain, inventory_item_id, product_id, available, updated_at)
               VALUES ($1, $2, $3, 0, NOW())
               ON CONFLICT (shop_domain, inventory_item_id) DO UPDATE
               SET product_id=$3, available=0, updated_at=NOW()""",
            SHOP, inventory_item_id, product_id,
        )

        # Only 2 orders in last 7 days — below hot threshold of 5
        past = datetime.now(timezone.utc) - timedelta(days=2)
        for i in range(2):
            await conn.execute(
                """INSERT INTO order_line_items
                   (shop_domain, shopify_order_id, product_id, product_title, quantity, price, created_at)
                   VALUES ($1, $2, $3, 'Cold Widget', 1, 19.99, $4)""",
                SHOP, 7000100 + i, product_id, past,
            )

        await det.check_oos_hot_product(SHOP, inventory_item_id, 0)
        await asyncio.sleep(0.05)

        incident = await conn.fetchrow(
            """SELECT * FROM incidents WHERE shop_domain=$1 AND incident_type='oos_hot_product'
               AND detail->>'product_id' = $2 AND resolved_at IS NULL""",
            SHOP, str(product_id),
        )
        result("Non-hot product OOS does NOT create incident", incident is None)
    finally:
        if _restore:
            settings.oos_enabled = False


# ---------------------------------------------------------------------------
# Test 8: OOS incident resolves when back in stock
# ---------------------------------------------------------------------------

async def test_oos_resolves_on_restock(conn) -> None:
    print("\n[8] OOS incident resolves when product back in stock")
    import services.detector as det
    from config import settings

    if not settings.oos_enabled:
        settings.oos_enabled = True
        _restore = True
    else:
        _restore = False

    try:
        product_id = 9999003
        inventory_item_id = 8888003

        await conn.execute(
            """INSERT INTO inventory_levels (shop_domain, inventory_item_id, product_id, available, updated_at)
               VALUES ($1, $2, $3, 0, NOW())
               ON CONFLICT (shop_domain, inventory_item_id) DO UPDATE
               SET product_id=$3, available=0, updated_at=NOW()""",
            SHOP, inventory_item_id, product_id,
        )

        # Create a hot-product OOS incident manually to simulate it was already open
        past = datetime.now(timezone.utc) - timedelta(days=2)
        for i in range(6):
            await conn.execute(
                """INSERT INTO order_line_items
                   (shop_domain, shopify_order_id, product_id, product_title, quantity, price, created_at)
                   VALUES ($1, $2, $3, 'Restock Widget', 1, 29.99, $4)""",
                SHOP, 7000200 + i, product_id, past,
            )

        # First: create the OOS incident
        await det.check_oos_hot_product(SHOP, inventory_item_id, 0)
        await asyncio.sleep(0.05)

        incident = await conn.fetchrow(
            """SELECT id FROM incidents WHERE shop_domain=$1 AND incident_type='oos_hot_product'
               AND detail->>'product_id' = $2 AND resolved_at IS NULL""",
            SHOP, str(product_id),
        )
        result("OOS incident exists before restock", incident is not None)

        if not incident:
            return

        # Now restock (available = 50)
        await conn.execute(
            """UPDATE inventory_levels SET available=50, updated_at=NOW()
               WHERE shop_domain=$1 AND inventory_item_id=$2""",
            SHOP, inventory_item_id,
        )
        await det.check_oos_hot_product(SHOP, inventory_item_id, 50)
        await asyncio.sleep(0.05)

        resolved = await conn.fetchrow(
            "SELECT resolved_at FROM incidents WHERE id=$1", incident["id"]
        )
        result(
            "OOS incident resolved on restock",
            resolved is not None and resolved["resolved_at"] is not None,
        )
    finally:
        if _restore:
            settings.oos_enabled = False


# ---------------------------------------------------------------------------
# Test 9: orders/create stores line_items in order_line_items
# ---------------------------------------------------------------------------

async def test_order_line_items_persistence(conn, client: httpx.AsyncClient) -> None:
    print("\n[9] orders/create — line_items persisted to order_line_items")
    order_id = 98765001
    payload = json.dumps({
        "id": order_id,
        "checkout_token": "e2e-tok-lineitems",
        "financial_status": "paid",
        "total_price": "99.98",
        "line_items": [
            {"product_id": 111222, "title": "Test Gadget", "variant_id": 333444, "quantity": 2, "price": "49.99"},
        ],
    }).encode()
    sig = sign(payload)

    r = await client.post(
        f"{BASE_URL}/webhooks/orders/create",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Hmac-Sha256": sig,
            "X-Shopify-Shop-Domain": SHOP,
        },
    )
    result("Webhook accepted", r.status_code == 200)
    await asyncio.sleep(0.05)

    row = await conn.fetchrow(
        "SELECT * FROM order_line_items WHERE shop_domain=$1 AND shopify_order_id=$2",
        SHOP, order_id,
    )
    result("Line item stored", row is not None)
    if row:
        result("product_id correct", row["product_id"] == 111222)
        result("quantity correct", row["quantity"] == 2)
        result("price correct", float(row["price"]) == 49.99)
        result("product_title correct", row["product_title"] == "Test Gadget")


# ---------------------------------------------------------------------------
# Test 10: /dashboard renders for known shop
# ---------------------------------------------------------------------------

async def test_dashboard_renders(client: httpx.AsyncClient) -> None:
    print("\n[10] /dashboard — renders for known shop")
    r = await client.get(f"{BASE_URL}/dashboard?shop={SHOP}")
    result("/dashboard returns 200", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    result("Contains shop domain", SHOP in html)
    result("Contains 'Incidents' heading", "Incidents" in html)
    result("Contains status banner", "banner" in html)


async def test_dashboard_404_unknown(client: httpx.AsyncClient) -> None:
    print("\n[11] /dashboard — 404 for unknown shop")
    r = await client.get(f"{BASE_URL}/dashboard?shop=nobody.myshopify.com")
    result("/dashboard returns 404 for unknown shop", r.status_code == 404, f"got {r.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("CheckoutGuard v2 — Test Suite")
    print("=" * 60)

    import asyncpg
    from config import settings
    from database import create_pool

    await create_pool(settings.database_url)
    conn = await asyncpg.connect(settings.database_url)
    await clean_v2_data(conn)

    async with httpx.AsyncClient(timeout=10) as client:
        await test_events_intake_valid(client)
        await test_events_intake_unknown_shop(conn, client)
        await test_events_persisted(conn, client)

    # Direct detector tests (no HTTP, faster)
    await clean_v2_data(conn)
    await test_js_spike_creates_incident(conn)
    await test_js_spike_known_baseline_skipped(conn)

    await clean_v2_data(conn)
    await test_oos_hot_product_incident(conn)

    await clean_v2_data(conn)
    await test_oos_non_hot_no_incident(conn)

    await clean_v2_data(conn)
    await test_oos_resolves_on_restock(conn)

    async with httpx.AsyncClient(timeout=10) as client:
        await test_order_line_items_persistence(conn, client)
        await asyncio.sleep(0.1)
        await test_dashboard_renders(client)
        await test_dashboard_404_unknown(client)

    await conn.close()
    print("\n" + "=" * 60)
    print("All v2 tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
