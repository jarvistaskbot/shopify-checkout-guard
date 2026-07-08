"""
Shopify webhook handlers with HMAC verification.
"""

import base64
import hashlib
import hmac

from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from database import get_pool
from services.detector import process_event

router = APIRouter(prefix="/webhooks")


async def _verify_hmac(request: Request, x_shopify_hmac_sha256: str) -> bytes:
    body = await request.body()
    digest = base64.b64encode(
        hmac.new(
            settings.shopify_api_secret.encode(),
            body,
            hashlib.sha256,
        ).digest()
    ).decode()
    if not hmac.compare_digest(digest, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="HMAC verification failed")
    return body


@router.post("/orders/create")
async def order_created(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> dict:
    body = await _verify_hmac(request, x_shopify_hmac_sha256)
    import json
    payload = json.loads(body)
    order_id = str(payload.get("id", ""))
    checkout_token = payload.get("checkout_token", "")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkout_events (shop_domain, event_type, checkout_token, order_id)
            VALUES ($1, 'order_created', $2, $3)
            """,
            x_shopify_shop_domain,
            checkout_token,
            order_id,
        )

    await process_event(x_shopify_shop_domain)
    return {"ok": True}


@router.post("/checkouts/create")
async def checkout_created(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> dict:
    body = await _verify_hmac(request, x_shopify_hmac_sha256)
    import json
    payload = json.loads(body)
    checkout_token = payload.get("token", "")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkout_events (shop_domain, event_type, checkout_token)
            VALUES ($1, 'checkout_created', $2)
            """,
            x_shopify_shop_domain,
            checkout_token,
        )

    await process_event(x_shopify_shop_domain)
    return {"ok": True}


@router.post("/checkouts/delete")
async def checkout_deleted(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> dict:
    body = await _verify_hmac(request, x_shopify_hmac_sha256)
    import json
    payload = json.loads(body)
    checkout_token = payload.get("token", "")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkout_events (shop_domain, event_type, checkout_token)
            VALUES ($1, 'checkout_deleted', $2)
            """,
            x_shopify_shop_domain,
            checkout_token,
        )

    return {"ok": True}


@router.post("/app/uninstalled")
async def app_uninstalled(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> dict:
    await _verify_hmac(request, x_shopify_hmac_sha256)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE merchants SET active = FALSE WHERE shop_domain = $1",
            x_shopify_shop_domain,
        )

    return {"ok": True}
