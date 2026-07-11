"""
End-to-end test for CheckoutGuard pipeline.

Tests:
  1. Webhook HMAC verification (valid + invalid)
  2. Event insertion into checkout_events
  3. Checkout funnel detection (incident creates on conversion rate collapse)
  4. Incident resolution (incident resolves when rate recovers)

Run: PYTHONPATH=. .venv/bin/python test_e2e.py
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

# ── config ────────────────────────────────────────────────────────────────────
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


async def clean_test_data(conn) -> None:
    await conn.execute("DELETE FROM incidents WHERE shop_domain = $1", SHOP)
    # Clean all test-generated checkout_events (safe: dev shop only)
    await conn.execute(
        """DELETE FROM checkout_events
           WHERE shop_domain = $1
             AND (checkout_token LIKE 'e2e-%' OR checkout_token LIKE 'baseline-tok-%'
               OR checkout_token LIKE 'recent-tok-%' OR order_id LIKE 'e2e-%'
               OR order_id LIKE 'test-%')""",
        SHOP,
    )
    # Reset merchant detection state
    await conn.execute(
        """UPDATE merchants SET drop_streak=0, recovery_streak=0,
           checkout_conversion_baseline=NULL
           WHERE shop_domain=$1""",
        SHOP,
    )


async def test_webhook_hmac(client: httpx.AsyncClient) -> None:
    print("\n[1] HMAC verification")
    payload = json.dumps({"id": 9999001, "checkout_token": "e2e-tok-1"}).encode()
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
    result("Valid HMAC accepted (200)", r.status_code == 200, f"got {r.status_code}: {r.text[:80]}")

    r2 = await client.post(
        f"{BASE_URL}/webhooks/orders/create",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Hmac-Sha256": "badsignature==",
            "X-Shopify-Shop-Domain": SHOP,
        },
    )
    result("Invalid HMAC rejected (401)", r2.status_code == 401, f"got {r2.status_code}")


async def test_event_persistence(conn, client: httpx.AsyncClient) -> None:
    print("\n[2] Event persistence")
    order_id = "e2e-order-persist-1"
    payload = json.dumps({"id": order_id, "checkout_token": "e2e-tok-persist"}).encode()
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

    row = await conn.fetchrow(
        "SELECT * FROM checkout_events WHERE order_id = $1", order_id
    )
    result("Event in DB", row is not None, f"order_id={order_id}")
    result("Event type is order_created", row["event_type"] == "order_created")
    result("Shop domain matches", row["shop_domain"] == SHOP)


async def test_detection_engine(conn) -> None:
    print("\n[3] Detection engine (checkout funnel collapse)")
    import services.detector as det

    # Ensure clean slate
    await conn.execute("DELETE FROM incidents WHERE shop_domain = $1", SHOP)
    await conn.execute(
        """UPDATE merchants SET drop_streak=0, recovery_streak=0,
           avg_order_value=100.00, checkout_conversion_baseline=NULL,
           alert_email=NULL, slack_webhook_url=NULL
           WHERE shop_domain=$1""",
        SHOP,
    )

    # Insert baseline data: 20 checkouts + 16 orders 3 days ago = 80% baseline rate
    past = datetime.now(timezone.utc) - timedelta(days=3)
    for i in range(20):
        await conn.execute(
            """INSERT INTO checkout_events (shop_domain, event_type, checkout_token, created_at)
               VALUES ($1, 'checkout_created', $2, $3)""",
            SHOP, f"baseline-tok-{i}", past,
        )
    for i in range(16):
        await conn.execute(
            """INSERT INTO checkout_events (shop_domain, event_type, checkout_token, order_id, created_at)
               VALUES ($1, 'order_created', $2, $3, $4)""",
            SHOP, f"baseline-tok-{i}", f"e2e-baseline-{i}", past,
        )

    # Insert 6 recent checkouts (last 5 min) with 0 orders -> 0% conversion
    recent = datetime.now(timezone.utc) - timedelta(minutes=2)
    for i in range(6):
        await conn.execute(
            """INSERT INTO checkout_events (shop_domain, event_type, checkout_token, created_at)
               VALUES ($1, 'checkout_created', $2, $3)""",
            SHOP, f"recent-tok-{i}", recent,
        )

    # Run detection directly (0% current rate vs 80% baseline → should trigger)
    await det._run_realtime_checks(SHOP, "checkout_created")
    await asyncio.sleep(0.1)

    incident = await conn.fetchrow(
        "SELECT * FROM incidents WHERE shop_domain = $1 AND incident_type = 'checkout_funnel_collapse' AND resolved_at IS NULL",
        SHOP,
    )
    result("Incident created on funnel collapse", incident is not None)
    if incident:
        result(
            "Revenue loss is positive",
            float(incident["estimated_revenue_loss_per_min"]) > 0,
            f"${incident['estimated_revenue_loss_per_min']}/min",
        )
        result(
            "Baseline rate stored",
            float(incident["checkout_rate_before"]) > 0,
            f"{incident['checkout_rate_before']}",
        )
    return incident


async def test_recovery(conn, incident) -> None:
    print("\n[4] Incident resolution")
    import services.detector as det

    if not incident:
        print("  [SKIP] No active incident to resolve")
        return

    # Insert 8 recent orders for the same 6 recent checkouts → 133% rate → above threshold
    recent = datetime.now(timezone.utc) - timedelta(minutes=2)
    for i in range(8):
        await conn.execute(
            """INSERT INTO checkout_events (shop_domain, event_type, checkout_token, order_id, created_at)
               VALUES ($1, 'order_created', $2, $3, $4)""",
            SHOP, f"recent-tok-{i % 6}", f"e2e-recovery-{i}", recent,
        )

    # Re-run detection — rate recovered → should resolve incident
    await det._run_realtime_checks(SHOP, "order_created")
    await asyncio.sleep(0.1)

    resolved = await conn.fetchrow(
        "SELECT resolved_at FROM incidents WHERE id = $1", incident["id"]
    )
    result(
        "Incident resolved on recovery",
        resolved is not None and resolved["resolved_at"] is not None,
    )


async def main() -> None:
    print("=" * 60)
    print("CheckoutGuard — End-to-End Test Suite")
    print("=" * 60)

    import asyncpg
    from config import settings
    from database import create_pool

    await create_pool(settings.database_url)

    conn = await asyncpg.connect(settings.database_url)
    await clean_test_data(conn)

    async with httpx.AsyncClient(timeout=10) as client:
        await test_webhook_hmac(client)
        await test_event_persistence(conn, client)

    incident = await test_detection_engine(conn)
    await test_recovery(conn, incident)

    await conn.close()
    print("\n" + "=" * 60)
    print("All tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
