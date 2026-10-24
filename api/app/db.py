"""Two engines: pcl_app (RLS applies) for user requests, pcl_worker (BYPASSRLS) for the rest.

Every query on worker_session() must filter by workspace_id / episode_id explicitly.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

_engines: dict[str, AsyncEngine] = {}


def init_engines(app_url: str, worker_url: str) -> None:
    _engines["app"] = create_async_engine(app_url, pool_pre_ping=True)
    _engines["worker"] = create_async_engine(worker_url, pool_pre_ping=True)


async def dispose_engines() -> None:
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()


@asynccontextmanager
async def user_session(user_id: UUID | str) -> AsyncIterator[AsyncConnection]:
    """Transaction on the app role with app.user_id set (transaction-local) for RLS."""
    async with _engines["app"].begin() as conn:
        await conn.execute(
            text("select set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        yield conn


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncConnection]:
    async with _engines["worker"].begin() as conn:
        yield conn
