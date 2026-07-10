"""
Signed session cookie helpers for CheckoutGuard.

Cookie: cg_session = "{shop}:{expires_unix}:{sig32}"
Signed with SECRET_KEY using HMAC-SHA256.
CSRF tokens are derived stateless from the session value.
"""

import hashlib
import hmac
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
