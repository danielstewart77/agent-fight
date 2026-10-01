import json
from dataclasses import replace

import httpx
import pytest

from app.ado import AdoClient, UnsupportedEvent, parse_comment_event, request_key
from app.orm import Settings
from tests.conftest import sample_comment_payload


def inputs():
    settings = Settings(ado_organization="contoso", ado_pat="pat")
    payload = sample_comment_payload()
    return settings, payload, parse_comment_event(payload, settings)


async def test_preparation_reads_live_command_and_pins_live_pr_commits():
    settings, payload, job = inputs()
    pr = payload["resource"]["pullRequest"]
    pr["lastMergeSourceCommit"]["commitId"] = "c" * 40
    urls = []
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=payload["resource"]["comment"]
                              if "/comments/" in request.url.path else pr)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AdoClient(client=http).prepare_request(settings, job)
    assert result.source_commit == "c" * 40
    assert result.target_commit == "b" * 40
    assert result.comment_id == job.comment_id
    assert all(url.startswith("https://dev.azure.com/contoso/") for url in urls)


@pytest.mark.parametrize("change", ["deleted", "content", "author", "closed", "missing_commit"])
async def test_live_validation_prevents_reviewing_invalid_requests(change):
    settings, payload, job = inputs()
    comment = payload["resource"]["comment"]
    pr = payload["resource"]["pullRequest"]
    if change == "deleted":
        comment["isDeleted"] = True
    elif change == "content":
        comment["content"] = "never mind"
    elif change == "author":
        comment["author"]["id"] = "someone-else"
    elif change == "closed":
        pr["status"] = "abandoned"
    else:
        pr.pop("lastMergeSourceCommit")
    def handler(request):
        return httpx.Response(200, json=comment if "/comments/" in request.url.path else pr)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RuntimeError if change == "missing_commit" else UnsupportedEvent):
            await AdoClient(client=http).prepare_request(settings, job)


async def test_status_reply_is_updated_in_place_and_never_itself_a_command():
    settings, _, job = inputs()
    comments = []
    writes = []
    def handler(request):
        if "/connectionData" in request.url.path:
            return httpx.Response(200, json={"authenticatedUser": {"id": "bot-id"}})
        if request.method == "GET":
            return httpx.Response(200, json={"comments": comments})
        body = json.loads(request.content)
        writes.append((request.method, request.url.path, body))
        comments[:] = [{"id": 9, "content": body["content"], "author": {"id": "bot-id"}}]
        return httpx.Response(200, json={"id": 9})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ado = AdoClient(client=http)
        await ado.request_status(settings, job, "Review queued.")
        # New client simulates recovering after a restart.
        await AdoClient(client=http).request_status(settings, job, "Review completed.")
    assert [w[0] for w in writes] == ["POST", "PATCH"]
    assert writes[0][2]["parentCommentId"] == 1
    assert writes[1][1].endswith("/threads/5/comments/9")
    with pytest.raises(UnsupportedEvent):
        parse_comment_event(sample_comment_payload(comments[0]["content"]), settings)


async def test_api_paths_encode_project_names_and_do_not_follow_payload_urls():
    _, _, job = inputs()
    url = AdoClient()._pr_url(replace(job, project="IT Development"))
    assert "/IT%20Development/" in url
