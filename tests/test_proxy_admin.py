"""/admin/proxy — per-role proxy configuration page."""

from __future__ import annotations

import httpx

from tests.conftest import login


async def test_requires_login(client):
    resp = await client.get("/admin/proxy", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_renders_three_blocks(client):
    await login(client)
    resp = await client.get("/admin/proxy")
    assert resp.status_code == 200
    assert "Claude harness" in resp.text
    assert "Codex harness" in resp.text
    assert "Coordinator" in resp.text


async def test_persists_url_and_model_key_write_only(client):
    await login(client)
    resp = await client.post(
        "/admin/proxy",
        data={
            "claude_proxy_url": "https://proxy.internal",
            "claude_proxy_key": "claude-secret",
            "claude_model": "claude-sonnet-4-6",
            "codex_proxy_url": "https://proxy.internal/v1",
            "codex_proxy_key": "codex-secret",
            "codex_model": "gpt-5.5",
            "coord_proxy_url": "https://proxy.internal",
            "coord_proxy_key": "coord-secret",
            "coord_model": "claude-opus-4-7",
        },
    )
    assert resp.status_code == 303

    page = await client.get("/admin/proxy")
    assert 'value="https://proxy.internal"' in page.text
    # Keys are write-only: the indicator flips to "set" and the value never renders.
    assert "claude-secret" not in page.text
    assert "(set)" in page.text

    # Blank key on re-save leaves it set.
    await client.post(
        "/admin/proxy",
        data={
            "claude_proxy_url": "https://proxy.internal",
            "claude_proxy_key": "",
            "claude_model": "claude-sonnet-4-6",
            "codex_proxy_url": "https://proxy.internal/v1",
            "codex_proxy_key": "",
            "codex_model": "gpt-5.5",
            "coord_proxy_url": "https://proxy.internal",
            "coord_proxy_key": "",
            "coord_model": "claude-opus-4-7",
        },
    )
    page = await client.get("/admin/proxy")
    assert "(not set)" not in page.text  # all three keys remain set


async def test_invalid_url_rejected(client):
    await login(client)
    resp = await client.post(
        "/admin/proxy",
        data={
            "claude_proxy_url": "ftp://nope",
            "claude_proxy_key": "",
            "claude_model": "",
            "codex_proxy_url": "",
            "codex_proxy_key": "",
            "codex_model": "",
            "coord_proxy_url": "",
            "coord_proxy_key": "",
            "coord_model": "",
        },
    )
    assert resp.status_code == 400


async def test_model_dropdown_populates_from_proxy(client, monkeypatch):
    await login(client)
    # Save a config so the page has a key to list models with.
    await client.post(
        "/admin/proxy",
        data={
            "claude_proxy_url": "https://proxy.internal",
            "claude_proxy_key": "k",
            "claude_model": "",
            "codex_proxy_url": "",
            "codex_proxy_key": "",
            "codex_model": "",
            "coord_proxy_url": "",
            "coord_proxy_key": "",
            "coord_model": "",
        },
    )

    async def fake_list_models(base_url, api_key, *, flavor, **kw):
        if base_url and api_key and flavor == "anthropic":
            return ["claude-sonnet-4-6", "claude-opus-4-7"]
        return []

    monkeypatch.setattr("app.admin.proxy.list_models", fake_list_models)
    page = await client.get("/admin/proxy")
    assert "<select" in page.text
    assert "claude-sonnet-4-6" in page.text
