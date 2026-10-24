import hashlib
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import JWT_AUDIENCE, JWT_ISSUER, get_settings


def create_access_token(user_id: uuid.UUID | str, email: str, now: int | None = None) -> str:
    s = get_settings()
    iat = int(now if now is not None else time.time())
    claims = {
        "sub": str(user_id),
        "email": email,
        "iat": iat,
        "nbf": iat,
        "exp": iat + s.ACCESS_TOKEN_TTL_SECONDS,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "typ": "access",
    }
    return jwt.encode(claims, s.JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Raises jwt.PyJWTError on any problem (bad signature, expired, wrong iss/aud/typ)."""
    claims = jwt.decode(
        token,
        get_settings().JWT_SECRET,
        algorithms=["HS256"],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
        options={"require": ["sub", "exp", "iat", "nbf", "iss", "aud"]},
    )
    if claims.get("typ") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return claims


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue_pair(
    conn: AsyncConnection, user_id: uuid.UUID, email: str
) -> tuple[dict, uuid.UUID]:
    """Insert a refresh token row (worker connection); return (token pair body, its row id)."""
    s = get_settings()
    raw = secrets.token_urlsafe(32)
    token_id = uuid.uuid4()
    await conn.execute(
        text(
            "insert into public.refresh_tokens (id, user_id, token_hash, expires_at) "
            "values (:id, :uid, :h, :exp)"
        ),
        {
            "id": token_id,
            "uid": user_id,
            "h": hash_token(raw),
            "exp": datetime.now(UTC) + timedelta(seconds=s.REFRESH_TOKEN_TTL_SECONDS),
        },
    )
    body = {
        "access_token": create_access_token(user_id, email),
        "token_type": "bearer",
        "expires_in": s.ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token": raw,
    }
    return body, token_id
