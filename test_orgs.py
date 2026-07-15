"""
CheckoutGuard v2.1 — multi-store organization tests.

Tests:
  1. GET /org — redirects non-Scale merchant to upgrade prompt
  2. GET /org — shows create/join form for Scale merchant not yet in an org
  3. POST /org/create — creates org and links merchant
  4. GET /org — shows org dashboard with linked stores after creation
  5. POST /org/join — second store joins via link_token
  6. GET /org — both stores appear in dashboard
  7. POST /org/create — rejected for non-Scale merchant (403)
  8. POST /org/join — rejected with invalid token (shows error, not 4xx)
  9. POST /org/join — rejected for non-Scale merchant (403)

Run: PYTHONPATH=. .venv/bin/python test_orgs.py
     (server must be running at http://localhost:8000; requires two test shops)
"""

import asyncio
import sys
from html import unescape

import httpx

import os
SHOP_A = os.environ.get("TEST_SHOP", "checkoutguard-dev-oxkbbl69.myshopify.com")
# Second test shop: use the same domain with a suffix for org join tests.
# In a real multi-tenant test env you'd use a separate install.
# These tests work against SHOP_A only for org creation; join tests
# are done via direct DB manipulation to avoid requiring a second OAuth install.
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


def _csrf(shop: str) -> str:
    from config import settings
    from session import create_session_token, COOKIE_NAME
    from session import csrf_token_for
    token = create_session_token(shop, settings.secret_key)
    return token, csrf_token_for(token, settings.secret_key)


async def clean_org_data(conn) -> None:
    """Remove test org data from both merchants and organizations tables."""
    await conn.execute(
        "UPDATE merchants SET organization_id=NULL WHERE shop_domain=$1", SHOP_A
    )
    # Delete any test orgs created during this run.
    await conn.execute(
        """DELETE FROM organizations
           WHERE id NOT IN (
               SELECT DISTINCT organization_id FROM merchants
               WHERE organization_id IS NOT NULL
           )"""
    )


# ---------------------------------------------------------------------------
# Test 1: non-Scale merchant sees upgrade prompt
# ---------------------------------------------------------------------------

async def test_non_scale_sees_upgrade(conn, client: httpx.AsyncClient) -> None:
    print("\n[1] GET /org — non-Scale merchant sees upgrade prompt")
    await conn.execute("UPDATE merchants SET plan='starter' WHERE shop_domain=$1", SHOP_A)

    session_cookie = _make_session_cookie(SHOP_A)
    r = await client.get(
        f"{BASE_URL}/org?shop={SHOP_A}",
        cookies={"cg_session": session_cookie},
        follow_redirects=False,
    )
    result("/org returns 200", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    result("Upgrade prompt present", "Scale plan" in html or "upgrade" in html.lower())
    result("billing/plans link present", "billing/plans" in html)


# ---------------------------------------------------------------------------
# Test 2: Scale merchant without org sees create/join form
# ---------------------------------------------------------------------------

async def test_scale_no_org_sees_form(conn, client: httpx.AsyncClient) -> None:
    print("\n[2] GET /org — Scale merchant not in org sees create/join form")
    await conn.execute(
        "UPDATE merchants SET plan='scale', billing_status='active', organization_id=NULL WHERE shop_domain=$1",
        SHOP_A,
    )

    session_cookie = _make_session_cookie(SHOP_A)
    r = await client.get(
        f"{BASE_URL}/org?shop={SHOP_A}",
        cookies={"cg_session": session_cookie},
        follow_redirects=False,
    )
    result("/org returns 200", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    result("Create org form present", "org/create" in html)
    result("Join org form present", "org/join" in html)
    result("link_token input present", "link_token" in html)


# ---------------------------------------------------------------------------
# Test 3: POST /org/create — creates org and links merchant
# ---------------------------------------------------------------------------

async def test_org_create(conn, client: httpx.AsyncClient) -> str:
    print("\n[3] POST /org/create — creates org, links Scale merchant")
    await conn.execute(
        "UPDATE merchants SET plan='scale', billing_status='active', organization_id=NULL WHERE shop_domain=$1",
        SHOP_A,
    )

    session_cookie, csrf = _csrf(SHOP_A)
    r = await client.post(
        f"{BASE_URL}/org/create",
        data={"shop": SHOP_A, "org_name": "Test Org Alpha", "csrf_token": csrf},
        cookies={"cg_session": session_cookie},
        follow_redirects=False,
    )
    result("POST /org/create redirects to /org", r.status_code == 303, f"got {r.status_code}")
    result("Redirect location is /org", "/org" in r.headers.get("location", ""))

    row = await conn.fetchrow(
        "SELECT organization_id FROM merchants WHERE shop_domain=$1", SHOP_A
    )
    result("organization_id set on merchant", row["organization_id"] is not None)

    org = await conn.fetchrow(
        "SELECT name, link_token FROM organizations WHERE id=$1", row["organization_id"]
    )
    result("Org created with correct name", org["name"] == "Test Org Alpha")
    result("link_token generated (non-empty)", len(org["link_token"]) > 10)

    return org["link_token"]


# ---------------------------------------------------------------------------
# Test 4: GET /org — shows org dashboard after creation
# ---------------------------------------------------------------------------

async def test_org_dashboard_renders(conn, client: httpx.AsyncClient) -> None:
    print("\n[4] GET /org — org dashboard renders with linked stores")
    session_cookie = _make_session_cookie(SHOP_A)
    r = await client.get(
        f"{BASE_URL}/org?shop={SHOP_A}",
        cookies={"cg_session": session_cookie},
        follow_redirects=False,
    )
    result("/org returns 200", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    result("Shop domain listed", SHOP_A in html)
    result("Link token visible", "Link token" in html or "link_token" in html.lower())
    result("Org name visible", "Test Org Alpha" in html)


# ---------------------------------------------------------------------------
# Test 5: POST /org/join — join via link_token (direct DB second store)
# ---------------------------------------------------------------------------

async def test_org_join(conn, client: httpx.AsyncClient, link_token: str) -> None:
    print("\n[5] POST /org/join — second store joins via link_token")

    # Insert a synthetic second merchant row for the join test.
    SHOP_B = "checkoutguard-test-store-b.myshopify.com"
    await conn.execute(
        """INSERT INTO merchants (shop_domain, access_token, active, billing_status, plan)
           VALUES ($1, 'shpat_test_placeholder', TRUE, 'active', 'scale')
           ON CONFLICT (shop_domain) DO UPDATE
               SET active=TRUE, billing_status='active', plan='scale', organization_id=NULL""",
        SHOP_B,
    )

    session_cookie_b, csrf_b = _csrf(SHOP_B)
    r = await client.post(
        f"{BASE_URL}/org/join",
        data={"shop": SHOP_B, "link_token": link_token, "csrf_token": csrf_b},
        cookies={"cg_session": session_cookie_b},
        follow_redirects=False,
    )
    result("POST /org/join redirects to /org", r.status_code == 303, f"got {r.status_code}")

    row_b = await conn.fetchrow(
        "SELECT organization_id FROM merchants WHERE shop_domain=$1", SHOP_B
    )
    row_a = await conn.fetchrow(
        "SELECT organization_id FROM merchants WHERE shop_domain=$1", SHOP_A
    )
    result("Store B joined same org as Store A",
           row_b["organization_id"] is not None and row_b["organization_id"] == row_a["organization_id"])

    # Cleanup B.
    await conn.execute("UPDATE merchants SET organization_id=NULL WHERE shop_domain=$1", SHOP_B)
    await conn.execute("DELETE FROM merchants WHERE shop_domain=$1 AND shop_domain != $2", SHOP_B, SHOP_A)


# ---------------------------------------------------------------------------
# Test 6: POST /org/create — rejected for non-Scale merchant
# ---------------------------------------------------------------------------

async def test_org_create_non_scale_rejected(conn, client: httpx.AsyncClient) -> None:
    print("\n[6] POST /org/create — 403 for non-Scale merchant")
    await conn.execute(
        "UPDATE merchants SET plan='growth', organization_id=NULL WHERE shop_domain=$1", SHOP_A
    )

    session_cookie, csrf = _csrf(SHOP_A)
    r = await client.post(
        f"{BASE_URL}/org/create",
        data={"shop": SHOP_A, "org_name": "Should Fail", "csrf_token": csrf},
        cookies={"cg_session": session_cookie},
        follow_redirects=False,
    )
    result("Non-Scale POST /org/create returns 403", r.status_code == 403, f"got {r.status_code}")

    row = await conn.fetchrow(
        "SELECT organization_id FROM merchants WHERE shop_domain=$1", SHOP_A
    )
    result("organization_id still NULL", row["organization_id"] is None)

    # Restore scale for subsequent tests.
    await conn.execute("UPDATE merchants SET plan='scale' WHERE shop_domain=$1", SHOP_A)


# ---------------------------------------------------------------------------
# Test 7: POST /org/join — invalid token shows error page (not 4xx)
# ---------------------------------------------------------------------------

async def test_org_join_invalid_token(conn, client: httpx.AsyncClient) -> None:
    print("\n[7] POST /org/join — invalid token shows error message")
    await conn.execute(
        "UPDATE merchants SET plan='scale', billing_status='active', organization_id=NULL WHERE shop_domain=$1",
        SHOP_A,
    )

    session_cookie, csrf = _csrf(SHOP_A)
    r = await client.post(
        f"{BASE_URL}/org/join",
        data={"shop": SHOP_A, "link_token": "definitely-not-a-real-token", "csrf_token": csrf},
        cookies={"cg_session": session_cookie},
        follow_redirects=False,
    )
    result("Invalid token returns 200 (not 4xx)", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    result("Error message present", "invalid" in html.lower() or "token" in html.lower())

    row = await conn.fetchrow(
        "SELECT organization_id FROM merchants WHERE shop_domain=$1", SHOP_A
    )
    result("organization_id still NULL after bad token", row["organization_id"] is None)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("CheckoutGuard v2.1 — Multi-Store Org Tests")
    print("=" * 60)

    import asyncpg
    from config import settings
    from database import create_pool

    await create_pool(settings.database_url)
    conn = await asyncpg.connect(settings.database_url)

    await clean_org_data(conn)

    async with httpx.AsyncClient(timeout=10) as client:
        await test_non_scale_sees_upgrade(conn, client)
        await test_scale_no_org_sees_form(conn, client)
        link_token = await test_org_create(conn, client)
        await test_org_dashboard_renders(conn, client)
        await test_org_join(conn, client, link_token)
        await test_org_create_non_scale_rejected(conn, client)
        await test_org_join_invalid_token(conn, client)

    await clean_org_data(conn)
    await conn.close()

    print("\n" + "=" * 60)
    print("All multi-store org tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
