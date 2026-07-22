"""Login / logout / session-guard against the users table."""

from __future__ import annotations

from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, login


async def test_login_success_opens_admin(client):
    await login(client)
    resp = await client.get("/admin/")
    assert resp.status_code == 200


async def test_login_case_insensitive_username(client):
    resp = await client.post(
        "/login", data={"username": ADMIN_USERNAME.upper(), "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 303


async def test_login_wrong_password_401(client):
    resp = await client.post(
        "/login", data={"username": ADMIN_USERNAME, "password": "nope"}
    )
    assert resp.status_code == 401


async def test_login_unknown_user_401(client):
    resp = await client.post(
        "/login", data={"username": "ghost", "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 401


async def test_admin_route_requires_login_redirects(client):
    resp = await client.get("/admin/users", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_logout_clears_session(client):
    await login(client)
    assert (await client.get("/admin/")).status_code == 200
    await client.post("/logout")
    resp = await client.get("/admin/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
