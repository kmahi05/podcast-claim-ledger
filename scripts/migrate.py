"""Apply db/migrations/NNNN_name.sql in name order, one transaction each.

Run: cd api; uv run python ../scripts/migrate.py [--database-url postgresql://...]
"""

import argparse
import asyncio
import os
from pathlib import Path

import asyncpg

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
DEFAULT_URL = os.environ.get(
    "MIGRATE_DATABASE_URL", "postgresql://pcl_owner:pcl_owner@localhost:5432/podcast_ledger"
)


async def migrate(url: str) -> list[str]:
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    applied_now: list[str] = []
    try:
        await conn.execute(
            "create table if not exists schema_migrations "
            "(version text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["version"] for r in await conn.fetch("select version from schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.stem in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("insert into schema_migrations(version) values ($1)", path.stem)
            applied_now.append(path.stem)
    finally:
        await conn.close()
    return applied_now


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=DEFAULT_URL)
    args = parser.parse_args()
    applied = asyncio.run(migrate(args.database_url))
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))


if __name__ == "__main__":
    main()
