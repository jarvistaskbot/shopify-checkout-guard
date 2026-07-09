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
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
import httpx

from config import settings
from database import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")

_SCOPES = "read_orders,read_checkouts"

# In-memory nonce store (per-process; replace with Redis for multi-instance).
_pending_nonces: set[str] = set()


@router.get("/shopify")
async def install(shop: str = Query(..., description="Shopify shop domain")) -> RedirectResponse:
    nonce = secrets.token_urlsafe(16)
    _pending_nonces.add(nonce)

    callback = f"{settings.app_url}/auth/callback"
    url = (
        f"https://{shop}/admin/oauth/authorize"
        f"?client_id={settings.shopify_api_key}"
        f"&scope={_SCOPES}"
        f"&redirect_uri={callback}"
        f"&state={nonce}"
        f"&grant_options%5B%5D=offline"
    )
    return RedirectResponse(url)


@router.get("/callback")
async def callback(
    shop: str = Query(...),
    code: str = Query(...),
    state: str = Query(...),
    hmac_param: str = Query(alias="hmac"),
    request: Request = None,
) -> RedirectResponse:
    if state not in _pending_nonces:
        raise HTTPException(status_code=400, detail="Invalid state nonce")
    _pending_nonces.discard(state)

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
        raise HTTPException(status_code=403, detail="HMAC verification failed")

    # Exchange code for access token.
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
    asyncio.create_task(_subscribe_webhooks(shop, access_token))
    asyncio.create_task(_fetch_and_store_aov(shop, access_token))

    return RedirectResponse(url=f"/onboarding?shop={shop}")


async def _subscribe_webhooks(shop: str, access_token: str) -> None:
    topics = [
        ("orders/create", "/webhooks/orders/create"),
        ("app/uninstalled", "/webhooks/app/uninstalled"),
        ("customers/data_request", "/webhooks/customers/data_request"),
        ("customers/redact", "/webhooks/customers/redact"),
        ("shop/redact", "/webhooks/shop/redact"),
        ("checkouts/create", "/webhooks/checkouts/create"),
        ("checkouts/delete", "/webhooks/checkouts/delete"),
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


async def _fetch_and_store_aov(shop: str, access_token: str) -> None:
    """Fetch recent orders from Shopify and compute real AOV for this merchant."""
    try:
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
