"""/admin/account — change your own password."""

from __future__ import annotations

from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, login


async def test_change_password_requires_login(client):
    resp = await client.post(
        "/admin/account/password",
        data={"current_password": "x", "new_password": "y" * 8, "confirm": "y" * 8},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_wrong_current_password_rejected(client):
    await login(client)
    resp = await client.post(
        "/admin/account/password",
        data={"current_password": "wrong", "new_password": "newpass12", "confirm": "newpass12"},
    )
    assert resp.status_code == 400
    assert "incorrect" in resp.text.lower()


async def test_mismatched_confirmation_rejected(client):
    await login(client)
    resp = await client.post(
        "/admin/account/password",
        data={"current_password": ADMIN_PASSWORD, "new_password": "newpass12", "confirm": "different12"},
    )
    assert resp.status_code == 400
    assert "match" in resp.text.lower()


async def test_too_short_rejected(client):
    await login(client)
    resp = await client.post(
        "/admin/account/password",
        data={"current_password": ADMIN_PASSWORD, "new_password": "short", "confirm": "short"},
    )
    assert resp.status_code == 400


async def test_successful_change_swaps_password(client):
    await login(client)
    resp = await client.post(
        "/admin/account/password",
        data={"current_password": ADMIN_PASSWORD, "new_password": "brand-new-pass", "confirm": "brand-new-pass"},
    )
    assert resp.status_code == 200
    assert "updated" in resp.text.lower()

    await client.post("/logout")
    # Old password no longer works.
    assert (await client.post("/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})).status_code == 401
    # New password does.
    assert (await client.post("/login", data={"username": ADMIN_USERNAME, "password": "brand-new-pass"})).status_code == 303
