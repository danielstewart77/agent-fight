"""POST /webhooks/ado — inbound Azure DevOps webhook authentication."""

from __future__ import annotations

from tests.conftest import login


async def _configure_webhook_secret(client, secret: str) -> None:
    await login(client)
    resp = await client.post(
        "/admin/settings",
        data={
            "round_cap": "3",
            "ado_organization": "",
            "ado_project": "",
            "ado_pat": "",
            "webhook_secret": secret,
            "clone_credential_kind": "token",
            "clone_credential_secret": "",
        },
    )
    assert resp.status_code == 303
    # Log out so subsequent webhook calls exercise the public, unauthenticated
    # route rather than riding an admin session.
    await client.post("/logout")


async def test_webhook_not_configured_yet_returns_503(client):
    resp = await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "anything"})
    assert resp.status_code == 503


async def test_webhook_missing_header_returns_401(client):
    await _configure_webhook_secret(client, "correct-secret")
    resp = await client.post("/webhooks/ado")
    assert resp.status_code == 401


async def test_webhook_wrong_secret_returns_401(client):
    await _configure_webhook_secret(client, "correct-secret")
    resp = await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "wrong-secret"})
    assert resp.status_code == 401


async def test_webhook_correct_secret_returns_202_not_yet_implemented(client):
    await _configure_webhook_secret(client, "correct-secret")
    resp = await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "correct-secret"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert "not yet implemented" in body["detail"]
