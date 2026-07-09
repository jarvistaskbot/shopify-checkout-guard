"""
Shopify expiring offline token management.

Handles:
- One-time exchange of shpat_ non-expiring tokens → expiring tokens
- Proactive refresh before expiry (30-min buffer)
- Transparent get_valid_token() for all API callers
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import asyncpg
import httpx

logger = logging.getLogger(__name__)

_REFRESH_BUFFER_SECONDS = 1800  # refresh 30 min before expiry


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

    # Non-expiring tokens have no expires_at — just return as-is (API will reject).
    if expires_at is None:
        return access_token

    now = datetime.now(timezone.utc)
    if expires_at - now > timedelta(seconds=_REFRESH_BUFFER_SECONDS):
        return access_token

    if not refresh_tok:
        logger.warning("Token expiring for %s but no refresh token stored", shop)
        return access_token

    logger.info("Refreshing expiring token for %s", shop)
    token_data = await _call_token_endpoint(
        shop,
        {
            "client_id": api_key,
            "client_secret": api_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
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
