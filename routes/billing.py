"""
Shopify Billing API — recurring application charges.

Flow:
  1. GET /billing/start?shop=... -> creates charge, redirects to Shopify confirmation
  2. Merchant approves on Shopify -> redirects to /billing/callback?charge_id=X&shop=Y
  3. GET /billing/callback -> activates charge, shows success page
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
from services.token_manager import get_valid_token
from session import COOKIE_NAME, verify_session_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing")

_PLAN_NAME = "CheckoutGuard Pro"
_PLAN_PRICE = 29.0
_TRIAL_DAYS = 14


def _require_session(request: Request, shop: str) -> Optional[str]:
    cookie_val = request.cookies.get(COOKIE_NAME)
    if not cookie_val:
        return None
    verified = verify_session_token(cookie_val, settings.secret_key)
    if not verified or verified != shop:
        return None
    return verified


@router.get("/start")
async def billing_start(request: Request, shop: str = Query(...)) -> RedirectResponse:
    if not _require_session(request, shop):
        return RedirectResponse(url=f"/auth/shopify?shop={escape(shop)}", status_code=302)

    pool = await get_pool()
    async with pool.acquire() as conn:
        token = await get_valid_token(
            conn, shop, settings.shopify_api_key, settings.shopify_api_secret
        )
        if token and token.startswith("shpat_"):
            await conn.execute(
                "UPDATE merchants SET billing_status = 'active', billing_activated_at = NOW() WHERE shop_domain = $1",
                shop,
            )
            logger.warning("Skipped billing for %s — permanent token (Partner Dashboard not yet migrated)", shop)
            resp = RedirectResponse(url=f"/billing/activated?shop={escape(shop)}")
            return resp

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/2024-10/recurring_application_charges.json",
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            },
            json={
                "recurring_application_charge": {
                    "name": _PLAN_NAME,
                    "price": str(_PLAN_PRICE),
                    "return_url": f"{settings.app_url}/billing/callback",
                    "trial_days": _TRIAL_DAYS,
                    "test": settings.billing_test_mode,
                }
            },
        )
        if resp.status_code not in (200, 201):
            logger.error("Failed to create billing charge for %s: %s", shop, resp.text)
            return RedirectResponse(url=f"/billing/error?shop={escape(shop)}")
        charge = resp.json()["recurring_application_charge"]

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE merchants SET billing_charge_id = $1, billing_status = 'pending' WHERE shop_domain = $2",
            str(charge["id"]),
            shop,
        )

    return RedirectResponse(url=charge["confirmation_url"])


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

    async with httpx.AsyncClient(timeout=15) as client:
        check = await client.get(
            f"https://{shop}/admin/api/2024-10/recurring_application_charges/{charge_id}.json",
            headers={"X-Shopify-Access-Token": token},
        )
        if check.status_code != 200:
            logger.error("Failed to fetch charge %s for %s: %s", charge_id, shop, check.text)
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
            SET billing_charge_id = $1, billing_status = $2,
                billing_activated_at = $3, trial_ends_at = $4
            WHERE shop_domain = $5
            """,
            charge_id,
            charge["status"],
            billing_activated_at,
            trial_ends_at,
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


_CSS = """
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    max-width: 560px; margin: 80px auto; padding: 0 20px; color: #1a1a1a;
}
h1 { font-size: 22px; margin-bottom: 8px; }
.sub { color: #666; font-size: 15px; }
.icon { font-size: 40px; margin-bottom: 16px; }
a { color: #008060; }
"""


def _success_html(shop: str, trial_ends_at) -> str:
    safe_shop = escape(shop)
    trial_line = ""
    if trial_ends_at:
        trial_date = trial_ends_at.strftime("%B %d, %Y")
        trial_line = f"<p class='sub'>Your 14-day free trial runs until <strong>{trial_date}</strong>. No charge until then.</p>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>CheckoutGuard — Active</title><style>{_CSS}</style></head>
<body>
  <div class="icon">&#x2705;</div>
  <h1>You&rsquo;re all set</h1>
  <p class="sub">CheckoutGuard is now monitoring <strong>{safe_shop}</strong> for revenue drops.</p>
  {trial_line}
  <p class="sub">You&rsquo;ll receive a Slack alert if order volume drops &ge;20% below your 7-day baseline.</p>
</body>
</html>"""


def _declined_html(shop: str) -> str:
    safe_shop = escape(shop)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>CheckoutGuard — Billing</title><style>{_CSS}</style></head>
<body>
  <h1>Billing not confirmed</h1>
  <p class="sub">You declined billing for <strong>{safe_shop}</strong>.</p>
  <p class="sub"><a href="/billing/start?shop={safe_shop}">Try again</a> to activate CheckoutGuard.</p>
</body>
</html>"""


def _error_html(shop: str) -> str:
    safe_shop = escape(shop)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>CheckoutGuard — Error</title><style>{_CSS}</style></head>
<body>
  <h1>Billing setup failed</h1>
  <p class="sub">Could not create a billing charge for <strong>{safe_shop}</strong>.</p>
  <p class="sub">Please contact <a href="mailto:support@checkoutguard.io">support@checkoutguard.io</a></p>
  <p class="sub"><a href="/billing/start?shop={safe_shop}">Retry</a></p>
</body>
</html>"""
