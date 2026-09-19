"""
Public pixel-event ingestion endpoint for the Web Pixel extension.

Web pixels run inside Shopify's sandboxed checkout environment and cannot
send session tokens or cookies — auth is a shop-domain allowlist from the
merchants table. Unknown shops are silently accepted (200) to avoid leaking
information about which shops are active.

Rate / size limits enforce basic DoS hygiene for a public unauthenticated path.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_BODY_BYTES = 4096

# Map Shopify Web Pixel event names → checkout_events.event_type column values.
# Unmapped events (e.g. page_viewed) are accepted but not persisted.
_EVENT_MAP = {
    "checkout_started": "checkout_created",
    "checkout_completed": "order_created",
}


def _cors_response(status_code: int, body: dict) -> JSONResponse:
    resp = JSONResponse(body, status_code=status_code)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@router.options("/pixel-events")
async def pixel_events_preflight() -> JSONResponse:
    return _cors_response(204, {})


@router.post("/pixel-events")
async def pixel_events(request: Request) -> JSONResponse:
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return _cors_response(413, {"ok": False, "error": "payload_too_large"})

    try:
        payload = await request.json()
    except Exception:
        return _cors_response(400, {"ok": False, "error": "invalid_json"})

    event_name = str(payload.get("event_name") or "")
    shop = str(payload.get("shop") or "")

    event_type = _EVENT_MAP.get(event_name)
    if not event_type:
        # page_viewed and any unknown events — accept silently, don't persist.
        return _cors_response(200, {"ok": True})

    if not shop or len(shop) > 300:
        return _cors_response(200, {"ok": True})

    checkout_token = str(payload.get("checkout_token") or "")[:512] or None
    order_id = str(payload.get("order_id") or "")[:100] or None

    try:
        pool = await get_pool()
    except RuntimeError:
        # Pool not yet initialised (very early startup race) — accept silently.
        return _cors_response(200, {"ok": True})

    try:
        async with pool.acquire() as conn:
            active = await conn.fetchval(
                "SELECT 1 FROM merchants WHERE shop_domain = $1 AND active = TRUE",
                shop,
            )
            if not active:
                # Unknown/inactive shop — silently ignore, no FK violation risk.
                return _cors_response(200, {"ok": True})

            await conn.execute(
                """
                INSERT INTO checkout_events (shop_domain, event_type, checkout_token, order_id)
                VALUES ($1, $2, $3, $4)
                """,
                shop,
                event_type,
                checkout_token,
                order_id,
            )
    except Exception as exc:
        logger.error("pixel_events insert failed for %s/%s: %s", shop, event_name, exc)

    return _cors_response(200, {"ok": True})
