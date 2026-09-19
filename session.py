"""
Signed session cookie helpers for CheckoutGuard.

Cookie: cg_session = "{shop}:{expires_unix}:{sig32}"
Signed with SECRET_KEY using HMAC-SHA256.
CSRF tokens are derived stateless from the session value.

Also provides verify_shopify_session_token() for App Bridge embedded mode.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

COOKIE_NAME = "cg_session"
_COOKIE_TTL = 86400 * 30  # 30 days


def create_session_token(shop: str, secret: str) -> str:
    expires = int(time.time()) + _COOKIE_TTL
    payload = f"{shop}:{expires}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def verify_session_token(token: str, secret: str) -> Optional[str]:
    """Return shop domain if token is valid and unexpired, else None."""
    try:
        # Split from right to handle shop domains that might theoretically contain colons
        sig = token[-32:]
        rest = token[: -(32 + 1)]  # strip :sig
        shop, expires_str = rest.rsplit(":", 1)
        if int(time.time()) > int(expires_str):
            return None
        payload = f"{shop}:{expires_str}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(expected, sig):
            return shop
    except Exception:
        pass
    return None


def csrf_token_for(session_value: str, secret: str) -> str:
    """Stateless CSRF token derived from the session cookie value."""
    return hmac.new(secret.encode(), session_value.encode(), hashlib.sha256).hexdigest()[:16]


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def verify_shopify_session_token(token: str, api_secret: str, client_id: str) -> Optional[str]:
    """Verify a Shopify App Bridge JWT session token (HS256).

    Returns the shop domain on success, None on any validation failure.
    The token is signed by Shopify with api_secret and has:
      - aud = client_id (app's API key)
      - dest = "https://{shop}" (source of the shop domain)
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts

        # Verify HS256 signature.
        signing_input = f"{header_b64}.{payload_b64}".encode()
        expected_sig = hmac.new(api_secret.encode(), signing_input, hashlib.sha256).digest()
        sig_bytes = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, sig_bytes):
            return None

        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "HS256":
            return None

        payload = json.loads(_b64url_decode(payload_b64))

        now = int(time.time())
        if payload.get("exp", 0) < now:
            return None
        if payload.get("nbf", 0) > now + 10:  # 10 s clock skew allowance
            return None
        if payload.get("aud") != client_id:
            return None

        dest = payload.get("dest", "")
        if dest.startswith("https://"):
            return dest[len("https://"):]
    except Exception:
        pass
    return None
