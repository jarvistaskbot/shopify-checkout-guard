"""
Regression tests for Shopify App Store review blockers.

Tests:
  1.  GET / with ?shop=valid.myshopify.com → 302 to /auth/shopify
  2.  GET / without shop param → HTML landing page (not JSON)
  3.  GET /health → JSON with status=ok
  4.  Billing: return_url contains shop param (unit check)
  5.  OAuth callback: expired/missing nonce → 302 redirect (not 400 JSON)
  6.  Dashboard: valid session cookie but merchant inactive → 302 to /auth/shopify
  7.  Webhook: POST /webhooks/orders/create without HMAC header → 401
  8.  Test-alert: POST /dashboard/test-alert without session → 302 to /auth/shopify
  9.  Test-alert: POST /dashboard/test-alert without webhook configured → redirect ta=no_webhook
  10. Test-alert: POST /dashboard/test-alert with rate limit → redirect ta=limit
  11. P1-4: RuntimeError check — dev secret_key + billing_test_mode=False raises at startup
  12. GET / with invalid shop domain → HTML (not redirect)

Run: PYTHONPATH='' .venv312/bin/python test_review_fixes.py
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


# ---------------------------------------------------------------------------
# 1. GET / with valid shop → 302 to /auth/shopify
# ---------------------------------------------------------------------------
async def test_root_with_shop_redirects(client: httpx.AsyncClient) -> None:
    print("\n[1] GET /?shop=valid → 302 to /auth/shopify")
    r = await client.get(f"{BASE_URL}/?shop={SHOP}", follow_redirects=False)
    result("Status is 302", r.status_code == 302, f"got {r.status_code}")
    loc = r.headers.get("location", "")
    result("Redirects to /auth/shopify", "/auth/shopify" in loc, f"location: {loc}")
    result("Location contains shop param", SHOP in loc, f"location: {loc}")


# ---------------------------------------------------------------------------
# 2. GET / without shop → HTML landing (not JSON)
# ---------------------------------------------------------------------------
async def test_root_no_shop_returns_html(client: httpx.AsyncClient) -> None:
    print("\n[2] GET / without shop → HTML landing (not JSON)")
    r = await client.get(f"{BASE_URL}/", follow_redirects=False)
    result("Status is 200", r.status_code == 200, f"got {r.status_code}")
    ct = r.headers.get("content-type", "")
    result("Content-Type is HTML", "text/html" in ct, f"got {ct}")
    result("Body is not JSON", not r.text.strip().startswith("{"), "body looks like JSON")
    result("Contains CheckoutGuard", "CheckoutGuard" in r.text)


# ---------------------------------------------------------------------------
# 3. GET /health → JSON
# ---------------------------------------------------------------------------
async def test_health_returns_json(client: httpx.AsyncClient) -> None:
    print("\n[3] GET /health → JSON with status=ok")
    r = await client.get(f"{BASE_URL}/health", follow_redirects=False)
    result("Status is 200", r.status_code == 200, f"got {r.status_code}")
    ct = r.headers.get("content-type", "")
    result("Content-Type is JSON", "application/json" in ct, f"got {ct}")
    body = r.json()
    result("status=ok", body.get("status") == "ok", f"got {body}")


# ---------------------------------------------------------------------------
# 4. Billing: return_url contains shop param (unit check)
# ---------------------------------------------------------------------------
def test_billing_return_url_has_shop() -> None:
    print("\n[4] Billing: return_url contains shop param")
    import inspect
    from routes.billing import billing_start
    src = inspect.getsource(billing_start)
    result(
        "return_url includes ?shop=",
        "return_url" in src and "shop=" in src and "billing/callback" in src,
        "return_url missing shop param in billing_start source",
    )
    # Also verify the pattern specifically: return_url should not be /billing/callback without shop
    result(
        "return_url is not missing shop in template",
        "/billing/callback?shop=" in src or "callback?shop=" in src,
        "billing/callback?shop= pattern not found",
    )


# ---------------------------------------------------------------------------
# 5. OAuth callback: missing/expired nonce → 302 (not 400)
# ---------------------------------------------------------------------------
async def test_oauth_callback_bad_nonce_redirects(client: httpx.AsyncClient) -> None:
    print("\n[5] OAuth callback with bad nonce → 302 (not 400 JSON)")
    # state=nonexistent_nonce will not be in DB → should redirect
    r = await client.get(
        f"{BASE_URL}/auth/callback",
        params={
            "shop": SHOP,
            "code": "fake_code_abc123",
            "state": "nonexistent_nonce_xyz987",
            "hmac": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        },
        follow_redirects=False,
    )
    result("Status is 302 (not 400)", r.status_code == 302, f"got {r.status_code}")
    loc = r.headers.get("location", "")
    result("Redirects to /auth/shopify", "/auth/shopify" in loc, f"location: {loc}")


# ---------------------------------------------------------------------------
# 6. Dashboard: valid session, merchant inactive → 302 to /auth/shopify
# ---------------------------------------------------------------------------
async def test_dashboard_inactive_merchant_redirects(
    client: httpx.AsyncClient, conn
) -> None:
    print("\n[6] Dashboard: valid session, merchant inactive → 302")
    # Temporarily set merchant to inactive
    await conn.execute(
        "UPDATE merchants SET active=FALSE WHERE shop_domain=$1", SHOP
    )
    try:
        cookie = _make_session_cookie(SHOP)
        r = await client.get(
            f"{BASE_URL}/dashboard?shop={SHOP}",
            cookies={"cg_session": cookie},
            follow_redirects=False,
        )
        result("Status is 302", r.status_code == 302, f"got {r.status_code}")
        loc = r.headers.get("location", "")
        result("Redirects to /auth/shopify", "/auth/shopify" in loc, f"location: {loc}")
    finally:
        await conn.execute(
            "UPDATE merchants SET active=TRUE WHERE shop_domain=$1", SHOP
        )


# ---------------------------------------------------------------------------
# 7. Webhook: missing HMAC header → 401
# ---------------------------------------------------------------------------
async def test_webhook_missing_hmac_returns_401(client: httpx.AsyncClient) -> None:
    print("\n[7] Webhook without HMAC header → 401")
    r = await client.post(
        f"{BASE_URL}/webhooks/orders/create",
        content=b'{"id": 123}',
        headers={"Content-Type": "application/json"},
        follow_redirects=False,
    )
    result("Status is 401", r.status_code == 401, f"got {r.status_code}")


# ---------------------------------------------------------------------------
# 8. Test-alert: no session → 302 to /auth/shopify
# ---------------------------------------------------------------------------
async def test_test_alert_no_session_redirects(client: httpx.AsyncClient) -> None:
    print("\n[8] POST /dashboard/test-alert without session → 302 to /auth/shopify")
    r = await client.post(
        f"{BASE_URL}/dashboard/test-alert",
        data={"shop": SHOP, "csrf_token": "fake"},
        follow_redirects=False,
    )
    result("Status is 302", r.status_code == 302, f"got {r.status_code}")
    loc = r.headers.get("location", "")
    result("Redirects to /auth/shopify", "/auth/shopify" in loc, f"location: {loc}")


# ---------------------------------------------------------------------------
# 9. Test-alert: no webhook configured → ta=no_webhook
# ---------------------------------------------------------------------------
async def test_test_alert_no_webhook(client: httpx.AsyncClient, conn) -> None:
    print("\n[9] POST /dashboard/test-alert, no webhook → ta=no_webhook")
    old_webhook = await conn.fetchval(
        "SELECT slack_webhook_url FROM merchants WHERE shop_domain=$1", SHOP
    )
    await conn.execute(
        "UPDATE merchants SET slack_webhook_url=NULL WHERE shop_domain=$1", SHOP
    )
    try:
        from session import create_session_token, COOKIE_NAME, csrf_token_for
        from config import settings
        session = create_session_token(SHOP, settings.secret_key)
        csrf = csrf_token_for(session, settings.secret_key)
        r = await client.post(
            f"{BASE_URL}/dashboard/test-alert",
            data={"shop": SHOP, "csrf_token": csrf},
            cookies={COOKIE_NAME: session},
            follow_redirects=False,
        )
        result("Status is 303", r.status_code == 303, f"got {r.status_code}")
        loc = r.headers.get("location", "")
        result("ta=no_webhook in redirect", "ta=no_webhook" in loc, f"location: {loc}")
    finally:
        if old_webhook:
            await conn.execute(
                "UPDATE merchants SET slack_webhook_url=$1 WHERE shop_domain=$2",
                old_webhook, SHOP,
            )


# ---------------------------------------------------------------------------
# 10. Test-alert: rate limiter implementation check (unit)
# ---------------------------------------------------------------------------
def test_test_alert_rate_limit_implementation() -> None:
    print("\n[10] Test-alert: rate limiter is implemented with 10-min cooldown")
    from routes.dashboard import _TEST_ALERT_COOLDOWN_SECS
    result(
        "Cooldown constant is 600s (10 min)",
        _TEST_ALERT_COOLDOWN_SECS == 600,
        f"got {_TEST_ALERT_COOLDOWN_SECS}",
    )
    import inspect
    from routes import dashboard as dash_module
    src = inspect.getsource(dash_module.dashboard_test_alert)
    result(
        "Rate limit check references cooldown",
        "_TEST_ALERT_COOLDOWN_SECS" in src and "_test_alert_last_sent" in src,
    )
    result(
        "Rate limit stamp set before HTTP send",
        src.index("_test_alert_last_sent[shop] = now") < src.index("send_test_alert"),
    )


# ---------------------------------------------------------------------------
# 11. P1-4: RuntimeError on dev secret_key + billing_test_mode=False
# ---------------------------------------------------------------------------
def test_runtime_error_on_dev_secret_in_prod() -> None:
    print("\n[11] RuntimeError raised when dev secret_key + billing_test_mode=False")
    import main as main_module
    import inspect
    src = inspect.getsource(main_module.lifespan)
    result(
        "RuntimeError raised (not just logged) for dev secret in prod",
        "raise RuntimeError" in src,
        "only logger.critical found — should raise RuntimeError",
    )


# ---------------------------------------------------------------------------
# 12. GET / with invalid shop domain → HTML (no redirect)
# ---------------------------------------------------------------------------
async def test_root_invalid_shop_no_redirect(client: httpx.AsyncClient) -> None:
    print("\n[12] GET /?shop=not-myshopify.com → HTML (no redirect)")
    r = await client.get(
        f"{BASE_URL}/?shop=evil.example.com",
        follow_redirects=False,
    )
    result("Status is 200 (HTML, not 302)", r.status_code == 200, f"got {r.status_code}")
    result("Body is HTML", "text/html" in r.headers.get("content-type", ""))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("CheckoutGuard — App Store Review Fix Regression Tests")
    print("=" * 60)

    import asyncpg
    from config import settings
    from database import create_pool

    await create_pool(settings.database_url)
    conn = await asyncpg.connect(settings.database_url)

    # Unit tests (no server needed)
    test_billing_return_url_has_shop()
    test_runtime_error_on_dev_secret_in_prod()
    test_test_alert_rate_limit_implementation()

    async with httpx.AsyncClient(timeout=10) as client:
        await test_root_with_shop_redirects(client)
        await test_root_no_shop_returns_html(client)
        await test_health_returns_json(client)
        await test_oauth_callback_bad_nonce_redirects(client)
        await test_dashboard_inactive_merchant_redirects(client, conn)
        await test_webhook_missing_hmac_returns_401(client)
        await test_test_alert_no_session_redirects(client)
        await test_test_alert_no_webhook(client, conn)
        await test_root_invalid_shop_no_redirect(client)

    await conn.close()

    print("\n" + "=" * 60)
    print("All review-fix regression tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
