"""
JS error event intake — public-facing, no Shopify HMAC needed.
Rate-limited and payload-capped; drops events from unknown shops.
"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_PAYLOAD_BYTES = 8192
_MAX_MESSAGE_LEN = 500
_MAX_SOURCE_LEN = 200
_MAX_URL_LEN = 500
_MAX_BATCH = 50
_RATE_LIMIT_EVENTS_PER_MIN = 120

# In-memory rate limit: shop_domain -> list of event timestamps in current minute window
_rate_windows: dict = {}


class _ErrorEvent(BaseModel):
    shop: str
    message: str
    source: Optional[str] = ""
    url: str
    ts: Optional[float] = None
    lineno: Optional[int] = None
    colno: Optional[int] = None


def _is_rate_limited(shop: str) -> bool:
    now = time.monotonic()
    window = _rate_windows.setdefault(shop, [])
    _rate_windows[shop] = [t for t in window if now - t < 60]
    if len(_rate_windows[shop]) >= _RATE_LIMIT_EVENTS_PER_MIN:
        return True
    _rate_windows[shop].append(now)
    return False


def _sanitize_shop(shop: str) -> str:
    shop = shop.strip().lower()
    if len(shop) > 100 or not shop:
        return ""
    if " " in shop or ".." in shop:
        return ""
    return shop


@router.post("/events")
async def ingest_events(request: Request) -> dict:
    # Read body first and enforce size limit on actual bytes (not Content-Length header,
    # which can be spoofed or omitted).
    body_bytes = await request.body()
    if len(body_bytes) > _MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    try:
        body = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if isinstance(body, dict):
        body = [body]
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected JSON array or object")

    pool = await get_pool()
    accepted = 0

    async with pool.acquire() as conn:
        for item in body[:_MAX_BATCH]:
            try:
                event = _ErrorEvent(**item)
            except Exception:
                continue

            shop = _sanitize_shop(event.shop)
            if not shop:
                continue

            if _is_rate_limited(shop):
                logger.debug("Rate limited: %s", shop)
                continue

            exists = await conn.fetchval(
                "SELECT 1 FROM merchants WHERE shop_domain = $1 AND active = TRUE", shop
            )
            if not exists:
                continue

            message = event.message[:_MAX_MESSAGE_LEN]
            source = (event.source or "")[:_MAX_SOURCE_LEN]
            page_url = event.url[:_MAX_URL_LEN]
            error_hash = hashlib.sha256(f"{message}|{source}".encode()).hexdigest()[:32]

            await conn.execute(
                """
                INSERT INTO js_error_events (shop_domain, error_hash, error_message, page_url)
                VALUES ($1, $2, $3, $4)
                """,
                shop, error_hash, message, page_url,
            )
            accepted += 1
            asyncio.create_task(_trigger_js_spike_check(shop, error_hash))

    return {"ok": True, "accepted": accepted}


async def _trigger_js_spike_check(shop: str, error_hash: str) -> None:
    try:
        from services.detector import check_js_error_spike
        await check_js_error_spike(shop, error_hash)
    except Exception as exc:
        logger.error("JS spike check failed for %s: %s", shop, exc)
