"""ProxyClient — ping and Anthropic-Messages completion via a mock transport."""

from __future__ import annotations

import httpx
import pytest

from app.proxy import ProxyClient, ProxyError


def _client(handler) -> ProxyClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return ProxyClient("https://proxy.internal/", "tok-123", client=http)


async def test_ping_true_on_200():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        assert request.headers["authorization"] == "Bearer tok-123"
        return httpx.Response(200, json={"ok": True})

    pc = _client(handler)
    assert await pc.ping() is True


async def test_ping_false_on_503():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    pc = _client(handler)
    assert await pc.ping() is False


async def test_ping_false_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    pc = _client(handler)
    assert await pc.ping() is False


async def test_complete_posts_messages_and_extracts_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/messages"
        assert request.headers["anthropic-version"]
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]},
        )

    pc = _client(handler)
    out = await pc.complete(
        messages=[{"role": "user", "content": "hi"}], system="be nice", model="claude-x"
    )
    assert out == "hello world"


async def test_complete_without_model_raises():
    import pytest as _pytest

    pc = _client(lambda r: httpx.Response(200, json={"content": []}))
    with _pytest.raises(ProxyError):
        await pc.complete(messages=[{"role": "user", "content": "hi"}])


def test_proxy_root_strips_v1():
    from app.proxy import proxy_root

    assert proxy_root("https://p.example/v1") == "https://p.example"
    assert proxy_root("https://p.example/") == "https://p.example"
    assert proxy_root("https://p.example") == "https://p.example"


async def test_list_models_anthropic_and_openai_paths():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}, {"bad": "x"}]})

    from app.proxy import list_models

    # Codex uses a /v1-suffixed URL -> normalized root -> /v1/models.
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got = await list_models("https://p.example/v1", "k", flavor="openai", client=http)
    assert got == ["m1", "m2"]
    assert seen["path"] == "/v1/models"
    assert seen["auth"] == "Bearer k"

    # Anthropic flavor -> /v1/anthropic/models.
    http2 = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await list_models("https://p.example", "k", flavor="anthropic", client=http2)
    assert seen["path"] == "/v1/anthropic/models"


async def test_list_models_empty_on_error_or_missing_creds():
    from app.proxy import list_models

    assert await list_models("", "k", flavor="openai") == []
    assert await list_models("https://p.example", "", flavor="openai") == []
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert await list_models("https://p.example", "k", flavor="openai", client=http) == []


async def test_complete_raises_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream down")

    pc = _client(handler)
    with pytest.raises(ProxyError):
        await pc.complete(messages=[{"role": "user", "content": "hi"}])
