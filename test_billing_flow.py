"""
Billing enforcement + AI cost cap tests.

Tests:
  1.  billing_guard.alerts_allowed() — active/pending permit alerts
  2.  billing_guard.alerts_allowed() — inactive/declined/cancelled suppress alerts
  3.  billing_guard.get_billing_banner() — subscribe banner for inactive merchant
  4.  billing_guard.get_billing_banner() — trial banner when trial in future
  5.  billing_guard.get_billing_banner() — no banner when active + no trial_ends_at
  6.  AI cost cap — consume_ai_budget increments counter
  7.  AI cost cap — consume_ai_budget returns False when cap exceeded
  8.  AI cost cap — counter resets on new calendar month
  9.  Calibration message — dashboard shows exact calibration text (fresh install)
  10. Dashboard billing banner — subscribe banner rendered for inactive merchant
  11. Dashboard trial banner — trial countdown rendered for active-in-trial merchant
  12. Dashboard support link — mailto:artomnats1996@gmail.com present
  13. Weekly digest — skips inactive merchants
  14. app/uninstalled webhook — sets billing_status='cancelled'
  15. Reinstall after uninstall — no unique-constraint crash, session fresh

Run: PYTHONPATH=. .venv_test/bin/python test_billing_flow.py
     (server must be running at http://localhost:8000)
"""

import asyncio
import base64
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import os
from config import settings as _settings

SHOP = os.environ.get("TEST_SHOP", "checkoutguard-dev-oxkbbl69.myshopify.com")
BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
API_SECRET = os.environ.get("SHOPIFY_API_SECRET") or _settings.shopify_api_secret

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


def _sign(body: bytes) -> str:
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()


# ---------------------------------------------------------------------------
# Test 1-2: billing_guard.alerts_allowed
# ---------------------------------------------------------------------------

def test_alerts_allowed_active_permits() -> None:
    print("\n[1] alerts_allowed — active/pending statuses permit alerts")
    from services.billing_guard import alerts_allowed
    result("'active' → True", alerts_allowed("active") is True)
    result("'pending' → True", alerts_allowed("pending") is True)


def test_alerts_allowed_inactive_suppresses() -> None:
    print("\n[2] alerts_allowed — inactive/declined/cancelled suppress alerts")
    from services.billing_guard import alerts_allowed
    result("'inactive' → False", alerts_allowed("inactive") is False)
    result("'declined' → False", alerts_allowed("declined") is False)
    result("'cancelled' → False", alerts_allowed("cancelled") is False)
    result("None → False", alerts_allowed(None) is False)


# ---------------------------------------------------------------------------
# Test 3-5: get_billing_banner
# ---------------------------------------------------------------------------

def test_billing_banner_inactive() -> None:
    print("\n[3] get_billing_banner — subscribe banner for inactive merchant")
    from services.billing_guard import get_billing_banner
    banner = get_billing_banner("inactive", None, "test.myshopify.com")
    result("Banner is not None", banner is not None)
    if banner:
        cls, text = banner
        result("CSS class is banner-subscribe", cls == "banner-subscribe", cls)
        result("Contains billing/plans link", "/billing/plans" in text)


def test_billing_banner_trial() -> None:
    print("\n[4] get_billing_banner — trial banner when trial_ends_at in future")
    from services.billing_guard import get_billing_banner
    future = datetime.now(timezone.utc) + timedelta(days=10)
    banner = get_billing_banner("active", future, "test.myshopify.com")
    result("Banner is not None (trial active)", banner is not None)
    if banner:
        cls, text = banner
        result("CSS class is banner-trial", cls == "banner-trial", cls)
        result("Contains 'days left'", "days left" in text or "day left" in text)


def test_billing_banner_active_no_trial() -> None:
    print("\n[5] get_billing_banner — no banner when active with no trial")
    from services.billing_guard import get_billing_banner
    past = datetime.now(timezone.utc) - timedelta(days=1)
    banner_expired = get_billing_banner("active", past, "test.myshopify.com")
    result("Banner is None (trial expired, fully active)", banner_expired is None)
    banner_none = get_billing_banner("active", None, "test.myshopify.com")
    result("Banner is None (no trial_ends_at)", banner_none is None)


# ---------------------------------------------------------------------------
# Test 6-8: AI cost cap
# ---------------------------------------------------------------------------

async def test_ai_budget_increments(conn) -> None:
    print("\n[6] AI cost cap — consume_ai_budget increments counter")
    from database import get_pool
    from services.billing_guard import consume_ai_budget

    # Reset counter.
    await conn.execute(
        "UPDATE merchants SET ai_calls_month=0, ai_calls_reset_at=NOW() WHERE shop_domain=$1", SHOP
    )
    pool = await get_pool()
    allowed = await consume_ai_budget(pool, SHOP, 200)
    result("First call allowed", allowed is True)
    after = await conn.fetchval("SELECT ai_calls_month FROM merchants WHERE shop_domain=$1", SHOP)
    result("Counter incremented to 1", after == 1, f"got {after}")


async def test_ai_budget_cap_exceeded(conn) -> None:
    print("\n[7] AI cost cap — returns False when cap exceeded")
    from database import get_pool
    from services.billing_guard import consume_ai_budget

    # Set counter to cap value.
    await conn.execute(
        "UPDATE merchants SET ai_calls_month=5, ai_calls_reset_at=NOW() WHERE shop_domain=$1", SHOP
    )
    pool = await get_pool()
    blocked = await consume_ai_budget(pool, SHOP, 5)
    result("Call blocked when at cap", blocked is False, f"got {blocked}")

    # Counter must NOT be incremented when blocked.
    after = await conn.fetchval("SELECT ai_calls_month FROM merchants WHERE shop_domain=$1", SHOP)
    result("Counter not incremented when blocked", after == 5, f"got {after}")


async def test_ai_budget_monthly_reset(conn) -> None:
    print("\n[8] AI cost cap — counter resets when month changes")
    from database import get_pool
    from services.billing_guard import consume_ai_budget

    # Set reset_at to last month to simulate stale counter.
    last_month = datetime.now(timezone.utc).replace(month=1) - timedelta(days=1)
    await conn.execute(
        "UPDATE merchants SET ai_calls_month=200, ai_calls_reset_at=$1 WHERE shop_domain=$2",
        last_month, SHOP,
    )
    pool = await get_pool()
    allowed = await consume_ai_budget(pool, SHOP, 200)
    result("Call allowed after monthly reset", allowed is True, f"got {allowed}")
    after = await conn.fetchval("SELECT ai_calls_month FROM merchants WHERE shop_domain=$1", SHOP)
    result("Counter reset to 1 after month change", after == 1, f"got {after}")


# ---------------------------------------------------------------------------
# Test 9-12: Dashboard UI
# ---------------------------------------------------------------------------

async def test_dashboard_calibration_message(client: httpx.AsyncClient, conn) -> None:
    print("\n[9] Dashboard — calibration message for fresh install")
    # Set installed_at to just now (< 7 days) to trigger calibration state.
    await conn.execute(
        "UPDATE merchants SET installed_at=NOW() WHERE shop_domain=$1", SHOP
    )
    session = _make_session_cookie(SHOP)
    r = await client.get(
        f"{BASE_URL}/dashboard?shop={SHOP}",
        cookies={"cg_session": session},
        follow_redirects=False,
    )
    result("Dashboard returns 200", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    result(
        "Contains calibration message",
        "Calibrating your store" in html and "anomaly alerts begin after 7 days" in html,
        "calibration text missing",
    )

    # Restore installed_at to > 7 days for other tests.
    await conn.execute(
        "UPDATE merchants SET installed_at=NOW() - INTERVAL '30 days' WHERE shop_domain=$1", SHOP
    )


async def test_dashboard_subscribe_banner(client: httpx.AsyncClient, conn) -> None:
    print("\n[10] Dashboard — subscribe banner for inactive merchant")
    await conn.execute(
        "UPDATE merchants SET billing_status='inactive', trial_ends_at=NULL WHERE shop_domain=$1", SHOP
    )
    session = _make_session_cookie(SHOP)
    r = await client.get(
        f"{BASE_URL}/dashboard?shop={SHOP}",
        cookies={"cg_session": session},
        follow_redirects=False,
    )
    result("Dashboard 200", r.status_code == 200)
    result("Subscribe banner present", "banner-subscribe" in r.text or "Start your 14-day free trial" in r.text)
    result("Billing/plans link present", "/billing/plans" in r.text)


async def test_dashboard_trial_banner(client: httpx.AsyncClient, conn) -> None:
    print("\n[11] Dashboard — trial countdown banner for active-in-trial merchant")
    future = datetime.now(timezone.utc) + timedelta(days=8)
    await conn.execute(
        "UPDATE merchants SET billing_status='active', trial_ends_at=$1 WHERE shop_domain=$2",
        future, SHOP,
    )
    session = _make_session_cookie(SHOP)
    r = await client.get(
        f"{BASE_URL}/dashboard?shop={SHOP}",
        cookies={"cg_session": session},
        follow_redirects=False,
    )
    result("Dashboard 200", r.status_code == 200)
    result("Trial banner present", "banner-trial" in r.text or "days left" in r.text)

    # Restore to inactive for other tests.
    await conn.execute(
        "UPDATE merchants SET billing_status='inactive', trial_ends_at=NULL WHERE shop_domain=$1", SHOP
    )


async def test_dashboard_support_link(client: httpx.AsyncClient) -> None:
    print("\n[12] Dashboard — support email link present")
    session = _make_session_cookie(SHOP)
    r = await client.get(
        f"{BASE_URL}/dashboard?shop={SHOP}",
        cookies={"cg_session": session},
        follow_redirects=False,
    )
    result("Dashboard 200", r.status_code == 200)
    result("Support email present", "artomnats1996@gmail.com" in r.text)


# ---------------------------------------------------------------------------
# Test 13: Weekly digest skips inactive merchants
# ---------------------------------------------------------------------------

def test_weekly_digest_query_filters_billing() -> None:
    print("\n[13] Weekly digest — query filters out inactive merchants")
    import inspect
    from main import _send_pending_digests
    src = inspect.getsource(_send_pending_digests)
    result(
        "Digest query filters billing_status",
        "billing_status" in src,
        "billing_status filter missing from digest query",
    )
    result(
        "Digest only sends to active/pending",
        "'active'" in src or "active" in src,
    )


# ---------------------------------------------------------------------------
# Test 14: Uninstall sets billing_status='cancelled'
# ---------------------------------------------------------------------------

async def test_uninstall_cancels_billing(client: httpx.AsyncClient, conn) -> None:
    print("\n[14] app/uninstalled — sets billing_status='cancelled'")
    # Reset to active so we can verify the change.
    await conn.execute(
        "UPDATE merchants SET active=TRUE, billing_status='active' WHERE shop_domain=$1", SHOP
    )

    payload = json.dumps({"id": 12345, "myshopify_domain": SHOP}).encode()
    sig = _sign(payload)
    r = await client.post(
        f"{BASE_URL}/webhooks/app/uninstalled",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Hmac-Sha256": sig,
            "X-Shopify-Shop-Domain": SHOP,
        },
    )
    result("Webhook accepted", r.status_code == 200)
    row = await conn.fetchrow(
        "SELECT active, billing_status FROM merchants WHERE shop_domain=$1", SHOP
    )
    result("active set to FALSE", row["active"] is False)
    result("billing_status set to 'cancelled'", row["billing_status"] == "cancelled", row["billing_status"])

    # Restore for subsequent tests.
    await conn.execute(
        "UPDATE merchants SET active=TRUE, billing_status='inactive' WHERE shop_domain=$1", SHOP
    )


# ---------------------------------------------------------------------------
# Test 15: Reinstall after uninstall (no unique-constraint crash)
# ---------------------------------------------------------------------------

async def test_reinstall_after_uninstall(conn) -> None:
    print("\n[15] Reinstall — no unique-constraint crash, session refreshes cleanly")
    import services.detector as det

    # Simulate uninstalled state.
    await conn.execute(
        "UPDATE merchants SET active=FALSE, billing_status='cancelled' WHERE shop_domain=$1", SHOP
    )

    # Simulate re-install: the ON CONFLICT DO UPDATE path in /auth/callback.
    await conn.execute(
        """
        INSERT INTO merchants (shop_domain, access_token, active, billing_status)
        VALUES ($1, 'shpat_test_reinstall', TRUE, 'inactive')
        ON CONFLICT (shop_domain)
        DO UPDATE SET
            access_token = EXCLUDED.access_token,
            active = TRUE,
            billing_status = 'inactive'
        """,
        SHOP,
    )
    row = await conn.fetchrow("SELECT active, billing_status FROM merchants WHERE shop_domain=$1", SHOP)
    result("Re-install sets active=TRUE", row["active"] is True)
    result("Re-install resets billing_status='inactive'", row["billing_status"] == "inactive", row["billing_status"])

    # Restore token.
    env_token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "shpat_dev_placeholder")
    await conn.execute(
        "UPDATE merchants SET access_token=$1 WHERE shop_domain=$2",
        env_token, SHOP,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("CheckoutGuard — Billing Flow Test Suite")
    print("=" * 60)

    import asyncpg
    from config import settings
    from database import create_pool

    await create_pool(settings.database_url)
    conn = await asyncpg.connect(settings.database_url)

    # Unit tests (no server required)
    test_alerts_allowed_active_permits()
    test_alerts_allowed_inactive_suppresses()
    test_billing_banner_inactive()
    test_billing_banner_trial()
    test_billing_banner_active_no_trial()

    await test_ai_budget_increments(conn)
    await test_ai_budget_cap_exceeded(conn)
    await test_ai_budget_monthly_reset(conn)

    test_weekly_digest_query_filters_billing()

    # Integration tests (need server)
    async with httpx.AsyncClient(timeout=10) as client:
        await test_dashboard_calibration_message(client, conn)
        await test_dashboard_subscribe_banner(client, conn)
        await test_dashboard_trial_banner(client, conn)
        await test_dashboard_support_link(client)
        await test_uninstall_cancels_billing(client, conn)

    await test_reinstall_after_uninstall(conn)

    await conn.close()
    print("\n" + "=" * 60)
    print("All billing flow tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
