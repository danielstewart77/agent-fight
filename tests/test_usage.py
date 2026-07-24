"""Token/cost accounting: harness `extract_usage` and proxy `complete_with_usage`.

The sample stream-json lines mirror the shapes captured from the real Claude and
Codex CLIs during triage (field names differ between the two harnesses)."""

from __future__ import annotations

import json

import httpx

from app.harness import extract_usage, harness_error_line
from app.proxy import ProxyClient
from app.review.models import HarnessType


# --- harness stdout -------------------------------------------------------- #


def test_extract_usage_claude_result_line():
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "total_cost_usd": 0.21191375,
                    "usage": {
                        "input_tokens": 2134,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 32183,
                    },
                }
            ),
        ]
    )
    u = extract_usage(HarnessType.CLAUDE, stdout)
    assert u.input_tokens == 2134
    assert u.output_tokens == 4
    assert u.cache_creation_tokens == 32183
    assert u.cache_read_tokens == 0
    assert u.cost_usd == 0.21191375
    assert not u.is_empty


def test_extract_usage_codex_turn_completed():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "x"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 8918,
                        "cached_input_tokens": 100,
                        "cache_write_input_tokens": 50,
                        "output_tokens": 5,
                    },
                }
            ),
        ]
    )
    u = extract_usage(HarnessType.CODEX, stdout)
    assert u.input_tokens == 8918
    assert u.output_tokens == 5
    assert u.cache_read_tokens == 100        # codex 'cached_input_tokens'
    assert u.cache_creation_tokens == 50     # codex 'cache_write_input_tokens'
    assert u.cost_usd is None                # codex reports no dollar cost


def test_extract_usage_sums_multiple_and_ignores_noise():
    stdout = "\n".join(
        [
            "not json at all",
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 1}}),
            json.dumps(["a", "list", "not", "a", "dict"]),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}}),
        ]
    )
    u = extract_usage(HarnessType.CODEX, stdout)
    assert u.input_tokens == 15
    assert u.output_tokens == 3


def test_harness_error_line_codex():
    stdout = "\n".join([
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "error", "message": "unexpected status 401 Unauthorized"}),
    ])
    assert "401 Unauthorized" in harness_error_line(HarnessType.CODEX, stdout)


def test_harness_error_line_claude_result_error():
    stdout = json.dumps({"type": "result", "is_error": True, "result": "context length exceeded"})
    assert "context length exceeded" in harness_error_line(HarnessType.CLAUDE, stdout)


def test_harness_error_line_none_when_clean():
    assert harness_error_line(HarnessType.CODEX, '{"type":"turn.completed","usage":{}}') == ""


def test_extract_usage_empty_when_no_usage():
    u = extract_usage(HarnessType.CLAUDE, "")
    assert u.is_empty
    # A harness that crashed before reporting usage -> empty, not an error.
    u2 = extract_usage(HarnessType.CLAUDE, "boom\ntraceback\n")
    assert u2.is_empty


# --- coordinator proxy ----------------------------------------------------- #


def _client(handler, **kw) -> ProxyClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ProxyClient("https://proxy.internal/", "tok", client=http, model="m", **kw)


async def test_complete_with_usage_anthropic():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "framed"}],
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 12,
                    "cache_creation_input_tokens": 7,
                },
            },
        )

    text, usage = await _client(handler).complete_with_usage(
        messages=[{"role": "user", "content": "hi"}]
    )
    assert text == "framed"
    assert (usage.input_tokens, usage.output_tokens) == (300, 40)
    assert usage.cache_read_tokens == 12 and usage.cache_creation_tokens == 7


async def test_complete_with_usage_openai():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hey"}}],
                "usage": {"prompt_tokens": 55, "completion_tokens": 9},
            },
        )

    text, usage = await _client(handler, flavor="openai").complete_with_usage(
        messages=[{"role": "user", "content": "hi"}]
    )
    assert text == "hey"
    assert (usage.input_tokens, usage.output_tokens) == (55, 9)


async def test_complete_still_returns_bare_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    assert await _client(handler).complete(messages=[{"role": "user", "content": "hi"}]) == "ok"
