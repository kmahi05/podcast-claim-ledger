"""Create the pcl_* roles and the podcast_ledger (+ _test) databases. Idempotent.

Role names and passwords come from MIGRATE/APP/WORKER_DATABASE_URL (dev defaults below),
so there is one source of truth. Run: cd api; uv run python ../scripts/db_setup.py
"""

import asyncio
import os
from urllib.parse import urlsplit

import asyncpg

SUPERUSER_URL = os.environ.get(
    "PG_SUPERUSER_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
URLS = {
    "owner": os.environ.get(
        "MIGRATE_DATABASE_URL", "postgresql://pcl_owner:pcl_owner@localhost:5432/podcast_ledger"
    ),
    "app": os.environ.get(
        "APP_DATABASE_URL", "postgresql+asyncpg://pcl_app:pcl_app@localhost:5432/podcast_ledger"
    ),
    "worker": os.environ.get(
        "WORKER_DATABASE_URL",
        "postgresql+asyncpg://pcl_worker:pcl_worker@localhost:5432/podcast_ledger",
    ),
}
ATTRS = {"owner": "NOBYPASSRLS", "app": "NOBYPASSRLS", "worker": "BYPASSRLS"}


def role_of(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.username or "", parts.password or ""


def db_name() -> str:
    return urlsplit(URLS["owner"]).path.lstrip("/")


async def ensure_roles(conn: asyncpg.Connection) -> str:
    """Create or update the three roles. Returns the owner role name."""
    for kind, url in URLS.items():
        name, password = role_of(url)
        exists = await conn.fetchval("select 1 from pg_roles where rolname = $1", name)
        verb = "alter" if exists else "create"
        sql = await conn.fetchval(
            f"select format('{verb} role %I login password %L {ATTRS[kind]}', $1::text, $2::text)",
            name,
            password,
        )
        await conn.execute(sql)
    return role_of(URLS["owner"])[0]


async def ensure_database(conn: asyncpg.Connection, name: str, owner: str) -> None:
    if not await conn.fetchval("select 1 from pg_database where datname = $1", name):
        sql = await conn.fetchval("select format('create database %I', $1::text)", name)
        await conn.execute(sql)
    sql = await conn.fetchval(
        "select format('alter database %I owner to %I', $1::text, $2::text)", name, owner
    )
    await conn.execute(sql)


async def main() -> None:
    conn = await asyncpg.connect(SUPERUSER_URL)
    try:
        owner = await ensure_roles(conn)
        for name in (db_name(), db_name() + "_test"):
            await ensure_database(conn, name, owner)
            print(f"database {name} ready (owner {owner})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
