"""RLS on every tenant table, using raw connections on the app and worker roles."""

import uuid

import asyncpg
import pytest

TENANT_TABLES = (
    "workspaces",
    "workspace_members",
    "shows",
    "episodes",
    "transcript_segments",
    "claims",
    "claim_links",
    "preserved_claim_links",
    "pipeline_jobs",
)


JOB_SQL = "insert into pipeline_jobs (workspace_id, episode_id, stage) values ($1, $2, '{stage}')"


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _seed_workspace(worker: asyncpg.Connection) -> dict[str, uuid.UUID]:
    """One workspace with an owner, an editor, a viewer and one row in every content table."""
    ids = {k: uuid.uuid4() for k in ("ws", "owner", "editor", "viewer", "show", "ep", "c1", "c2")}
    for role in ("owner", "editor", "viewer"):
        await worker.execute(
            "insert into users (id, email, password_hash, display_name) values ($1, $2, 'x', $3)",
            ids[role],
            f"{role}-{ids[role].hex[:8]}@example.com",
            role,
        )
    await worker.execute("insert into workspaces (id, name) values ($1, 'W')", ids["ws"])
    for role in ("owner", "editor", "viewer"):
        await worker.execute(
            "insert into workspace_members values ($1, $2, $3)", ids["ws"], ids[role], role
        )
    await worker.execute(
        "insert into shows (id, workspace_id, title) values ($1, $2, 'S')", ids["show"], ids["ws"]
    )
    await worker.execute(
        "insert into episodes (id, workspace_id, show_id, title) values ($1, $2, $3, 'E')",
        ids["ep"],
        ids["ws"],
        ids["show"],
    )
    await worker.execute(
        "insert into transcript_segments (workspace_id, episode_id, seq, start_ms, end_ms, text) "
        "values ($1, $2, 0, 0, 1000, 'the bridge opened in 1932')",
        ids["ws"],
        ids["ep"],
    )
    for cid in ("c1", "c2"):
        await worker.execute(
            "insert into claims (id, workspace_id, episode_id, window_id, claim_type, normalized, "
            "verbatim, subject, start_ms, end_ms) "
            "values ($1, $2, $3, 'w', 'event', 'n', 'v', 'bridge', 0, 1000)",
            ids[cid],
            ids["ws"],
            ids["ep"],
        )
    await worker.execute(
        "insert into claim_links "
        "(workspace_id, claim_id, other_id, relation, confidence, rationale) "
        "values ($1, $2, $3, 'contradicts', 0.9, 'r')",
        ids["ws"],
        ids["c1"],
        ids["c2"],
    )
    await worker.execute(
        "insert into preserved_claim_links (workspace_id, episode_id, verbatim, start_ms, "
        "counterpart_id, side, relation, confidence, rationale, status) "
        "values ($1, $2, 'v', 0, $3, 'claim', 'contradicts', 0.9, 'r', 'confirmed')",
        ids["ws"],
        ids["ep"],
        ids["c2"],
    )
    await worker.execute(
        "insert into pipeline_jobs (workspace_id, episode_id, stage) values ($1, $2, 'transcribe')",
        ids["ws"],
        ids["ep"],
    )
    return ids


async def _as_user(app: asyncpg.Connection, user_id: uuid.UUID) -> None:
    await app.execute("select set_config('app.user_id', $1, true)", str(user_id))


async def test_every_tenant_table_is_isolated(database):
    worker = await asyncpg.connect(_plain(database["WORKER_DATABASE_URL"]))
    app = await asyncpg.connect(_plain(database["APP_DATABASE_URL"]))
    try:
        a = await _seed_workspace(worker)
        b = await _seed_workspace(worker)
        for table in TENANT_TABLES:
            col = "id" if table == "workspaces" else "workspace_id"
            seen_by_worker = await worker.fetchval(
                f"select count(distinct {col}) from {table} where {col} = any($1::uuid[])",
                [a["ws"], b["ws"]],
            )
            assert seen_by_worker == 2, table
            async with app.transaction():
                await _as_user(app, b["viewer"])
                rows = await app.fetch(f"select distinct {col} as ws from {table}")
                assert {r["ws"] for r in rows} == {b["ws"]}, table
    finally:
        await app.close()
        await worker.close()


async def test_roles_on_app_connection(database):
    worker = await asyncpg.connect(_plain(database["WORKER_DATABASE_URL"]))
    app = await asyncpg.connect(_plain(database["APP_DATABASE_URL"]))
    try:
        ids = await _seed_workspace(worker)
        update = "update claims set verification = 'sourced' where id = $1"
        async with app.transaction():
            await _as_user(app, ids["viewer"])
            assert await app.execute(update, ids["c1"]) == "UPDATE 0"
            assert await app.execute(
                "update claim_links set status = 'confirmed' where claim_id = $1", ids["c1"]
            ) == ("UPDATE 0")
        async with app.transaction():
            await _as_user(app, ids["editor"])
            assert await app.execute(update, ids["c1"]) == "UPDATE 1"
        new_user = uuid.uuid4()
        await worker.execute(
            "insert into users (id, email, password_hash, display_name) values ($1, $2, 'x', 'n')",
            new_user,
            f"n-{new_user.hex[:8]}@example.com",
        )
        add_member = "insert into workspace_members values ($1, $2, 'viewer')"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with app.transaction():
                await _as_user(app, ids["editor"])
                await app.execute(add_member, ids["ws"], new_user)
        async with app.transaction():
            await _as_user(app, ids["owner"])
            await app.execute(add_member, ids["ws"], new_user)
    finally:
        await app.close()
        await worker.close()


async def test_one_active_job_per_episode(database):
    worker = await asyncpg.connect(_plain(database["WORKER_DATABASE_URL"]))
    try:
        ids = await _seed_workspace(worker)  # already has one queued job
        with pytest.raises(asyncpg.UniqueViolationError):
            await worker.execute(
                JOB_SQL.format(stage="index"),
                ids["ws"],
                ids["ep"],
            )
        await worker.execute(
            "update pipeline_jobs set status = 'succeeded' where episode_id = $1", ids["ep"]
        )
        await worker.execute(
            JOB_SQL.format(stage="diarize"),
            ids["ws"],
            ids["ep"],
        )
    finally:
        await worker.close()
