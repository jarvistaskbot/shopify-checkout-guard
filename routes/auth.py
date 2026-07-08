"""
Shopify OAuth flow.

Install URL pattern (offline token):
  https://{shop}/admin/oauth/authorize
    ?client_id={api_key}
    &scope={scopes}
    &redirect_uri={callback_url}
    &state={nonce}
"""

import hashlib
import hmac
import secrets

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
import httpx

from config import settings
from database import get_pool

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
    )
    return RedirectResponse(url)


@router.get("/callback")
async def callback(
    shop: str = Query(...),
    code: str = Query(...),
    state: str = Query(...),
    hmac_param: str = Query(alias="hmac"),
    request: Request = None,
) -> dict:
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

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO merchants (shop_domain, access_token, active)
            VALUES ($1, $2, TRUE)
            ON CONFLICT (shop_domain)
            DO UPDATE SET access_token = EXCLUDED.access_token, active = TRUE
            """,
            shop,
            access_token,
        )

    await _subscribe_webhooks(shop, access_token)

    return {"status": "installed", "shop": shop}


async def _subscribe_webhooks(shop: str, access_token: str) -> None:
    base_url = f"{settings.app_url}/webhooks"
    topics = [
        ("orders/create", f"{base_url}/orders/create"),
        ("checkouts/create", f"{base_url}/checkouts/create"),
        ("checkouts/delete", f"{base_url}/checkouts/delete"),
        ("app/uninstalled", f"{base_url}/app/uninstalled"),
    ]
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        for topic, address in topics:
            await client.post(
                f"https://{shop}/admin/api/2024-04/webhooks.json",
                headers=headers,
                json={"webhook": {"topic": topic, "address": address, "format": "json"}},
            )
