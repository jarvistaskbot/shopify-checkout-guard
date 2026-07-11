"""
CheckoutGuard v2.1 — order-volume cap tests.

Tests:
  1. plans.get_order_cap() returns correct values per plan
  2. plan_allows(plan, 'multi_store') gates correctly
  3. track_order_for_cap increments orders_month counter
  4. Month rollover resets counter and clears notice flag
  5. Cap not exceeded for scale plan (unlimited)
  6. Cap exceeded: notice claimed atomically (only once per month)
  7. Dashboard shows upgrade banner when cap is exceeded

Run: PYTHONPATH=. .venv/bin/python test_order_caps.py
     (server must be running at http://localhost:8000)
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta

import httpx

import os
SHOP = os.environ.get("TEST_SHOP", "checkoutguard-dev-oxkbbl69.myshopify.com")
BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def result(label: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    if not ok:
        sys.exit(1)


def _make_session_cookie(shop: str) -> str:
    from config import settings
    from session import create_session_token
    return create_session_token(shop, settings.secret_key)


# ---------------------------------------------------------------------------
# Test 1: plan order caps correct
# ---------------------------------------------------------------------------

def test_plan_order_caps() -> None:
    print("\n[1] plans.get_order_cap() — correct values per plan")
    from services.plans import get_order_cap

    result("starter cap=500",  get_order_cap("starter") == 500)
    result("growth cap=5000",  get_order_cap("growth") == 5000)
    result("pro cap=20000",    get_order_cap("pro") == 20000)
    result("scale cap=None",   get_order_cap("scale") is None)
    result("None defaults to starter", get_order_cap(None) == 500)


# ---------------------------------------------------------------------------
# Test 2: multi_store feature gate
# ---------------------------------------------------------------------------

def test_plan_allows_multi_store() -> None:
    print("\n[2] plan_allows multi_store feature gate")
    from services.plans import plan_allows

    result("starter no multi_store",  not plan_allows("starter", "multi_store"))
    result("growth no multi_store",   not plan_allows("growth",  "multi_store"))
    result("pro no multi_store",      not plan_allows("pro",     "multi_store"))
    result("scale has multi_store",   plan_allows("scale",   "multi_store"))
    result("None defaults to starter (no multi_store)", not plan_allows(None, "multi_store"))


# ---------------------------------------------------------------------------
# Test 3: track_order_for_cap increments counter
# ---------------------------------------------------------------------------

async def test_counter_increments(conn) -> None:
    print("\n[3] track_order_for_cap — increments orders_month counter")
    from services.billing_guard import track_order_for_cap
    from database import get_pool

    now = datetime.now(timezone.utc)
    # Reset counter to known state.
    await conn.execute(
        """UPDATE merchants SET orders_month=0, orders_month_reset_at=$1,
               orders_cap_notice_sent_at=NULL
           WHERE shop_domain=$2""",
        now, SHOP,
    )

    pool = await get_pool()
    await track_order_for_cap(pool, SHOP)
    await track_order_for_cap(pool, SHOP)
    await track_order_for_cap(pool, SHOP)

    count = await conn.fetchval(
        "SELECT orders_month FROM merchants WHERE shop_domain=$1", SHOP
    )
    result("Counter reached 3 after 3 calls", count == 3, f"got {count}")


# ---------------------------------------------------------------------------
# Test 4: month rollover resets counter
# ---------------------------------------------------------------------------

async def test_month_rollover(conn) -> None:
    print("\n[4] track_order_for_cap — month rollover resets counter")
    from services.billing_guard import track_order_for_cap
    from database import get_pool

    # Set reset_at to previous month to trigger rollover.
    last_month = datetime.now(timezone.utc) - timedelta(days=35)
    await conn.execute(
        """UPDATE merchants SET orders_month=999, orders_month_reset_at=$1,
               orders_cap_notice_sent_at=$1
           WHERE shop_domain=$2""",
        last_month, SHOP,
    )

    pool = await get_pool()
    await track_order_for_cap(pool, SHOP)

    row = await conn.fetchrow(
        "SELECT orders_month, orders_cap_notice_sent_at FROM merchants WHERE shop_domain=$1",
        SHOP,
    )
    result("Counter reset to 1 on rollover", row["orders_month"] == 1, f"got {row['orders_month']}")
    result("Notice flag cleared on rollover", row["orders_cap_notice_sent_at"] is None)


# ---------------------------------------------------------------------------
# Test 5: scale plan is unlimited — no notice sent
# ---------------------------------------------------------------------------

async def test_scale_is_unlimited(conn) -> None:
    print("\n[5] scale plan — no cap, no notice")
    from services.billing_guard import track_order_for_cap
    from database import get_pool

    now = datetime.now(timezone.utc)
    await conn.execute(
        """UPDATE merchants SET plan='scale', orders_month=999999,
               orders_month_reset_at=$1, orders_cap_notice_sent_at=NULL
           WHERE shop_domain=$2""",
        now, SHOP,
    )

    pool = await get_pool()
    await track_order_for_cap(pool, SHOP)  # Should not write notice flag

    notice = await conn.fetchval(
        "SELECT orders_cap_notice_sent_at FROM merchants WHERE shop_domain=$1", SHOP
    )
    result("No notice sent for scale plan", notice is None)

    # Restore original plan for subsequent tests.
    await conn.execute("UPDATE merchants SET plan='starter' WHERE shop_domain=$1", SHOP)


# ---------------------------------------------------------------------------
# Test 6: notice claimed only once per month
# ---------------------------------------------------------------------------

async def test_notice_sent_once(conn) -> None:
    print("\n[6] track_order_for_cap — notice sent at most once per month")
    from services.billing_guard import track_order_for_cap
    from database import get_pool

    now = datetime.now(timezone.utc)
    # Put starter merchant just above their cap (500 → 501).
    await conn.execute(
        """UPDATE merchants SET plan='starter', orders_month=501,
               orders_month_reset_at=$1, orders_cap_notice_sent_at=NULL
           WHERE shop_domain=$2""",
        now, SHOP,
    )

    pool = await get_pool()
    # First call should claim the notice slot.
    await track_order_for_cap(pool, SHOP)

    notice_1 = await conn.fetchval(
        "SELECT orders_cap_notice_sent_at FROM merchants WHERE shop_domain=$1", SHOP
    )
    result("Notice flag set after first over-cap call", notice_1 is not None)

    # Second call this month must not overwrite the flag.
    await track_order_for_cap(pool, SHOP)
    notice_2 = await conn.fetchval(
        "SELECT orders_cap_notice_sent_at FROM merchants WHERE shop_domain=$1", SHOP
    )
    result("Notice flag unchanged on second over-cap call", notice_2 == notice_1, str(notice_2))

    # Restore.
    await conn.execute("UPDATE merchants SET plan='starter', orders_month=0 WHERE shop_domain=$1", SHOP)


# ---------------------------------------------------------------------------
# Test 7: dashboard shows order-cap upgrade banner
# ---------------------------------------------------------------------------

async def test_dashboard_shows_cap_banner(conn, client: httpx.AsyncClient) -> None:
    print("\n[7] /dashboard — order-cap upgrade banner shown when exceeded")

    now = datetime.now(timezone.utc)
    # Set counter above starter cap (500).
    await conn.execute(
        """UPDATE merchants SET plan='starter', orders_month=600,
               orders_month_reset_at=$1
           WHERE shop_domain=$2""",
        now, SHOP,
    )

    session_cookie = _make_session_cookie(SHOP)
    r = await client.get(
        f"{BASE_URL}/dashboard?shop={SHOP}",
        cookies={"cg_session": session_cookie},
        follow_redirects=False,
    )
    result("/dashboard returns 200", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    result("Order cap banner present", "order volume limit" in html.lower() or "outgrown" in html.lower(), "banner text not found")
    result("Upgrade link present in banner", "billing/plans" in html)

    # Also check: when under cap, no banner.
    await conn.execute(
        "UPDATE merchants SET orders_month=100 WHERE shop_domain=$1", SHOP
    )
    r2 = await client.get(
        f"{BASE_URL}/dashboard?shop={SHOP}",
        cookies={"cg_session": session_cookie},
        follow_redirects=False,
    )
    html2 = r2.text
    result("No cap banner when under limit", "outgrown" not in html2.lower())

    # Restore.
    await conn.execute("UPDATE merchants SET orders_month=0 WHERE shop_domain=$1", SHOP)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("CheckoutGuard v2.1 — Order Cap Tests")
    print("=" * 60)

    import asyncpg
    from config import settings
    from database import create_pool

    await create_pool(settings.database_url)
    conn = await asyncpg.connect(settings.database_url)

    # Unit tests (no server needed).
    test_plan_order_caps()
    test_plan_allows_multi_store()

    # DB + integration tests.
    await test_counter_increments(conn)
    await test_month_rollover(conn)
    await test_scale_is_unlimited(conn)
    await test_notice_sent_once(conn)

    async with httpx.AsyncClient(timeout=10) as client:
        await test_dashboard_shows_cap_banner(conn, client)

    await conn.close()
    print("\n" + "=" * 60)
    print("All order cap tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
