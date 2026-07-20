"""/admin/settings — the admin-configured run settings surface."""

from __future__ import annotations

from tests.conftest import login


async def test_get_settings_requires_admin_session_redirects_to_login(client):
    resp = await client.get("/admin/settings")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_get_settings_renders_get_or_create_row(client):
    await login(client)
    resp = await client.get("/admin/settings")
    assert resp.status_code == 200
    assert "round_cap" in resp.text or "Round cap" in resp.text
    # Default round_cap (3) should show up in the rendered form value.
    assert 'value="3"' in resp.text


async def test_post_updates_round_cap_and_ado_org_project(client):
    await login(client)
    resp = await client.post(
        "/admin/settings",
        data={
            "round_cap": "5",
            "ado_organization": "contoso",
            "ado_project": "widgets",
            "ado_pat": "",
            "webhook_secret": "",
            "clone_credential_kind": "token",
            "clone_credential_secret": "",
        },
    )
    assert resp.status_code == 303

    resp = await client.get("/admin/settings")
    assert 'value="5"' in resp.text
    assert 'value="contoso"' in resp.text
    assert 'value="widgets"' in resp.text


async def test_post_with_blank_secrets_leaves_stored_values_untouched(client):
    await login(client)
    await client.post(
        "/admin/settings",
        data={
            "round_cap": "3",
            "ado_organization": "contoso",
            "ado_project": "widgets",
            "ado_pat": "initial-pat",
            "webhook_secret": "initial-webhook-secret",
            "clone_credential_kind": "token",
            "clone_credential_secret": "initial-token",
        },
    )

    # Second save with every secret field blank.
    resp = await client.post(
        "/admin/settings",
        data={
            "round_cap": "3",
            "ado_organization": "contoso",
            "ado_project": "widgets",
            "ado_pat": "",
            "webhook_secret": "",
            "clone_credential_kind": "token",
            "clone_credential_secret": "",
        },
    )
    assert resp.status_code == 303

    # Verify via a fresh webhook call that the secret is still the first one
    # (indirect check, since the admin page never echoes secret values).
    resp = await client.post(
        "/webhooks/ado", headers={"X-Webhook-Secret": "initial-webhook-secret"}
    )
    assert resp.status_code == 202


async def test_post_with_nonblank_secret_updates_it(client):
    await login(client)
    await client.post(
        "/admin/settings",
        data={
            "round_cap": "3",
            "ado_organization": "",
            "ado_project": "",
            "ado_pat": "",
            "webhook_secret": "first-secret",
            "clone_credential_kind": "token",
            "clone_credential_secret": "",
        },
    )
    await client.post(
        "/admin/settings",
        data={
            "round_cap": "3",
            "ado_organization": "",
            "ado_project": "",
            "ado_pat": "",
            "webhook_secret": "second-secret",
            "clone_credential_kind": "token",
            "clone_credential_secret": "",
        },
    )

    # Old secret no longer authenticates.
    resp = await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "first-secret"})
    assert resp.status_code == 401

    # New secret does.
    resp = await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "second-secret"})
    assert resp.status_code == 202


async def test_invalid_round_cap_rejected(client):
    await login(client)
    for bad_value in ("0", "-1", "not-a-number"):
        resp = await client.post(
            "/admin/settings",
            data={
                "round_cap": bad_value,
                "ado_organization": "",
                "ado_project": "",
                "ado_pat": "",
                "webhook_secret": "",
                "clone_credential_kind": "token",
                "clone_credential_secret": "",
            },
        )
        assert resp.status_code == 400, f"expected 400 for round_cap={bad_value!r}"


async def test_invalid_clone_credential_kind_rejected(client):
    await login(client)
    resp = await client.post(
        "/admin/settings",
        data={
            "round_cap": "3",
            "ado_organization": "",
            "ado_project": "",
            "ado_pat": "",
            "webhook_secret": "",
            "clone_credential_kind": "carrier-pigeon",
            "clone_credential_secret": "",
        },
    )
    assert resp.status_code == 400
