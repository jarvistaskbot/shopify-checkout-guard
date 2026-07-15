"""
Shopify Billing API — 4-tier recurring application charges.

Flow:
  1. POST /onboarding → GET /billing/plans?shop=... (choose plan)
  2. GET /billing/start?shop=...&plan=growth → creates charge, redirects to Shopify
  3. Merchant approves on Shopify → GET /billing/callback?charge_id=X&shop=Y
  4. GET /billing/callback → activates charge, stores plan, shows success page
"""

import logging
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Optional

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from database import get_pool
from services.plans import PLANS
from services.token_manager import get_valid_token
from session import COOKIE_NAME, verify_session_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing")

_TRIAL_DAYS = 14


def _require_session(request: Request, shop: str) -> Optional[str]:
    cookie_val = request.cookies.get(COOKIE_NAME)
    if not cookie_val:
        return None
    verified = verify_session_token(cookie_val, settings.secret_key)
    if not verified or verified != shop:
        return None
    return verified


# ---------------------------------------------------------------------------
# GET /billing/plans — plan selection page
# ---------------------------------------------------------------------------

@router.get("/plans")
async def billing_plans(request: Request, shop: str = Query(...)) -> HTMLResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    safe_shop = escape(shop)

    # Fetch current plan and trailing-30-day GMV for recommendation.
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT plan, avg_order_value FROM merchants WHERE shop_domain=$1", shop
        )
        current_plan = (row["plan"] if row else None) or "starter"

        gmv_30d = await conn.fetchval(
            """SELECT COALESCE(SUM(price * quantity), 0)
               FROM order_line_items
               WHERE shop_domain=$1 AND created_at >= NOW() - INTERVAL '30 days'""",
            shop,
        ) or 0

    # Pick recommended tier based on GMV.
    if gmv_30d >= 500_000:
        recommended = "scale"
    elif gmv_30d >= 200_000:
        recommended = "pro"
    else:
        recommended = "growth"

    return HTMLResponse(content=_plans_html(safe_shop, current_plan, recommended))


# ---------------------------------------------------------------------------
# GET /billing/start — create charge for chosen plan
# ---------------------------------------------------------------------------

@router.get("/start")
async def billing_start(
    request: Request,
    shop: str = Query(...),
    plan: str = Query(default="starter"),
) -> RedirectResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    if plan not in PLANS:
        plan = "starter"

    plan_cfg = PLANS[plan]

    pool = await get_pool()
    async with pool.acquire() as conn:
        token = await get_valid_token(
            conn, shop, settings.shopify_api_key, settings.shopify_api_secret
        )
        # Expiring tokens also use the shpat_ prefix, so the prefix alone says
        # nothing. Only bypass billing if the token is genuinely non-expiring
        # (exchange failed) — Shopify's billing API would reject it anyway.
        expires_at = await conn.fetchval(
            "SELECT token_expires_at FROM merchants WHERE shop_domain=$1", shop
        )
        if token and expires_at is None:
            await conn.execute(
                """UPDATE merchants
                   SET billing_status='active', billing_activated_at=NOW(), plan=$1
                   WHERE shop_domain=$2""",
                plan,
                shop,
            )
            logger.warning(
                "Skipped billing for %s — non-expiring token, exchange failed (plan=%s)", shop, plan
            )
            return RedirectResponse(url=f"/billing/activated?shop={escape(shop)}")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/2024-10/recurring_application_charges.json",
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            },
            json={
                "recurring_application_charge": {
                    "name": plan_cfg["name"],
                    "price": str(plan_cfg["price"]),
                    "return_url": f"{settings.app_url}/billing/callback",
                    "trial_days": plan_cfg["trial_days"],
                    "test": settings.billing_test_mode,
                }
            },
        )
        if resp.status_code not in (200, 201):
            logger.error(
                "Failed to create billing charge for %s (plan=%s): %s", shop, plan, resp.text
            )
            return RedirectResponse(url=f"/billing/error?shop={escape(shop)}")
        charge = resp.json()["recurring_application_charge"]

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE merchants
               SET billing_charge_id=$1, billing_status='pending', plan=$2
               WHERE shop_domain=$3""",
            str(charge["id"]),
            plan,
            shop,
        )

    return RedirectResponse(url=charge["confirmation_url"])


# ---------------------------------------------------------------------------
# GET /billing/callback — activate charge
# ---------------------------------------------------------------------------

@router.get("/callback")
async def billing_callback(
    request: Request,
    charge_id: str = Query(...),
    shop: str = Query(...),
) -> HTMLResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    pool = await get_pool()
    async with pool.acquire() as conn:
        token = await get_valid_token(
            conn, shop, settings.shopify_api_key, settings.shopify_api_secret
        )
        stored_plan = await conn.fetchval(
            "SELECT plan FROM merchants WHERE shop_domain=$1", shop
        ) or "starter"

    async with httpx.AsyncClient(timeout=15) as client:
        check = await client.get(
            f"https://{shop}/admin/api/2024-10/recurring_application_charges/{charge_id}.json",
            headers={"X-Shopify-Access-Token": token},
        )
        if check.status_code != 200:
            logger.error(
                "Failed to fetch charge %s for %s: %s", charge_id, shop, check.text
            )
            return HTMLResponse(content=_error_html(shop))

        charge = check.json()["recurring_application_charge"]

        if charge["status"] == "pending":
            activate = await client.post(
                f"https://{shop}/admin/api/2024-10/recurring_application_charges/{charge_id}/activate.json",
                headers={"X-Shopify-Access-Token": token},
                json={"recurring_application_charge": charge},
            )
            if activate.status_code in (200, 201):
                charge = activate.json()["recurring_application_charge"]

    now = datetime.now(timezone.utc)
    trial_ends_at = now + timedelta(days=_TRIAL_DAYS) if charge.get("trial_days") else None
    billing_activated_at = now if charge["status"] in ("active", "pending") else None

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE merchants
            SET billing_charge_id=$1, billing_status=$2,
                billing_activated_at=$3, trial_ends_at=$4, plan=$5
            WHERE shop_domain=$6
            """,
            charge_id,
            charge["status"],
            billing_activated_at,
            trial_ends_at,
            stored_plan,
            shop,
        )

    if charge["status"] in ("active", "pending"):
        return HTMLResponse(content=_success_html(shop, trial_ends_at))
    else:
        return HTMLResponse(content=_declined_html(shop))


@router.get("/activated")
async def billing_activated(request: Request, shop: str = Query(...)) -> HTMLResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)
    return HTMLResponse(content=_success_html(shop, None))


@router.get("/error")
async def billing_error(request: Request, shop: str = Query(...)) -> HTMLResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)
    return HTMLResponse(content=_error_html(shop))


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

_CSS = """
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #1a1a1a;
}
"""

_PLANS_CSS = """
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    max-width: 900px; margin: 60px auto; padding: 0 20px; color: #1a1a1a;
}
h1 { font-size: 22px; margin-bottom: 4px; }
.sub { color: #666; font-size: 14px; margin-bottom: 36px; }
.plans { display: flex; gap: 20px; flex-wrap: wrap; }
.plan-card {
    flex: 1 1 180px;
    border: 1px solid #e0e0e0;
    border-radius: 12px;
    padding: 24px 20px;
    position: relative;
    min-width: 180px;
}
.plan-card.current { border-color: #008060; border-width: 2px; }
.plan-card.recommended { border-color: #005c44; background: #f0faf6; border-width: 2px; }
.plan-name { font-size: 16px; font-weight: 700; margin-bottom: 6px; }
.plan-price { font-size: 26px; font-weight: 700; margin-bottom: 4px; color: #1a1a1a; }
.plan-price span { font-size: 14px; font-weight: 400; color: #666; }
.plan-trial { font-size: 12px; color: #666; margin-bottom: 16px; }
.plan-features { list-style: none; padding: 0; margin: 0 0 20px; font-size: 13px; }
.plan-features li { padding: 3px 0; color: #333; }
.plan-features li::before { content: "✓ "; color: #008060; font-weight: 700; }
.plan-features li.no::before { content: "— "; color: #bbb; }
.plan-features li.no { color: #999; }
.badge {
    display: inline-block; font-size: 11px; font-weight: 700;
    padding: 2px 8px; border-radius: 12px; margin-bottom: 8px;
}
.badge-current { background: #e6f4ef; color: #006b45; }
.badge-recommended { background: #005c44; color: white; }
.btn {
    display: block; width: 100%; padding: 10px 0;
    background: #008060; color: white; border: none;
    border-radius: 6px; font-size: 14px; font-weight: 600;
    cursor: pointer; text-align: center; text-decoration: none;
    box-sizing: border-box;
}
.btn:hover { background: #006e52; }
.btn-secondary {
    background: #f0f0f0; color: #333; border: 1px solid #ccc;
}
.btn-secondary:hover { background: #e0e0e0; }
a { color: #008060; }
"""

_SIMPLE_CSS = """
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    max-width: 560px; margin: 80px auto; padding: 0 20px; color: #1a1a1a;
}
h1 { font-size: 22px; margin-bottom: 8px; }
.sub { color: #666; font-size: 15px; }
.icon { font-size: 40px; margin-bottom: 16px; }
a { color: #008060; }
"""


def _feature_li(label: str, enabled: bool) -> str:
    cls = "" if enabled else " class=\"no\""
    return f"<li{cls}>{escape(label)}</li>"


def _plans_html(safe_shop: str, current_plan: str, recommended: str) -> str:
    cards = ""
    for key, cfg in PLANS.items():
        is_current = key == current_plan
        is_rec = key == recommended and not is_current

        card_cls = "plan-card"
        if is_current:
            card_cls += " current"
        elif is_rec:
            card_cls += " recommended"

        badges = ""
        if is_current:
            badges += '<span class="badge badge-current">Current plan</span><br>'
        if is_rec:
            badges += '<span class="badge badge-recommended">Recommended for your store</span><br>'

        features = (
            _feature_li("Checkout drop alerts", True)
            + _feature_li("Payment failure alerts", True)
            + _feature_li("Slack + email notifications", True)
            + _feature_li("Dashboard", True)
            + _feature_li("JS-error monitoring + AI diagnosis", cfg["js_errors"])
            + _feature_li("Weekly digest email", cfg["weekly_digest"])
            + _feature_li("OOS revenue-clock alerts", cfg["oos"])
            + _feature_li(
                f"AI analysis ({cfg['ai_cap']}/mo cap)", cfg["ai_analysis"]
            )
            + _feature_li("Proactive checks every 1 min", cfg["fast_checks"])
            + _feature_li("Custom alert thresholds", cfg["custom_thresholds"])
            + _feature_li("Multi-store ready", cfg["multi_store"])
        )

        if is_current:
            btn = f'<a class="btn btn-secondary" href="/billing/start?shop={safe_shop}&amp;plan={key}">Renew / keep plan</a>'
        else:
            btn = f'<a class="btn" href="/billing/start?shop={safe_shop}&amp;plan={key}">Start {cfg["trial_days"]}-day trial</a>'

        price_int = int(cfg["price"])
        cards += f"""
<div class="{card_cls}">
  {badges}
  <div class="plan-name">{escape(cfg["name"])}</div>
  <div class="plan-price">${price_int}<span>/mo</span></div>
  <div class="plan-trial">{cfg["trial_days"]}-day free trial</div>
  <ul class="plan-features">{features}</ul>
  {btn}
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>CheckoutGuard — Choose a Plan</title>
  <style>{_PLANS_CSS}</style>
</head>
<body>
  <h1>Choose your CheckoutGuard plan</h1>
  <p class="sub">Monitoring <strong>{safe_shop}</strong> &bull; All plans include a 14-day free trial</p>
  <div class="plans">{cards}</div>
  <p style="margin-top:32px; font-size:13px; color:#999;">
    Questions? <a href="mailto:artomnats1996@gmail.com">Contact support</a>
  </p>
</body>
</html>"""


def _success_html(shop: str, trial_ends_at) -> str:
    safe_shop = escape(shop)
    trial_line = ""
    if trial_ends_at:
        trial_date = trial_ends_at.strftime("%B %d, %Y")
        trial_line = (
            f"<p class='sub'>Your 14-day free trial runs until <strong>{trial_date}</strong>. "
            f"No charge until then.</p>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>CheckoutGuard — Active</title><style>{_SIMPLE_CSS}</style></head>
<body>
  <div class="icon">&#x2705;</div>
  <h1>You&rsquo;re all set</h1>
  <p class="sub">CheckoutGuard is now monitoring <strong>{safe_shop}</strong> for revenue drops.</p>
  {trial_line}
  <p class="sub">You&rsquo;ll receive a Slack alert if checkout conversion drops &ge;20% below your 7-day baseline.</p>
  <p class="sub"><a href="/dashboard?shop={safe_shop}">Go to dashboard</a></p>
</body>
</html>"""


def _declined_html(shop: str) -> str:
    safe_shop = escape(shop)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>CheckoutGuard — Billing</title><style>{_SIMPLE_CSS}</style></head>
<body>
  <h1>Billing not confirmed</h1>
  <p class="sub">You declined billing for <strong>{safe_shop}</strong>.</p>
  <p class="sub"><a href="/billing/plans?shop={safe_shop}">Choose a plan</a> to activate CheckoutGuard.</p>
</body>
</html>"""


def _error_html(shop: str) -> str:
    safe_shop = escape(shop)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>CheckoutGuard — Error</title><style>{_SIMPLE_CSS}</style></head>
<body>
  <h1>Billing setup failed</h1>
  <p class="sub">Could not create a billing charge for <strong>{safe_shop}</strong>.</p>
  <p class="sub">Please contact <a href="mailto:artomnats1996@gmail.com">artomnats1996@gmail.com</a></p>
  <p class="sub"><a href="/billing/plans?shop={safe_shop}">Try again</a></p>
</body>
</html>"""
