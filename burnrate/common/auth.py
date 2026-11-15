"""Session tokens for the browser (signed JWT in an HttpOnly cookie) and one-time links."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets

import jwt

from burnrate.common.config import settings
from burnrate.common.errors import Unauthorized

COOKIE = "br_session"
SESSION_DAYS = 30


def make_session_token(user_id: str, *, demo: bool = False, sid: str | None = None, off: int = 0, tz: str = "") -> str:
    """`off` is the demo clock offset in seconds; it lives in the cookie so no lookup is needed per request."""
    now = dt.datetime.now(dt.UTC)
    claims = {"sub": user_id, "demo": demo, "off": int(off), "tz": tz, "sid": sid or secrets.token_hex(8),
              "iat": int(now.timestamp()), "exp": int((now + dt.timedelta(days=SESSION_DAYS)).timestamp())}
    return jwt.encode(claims, settings().jwt_secret, algorithm="HS256")


def read_session_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise Unauthorized("session is not valid", code="bad_session") from e


def cookie_params() -> dict:
    return {"httponly": True, "secure": settings().cookie_secure, "samesite": "lax",
            "max_age": SESSION_DAYS * 86400, "path": "/"}


def new_link_token() -> tuple[str, str]:
    """Returns (token for the email, hash to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hmac.new(settings().jwt_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def signed(value: str) -> str:
    """Short signature for unguessable public links (calendar feed, unsubscribe)."""
    return hmac.new(settings().jwt_secret.encode(), value.encode(), hashlib.sha256).hexdigest()[:24]
