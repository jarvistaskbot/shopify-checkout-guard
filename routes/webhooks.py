"""
Shopify webhook handlers with HMAC verification.
"""

import base64
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from database import get_pool
from services.detector import process_event

logger = logging.getLogger(__name__)
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

        # Store line items for hot-product OOS detection (v2).
        line_items = payload.get("line_items", [])
        shopify_order_id = payload.get("id")
        if shopify_order_id and line_items:
            for item in line_items:
                try:
                    await conn.execute(
                        """
                        INSERT INTO order_line_items
                            (shop_domain, shopify_order_id, product_id, product_title,
                             variant_id, quantity, price)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        x_shopify_shop_domain,
                        int(shopify_order_id),
                        item.get("product_id"),
                        (item.get("title") or "")[:500],
                        item.get("variant_id"),
                        int(item.get("quantity", 1)),
                        float(item.get("price", 0)) if item.get("price") else None,
                    )
                except Exception as exc:
                    logger.warning("line_item insert failed for order %s: %s", order_id, exc)

    await process_event(x_shopify_shop_domain, "order_created")
    return {"ok": True}


@router.post("/checkouts/create")
async def checkout_created(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> dict:
    body = await _verify_hmac(request, x_shopify_hmac_sha256)
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

    await process_event(x_shopify_shop_domain, "checkout_created")
    return {"ok": True}


@router.post("/checkouts/delete")
async def checkout_deleted(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> dict:
    body = await _verify_hmac(request, x_shopify_hmac_sha256)
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

    await process_event(x_shopify_shop_domain, "checkout_deleted")
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


# ---------------------------------------------------------------------------
# GDPR mandatory webhooks (required for Shopify App Store review)
# ---------------------------------------------------------------------------

@router.post("/customers/data_request")
async def customers_data_request(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
) -> dict:
    """Shopify requests what customer data we hold for a given customer."""
    await _verify_hmac(request, x_shopify_hmac_sha256)
    # We do not store customer PII — only order timestamps and order IDs.
    # No further action required.
    logger.info("GDPR customers/data_request received — no PII stored")
    return {"ok": True}


@router.post("/customers/redact")
async def customers_redact(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
) -> dict:
    """Shopify requests deletion of customer data."""
    await _verify_hmac(request, x_shopify_hmac_sha256)
    # We store no customer PII; order IDs are anonymised aggregates.
    logger.info("GDPR customers/redact received — no PII to delete")
    return {"ok": True}


@router.post("/shop/redact")
async def shop_redact(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> dict:
    """Shopify requests permanent deletion of all merchant data (48h after uninstall)."""
    await _verify_hmac(request, x_shopify_hmac_sha256)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # CASCADE deletes checkout_events and incidents via FK constraints.
        await conn.execute(
            "DELETE FROM merchants WHERE shop_domain = $1 AND active = FALSE",
            x_shopify_shop_domain,
        )

    logger.info("GDPR shop/redact: deleted data for %s", x_shopify_shop_domain)
    return {"ok": True}


# ---------------------------------------------------------------------------
# v2 webhook: inventory level updates (requires read_inventory scope)
# ---------------------------------------------------------------------------

@router.post("/inventory/update")
async def inventory_updated(
    request: Request,
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> dict:
    body = await _verify_hmac(request, x_shopify_hmac_sha256)
    payload = json.loads(body)

    inventory_item_id = payload.get("inventory_item_id")
    available = payload.get("available")

    if inventory_item_id is None or available is None:
        return {"ok": True}

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO inventory_levels (shop_domain, inventory_item_id, available, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (shop_domain, inventory_item_id)
            DO UPDATE SET available = EXCLUDED.available, updated_at = NOW()
            """,
            x_shopify_shop_domain,
            int(inventory_item_id),
            int(available),
        )

    from services.detector import check_oos_hot_product
    import asyncio
    asyncio.create_task(
        check_oos_hot_product(x_shopify_shop_domain, int(inventory_item_id), int(available))
    )
    return {"ok": True}
