"""Only explicit commands can launch reviews; delivery metadata isn't a command."""
import pytest

from app.ado import MalformedPayload, UnsupportedEvent, parse_comment_event
from app.orm import Settings
from tests.conftest import sample_comment_payload, sample_ado_payload


def settings():
    return Settings(id=1, ado_organization="contoso")


def test_command_extracts_nested_pr_and_comment_identity():
    job = parse_comment_event(sample_comment_payload(" /agent-fight review\n"), settings())
    assert (job.pr_id, job.thread_id, job.comment_id) == (42, 5, 1)
    assert job.requester_id == "developer-id"
    assert job.event_id == "event-123"


@pytest.mark.parametrize("content", [
    "hello", "please /agent-fight review", "/agent-fight review please",
    "> /agent-fight review", "```\n/agent-fight review\n```",
    "**Agent Fight**\n/agent-fight review", "/agent-fight reviews", "",
])
def test_only_whole_comment_commands_are_accepted(content):
    with pytest.raises(UnsupportedEvent):
        parse_comment_event(sample_comment_payload(content), settings())


@pytest.mark.parametrize("event", ["git.pullrequest.created", "git.pullrequest.updated", "git.push"])
def test_automatic_events_never_request_a_review(event):
    with pytest.raises(UnsupportedEvent):
        parse_comment_event(sample_ado_payload(event), settings())


@pytest.mark.parametrize("mutation", ["deleted", "system", "closed"])
def test_nonactionable_comments_ignored(mutation):
    payload = sample_comment_payload()
    if mutation == "deleted":
        payload["resource"]["comment"]["isDeleted"] = True
    elif mutation == "system":
        payload["resource"]["comment"]["commentType"] = "system"
    else:
        payload["resource"]["pullRequest"]["status"] = "completed"
    with pytest.raises(UnsupportedEvent):
        parse_comment_event(payload, settings())


@pytest.mark.parametrize("resource", [None, [], "text", {}])
def test_malformed_resource_is_a_client_error(resource):
    payload = sample_comment_payload()
    payload["resource"] = resource
    with pytest.raises(MalformedPayload):
        parse_comment_event(payload, settings())


def test_thread_link_must_identify_the_same_pr_and_comment():
    payload = sample_comment_payload()
    payload["resource"]["comment"]["_links"]["self"]["href"] = (
        "https://example.test/_apis/git/repositories/repo-guid-123/"
        "pullRequests/999/threads/5/comments/1"
    )
    with pytest.raises(MalformedPayload):
        parse_comment_event(payload, settings())
