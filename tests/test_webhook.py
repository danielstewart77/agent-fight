"""POST /webhooks/ado — inbound Azure DevOps webhook: auth, parse, dispatch."""

from __future__ import annotations

import json
import pytest
from sqlalchemy import select
from app.orm import ReviewRequest
from tests.conftest import login, sample_ado_payload, sample_comment_payload


async def _configure_webhook_secret(client, secret: str) -> None:
    await login(client)
    resp = await client.post(
        "/admin/settings",
        data={
            "round_cap": "3",
            "ado_organization": "contoso",
            "ado_project": "",
            "ado_pat": "test-pat",
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


async def test_webhook_valid_command_persists_before_returning_202(client):
    await _configure_webhook_secret(client, "correct-secret")
    resp = await client.post(
        "/webhooks/ado",
        headers={"X-Webhook-Secret": "correct-secret"},
        json=sample_comment_payload(),
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "pr_id": 42}
    async with client.sessions() as session:
        row = (await session.scalars(select(ReviewRequest))).one()
        assert row.status == "pending"
        assert json.loads(row.job_json)["comment_id"] == 1
    assert client.dispatcher.jobs == []  # no untracked fire-and-forget dispatch


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
        json={"eventType": "ms.vss-code.git-pullrequest-comment-event", "resource": {}},
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


async def test_retries_and_comment_edits_do_not_repeat_but_new_comments_do(client):
    await _configure_webhook_secret(client, "secret")
    payload = sample_comment_payload()
    async def post():
        return await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "secret"}, json=payload)
    assert (await post()).json()["status"] == "accepted"
    assert (await post()).json()["status"] == "duplicate"
    payload["id"] = "different-delivery-for-edited-comment"
    assert (await post()).json()["status"] == "duplicate"
    payload = sample_comment_payload(comment_id=2)
    assert (await post()).json()["status"] == "accepted"
    # Comment IDs are scoped to threads; another thread's comment 1 is independent.
    payload = sample_comment_payload(thread_id=6)
    assert (await post()).json()["status"] == "accepted"
    async with client.sessions() as session:
        assert len((await session.scalars(select(ReviewRequest))).all()) == 3


@pytest.mark.parametrize("event", ["git.pullrequest.created", "git.pullrequest.updated"])
async def test_legacy_automatic_hooks_acknowledged_without_jobs(client, event):
    await _configure_webhook_secret(client, "secret")
    response = await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "secret"},
                                 json=sample_ado_payload(event))
    assert response.json()["status"] == "ignored"
    async with client.sessions() as session:
        assert (await session.scalars(select(ReviewRequest))).all() == []


async def test_command_flows_from_http_through_live_verification_to_review_and_status(client):
    import asyncio
    import httpx
    from app.ado import AdoClient
    from app.review_queue import ReviewDispatcher

    await _configure_webhook_secret(client, "secret")
    payload = sample_comment_payload()
    comment = payload["resource"]["comment"]
    # The operator's PAT may also be the posting credential. Their commands work.
    author = comment["author"]
    command_thread = {"id": 5, "comments": [comment]}
    posted_reviews = []
    def handler(request):
        path = request.url.path
        if "/connectionData" in path:
            return httpx.Response(200, json={"authenticatedUser": author})
        if request.method == "GET":
            if path.endswith("/comments/1"):
                body = comment
            elif path.endswith("/threads/5"):
                body = command_thread
            elif path.endswith("/threads"):
                body = {"value": [command_thread]}
            else:
                body = payload["resource"]["pullRequest"]
        else:
            body = json.loads(request.content)
            if path.endswith("/threads"):
                posted_reviews.append(body)
            else:
                status = {"id": 2, "content": body["content"], "author": author}
                command_thread["comments"] = [comment, status]
        return httpx.Response(200, json=body)

    async def run(job, settings):
        assert job.source_commit == "a" * 40
        await AdoClient(client=http).post_pr_review(settings, job, "A useful review")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        worker = ReviewDispatcher(run, sessions=client.sessions,
                                  ado_factory=lambda: AdoClient(client=http), poll_seconds=.01)
        response = await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "secret"}, json=payload)
        assert response.json()["status"] == "accepted"
        await worker.start()
        try:
            async def wait_completed():
                while True:
                    async with client.sessions() as session:
                        state = (await session.scalars(select(ReviewRequest.status))).one()
                    if state == "completed" and "Review completed" in command_thread["comments"][-1]["content"]:
                        return
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_completed(), 3)
        finally:
            await worker.stop()
    assert len(posted_reviews) == 1
    assert "A useful review" in posted_reviews[0]["comments"][0]["content"]
    assert len(command_thread["comments"]) == 2  # one updated status reply
    assert (await client.post("/webhooks/ado", headers={"X-Webhook-Secret": "secret"},
                              json=payload)).json()["status"] == "duplicate"
