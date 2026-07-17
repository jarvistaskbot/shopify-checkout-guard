"""
Tests for the onboarding "Skip for now" path.

Tests:
  1. POST /onboarding/skip with valid session+CSRF → 303 to /billing/plans
     and onboarding_seen=TRUE in DB.
  2. Auth callback routing: merchant with onboarding_seen=TRUE and no webhook
     is NOT sent to /onboarding (source-level check on routing logic).
  3. Dashboard for webhook-less merchant → 200 HTML containing the
     Slack-not-connected banner.
  4. POST /dashboard/test-alert with no webhook → graceful 303 redirect with
     ta=no_webhook, no 500.

Run: PYTHONPATH='' .venv312/bin/python test_onboarding_skip.py
     (server must be running at http://localhost:8000; local Postgres must be running)
"""

import asyncio
import inspect
import sys
import os

import httpx

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


def _make_csrf(session_cookie: str) -> str:
    from config import settings
    from session import csrf_token_for
    return csrf_token_for(session_cookie, settings.secret_key)


# ---------------------------------------------------------------------------
# 1. POST /onboarding/skip → 303 to /billing/plans + onboarding_seen=TRUE
# ---------------------------------------------------------------------------
async def test_skip_sets_onboarding_seen(client: httpx.AsyncClient, conn) -> None:
    print("\n[1] POST /onboarding/skip → 303 to /billing/plans + onboarding_seen=TRUE")

    from session import COOKIE_NAME

    # Reset state before test.
    await conn.execute(
        "UPDATE merchants SET onboarding_seen = FALSE WHERE shop_domain = $1",
        SHOP,
    )

    session = _make_session_cookie(SHOP)
    csrf = _make_csrf(session)

    r = await client.post(
        f"{BASE_URL}/onboarding/skip",
        data={"shop": SHOP, "csrf_token": csrf},
        cookies={COOKIE_NAME: session},
        follow_redirects=False,
    )
    result("Status is 303", r.status_code == 303, f"got {r.status_code}")
    loc = r.headers.get("location", "")
    result("Redirects to /billing/plans", "/billing/plans" in loc, f"location: {loc}")
    result("Location contains shop param", SHOP in loc, f"location: {loc}")

    row = await conn.fetchrow(
        "SELECT onboarding_seen FROM merchants WHERE shop_domain = $1",
        SHOP,
    )
    result("onboarding_seen=TRUE in DB", row is not None and row["onboarding_seen"] is True,
           f"got {row}")

    # Clean up.
    await conn.execute(
        "UPDATE merchants SET onboarding_seen = FALSE WHERE shop_domain = $1",
        SHOP,
    )


# ---------------------------------------------------------------------------
# 2. Auth callback routing: onboarding_seen=TRUE + no webhook → NOT /onboarding
# ---------------------------------------------------------------------------
def test_auth_routing_respects_onboarding_seen() -> None:
    print("\n[2] Auth routing: onboarding_seen=TRUE + no webhook → skip /onboarding")
    from routes import auth as auth_module
    src = inspect.getsource(auth_module.callback)

    result(
        "SELECT includes onboarding_seen",
        "onboarding_seen" in src,
        "onboarding_seen missing from merchant SELECT in auth callback",
    )
    result(
        "Routing condition guards on onboarding_seen",
        "onboarding_seen" in src and "not onboarding_seen" in src,
        "auth callback routing does not check onboarding_seen",
    )
    result(
        "Condition is AND (both checks required to reach /onboarding)",
        "not has_config and not onboarding_seen" in src,
        "routing may still force merchants with onboarding_seen=TRUE to /onboarding",
    )


# ---------------------------------------------------------------------------
# 3. Dashboard for webhook-less merchant → 200 HTML with Slack banner
# ---------------------------------------------------------------------------
async def test_dashboard_shows_slack_banner_when_no_webhook(
    client: httpx.AsyncClient, conn
) -> None:
    print("\n[3] Dashboard without Slack webhook → 200 HTML with connect-Slack banner")

    from session import COOKIE_NAME

    # Save existing webhook, temporarily clear it.
    row = await conn.fetchrow(
        "SELECT slack_webhook_url FROM merchants WHERE shop_domain = $1",
        SHOP,
    )
    old_webhook = row["slack_webhook_url"] if row else None

    try:
        await conn.execute(
            "UPDATE merchants SET slack_webhook_url = NULL WHERE shop_domain = $1",
            SHOP,
        )

        session = _make_session_cookie(SHOP)
        r = await client.get(
            f"{BASE_URL}/dashboard?shop={SHOP}",
            cookies={COOKIE_NAME: session},
            follow_redirects=False,
        )
        result("Status is 200", r.status_code == 200, f"got {r.status_code}")
        result(
            "Contains Slack-not-connected banner",
            "Slack is not connected" in r.text,
            "banner text not found in dashboard HTML",
        )
        result(
            "Banner links to /onboarding",
            "/onboarding" in r.text and "Connect Slack" in r.text,
            "connect-Slack link not found in dashboard HTML",
        )
    finally:
        if old_webhook:
            await conn.execute(
                "UPDATE merchants SET slack_webhook_url = $1 WHERE shop_domain = $2",
                old_webhook, SHOP,
            )


# ---------------------------------------------------------------------------
# 4. POST /dashboard/test-alert with no webhook → graceful 303, no 500
# ---------------------------------------------------------------------------
async def test_test_alert_no_webhook_graceful(client: httpx.AsyncClient, conn) -> None:
    print("\n[4] POST /dashboard/test-alert with no webhook → 303 ta=no_webhook, no 500")

    from session import COOKIE_NAME

    row = await conn.fetchrow(
        "SELECT slack_webhook_url FROM merchants WHERE shop_domain = $1",
        SHOP,
    )
    old_webhook = row["slack_webhook_url"] if row else None

    try:
        await conn.execute(
            "UPDATE merchants SET slack_webhook_url = NULL WHERE shop_domain = $1",
            SHOP,
        )

        session = _make_session_cookie(SHOP)
        csrf = _make_csrf(session)

        r = await client.post(
            f"{BASE_URL}/dashboard/test-alert",
            data={"shop": SHOP, "csrf_token": csrf},
            cookies={COOKIE_NAME: session},
            follow_redirects=False,
        )
        result("Status is 303 (not 500)", r.status_code == 303, f"got {r.status_code}")
        loc = r.headers.get("location", "")
        result("ta=no_webhook in redirect", "ta=no_webhook" in loc, f"location: {loc}")
    finally:
        if old_webhook:
            await conn.execute(
                "UPDATE merchants SET slack_webhook_url = $1 WHERE shop_domain = $2",
                old_webhook, SHOP,
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("CheckoutGuard — Onboarding Skip Path Tests")
    print("=" * 60)

    import asyncpg
    from config import settings
    from database import create_pool

    await create_pool(settings.database_url)
    conn = await asyncpg.connect(settings.database_url)

    # Unit test (no server needed)
    test_auth_routing_respects_onboarding_seen()

    async with httpx.AsyncClient(timeout=10) as client:
        await test_skip_sets_onboarding_seen(client, conn)
        await test_dashboard_shows_slack_banner_when_no_webhook(client, conn)
        await test_test_alert_no_webhook_graceful(client, conn)

    await conn.close()

    print("\n" + "=" * 60)
    print("All onboarding-skip tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
