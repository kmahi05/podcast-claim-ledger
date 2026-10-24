"""Test setup: a real PostgreSQL test database (podcast_ledger_test), recreated once per session.

DB tests are skipped (not failed) when PostgreSQL is unreachable.
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

TMP = Path(tempfile.mkdtemp(prefix="pcl-tests-"))
SUPERUSER_URL = os.environ.get(
    "PG_SUPERUSER_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)


def _to_test_db(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "_test"))


_DEV = {
    "MIGRATE_DATABASE_URL": "postgresql://pcl_owner:pcl_owner@localhost:5432/podcast_ledger",
    "APP_DATABASE_URL": "postgresql+asyncpg://pcl_app:pcl_app@localhost:5432/podcast_ledger",
    "WORKER_DATABASE_URL": (
        "postgresql+asyncpg://pcl_worker:pcl_worker@localhost:5432/podcast_ledger"
    ),
}
TEST_URLS = {k: _to_test_db(os.environ.get(k, v)) for k, v in _DEV.items()}

# settings are read from the environment, which wins over .env
os.environ.update(
    {
        **TEST_URLS,
        "JWT_SECRET": "test-jwt-secret-0123456789abcdef0123456789",
        "FILE_URL_SECRET": "test-file-secret-0123456789abcdef012345678",
        "CHROMA_PATH": str(TMP / "chroma"),
        "STORAGE_DIR": str(TMP / "storage"),
        "GOOGLE_API_KEY": "test-key-not-used",
        "WEB_ORIGIN": "http://localhost:3002",
        "COOKIE_SECURE": "false",
    }
)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


async def _setup_database() -> None:
    import asyncpg
    import db_setup
    import migrate

    conn = await asyncpg.connect(SUPERUSER_URL, timeout=5)
    try:
        await db_setup.ensure_roles(conn)
        name = urlsplit(TEST_URLS["MIGRATE_DATABASE_URL"]).path.lstrip("/")
        owner = urlsplit(TEST_URLS["MIGRATE_DATABASE_URL"]).username
        await conn.execute(f'drop database if exists "{name}" with (force)')
        await conn.execute(f'create database "{name}" owner "{owner}"')
    finally:
        await conn.close()
    await migrate.migrate(TEST_URLS["MIGRATE_DATABASE_URL"])


@pytest.fixture(scope="session")
def database() -> dict[str, str]:
    import asyncpg

    async def reachable() -> bool:
        try:
            conn = await asyncpg.connect(SUPERUSER_URL, timeout=5)
        except (OSError, asyncpg.PostgresError, TimeoutError):
            return False
        await conn.close()
        return True

    if not asyncio.run(reachable()):
        pytest.skip(f"PostgreSQL not reachable at {SUPERUSER_URL}")
    asyncio.run(_setup_database())
    return TEST_URLS


@pytest.fixture
async def client(database):
    import httpx

    from app.main import create_app
    from app.ratelimit import limiter

    limiter.reset()
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c


PASSWORD = "correct-horse-battery"


def new_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


async def signup(client, email: str | None = None) -> dict:
    """Register + log in; returns {"email", "user_id", "headers", "tokens"}."""
    email = email or new_email()
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Test User"},
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["user_id"]
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    tokens = r.json()
    client.cookies.clear()  # tests pass refresh tokens explicitly unless they test the cookie
    return {
        "email": email,
        "user_id": user_id,
        "tokens": tokens,
        "headers": {"Authorization": f"Bearer {tokens['access_token']}"},
    }
