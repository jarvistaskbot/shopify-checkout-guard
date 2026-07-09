"""
One-time script: exchange the stored shpat_ non-expiring token for an expiring token,
then re-register webhooks to the VPS.

Run inside the app container on VPS:
  docker exec -it checkoutguard-app-1 python scripts/exchange_token.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import settings
from database import create_pool, get_pool
from services.token_manager import exchange_to_expiring
from routes.auth import _subscribe_webhooks


async def main() -> None:
    pool = await create_pool(settings.database_url)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT shop_domain, access_token, token_expires_at FROM merchants WHERE active = TRUE"
        )
        if not row:
            print("No active merchant found.")
            return

        shop = row["shop_domain"]
        old_token = row["access_token"]
        expires_at = row["token_expires_at"]

        if expires_at is not None:
            print(f"Token for {shop} is already expiring (expires {expires_at}). No exchange needed.")
            print("Re-registering webhooks with existing token...")
            await _subscribe_webhooks(shop, old_token)
            return

        print(f"Exchanging non-expiring token for {shop}...")
        print("WARNING: The old shpat_ token will be revoked. This is irreversible.")

        new_token, refresh_token, new_expires_at = await exchange_to_expiring(
            shop, old_token, settings.shopify_api_key, settings.shopify_api_secret
        )

        await conn.execute(
            """
            UPDATE merchants
            SET access_token = $1, refresh_token = $2, token_expires_at = $3
            WHERE shop_domain = $4
            """,
            new_token,
            refresh_token,
            new_expires_at,
            shop,
        )

        print(f"Token exchanged successfully.")
        print(f"  New token: {new_token[:20]}...")
        print(f"  Expires at: {new_expires_at}")
        print(f"  Refresh token stored: {'yes' if refresh_token else 'no'}")

        print("Registering webhooks...")
        await _subscribe_webhooks(shop, new_token)
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
