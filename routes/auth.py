"""
Shopify OAuth flow.

Install URL pattern (offline token):
  https://{shop}/admin/oauth/authorize
    ?client_id={api_key}
    &scope={scopes}
    &redirect_uri={callback_url}
    &state={nonce}
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import httpx

from config import settings
from database import get_pool
from services.token_manager import get_valid_token
from session import create_session_token, COOKIE_NAME

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")

_SCOPES = "read_orders,read_checkouts"
_NONCE_TTL_MINUTES = 15

# Valid *.myshopify.com shop domains only (used before echoing shop into URLs).
SHOP_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*\.myshopify\.com$", re.IGNORECASE)


def is_valid_shop_domain(shop: str) -> bool:
    return bool(shop) and bool(SHOP_DOMAIN_RE.match(shop))


def _invalid_shop_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>CheckoutGuard — Invalid shop</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           max-width: 560px; margin: 80px auto; padding: 0 20px; color: #1a1a1a; }
    h1 { font-size: 22px; margin-bottom: 8px; }
    .sub { color: #666; font-size: 15px; }
  </style>
</head>
<body>
  <h1>Invalid shop domain</h1>
  <p class="sub">The <code>shop</code> parameter must be a valid
  <code>*.myshopify.com</code> domain. Please install CheckoutGuard from the
  Shopify App Store.</p>
</body>
</html>"""


def _restart_oauth(shop: str) -> RedirectResponse:
    """Cleanly restart the OAuth flow for a validated shop domain."""
    return RedirectResponse(url=f"/auth/shopify?shop={quote(shop)}", status_code=302)


@router.get("/shopify")
async def install(shop: str = Query(..., description="Shopify shop domain")) -> Response:
    if not is_valid_shop_domain(shop):
        logger.warning("Rejected /auth/shopify with invalid shop param: %r", shop[:100])
        return HTMLResponse(content=_invalid_shop_html(), status_code=400)

    nonce = secrets.token_urlsafe(16)

    # Persist nonce to DB so it survives restarts and multi-instance deployments.
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO pending_nonces (nonce) VALUES ($1) ON CONFLICT DO NOTHING",
                nonce,
            )
    except Exception as exc:
        logger.error("Failed to store nonce: %s", exc)

    callback = f"{settings.app_url}/auth/callback"
    url = (
        f"https://{shop}/admin/oauth/authorize"
        f"?client_id={settings.shopify_api_key}"
        f"&scope={_SCOPES}"
        f"&redirect_uri={quote(callback, safe='')}"
        f"&state={nonce}"
    )
    return RedirectResponse(url)


@router.get("/callback")
async def callback(
    shop: str = Query(...),
    code: str = Query(...),
    state: str = Query(...),
    hmac_param: str = Query(alias="hmac"),
    request: Request = None,
) -> Response:
    # Validate shop before using it in any redirect or URL.
    if not is_valid_shop_domain(shop):
        logger.warning("OAuth callback with invalid shop param: %r", shop[:100])
        return HTMLResponse(content=_invalid_shop_html(), status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify nonce exists in DB and delete atomically.
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=_NONCE_TTL_MINUTES)
        deleted = await conn.fetchval(
            "DELETE FROM pending_nonces WHERE nonce=$1 AND created_at > $2 RETURNING nonce",
            state, cutoff,
        )
    if not deleted:
        # Back-button / retried callback: nonce already consumed or expired.
        # Restart OAuth cleanly instead of showing a raw JSON error.
        logger.info("OAuth callback with missing/expired nonce for %s — restarting OAuth", shop)
        return _restart_oauth(shop)

    # Verify HMAC from Shopify.
    params = dict(request.query_params)
    params.pop("hmac", None)
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    digest = hmac.new(
        settings.shopify_api_secret.encode(),
        sorted_params.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(digest, hmac_param):
        logger.warning("OAuth callback HMAC mismatch for %s — restarting OAuth", shop)
        return _restart_oauth(shop)

    # Exchange code for access token.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://{shop}/admin/oauth/access_token",
                json={
                    "client_id": settings.shopify_api_key,
                    "client_secret": settings.shopify_api_secret,
                    "code": code,
                },
            )
            resp.raise_for_status()
            token_data = resp.json()
    except Exception as exc:
        # Reused/expired authorization code (e.g. page refresh on the callback).
        logger.warning("OAuth code exchange failed for %s (%s) — restarting OAuth", shop, exc)
        return _restart_oauth(shop)

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")
    token_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        if expires_in else None
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO merchants (shop_domain, access_token, refresh_token, token_expires_at, active)
            VALUES ($1, $2, $3, $4, TRUE)
            ON CONFLICT (shop_domain)
            DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_expires_at = EXCLUDED.token_expires_at,
                active = TRUE
            """,
            shop,
            access_token,
            refresh_token,
            token_expires_at,
        )

    # Run non-blocking tasks: webhook registration and AOV fetch.
    # These fetch a valid (expiring) token themselves — the raw OAuth token is
    # non-expiring and Shopify's Admin API now rejects it.
    asyncio.create_task(_subscribe_webhooks(shop))
    asyncio.create_task(_fetch_and_store_aov(shop))

    # Determine redirect destination.
    pool2 = await get_pool()
    async with pool2.acquire() as conn2:
        row = await conn2.fetchrow(
            "SELECT slack_webhook_url, billing_status, onboarding_seen FROM merchants WHERE shop_domain = $1",
            shop,
        )
    has_config = row["slack_webhook_url"] if row else None
    billing_status = row["billing_status"] if row else None
    onboarding_seen = row["onboarding_seen"] if row else False

    # No Slack config and onboarding not yet seen → onboarding (which forwards
    # to /billing/plans). Merchants who skipped onboarding bypass this step.
    # Slack configured (or onboarding seen) but billing not active/pending
    # (e.g. reinstall after uninstall left billing_status='cancelled') → billing plans.
    # Otherwise → dashboard.
    if not has_config and not onboarding_seen:
        destination = f"/onboarding?shop={quote(shop)}"
    elif billing_status not in ("active", "pending"):
        destination = f"/billing/plans?shop={quote(shop)}"
    else:
        destination = f"/dashboard?shop={quote(shop)}"

    # Set signed session cookie and redirect.
    session_token = create_session_token(shop, settings.secret_key)
    response = RedirectResponse(url=destination)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=86400 * 30,
        path="/",
    )
    return response


async def _subscribe_webhooks(shop: str) -> None:
    from config import settings as _settings
    # Obtain an expiring token (exchanges the non-expiring OAuth token if needed);
    # Shopify's Admin API rejects non-expiring tokens for webhook registration.
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            access_token = await get_valid_token(
                conn, shop, settings.shopify_api_key, settings.shopify_api_secret
            )
    except Exception as exc:
        logger.error("Cannot register webhooks for %s — token unavailable: %s", shop, exc)
        return
    # GDPR compliance topics (customers/data_request, customers/redact,
    # shop/redact) are intentionally absent: Shopify rejects them via the
    # Admin API — they must be configured as compliance webhooks in the
    # Partner Dashboard. The HTTP endpoints for them remain in routes/webhooks.
    topics = [
        ("orders/create", "/webhooks/orders/create"),
        ("app/uninstalled", "/webhooks/app/uninstalled"),
        ("checkouts/create", "/webhooks/checkouts/create"),
        ("checkouts/delete", "/webhooks/checkouts/delete"),
        # v2: inventory topic (only registers if OOS_ENABLED — requires read_inventory scope granted post-approval)
        *([("inventory_levels/update", "/webhooks/inventory/update")] if _settings.oos_enabled else []),
    ]
    async with httpx.AsyncClient(timeout=15) as client:
        existing = await client.get(
            f"https://{shop}/admin/api/2024-10/webhooks.json",
            headers={"X-Shopify-Access-Token": access_token},
        )
        existing_topics = {w["topic"] for w in existing.json().get("webhooks", [])}

        for topic, path in topics:
            if topic in existing_topics:
                logger.info("Webhook already registered: %s", topic)
                continue
            resp = await client.post(
                f"https://{shop}/admin/api/2024-10/webhooks.json",
                headers={"X-Shopify-Access-Token": access_token},
                json={"webhook": {"topic": topic, "address": f"{settings.app_url}{path}", "format": "json"}},
            )
            if resp.status_code in (200, 201):
                logger.info("Registered webhook: %s", topic)
            else:
                logger.error("Failed to register %s: %s", topic, resp.text)


async def _fetch_and_store_aov(shop: str) -> None:
    """Fetch recent orders from Shopify and compute real AOV for this merchant."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            access_token = await get_valid_token(
                conn, shop, settings.shopify_api_key, settings.shopify_api_secret
            )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://{shop}/admin/api/2024-10/orders.json",
                headers={"X-Shopify-Access-Token": access_token},
                params={"status": "paid", "limit": 50, "fields": "total_price"},
            )
            if resp.status_code != 200:
                return
            orders = resp.json().get("orders", [])

        prices = [float(o["total_price"]) for o in orders if o.get("total_price")]
        if not prices:
            return

        aov = round(sum(prices) / len(prices), 2)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE merchants SET avg_order_value = $1 WHERE shop_domain = $2",
                aov,
                shop,
            )
        logger.info("AOV for %s set to $%.2f (from %d orders)", shop, aov, len(prices))
    except Exception as exc:
        logger.warning("AOV fetch failed for %s: %s", shop, exc)
