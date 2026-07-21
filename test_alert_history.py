"""
Tests for alert delivery history + test-alert rate limit changes.

Tests:
  1. POST /dashboard/test-alert with a working webhook → 303 ta=sent and an
     alert_deliveries row with success=TRUE, alert_type='test'.
  2. Immediate second POST → 303 ta=limit with a w= countdown param
     (cooldown is 60s, not the old 600s).
  3. POST with an unreachable webhook (different shop, no cooldown clash)
     → 303 ta=error and an alert_deliveries row with success=FALSE.
  4. GET /dashboard shows the "Recent Alerts" section with a
     "Delivered to Slack" badge for the successful test alert.
  5. GET /dashboard for a shop with no deliveries shows the empty-state hint.
  6. Wiring: cooldown constant is 60; alerter records via _post for every
     incident alert type (source-level check).

Run: PYTHONPATH='' .venv312/bin/python test_alert_history.py
     (server must be running at http://localhost:8000; local Postgres must be running)
"""

import asyncio
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

SHOP_OK = "cg-alert-history-ok.myshopify.com"
SHOP_FAIL = "cg-alert-history-fail.myshopify.com"
SHOP_EMPTY = "cg-alert-history-empty.myshopify.com"
BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
MOCK_SLACK_PORT = 18999

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def result(label: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    if not ok:
        sys.exit(1)


class _MockSlackHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_mock_slack() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", MOCK_SLACK_PORT), _MockSlackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _make_session_cookie(shop: str) -> str:
    from config import settings
    from session import create_session_token
    return create_session_token(shop, settings.secret_key)


def _make_csrf(session_cookie: str) -> str:
    from config import settings
    from session import csrf_token_for
    return csrf_token_for(session_cookie, settings.secret_key)


async def _setup_merchant(conn, shop: str, webhook_url) -> None:
    await conn.execute("DELETE FROM merchants WHERE shop_domain=$1", shop)
    await conn.execute(
        """INSERT INTO merchants (shop_domain, access_token, slack_webhook_url,
                                  active, billing_status, onboarding_seen)
           VALUES ($1, 'test-token', $2, TRUE, 'active', TRUE)""",
        shop, webhook_url,
    )


async def _post_test_alert(client: httpx.AsyncClient, shop: str) -> httpx.Response:
    from session import COOKIE_NAME
    cookie = _make_session_cookie(shop)
    csrf = _make_csrf(cookie)
    return await client.post(
        f"{BASE_URL}/dashboard/test-alert",
        data={"shop": shop, "csrf_token": csrf},
        cookies={COOKIE_NAME: cookie},
        follow_redirects=False,
    )


async def test_1_sent_and_recorded(client, conn) -> None:
    print("\n[1] test-alert with working webhook → ta=sent + success row in alert_deliveries")
    await _setup_merchant(conn, SHOP_OK, f"http://127.0.0.1:{MOCK_SLACK_PORT}/hook")
    await conn.execute("DELETE FROM alert_deliveries WHERE shop_domain=$1", SHOP_OK)

    resp = await _post_test_alert(client, SHOP_OK)
    loc = resp.headers.get("location", "")
    result("303 redirect with ta=sent", resp.status_code == 303 and "ta=sent" in loc, loc)

    row = await conn.fetchrow(
        """SELECT alert_type, success, status_detail FROM alert_deliveries
           WHERE shop_domain=$1 ORDER BY sent_at DESC LIMIT 1""",
        SHOP_OK,
    )
    result("alert_deliveries row exists", row is not None)
    result("row: type=test, success=TRUE",
           row["alert_type"] == "test" and row["success"] is True,
           f"type={row['alert_type']} success={row['success']} detail={row['status_detail']}")


async def test_2_rate_limit(client, conn) -> None:
    print("\n[2] immediate second test-alert → ta=limit with countdown param")
    resp = await _post_test_alert(client, SHOP_OK)
    loc = resp.headers.get("location", "")
    result("303 redirect with ta=limit", resp.status_code == 303 and "ta=limit" in loc, loc)
    m = re.search(r"[&?]w=(\d+)", loc)
    result("countdown param present and <= 60", bool(m) and 0 < int(m.group(1)) <= 60,
           f"w={m.group(1) if m else 'missing'}")


async def test_3_failed_delivery_recorded(client, conn) -> None:
    print("\n[3] unreachable webhook → ta=error + failure row in alert_deliveries")
    # Port 18998 has no listener — connection refused.
    await _setup_merchant(conn, SHOP_FAIL, "http://127.0.0.1:18998/hook")
    await conn.execute("DELETE FROM alert_deliveries WHERE shop_domain=$1", SHOP_FAIL)

    resp = await _post_test_alert(client, SHOP_FAIL)
    loc = resp.headers.get("location", "")
    result("303 redirect with ta=error", resp.status_code == 303 and "ta=error" in loc, loc)

    row = await conn.fetchrow(
        """SELECT alert_type, success, status_detail FROM alert_deliveries
           WHERE shop_domain=$1 ORDER BY sent_at DESC LIMIT 1""",
        SHOP_FAIL,
    )
    result("failure row exists with success=FALSE",
           row is not None and row["success"] is False,
           f"detail={row['status_detail'][:60] if row else None}")


async def test_4_dashboard_shows_history(client, conn) -> None:
    print("\n[4] dashboard renders Recent Alerts with Delivered badge")
    from session import COOKIE_NAME
    cookie = _make_session_cookie(SHOP_OK)
    resp = await client.get(
        f"{BASE_URL}/dashboard",
        params={"shop": SHOP_OK},
        cookies={COOKIE_NAME: cookie},
        follow_redirects=False,
    )
    result("dashboard 200", resp.status_code == 200, str(resp.status_code))
    html = resp.text
    result("contains Recent Alerts section", "Recent Alerts" in html)
    result("contains Delivered to Slack badge", "Delivered to Slack" in html)
    result("contains Test Alert type label", "Test Alert" in html)


async def test_5_empty_state(client, conn) -> None:
    print("\n[5] dashboard with no deliveries shows empty-state hint")
    from session import COOKIE_NAME
    await _setup_merchant(conn, SHOP_EMPTY, None)
    cookie = _make_session_cookie(SHOP_EMPTY)
    resp = await client.get(
        f"{BASE_URL}/dashboard",
        params={"shop": SHOP_EMPTY},
        cookies={COOKIE_NAME: cookie},
        follow_redirects=False,
    )
    result("dashboard 200", resp.status_code == 200, str(resp.status_code))
    result("contains empty-state hint", "No alerts sent yet" in resp.text)


def test_6_wiring() -> None:
    print("\n[6] wiring: cooldown=60; every incident alert passes metadata to _post")
    from routes import dashboard as dash
    result("cooldown constant is 60", dash._TEST_ALERT_COOLDOWN_SECS == 60,
           str(dash._TEST_ALERT_COOLDOWN_SECS))

    src = open(os.path.join(os.path.dirname(__file__), "services", "alerter.py")).read()
    for atype in ["checkout_funnel_collapse", "volume_drop", "abandonment_spike",
                  "payment_failure", "js_error_spike", "oos_hot_product",
                  "recovery", "slow_bleed", "test"]:
        result(f"_post records alert_type={atype}", f'alert_type="{atype}"' in src)
    result("_record_delivery never raises (guarded)", "except Exception" in
           src.split("async def _record_delivery")[1].split("async def")[0])


async def _cleanup(conn) -> None:
    for shop in (SHOP_OK, SHOP_FAIL, SHOP_EMPTY):
        await conn.execute("DELETE FROM merchants WHERE shop_domain=$1", shop)


async def main() -> None:
    import asyncpg
    from config import settings

    mock = start_mock_slack()
    db_url = settings.database_url.replace("db:5432", "localhost:5432")
    conn = await asyncpg.connect(db_url)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await test_1_sent_and_recorded(client, conn)
            await test_2_rate_limit(client, conn)
            await test_3_failed_delivery_recorded(client, conn)
            await test_4_dashboard_shows_history(client, conn)
            await test_5_empty_state(client, conn)
        test_6_wiring()
    finally:
        await _cleanup(conn)
        await conn.close()
        mock.shutdown()

    print("\nAll alert-history tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
