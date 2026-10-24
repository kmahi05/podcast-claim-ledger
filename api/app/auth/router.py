"""Auth routes. They run before a user exists, so they use the worker role (BYPASSRLS)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.tokens import hash_token, issue_pair
from app.config import get_settings
from app.db import worker_session
from app.errors import AppError
from app.ratelimit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = "refresh_token"
COOKIE_PATH = "/api/v1/auth"

Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    ),
]
Password = Annotated[str, Field(min_length=10, max_length=128)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class RegisterIn(BaseModel):
    email: Email
    password: Password
    display_name: DisplayName


class LoginIn(BaseModel):
    email: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=254)]
    password: Annotated[str, Field(max_length=128)]


class RefreshIn(BaseModel):
    refresh_token: str | None = None


class AcceptInviteIn(BaseModel):
    token: Annotated[str, Field(min_length=10, max_length=200)]
    password: Password
    display_name: DisplayName


def _invalid_credentials() -> AppError:
    return AppError(401, "invalid_credentials", "Invalid email or password")


def _set_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        COOKIE,
        raw,
        max_age=get_settings().REFRESH_TOKEN_TTL_SECONDS,
        path=COOKIE_PATH,
        httponly=True,
        secure=get_settings().COOKIE_SECURE,
        samesite="strict",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite="strict")


def _raw_token(request: Request, body: RefreshIn | None) -> str | None:
    return (body.refresh_token if body else None) or request.cookies.get(COOKIE)


@router.post("/register", status_code=201)
async def register(body: RegisterIn) -> dict:
    try:
        async with worker_session() as conn:
            user_id = (
                await conn.execute(
                    text(
                        "insert into public.users (email, password_hash, display_name) "
                        "values (:e, :p, :d) returning id"
                    ),
                    {"e": body.email, "p": hash_password(body.password), "d": body.display_name},
                )
            ).scalar_one()
    except IntegrityError:
        raise AppError(409, "email_taken", "An account with this email already exists") from None
    return {"user_id": str(user_id), "email": body.email}


@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, response: Response, body: LoginIn) -> dict:
    async with worker_session() as conn:
        user = (
            (
                await conn.execute(
                    text(
                        "select id, email, password_hash, is_active from public.users "
                        "where lower(email) = :e"
                    ),
                    {"e": body.email},
                )
            )
            .mappings()
            .first()
        )
        ok = verify_password(user["password_hash"] if user else None, body.password)
        if not user or not ok or not user["is_active"]:
            raise _invalid_credentials()
        if needs_rehash(user["password_hash"]):
            await conn.execute(
                text("update public.users set password_hash = :p where id = :id"),
                {"p": hash_password(body.password), "id": user["id"]},
            )
        pair, _ = await issue_pair(conn, user["id"], user["email"])
    _set_cookie(response, pair["refresh_token"])
    return pair


@router.post("/refresh")
async def refresh(request: Request, response: Response, body: RefreshIn | None = None) -> dict:
    raw = _raw_token(request, body)
    if not raw:
        raise AppError(401, "unauthorized", "Missing refresh token")
    pair = None
    async with worker_session() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "select t.id, t.user_id, t.expires_at, t.revoked_at, u.email, u.is_active "
                        "from public.refresh_tokens t join public.users u on u.id = t.user_id "
                        "where t.token_hash = :h for update of t"
                    ),
                    {"h": hash_token(raw)},
                )
            )
            .mappings()
            .first()
        )
        if row and row["revoked_at"] is not None:
            # reuse of a rotated token: assume theft, revoke every session of this user
            await conn.execute(
                text(
                    "update public.refresh_tokens set revoked_at = now() "
                    "where user_id = :uid and revoked_at is null"
                ),
                {"uid": row["user_id"]},
            )
        elif row and row["expires_at"] > datetime.now(UTC) and row["is_active"]:
            pair, new_id = await issue_pair(conn, row["user_id"], row["email"])
            await conn.execute(
                text(
                    "update public.refresh_tokens set revoked_at = now(), replaced_by = :new "
                    "where id = :id"
                ),
                {"new": new_id, "id": row["id"]},
            )
    if pair is None:
        _clear_cookie(response)
        raise AppError(401, "unauthorized", "Invalid refresh token")
    _set_cookie(response, pair["refresh_token"])
    return pair


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, body: RefreshIn | None = None) -> Response:
    raw = _raw_token(request, body)
    if raw:
        async with worker_session() as conn:
            await conn.execute(
                text(
                    "update public.refresh_tokens set revoked_at = now() "
                    "where token_hash = :h and revoked_at is null"
                ),
                {"h": hash_token(raw)},
            )
    response.status_code = 204
    _clear_cookie(response)
    return response


@router.post("/accept-invite")
async def accept_invite(response: Response, body: AcceptInviteIn) -> dict:
    async with worker_session() as conn:
        invite = (
            (
                await conn.execute(
                    text(
                        "select id, workspace_id, email, role from public.invites "
                        "where token_hash = :h and accepted_at is null and expires_at > now() "
                        "for update"
                    ),
                    {"h": hash_token(body.token)},
                )
            )
            .mappings()
            .first()
        )
        if not invite:
            raise AppError(400, "invalid_invite", "This invite is invalid or has expired")
        user = (
            (
                await conn.execute(
                    text(
                        "select id, email, password_hash from public.users "
                        "where lower(email) = lower(:e)"
                    ),
                    {"e": invite["email"]},
                )
            )
            .mappings()
            .first()
        )
        if user:
            # an existing account must prove it owns the password
            if not verify_password(user["password_hash"], body.password):
                raise _invalid_credentials()
            user_id: uuid.UUID = user["id"]
        else:
            user_id = (
                await conn.execute(
                    text(
                        "insert into public.users (email, password_hash, display_name) "
                        "values (:e, :p, :d) returning id"
                    ),
                    {
                        "e": invite["email"],
                        "p": hash_password(body.password),
                        "d": body.display_name,
                    },
                )
            ).scalar_one()
        await conn.execute(
            text(
                "insert into public.workspace_members (workspace_id, user_id, role) "
                "values (:ws, :uid, :role) on conflict (workspace_id, user_id) do nothing"
            ),
            {"ws": invite["workspace_id"], "uid": user_id, "role": invite["role"]},
        )
        await conn.execute(
            text("update public.invites set accepted_at = now() where id = :id"),
            {"id": invite["id"]},
        )
        pair, _ = await issue_pair(conn, user_id, invite["email"])
    _set_cookie(response, pair["refresh_token"])
    return pair
