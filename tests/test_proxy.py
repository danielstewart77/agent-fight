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
    out = await pc.complete(messages=[{"role": "user", "content": "hi"}], system="be nice")
    assert out == "hello world"


async def test_complete_raises_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream down")

    pc = _client(handler)
    with pytest.raises(ProxyError):
        await pc.complete(messages=[{"role": "user", "content": "hi"}])
