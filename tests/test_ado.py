"""ADO payload parsing and PR post-back."""

from __future__ import annotations

import base64

import httpx
import pytest

from app.ado import (
    AdoClient,
    AdoNotConfigured,
    MalformedPayload,
    UnsupportedEvent,
    parse_pull_request_event,
)
from app.orm import Settings
from tests.conftest import sample_ado_payload


def _settings(**kw) -> Settings:
    s = Settings(id=1, round_cap=3, clone_credential_kind="token")
    s.ado_organization = kw.get("ado_organization", "contoso")
    s.ado_project = kw.get("ado_project", "fallback-project")
    s.ado_pat = kw.get("ado_pat")
    return s


def test_parse_valid_payload():
    job = parse_pull_request_event(sample_ado_payload(), _settings())
    assert job.pr_id == 42
    assert job.repo_id == "repo-guid-123"
    assert job.repo_clone_url.endswith("_git/widgets")
    assert job.source_branch == "feature-x"
    assert job.target_branch == "main"
    assert job.source_ref == "refs/heads/feature-x"
    assert job.organization == "contoso"
    assert job.project == "widgets-project"  # from payload, not the fallback
    assert job.pr_web_url.endswith("/pullrequest/42")


def test_parse_extracts_source_commit():
    p = sample_ado_payload()
    p["resource"]["lastMergeSourceCommit"] = {"commitId": "abc123def"}
    job = parse_pull_request_event(p, _settings())
    assert job.source_commit == "abc123def"


def test_parse_source_commit_empty_when_absent():
    assert parse_pull_request_event(sample_ado_payload(), _settings()).source_commit == ""


def test_project_falls_back_to_settings_when_absent():
    payload = sample_ado_payload()
    del payload["resource"]["repository"]["project"]
    job = parse_pull_request_event(payload, _settings())
    assert job.project == "fallback-project"


def test_updated_event_is_supported():
    job = parse_pull_request_event(sample_ado_payload("git.pullrequest.updated"), _settings())
    assert job.event_type == "git.pullrequest.updated"


def test_unsupported_event_raises():
    with pytest.raises(UnsupportedEvent):
        parse_pull_request_event(sample_ado_payload("git.push"), _settings())


def test_missing_required_field_raises():
    payload = sample_ado_payload()
    del payload["resource"]["pullRequestId"]
    with pytest.raises(MalformedPayload):
        parse_pull_request_event(payload, _settings())


def test_non_dict_payload_raises():
    with pytest.raises(MalformedPayload):
        parse_pull_request_event([], _settings())  # type: ignore[arg-type]


async def test_post_pr_review_builds_correct_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"id": 1})

    job = parse_pull_request_event(sample_ado_payload(), _settings())
    client = AdoClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.post_pr_review(_settings(ado_pat="my-pat"), job, "# Review\n\nlooks good")

    assert "/git/repositories/repo-guid-123/pullRequests/42/threads" in captured["url"]
    assert "api-version=7.1" in captured["url"]
    expected_auth = "Basic " + base64.b64encode(b":my-pat").decode()
    assert captured["auth"] == expected_auth
    assert "looks good" in captured["body"]


async def test_set_pr_vote_resolves_reviewer_and_puts_vote():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        calls.append((request.method, str(request.url)))
        if "connectionData" in str(request.url):
            return httpx.Response(200, json={"authenticatedUser": {"id": "rev-guid-9"}})
        # the PUT reviewer vote
        calls.append(("BODY", _json.loads(request.read())))
        return httpx.Response(200, json={"vote": -5})

    job = parse_pull_request_event(sample_ado_payload(), _settings())
    client = AdoClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.set_pr_vote(_settings(ado_pat="p"), job, -5)

    put = [c for c in calls if c[0] == "PUT"]
    assert put and "/pullRequests/42/reviewers/rev-guid-9" in put[0][1]
    body = [c for c in calls if c[0] == "BODY"]
    assert body and body[0][1] == {"vote": -5}
    # connectionData is preview-only — a plain api-version=7.1 gets a 400.
    conn = [c for c in calls if "connectionData" in c[1]]
    assert conn and "preview" in conn[0][1]


async def test_set_pr_vote_zero_is_noop():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    job = parse_pull_request_event(sample_ado_payload(), _settings())
    client = AdoClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.set_pr_vote(_settings(ado_pat="p"), job, 0)
    assert called is False  # vote 0 -> nothing sent (not even connectionData)


async def test_post_or_update_updates_existing_thread():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            # An existing thread carrying our marker.
            return httpx.Response(200, json={"value": [
                {"id": 7, "comments": [{"id": 3, "content": "🤖 Agent-fight consensus review — old"}]}
            ]})
        return httpx.Response(200, json={})  # PATCH

    job = parse_pull_request_event(sample_ado_payload(), _settings())
    client = AdoClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    outcome = await client.post_or_update_pr_review(_settings(ado_pat="p"), job, "new review")
    assert outcome == "updated"
    methods = [m for m, _ in calls]
    assert methods == ["GET", "PATCH"]
    assert "/threads/7/comments/3" in calls[1][1]


async def test_post_or_update_creates_when_absent():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"value": []})  # no prior thread
        return httpx.Response(200, json={"id": 1})  # POST create

    job = parse_pull_request_event(sample_ado_payload(), _settings())
    client = AdoClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    outcome = await client.post_or_update_pr_review(_settings(ado_pat="p"), job, "new review")
    assert outcome == "created"


async def test_post_pr_review_without_pat_raises():
    job = parse_pull_request_event(sample_ado_payload(), _settings())
    client = AdoClient(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    with pytest.raises(AdoNotConfigured):
        await client.post_pr_review(_settings(ado_pat=None), job, "x")


async def test_post_pr_review_raises_on_non_2xx():
    job = parse_pull_request_event(sample_ado_payload(), _settings())
    client = AdoClient(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="no"))))
    with pytest.raises(RuntimeError):
        await client.post_pr_review(_settings(ado_pat="p"), job, "x")
