import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.tokens import decode_access_token
from app.errors import AppError

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str


def _unauthorized(message: str) -> AppError:
    return AppError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise _unauthorized("Missing bearer token")
    try:
        claims = decode_access_token(creds.credentials)
        return CurrentUser(id=uuid.UUID(claims["sub"]), email=claims.get("email", ""))
    except (jwt.PyJWTError, ValueError):
        raise _unauthorized("Invalid or expired token") from None


AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
