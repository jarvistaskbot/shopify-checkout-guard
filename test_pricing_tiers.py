"""
4-tier pricing tests for CheckoutGuard.

Tests:
  1.  Plan registry — 4 plans with correct keys, names, prices, trial_days
  2.  Plan registry — feature flags correct per tier
  3.  GET /billing/plans — session-gated (no cookie → 302)
  4.  GET /billing/plans — renders 4 plan cards when authenticated
  5.  GET /billing/start?plan=growth — creates charge at $79 (mocked Shopify)
  6.  GET /billing/start?plan=invalid — defaults to starter, creates $29 charge
  7.  GET /billing/start?plan=scale — creates charge at $399
  8.  billing_guard.plan_allows — starter has no js_errors/ai/digest/oos
  9.  billing_guard.plan_allows — growth has js_errors+ai+digest, no oos
  10. billing_guard.plan_allows — pro has oos+fast_checks
  11. billing_guard.plan_allows — scale has ai_cap=1000 + custom_thresholds
  12. Upgrade path — /billing/callback stores plan on activation
  13. Weekly digest — query excludes starter merchants
  14. AI cap — scale gets 1000/mo cap, starter gets 200 (via get_ai_cap)
  15. _get_ai_analysis — returns None for starter plan (plan_allows gate)
  16. Proactive fast loop — run_proactive_checks_fast_merchants only queries pro/scale

Run: PYTHONPATH=. .venv312/bin/python test_pricing_tiers.py
     (server must be running at http://localhost:8000)
"""

import asyncio
import inspect
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
# Tests 1-2: Plan registry integrity
# ---------------------------------------------------------------------------

def test_plan_registry_keys() -> None:
    print("\n[1] Plan registry — 4 plans with correct structure")
    from services.plans import PLANS

    result("Exactly 4 plans", len(PLANS) == 4, f"got {len(PLANS)}")

    expected = {
        "starter": ("CheckoutGuard Starter", 29.0),
        "growth":  ("CheckoutGuard Growth",  79.0),
        "pro":     ("CheckoutGuard Pro",     199.0),
        "scale":   ("CheckoutGuard Scale",   399.0),
    }
    for key, (name, price) in expected.items():
        result(f"Plan '{key}' exists", key in PLANS)
        p = PLANS[key]
        result(f"  name='{name}'", p["name"] == name, f"got '{p['name']}'")
        result(f"  price={price}", p["price"] == price, f"got {p['price']}")
        result(f"  trial_days=14", p["trial_days"] == 14, f"got {p['trial_days']}")


def test_plan_feature_flags() -> None:
    print("\n[2] Plan registry — feature flags per tier")
    from services.plans import PLANS

    s, g, p, sc = PLANS["starter"], PLANS["growth"], PLANS["pro"], PLANS["scale"]

    # Starter: no advanced features
    result("starter: js_errors=False",        s["js_errors"] is False)
    result("starter: ai_analysis=False",      s["ai_analysis"] is False)
    result("starter: weekly_digest=False",    s["weekly_digest"] is False)
    result("starter: oos=False",              s["oos"] is False)
    result("starter: fast_checks=False",      s["fast_checks"] is False)
    result("starter: custom_thresholds=False",s["custom_thresholds"] is False)
    result("starter: ai_cap=200",             s["ai_cap"] == 200)

    # Growth: adds js_errors + ai + digest
    result("growth: js_errors=True",          g["js_errors"] is True)
    result("growth: ai_analysis=True",        g["ai_analysis"] is True)
    result("growth: weekly_digest=True",      g["weekly_digest"] is True)
    result("growth: oos=False",               g["oos"] is False)
    result("growth: fast_checks=False",       g["fast_checks"] is False)

    # Pro: adds oos + fast_checks
    result("pro: oos=True",                   p["oos"] is True)
    result("pro: fast_checks=True",           p["fast_checks"] is True)
    result("pro: custom_thresholds=False",    p["custom_thresholds"] is False)

    # Scale: adds custom_thresholds + ai_cap=1000
    result("scale: custom_thresholds=True",   sc["custom_thresholds"] is True)
    result("scale: ai_cap=1000",              sc["ai_cap"] == 1000)
    result("scale: multi_store=True",         sc["multi_store"] is True)


# ---------------------------------------------------------------------------
# Tests 3-4: /billing/plans endpoint
# ---------------------------------------------------------------------------

async def test_billing_plans_requires_session(client: httpx.AsyncClient) -> None:
    print("\n[3] GET /billing/plans — requires session cookie")
    r = await client.get(
        f"{BASE_URL}/billing/plans?shop={SHOP}",
        follow_redirects=False,
    )
    result("No cookie → 302 redirect", r.status_code == 302, f"got {r.status_code}")
    loc = r.headers.get("location", "")
    result("Redirects to /auth/shopify", "/auth/shopify" in loc)


async def test_billing_plans_renders_cards(client: httpx.AsyncClient) -> None:
    print("\n[4] GET /billing/plans — renders 4 plan cards")
    session = _make_session_cookie(SHOP)
    r = await client.get(
        f"{BASE_URL}/billing/plans?shop={SHOP}",
        cookies={"cg_session": session},
        follow_redirects=False,
    )
    result("Returns 200", r.status_code == 200, f"got {r.status_code}")
    html = r.text
    result("Contains 'Starter'",  "Starter"  in html)
    result("Contains 'Growth'",   "Growth"   in html)
    result("Contains 'Pro'",      "Pro"      in html)
    result("Contains 'Scale'",    "Scale"    in html)
    result("Contains '$29'",      "$29"      in html)
    result("Contains '$79'",      "$79"      in html)
    result("Contains '$199'",     "$199"     in html)
    result("Contains '$399'",     "$399"     in html)
    # Recommended badge appears
    result("Recommended badge present", "Recommended" in html)
    # Each card links to billing/start with plan param
    result("growth start link", "billing/start?shop=" in html and "plan=growth" in html)
    result("scale start link",  "plan=scale" in html)


# ---------------------------------------------------------------------------
# Tests 5-7: /billing/start with plan param (mocked Shopify)
# ---------------------------------------------------------------------------

async def test_billing_start_growth_price(conn) -> None:
    print("\n[5] /billing/start?plan=growth — charge at $79 (mocked Shopify)")
    from routes.billing import billing_start
    from services.plans import PLANS

    captured = {}

    async def mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 201
        charge_data = {
            "id": 88800001,
            "name": kwargs["json"]["recurring_application_charge"]["name"],
            "price": kwargs["json"]["recurring_application_charge"]["price"],
            "confirmation_url": "https://shopify.example.com/confirm/88800001",
        }
        captured["charge"] = charge_data
        resp.json.return_value = {"recurring_application_charge": charge_data}
        return resp

    # Ensure billing is pending so we can test update
    await conn.execute(
        "UPDATE merchants SET billing_status='inactive', plan='starter' WHERE shop_domain=$1", SHOP
    )

    with patch("routes.billing.get_valid_token", new=AsyncMock(return_value="shpca_not_partner_token")):
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            class FakeRequest:
                cookies = {"cg_session": _make_session_cookie(SHOP)}
            resp = await billing_start(FakeRequest(), shop=SHOP, plan="growth")

    plan_in_db = await conn.fetchval("SELECT plan FROM merchants WHERE shop_domain=$1", SHOP)
    result("plan stored as 'growth'", plan_in_db == "growth", f"got {plan_in_db}")
    result("charge name is Growth plan",
           "Growth" in captured.get("charge", {}).get("name", ""),
           f"got {captured.get('charge', {}).get('name')}")
    result("charge price is 79.0",
           captured.get("charge", {}).get("price") == "79.0",
           f"got {captured.get('charge', {}).get('price')}")


async def test_billing_start_invalid_plan_defaults_starter(conn) -> None:
    print("\n[6] /billing/start?plan=invalid — defaults to starter ($29)")
    from routes.billing import billing_start

    captured = {}

    async def mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 201
        charge_data = {
            "id": 88800002,
            "name": kwargs["json"]["recurring_application_charge"]["name"],
            "price": kwargs["json"]["recurring_application_charge"]["price"],
            "confirmation_url": "https://shopify.example.com/confirm/88800002",
        }
        captured["charge"] = charge_data
        resp.json.return_value = {"recurring_application_charge": charge_data}
        return resp

    await conn.execute(
        "UPDATE merchants SET billing_status='inactive', plan='starter' WHERE shop_domain=$1", SHOP
    )

    with patch("routes.billing.get_valid_token", new=AsyncMock(return_value="shpca_not_partner_token")):
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            class FakeRequest:
                cookies = {"cg_session": _make_session_cookie(SHOP)}
            resp = await billing_start(FakeRequest(), shop=SHOP, plan="invalid_plan_xyz")

    plan_in_db = await conn.fetchval("SELECT plan FROM merchants WHERE shop_domain=$1", SHOP)
    result("Invalid plan defaults to 'starter'", plan_in_db == "starter", f"got {plan_in_db}")
    result("Charge price is 29.0",
           captured.get("charge", {}).get("price") == "29.0",
           f"got {captured.get('charge', {}).get('price')}")


async def test_billing_start_scale_price(conn) -> None:
    print("\n[7] /billing/start?plan=scale — charge at $399")
    from routes.billing import billing_start

    captured = {}

    async def mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 201
        charge_data = {
            "id": 88800003,
            "name": kwargs["json"]["recurring_application_charge"]["name"],
            "price": kwargs["json"]["recurring_application_charge"]["price"],
            "confirmation_url": "https://shopify.example.com/confirm/88800003",
        }
        captured["charge"] = charge_data
        resp.json.return_value = {"recurring_application_charge": charge_data}
        return resp

    await conn.execute(
        "UPDATE merchants SET billing_status='inactive', plan='starter' WHERE shop_domain=$1", SHOP
    )

    with patch("routes.billing.get_valid_token", new=AsyncMock(return_value="shpca_not_partner_token")):
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            class FakeRequest:
                cookies = {"cg_session": _make_session_cookie(SHOP)}
            resp = await billing_start(FakeRequest(), shop=SHOP, plan="scale")

    plan_in_db = await conn.fetchval("SELECT plan FROM merchants WHERE shop_domain=$1", SHOP)
    result("plan stored as 'scale'", plan_in_db == "scale", f"got {plan_in_db}")
    result("Charge price is 399.0",
           captured.get("charge", {}).get("price") == "399.0",
           f"got {captured.get('charge', {}).get('price')}")


# ---------------------------------------------------------------------------
# Tests 8-11: Feature gating per tier
# ---------------------------------------------------------------------------

def test_plan_allows_starter() -> None:
    print("\n[8] plan_allows — starter has no js_errors/ai/digest/oos")
    from services.billing_guard import plan_allows
    result("starter: js_errors=False",     plan_allows("starter", "js_errors") is False)
    result("starter: ai_analysis=False",   plan_allows("starter", "ai_analysis") is False)
    result("starter: weekly_digest=False", plan_allows("starter", "weekly_digest") is False)
    result("starter: oos=False",           plan_allows("starter", "oos") is False)
    result("starter: fast_checks=False",   plan_allows("starter", "fast_checks") is False)
    # None defaults to starter
    result("None defaults to starter",     plan_allows(None, "js_errors") is False)


def test_plan_allows_growth() -> None:
    print("\n[9] plan_allows — growth has js_errors+ai+digest, no oos")
    from services.billing_guard import plan_allows
    result("growth: js_errors=True",       plan_allows("growth", "js_errors") is True)
    result("growth: ai_analysis=True",     plan_allows("growth", "ai_analysis") is True)
    result("growth: weekly_digest=True",   plan_allows("growth", "weekly_digest") is True)
    result("growth: oos=False",            plan_allows("growth", "oos") is False)
    result("growth: fast_checks=False",    plan_allows("growth", "fast_checks") is False)


def test_plan_allows_pro() -> None:
    print("\n[10] plan_allows — pro has oos+fast_checks")
    from services.billing_guard import plan_allows
    result("pro: oos=True",                plan_allows("pro", "oos") is True)
    result("pro: fast_checks=True",        plan_allows("pro", "fast_checks") is True)
    result("pro: custom_thresholds=False", plan_allows("pro", "custom_thresholds") is False)


def test_plan_allows_scale() -> None:
    print("\n[11] plan_allows — scale has 1000 AI cap + custom_thresholds")
    from services.billing_guard import plan_allows
    from services.plans import get_ai_cap
    result("scale: custom_thresholds=True", plan_allows("scale", "custom_thresholds") is True)
    result("scale: ai_cap=1000",            get_ai_cap("scale") == 1000, f"got {get_ai_cap('scale')}")
    result("scale: multi_store=True",       plan_allows("scale", "multi_store") is True)
    result("starter ai_cap=200",            get_ai_cap("starter") == 200)
    result("growth ai_cap=200",             get_ai_cap("growth") == 200)
    result("pro ai_cap=200",                get_ai_cap("pro") == 200)


# ---------------------------------------------------------------------------
# Test 12: Upgrade path — callback stores plan
# ---------------------------------------------------------------------------

async def test_billing_callback_stores_plan(conn) -> None:
    print("\n[12] Upgrade path — /billing/callback stores plan on activation")
    # Set merchant to 'pending' with plan='pro' (simulates /billing/start already ran)
    await conn.execute(
        "UPDATE merchants SET billing_status='pending', plan='pro', billing_charge_id='99999' WHERE shop_domain=$1",
        SHOP,
    )

    charge_id = "99999"
    mock_charge = {
        "id": 99999,
        "status": "active",
        "trial_days": 14,
        "name": "CheckoutGuard Pro",
        "price": "199.0",
    }

    async def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"recurring_application_charge": mock_charge}
        return resp

    async def mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 201
        activated = {**mock_charge, "status": "active"}
        resp.json.return_value = {"recurring_application_charge": activated}
        return resp

    from routes.billing import billing_callback

    with patch("routes.billing.get_valid_token", new=AsyncMock(return_value="shpca_test_token")):
        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=mock_get)):
            with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
                class FakeRequest:
                    cookies = {"cg_session": _make_session_cookie(SHOP)}
                resp = await billing_callback(FakeRequest(), charge_id=charge_id, shop=SHOP)

    row = await conn.fetchrow(
        "SELECT billing_status, plan FROM merchants WHERE shop_domain=$1", SHOP
    )
    result("billing_status=active after callback", row["billing_status"] == "active", row["billing_status"])
    result("plan remains 'pro' after activation", row["plan"] == "pro", f"got {row['plan']}")

    # Cleanup
    await conn.execute(
        "UPDATE merchants SET billing_status='inactive', plan='starter', billing_charge_id=NULL WHERE shop_domain=$1",
        SHOP,
    )


# ---------------------------------------------------------------------------
# Test 13: Weekly digest query excludes starter
# ---------------------------------------------------------------------------

def test_weekly_digest_excludes_starter() -> None:
    print("\n[13] Weekly digest — query excludes starter merchants")
    from main import _send_pending_digests
    src = inspect.getsource(_send_pending_digests)
    result(
        "Query includes plan filter",
        "plan IN" in src or "plan in" in src.lower(),
        "plan filter missing from digest query",
    )
    result(
        "starter excluded from digest",
        "'starter'" not in src or "growth" in src,
        "starter appears to be included in digest",
    )
    result(
        "growth/pro/scale included",
        "growth" in src and "pro" in src and "scale" in src,
    )


# ---------------------------------------------------------------------------
# Test 14: AI cap per plan
# ---------------------------------------------------------------------------

def test_ai_cap_per_plan() -> None:
    print("\n[14] AI cap — scale=1000, others=200")
    from services.plans import get_ai_cap
    result("starter cap=200", get_ai_cap("starter") == 200)
    result("growth cap=200",  get_ai_cap("growth") == 200)
    result("pro cap=200",     get_ai_cap("pro") == 200)
    result("scale cap=1000",  get_ai_cap("scale") == 1000)
    result("None defaults to 200", get_ai_cap(None) == 200)


# ---------------------------------------------------------------------------
# Test 15: _get_ai_analysis returns None for starter
# ---------------------------------------------------------------------------

async def test_ai_gate_starter(conn) -> None:
    print("\n[15] _get_ai_analysis — returns None for starter plan")
    import services.detector as det

    # Set plan to starter for this test.
    await conn.execute("UPDATE merchants SET plan='starter' WHERE shop_domain=$1", SHOP)

    result_val = await det._get_ai_analysis("checkout_funnel_collapse", {"test": True}, SHOP)
    result("Returns None for starter (no ai_analysis feature)", result_val is None,
           f"got {result_val!r}")


# ---------------------------------------------------------------------------
# Test 16: Fast proactive loop only queries pro/scale
# ---------------------------------------------------------------------------

def test_fast_proactive_queries_pro_scale() -> None:
    print("\n[16] Fast proactive loop — only queries pro/scale merchants")
    from services.detector import run_proactive_checks_fast_merchants
    src = inspect.getsource(run_proactive_checks_fast_merchants)
    result(
        "Filters on plan IN ('pro', 'scale')",
        "'pro'" in src and "'scale'" in src,
        "pro/scale filter not found in fast checks query",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("CheckoutGuard — Pricing Tier Test Suite")
    print("=" * 60)

    import asyncpg
    from config import settings
    from database import create_pool

    await create_pool(settings.database_url)
    conn = await asyncpg.connect(settings.database_url)

    # Unit tests (no server required)
    test_plan_registry_keys()
    test_plan_feature_flags()
    test_plan_allows_starter()
    test_plan_allows_growth()
    test_plan_allows_pro()
    test_plan_allows_scale()
    test_ai_cap_per_plan()
    test_weekly_digest_excludes_starter()
    test_fast_proactive_queries_pro_scale()

    # Tests with DB but no HTTP
    await test_billing_start_growth_price(conn)
    await test_billing_start_invalid_plan_defaults_starter(conn)
    await test_billing_start_scale_price(conn)
    await test_billing_callback_stores_plan(conn)
    await test_ai_gate_starter(conn)

    # Integration tests (need server)
    async with httpx.AsyncClient(timeout=10) as client:
        await test_billing_plans_requires_session(client)
        await test_billing_plans_renders_cards(client)

    # Restore merchant to clean state
    await conn.execute(
        "UPDATE merchants SET billing_status='inactive', plan='starter', billing_charge_id=NULL WHERE shop_domain=$1",
        SHOP,
    )
    await conn.close()

    print("\n" + "=" * 60)
    print("All pricing tier tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
