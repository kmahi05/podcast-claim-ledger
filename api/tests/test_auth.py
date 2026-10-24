import time

import jwt

from app.auth.tokens import create_access_token
from tests.conftest import PASSWORD, new_email, signup


async def test_health(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "chroma": "ok", "whisper_model": "small.en"}


async def test_register(client):
    email = new_email()
    body = {"email": email, "password": PASSWORD, "display_name": "A"}
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201
    assert r.json()["email"] == email

    r = await client.post("/api/v1/auth/register", json={**body, "email": email.upper()})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"

    r = await client.post(
        "/api/v1/auth/register", json={**body, "email": new_email(), "password": "short"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_login(client):
    user = await signup(client)
    assert user["tokens"]["token_type"] == "bearer"
    assert user["tokens"]["expires_in"] == 900

    wrong = await client.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": "wrong-password-x"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": new_email(), "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "invalid_credentials"


async def test_login_sets_refresh_cookie(client):
    user = await signup(client)
    r = await client.post("/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "refresh_token=" in cookie
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api/v1/auth" in cookie
    # refresh with only the cookie (what the SPA does on load)
    r = await client.post("/api/v1/auth/refresh")
    assert r.status_code == 200, r.text


async def test_access_token_required(client):
    assert (await client.get("/api/v1/me")).status_code == 401
    user = await signup(client)
    assert (await client.get("/api/v1/me", headers=user["headers"])).status_code == 200

    expired = create_access_token(user["user_id"], user["email"], now=int(time.time()) - 3600)
    r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"

    token = user["tokens"]["access_token"]
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {tampered}"})
    assert r.status_code == 401

    forged = jwt.encode(
        {"sub": user["user_id"], "exp": int(time.time()) + 60}, "x" * 40, algorithm="HS256"
    )
    r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


async def test_refresh_rotation(client):
    user = await signup(client)
    first = user["tokens"]["refresh_token"]
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": first})
    assert r.status_code == 200
    second = r.json()["refresh_token"]
    assert second != first
    r = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}
    )
    assert r.status_code == 200


async def test_refresh_reuse_revokes_all(client):
    user = await signup(client)
    first = user["tokens"]["refresh_token"]
    second = (await client.post("/api/v1/auth/refresh", json={"refresh_token": first})).json()[
        "refresh_token"
    ]
    client.cookies.clear()
    # replaying the rotated token is treated as theft
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": first})
    assert r.status_code == 401
    # ... so the legitimate newer token is revoked too
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": second})
    assert r.status_code == 401


async def test_logout(client):
    user = await signup(client)
    token = user["tokens"]["refresh_token"]
    r = await client.post("/api/v1/auth/logout", json={"refresh_token": token})
    assert r.status_code == 204
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
    assert r.status_code == 401


async def test_login_rate_limit(client):
    body = {"email": new_email(), "password": PASSWORD}
    codes = [(await client.post("/api/v1/auth/login", json=body)).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


async def test_unexpected_error_shape(client):
    async def boom():
        raise RuntimeError("secret internals")

    client.app.add_api_route("/api/v1/_boom", boom)
    r = await client.get("/api/v1/_boom")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal_error", "message": "Internal server error"}}
