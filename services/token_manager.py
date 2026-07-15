"""
Shopify expiring offline token management.

Handles:
- One-time exchange of shpat_ non-expiring tokens → expiring tokens
- Proactive refresh before expiry (30-min buffer)
- Transparent get_valid_token() for all API callers

Race condition prevention: per-shop asyncio.Lock ensures only one refresh
runs at a time, avoiding double-refresh (second call would get invalid refresh token).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import asyncpg
import httpx

logger = logging.getLogger(__name__)

_REFRESH_BUFFER_SECONDS = 1800  # refresh 30 min before expiry

# Per-shop lock prevents concurrent refresh calls from both exchanging the refresh token.
_refresh_locks: dict[str, asyncio.Lock] = {}


def _get_lock(shop: str) -> asyncio.Lock:
    if shop not in _refresh_locks:
        _refresh_locks[shop] = asyncio.Lock()
    return _refresh_locks[shop]


async def get_valid_token(
    conn: asyncpg.Connection,
    shop: str,
    api_key: str,
    api_secret: str,
) -> str:
    """Return a live access token, refreshing if near expiry."""
    row = await conn.fetchrow(
        "SELECT access_token, refresh_token, token_expires_at FROM merchants WHERE shop_domain = $1",
        shop,
    )
    if not row:
        raise RuntimeError(f"No merchant found: {shop}")

    access_token = row["access_token"]
    expires_at = row["token_expires_at"]
    refresh_tok = row["refresh_token"]

    if expires_at is None:
        # Legacy non-expiring token — Shopify's Admin API rejects these.
        # Exchange it for an expiring token on first use (irreversible).
        if access_token.startswith("shpat_"):
            async with _get_lock(shop):
                row2 = await conn.fetchrow(
                    "SELECT access_token, token_expires_at FROM merchants WHERE shop_domain = $1",
                    shop,
                )
                if row2 and row2["token_expires_at"] is not None:
                    return row2["access_token"]
                try:
                    logger.info("Exchanging permanent token for expiring token: %s", shop)
                    new_token, new_refresh, new_expires_at = await exchange_to_expiring(
                        shop, access_token, api_key, api_secret
                    )
                    await conn.execute(
                        """
                        UPDATE merchants
                        SET access_token = $1, refresh_token = $2, token_expires_at = $3
                        WHERE shop_domain = $4
                        """,
                        new_token,
                        new_refresh,
                        new_expires_at,
                        shop,
                    )
                    logger.info("Token exchange complete for %s, expires %s", shop, new_expires_at)
                    return new_token
                except Exception as exc:
                    logger.error("Token exchange failed for %s: %s", shop, exc)
                    return access_token
        return access_token

    now = datetime.now(timezone.utc)
    if expires_at - now > timedelta(seconds=_REFRESH_BUFFER_SECONDS):
        return access_token

    if not refresh_tok:
        logger.warning("Token expiring for %s but no refresh token stored", shop)
        return access_token

    # Acquire per-shop lock to prevent concurrent refresh (Shopify invalidates
    # the refresh token after first use; second concurrent call would fail).
    async with _get_lock(shop):
        # Re-read after acquiring lock — another task may have already refreshed.
        row2 = await conn.fetchrow(
            "SELECT access_token, refresh_token, token_expires_at FROM merchants WHERE shop_domain = $1",
            shop,
        )
        if row2 and row2["token_expires_at"] and \
                row2["token_expires_at"] - datetime.now(timezone.utc) > timedelta(seconds=_REFRESH_BUFFER_SECONDS):
            return row2["access_token"]

        logger.info("Refreshing expiring token for %s", shop)
        token_data = await _call_token_endpoint(
            shop,
            {
                "client_id": api_key,
                "client_secret": api_secret,
                "grant_type": "refresh_token",
                "refresh_token": row2["refresh_token"] if row2 else refresh_tok,
            },
        )

        new_token, new_refresh, new_expires_at = _parse_token_response(token_data)
        await conn.execute(
            """
            UPDATE merchants
            SET access_token = $1, refresh_token = $2, token_expires_at = $3
            WHERE shop_domain = $4
            """,
            new_token,
            new_refresh,
            new_expires_at,
            shop,
        )
        return new_token


async def exchange_to_expiring(
    shop: str,
    old_token: str,
    api_key: str,
    api_secret: str,
) -> tuple[str, str, datetime]:
    """
    Convert a non-expiring shpat_ token to an expiring token.
    Returns (access_token, refresh_token, expires_at).
    WARNING: original token is revoked — this is irreversible.
    """
    token_data = await _call_token_endpoint(
        shop,
        {
            "client_id": api_key,
            "client_secret": api_secret,
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": old_token,
            "subject_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
            "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
            "expiring": 1,
        },
    )
    return _parse_token_response(token_data)


async def _call_token_endpoint(shop: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={k: str(v) for k, v in payload.items()},
        )
        resp.raise_for_status()
        return resp.json()


def _parse_token_response(data: dict) -> tuple[str, str, datetime]:
    access_token = data["access_token"]
    refresh_token = data.get("refresh_token", "")
    expires_in = int(data.get("expires_in", 86400))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return access_token, refresh_token, expires_at
