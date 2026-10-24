import asyncpg

from tests.conftest import PASSWORD, new_email, signup


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _create_ws(client, user, name="Alpha") -> str:
    r = await client.post("/api/v1/workspaces", json={"name": name}, headers=user["headers"])
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "owner"
    return r.json()["workspace_id"]


async def test_create_and_list_mine(client):
    user = await signup(client)
    ws = await _create_ws(client, user)
    r = await client.get("/api/v1/workspaces", headers=user["headers"])
    assert [(w["workspace_id"], w["role"]) for w in r.json()["items"]] == [(ws, "owner")]
    me = (await client.get("/api/v1/me", headers=user["headers"])).json()
    assert me["email"] == user["email"]
    assert me["workspaces"] == [{"workspace_id": ws, "name": "Alpha", "role": "owner"}]


async def test_other_user_cannot_see_workspace(client):
    a = await signup(client)
    b = await signup(client)
    ws = await _create_ws(client, a)
    r = await client.get("/api/v1/workspaces", headers=b["headers"])
    assert r.json()["items"] == []
    r = await client.get(f"/api/v1/workspaces/{ws}/members", headers=b["headers"])
    assert r.status_code == 404
    r = await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": b["email"], "role": "editor"},
        headers=b["headers"],
    )
    assert r.status_code == 404


async def test_rls_on_app_role_and_worker_sees_all(client, database):
    a = await signup(client)
    b = await signup(client)
    ws_a = await _create_ws(client, a, "A")
    ws_b = await _create_ws(client, b, "B")

    worker = await asyncpg.connect(_plain(database["WORKER_DATABASE_URL"]))
    try:
        for ws, title in ((ws_a, "Show A"), (ws_b, "Show B")):
            await worker.execute(
                "insert into shows (workspace_id, title) values ($1, $2)", ws, title
            )
        shows = await worker.fetch(
            "select workspace_id::text from shows where workspace_id = any($1::uuid[])",
            [ws_a, ws_b],
        )
        assert {r[0] for r in shows} == {ws_a, ws_b}
    finally:
        await worker.close()

    app = await asyncpg.connect(_plain(database["APP_DATABASE_URL"]))
    try:
        async with app.transaction():
            await app.execute("select set_config('app.user_id', $1, true)", b["user_id"])
            for table, col in (
                ("workspaces", "id"),
                ("workspace_members", "workspace_id"),
                ("shows", "workspace_id"),
            ):
                rows = await app.fetch(f"select {col}::text from {table}")
                assert {r[0] for r in rows} == {ws_b}, table
            # app role cannot write into another workspace either
            status = await app.execute(
                f"update shows set title = 'x' where workspace_id = '{ws_a}'"
            )
            assert status == "UPDATE 0"
            users = await app.fetch("select id::text from users")
            assert [r[0] for r in users] == [b["user_id"]]
        # without app.user_id nothing is visible
        assert await app.fetchval("select count(*) from workspaces") == 0
    finally:
        await app.close()


async def test_create_workspace_function_needs_user(database):
    conn = await asyncpg.connect(_plain(database["APP_DATABASE_URL"]))
    try:
        try:
            await conn.fetchval("select public.create_workspace('x')")
        except asyncpg.InsufficientPrivilegeError:
            pass
        else:
            raise AssertionError("create_workspace must fail without app.user_id")
        # the app role cannot insert workspaces directly
        try:
            await conn.execute("insert into workspaces (name) values ('x')")
        except asyncpg.InsufficientPrivilegeError:
            pass
        else:
            raise AssertionError("direct insert must be denied")
    finally:
        await conn.close()


async def test_invite_existing_user(client):
    owner = await signup(client)
    other = await signup(client)
    ws = await _create_ws(client, owner)
    r = await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": other["email"], "role": "editor"},
        headers=owner["headers"],
    )
    assert r.status_code == 201
    assert r.json() == {"user_id": other["user_id"], "role": "editor"}
    members = (
        await client.get(f"/api/v1/workspaces/{ws}/members", headers=owner["headers"])
    ).json()
    assert {m["email"]: m["role"] for m in members["items"]} == {
        owner["email"]: "owner",
        other["email"]: "editor",
    }
    # the editor can see it but cannot add members
    r = await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": new_email(), "role": "viewer"},
        headers=other["headers"],
    )
    assert r.status_code == 403
    r = await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": other["email"], "role": "viewer"},
        headers=owner["headers"],
    )
    assert r.status_code == 409


async def test_invite_new_email_and_accept(client):
    owner = await signup(client)
    ws = await _create_ws(client, owner)
    email = new_email()
    r = await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": email, "role": "viewer"},
        headers=owner["headers"],
    )
    assert r.status_code == 201
    token = r.json()["invite_token"]

    r = await client.post(
        "/api/v1/auth/accept-invite",
        json={"token": token, "password": PASSWORD, "display_name": "New Person"},
    )
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await client.get("/api/v1/me", headers=headers)).json()
    assert me["email"] == email
    assert me["workspaces"] == [{"workspace_id": ws, "name": "Alpha", "role": "viewer"}]

    # single use
    r = await client.post(
        "/api/v1/auth/accept-invite",
        json={"token": token, "password": PASSWORD, "display_name": "Again"},
    )
    assert r.status_code == 400
    # and the new account can log in normally
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
