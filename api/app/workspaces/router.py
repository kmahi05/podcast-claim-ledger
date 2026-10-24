import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import BaseModel, StringConstraints
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.deps import AuthUser
from app.auth.router import Email
from app.auth.tokens import hash_token
from app.db import user_session, worker_session
from app.errors import AppError

router = APIRouter(tags=["workspaces"])

INVITE_TTL = timedelta(days=7)


class WorkspaceIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class MemberIn(BaseModel):
    email: Email
    role: Literal["owner", "editor", "viewer"] = "viewer"


async def _role_or_404(conn: AsyncConnection, workspace_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Worker-connection membership check, filtered by workspace explicitly."""
    role = (
        await conn.execute(
            text(
                "select role from public.workspace_members "
                "where workspace_id = :ws and user_id = :uid"
            ),
            {"ws": workspace_id, "uid": user_id},
        )
    ).scalar_one_or_none()
    if role is None:
        raise AppError(404, "not_found", "Workspace not found")
    return role


@router.get("/me")
async def me(user: AuthUser) -> dict:
    async with user_session(user.id) as conn:
        row = (
            (
                await conn.execute(
                    text("select id, email, display_name from public.users where id = :id"),
                    {"id": user.id},
                )
            )
            .mappings()
            .first()
        )
        memberships = (
            (
                await conn.execute(
                    text(
                        "select w.id as workspace_id, w.name, m.role "
                        "from public.workspace_members m "
                        "join public.workspaces w on w.id = m.workspace_id "
                        "where m.user_id = :id order by w.name"
                    ),
                    {"id": user.id},
                )
            )
            .mappings()
            .all()
        )
    if row is None:
        raise AppError(401, "unauthorized", "Account not found")
    return {
        "user_id": str(row["id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "workspaces": [{**m, "workspace_id": str(m["workspace_id"])} for m in memberships],
    }


@router.post("/workspaces", status_code=201)
async def create_workspace(user: AuthUser, body: WorkspaceIn) -> dict:
    async with user_session(user.id) as conn:
        ws_id = (
            await conn.execute(text("select public.create_workspace(:name)"), {"name": body.name})
        ).scalar_one()
    return {"workspace_id": str(ws_id), "name": body.name, "role": "owner"}


@router.get("/workspaces")
async def list_workspaces(user: AuthUser) -> dict:
    # RLS limits workspaces to the caller's memberships; no explicit filter needed
    async with user_session(user.id) as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "select id, name, public.ws_role(id) as role, created_at "
                        "from public.workspaces order by created_at"
                    )
                )
            )
            .mappings()
            .all()
        )
    return {
        "items": [
            {
                "workspace_id": str(r["id"]),
                "name": r["name"],
                "role": r["role"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/workspaces/{workspace_id}/members")
async def list_members(user: AuthUser, workspace_id: uuid.UUID) -> dict:
    # worker role: the app role may only read its own users row, and we need members' emails
    async with worker_session() as conn:
        await _role_or_404(conn, workspace_id, user.id)
        rows = (
            (
                await conn.execute(
                    text(
                        "select u.id, u.email, u.display_name, m.role "
                        "from public.workspace_members m join public.users u on u.id = m.user_id "
                        "where m.workspace_id = :ws order by u.email"
                    ),
                    {"ws": workspace_id},
                )
            )
            .mappings()
            .all()
        )
    return {
        "items": [
            {
                "user_id": str(r["id"]),
                "email": r["email"],
                "display_name": r["display_name"],
                "role": r["role"],
            }
            for r in rows
        ]
    }


@router.post("/workspaces/{workspace_id}/members", status_code=201)
async def add_member(user: AuthUser, workspace_id: uuid.UUID, body: MemberIn) -> dict:
    async with worker_session() as conn:
        if await _role_or_404(conn, workspace_id, user.id) != "owner":
            raise AppError(403, "forbidden", "Only owners can add members")
        existing = (
            await conn.execute(
                text("select id from public.users where lower(email) = :e"), {"e": body.email}
            )
        ).scalar_one_or_none()
        if existing is not None:
            added = (
                await conn.execute(
                    text(
                        "insert into public.workspace_members (workspace_id, user_id, role) "
                        "values (:ws, :uid, :role) on conflict do nothing returning user_id"
                    ),
                    {"ws": workspace_id, "uid": existing, "role": body.role},
                )
            ).scalar_one_or_none()
            if added is None:
                raise AppError(409, "already_member", "This user is already a member")
            return {"user_id": str(existing), "role": body.role}
        raw = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + INVITE_TTL
        await conn.execute(
            text(
                "insert into public.invites "
                "(workspace_id, email, role, token_hash, invited_by, expires_at) "
                "values (:ws, :e, :role, :h, :by, :exp)"
            ),
            {
                "ws": workspace_id,
                "e": body.email,
                "role": body.role,
                "h": hash_token(raw),
                "by": user.id,
                "exp": expires_at,
            },
        )
    return {"invite_token": raw, "expires_at": expires_at.isoformat(), "role": body.role}
