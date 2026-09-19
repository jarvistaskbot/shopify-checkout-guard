#!/usr/bin/env python3
"""
Backfill webPixelCreate for all currently active merchants.

Run once after deploying the web pixel extension to activate it on existing
shops. Safe to re-run — shops that already have the pixel get PIXEL_ALREADY_EXISTS
which is treated as success.

Usage (from repo root):
  DATABASE_URL=... SHOPIFY_API_KEY=... SHOPIFY_API_SECRET=... python scripts/backfill_web_pixels.py

Or with .env:
  python scripts/backfill_web_pixels.py
"""

import asyncio
import logging
import os
import sys

import asyncpg
import httpx

# Load .env if present.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SHOPIFY_API_KEY = os.environ.get("SHOPIFY_API_KEY", "")
SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")

_MUTATION = """
mutation webPixelCreate($settings: String!) {
  webPixelCreate(settings: $settings) {
    webPixel { id }
    userErrors { code field message }
  }
}
"""


async def _get_valid_token(pool, shop: str) -> str:
    """Return a valid access token, refreshing if expired."""
    # Import from services if available; otherwise use raw token.
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from services.token_manager import get_valid_token
        async with pool.acquire() as conn:
            return await get_valid_token(conn, shop, SHOPIFY_API_KEY, SHOPIFY_API_SECRET)
    except Exception:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT access_token FROM merchants WHERE shop_domain = $1", shop
            )


async def main() -> None:
    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set")
        sys.exit(1)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    rows = await pool.fetch(
        "SELECT shop_domain FROM merchants WHERE active = TRUE ORDER BY installed_at"
    )
    logger.info("Found %d active merchants", len(rows))

    ok = 0
    already = 0
    failed = 0

    async with httpx.AsyncClient(timeout=20) as client:
        for row in rows:
            shop = row["shop_domain"]
            try:
                token = await _get_valid_token(pool, shop)
                if not token:
                    logger.warning("  %s: no token — skipped", shop)
                    failed += 1
                    continue

                resp = await client.post(
                    f"https://{shop}/admin/api/2024-10/graphql.json",
                    headers={
                        "X-Shopify-Access-Token": token,
                        "Content-Type": "application/json",
                    },
                    json={"query": _MUTATION, "variables": {"settings": "{}"}},
                )
                data = resp.json()
                errors = data.get("data", {}).get("webPixelCreate", {}).get("userErrors", [])
                if errors:
                    codes = [e.get("code") for e in errors]
                    if any(c in ("PIXEL_ALREADY_EXISTS", "WEB_PIXEL_ALREADY_EXISTS") for c in codes):
                        logger.info("  %s: already activated (OK)", shop)
                        already += 1
                    else:
                        logger.error("  %s: ERROR %s", shop, errors)
                        failed += 1
                else:
                    pixel_id = (
                        data.get("data", {})
                        .get("webPixelCreate", {})
                        .get("webPixel", {})
                        .get("id")
                    )
                    logger.info("  %s: activated (id=%s)", shop, pixel_id)
                    ok += 1
            except Exception as exc:
                logger.error("  %s: EXCEPTION %s", shop, exc)
                failed += 1

    await pool.close()
    logger.info("Done. ok=%d already=%d failed=%d", ok, already, failed)


if __name__ == "__main__":
    asyncio.run(main())
