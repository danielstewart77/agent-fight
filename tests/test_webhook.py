"""POST /webhooks/ado — inbound Azure DevOps webhook: auth, parse, dispatch."""

from __future__ import annotations

from tests.conftest import login, sample_ado_payload


async def _configure_webhook_secret(client, secret: str) -> None:
    await login(client)
    resp = await client.post(
        "/admin/settings",
        data={
            "round_cap": "3",
            "ado_organization": "contoso",
            "ado_project": "",
            "ado_pat": "",
            "webhook_secret": secret,
            "clone_credential_kind": "token",
            "clone_credential_secret": "",
            "proxy_base_url": "",
            "proxy_api_key": "",
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


async def test_webhook_valid_payload_dispatches_and_returns_202(client):
    await _configure_webhook_secret(client, "correct-secret")
    resp = await client.post(
        "/webhooks/ado",
        headers={"X-Webhook-Secret": "correct-secret"},
        json=sample_ado_payload(),
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "pr_id": 42}
    # The parsed job reached the dispatcher.
    assert len(client.dispatcher.jobs) == 1
    assert client.dispatcher.jobs[0].pr_id == 42
    assert client.dispatcher.jobs[0].source_branch == "feature-x"


async def test_webhook_non_json_body_returns_400(client):
    await _configure_webhook_secret(client, "correct-secret")
    resp = await client.post(
        "/webhooks/ado",
        headers={"X-Webhook-Secret": "correct-secret", "content-type": "application/json"},
        content=b"not json",
    )
    assert resp.status_code == 400


async def test_webhook_malformed_payload_returns_400(client):
    await _configure_webhook_secret(client, "correct-secret")
    resp = await client.post(
        "/webhooks/ado",
        headers={"X-Webhook-Secret": "correct-secret"},
        json={"eventType": "git.pullrequest.created", "resource": {}},
    )
    assert resp.status_code == 400


async def test_webhook_unsupported_event_is_ignored_not_dispatched(client):
    await _configure_webhook_secret(client, "correct-secret")
    resp = await client.post(
        "/webhooks/ado",
        headers={"X-Webhook-Secret": "correct-secret"},
        json={"eventType": "git.push", "resource": {}},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert client.dispatcher.jobs == []
