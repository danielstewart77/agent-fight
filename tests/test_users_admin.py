"""/admin/users — create, reset, delete operator accounts."""

from __future__ import annotations

import re

from tests.conftest import ADMIN_USERNAME, login


def _user_ids(html: str) -> dict[str, int]:
    """Map username -> id by parsing the users table rows."""
    ids: dict[str, int] = {}
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        m_id = re.search(r"/admin/users/(\d+)/delete", row)
        m_name = re.search(r"<td>([a-z0-9._-]+)", row)
        if m_id and m_name:
            ids[m_name.group(1)] = int(m_id.group(1))
    return ids


async def _add_user(client, username, password="password123"):
    return await client.post(
        "/admin/users",
        data={"username": username, "password": password, "confirm": password},
    )


async def test_create_user_appears_in_list(client):
    await login(client)
    resp = await _add_user(client, "alice")
    assert resp.status_code == 303
    listing = await client.get("/admin/users")
    assert "alice" in listing.text
    assert ADMIN_USERNAME in listing.text


async def test_duplicate_username_rejected(client):
    await login(client)
    await _add_user(client, "bob")
    resp = await _add_user(client, "bob")
    assert resp.status_code == 400
    assert "already exists" in resp.text.lower()


async def test_username_normalized_lowercase(client):
    await login(client)
    await _add_user(client, "Carol")
    listing = await client.get("/admin/users")
    assert "carol" in listing.text


async def test_invalid_username_rejected(client):
    await login(client)
    resp = await _add_user(client, "has spaces!")
    assert resp.status_code == 400


async def test_short_password_rejected(client):
    await login(client)
    resp = await _add_user(client, "dave", password="short")
    assert resp.status_code == 400


async def test_mismatched_password_rejected(client):
    await login(client)
    resp = await client.post(
        "/admin/users",
        data={"username": "erin", "password": "password123", "confirm": "password999"},
    )
    assert resp.status_code == 400


async def test_delete_user(client):
    await login(client)
    await _add_user(client, "frank")
    ids = _user_ids((await client.get("/admin/users")).text)
    resp = await client.post(f"/admin/users/{ids['frank']}/delete")
    assert resp.status_code == 303
    assert "frank" not in (await client.get("/admin/users")).text


async def test_cannot_delete_last_user(client):
    await login(client)
    ids = _user_ids((await client.get("/admin/users")).text)
    # Only the seeded operator exists.
    resp = await client.post(f"/admin/users/{ids[ADMIN_USERNAME]}/delete")
    assert resp.status_code == 400
    assert "last remaining" in resp.text.lower()
    # Still there.
    assert ADMIN_USERNAME in (await client.get("/admin/users")).text


async def test_reset_password_lets_user_log_in(client):
    await login(client)
    await _add_user(client, "gina", password="initial-pass")
    ids = _user_ids((await client.get("/admin/users")).text)
    resp = await client.post(
        f"/admin/users/{ids['gina']}/password",
        data={"password": "reset-pass-99", "confirm": "reset-pass-99"},
    )
    assert resp.status_code == 303

    await client.post("/logout")
    assert (await client.post("/login", data={"username": "gina", "password": "initial-pass"})).status_code == 401
    assert (await client.post("/login", data={"username": "gina", "password": "reset-pass-99"})).status_code == 303
