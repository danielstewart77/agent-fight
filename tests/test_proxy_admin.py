"""/admin/proxy — per-role, two-step proxy configuration."""

from __future__ import annotations

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
    # Each block has its own per-role form.
    for role in ("claude", "codex", "coord"):
        assert f'action="/admin/proxy/{role}"' in resp.text


async def test_step1_saves_url_and_key_only_for_its_role(client):
    await login(client)
    # Save just the Claude block (step 1).
    resp = await client.post(
        "/admin/proxy/claude",
        data={"proxy_url": "https://proxy.internal", "proxy_key": "claude-secret", "model": ""},
    )
    assert resp.status_code == 303

    page = await client.get("/admin/proxy")
    assert 'value="https://proxy.internal"' in page.text
    assert "claude-secret" not in page.text  # write-only
    # Claude key is set; the other two are still not set.
    assert page.text.count("(not set)") == 2


async def test_unknown_role_404(client):
    await login(client)
    resp = await client.post("/admin/proxy/bogus", data={"proxy_url": "", "proxy_key": "", "model": ""})
    assert resp.status_code == 404


async def test_invalid_url_rejected(client):
    await login(client)
    resp = await client.post(
        "/admin/proxy/claude", data={"proxy_url": "ftp://nope", "proxy_key": "", "model": ""}
    )
    assert resp.status_code == 400


async def test_blank_key_leaves_stored_value(client):
    await login(client)
    await client.post(
        "/admin/proxy/codex",
        data={"proxy_url": "https://proxy.internal/v1", "proxy_key": "codex-secret", "model": ""},
    )
    # Re-save with blank key.
    await client.post(
        "/admin/proxy/codex",
        data={"proxy_url": "https://proxy.internal/v1", "proxy_key": "", "model": "gpt-5.5"},
    )
    page = await client.get("/admin/proxy")
    # Codex still shows "set" and the model persisted.
    assert "gpt-5.5" in page.text


async def test_coordinator_has_provider_selector_and_saves_flavor(client):
    await login(client)
    page = await client.get("/admin/proxy")
    # Only the coordinator block exposes a provider <select name="flavor">.
    assert 'name="flavor"' in page.text
    assert page.text.count('name="flavor"') == 1

    resp = await client.post(
        "/admin/proxy/coord",
        data={"proxy_url": "https://proxy.internal/v1", "proxy_key": "k", "model": "", "flavor": "openai"},
    )
    assert resp.status_code == 303
    # Reload shows openai selected for the coordinator.
    page = await client.get("/admin/proxy")
    assert 'value="openai"' in page.text


async def test_coordinator_rejects_bad_flavor(client):
    await login(client)
    resp = await client.post(
        "/admin/proxy/coord",
        data={"proxy_url": "", "proxy_key": "", "model": "", "flavor": "sideways"},
    )
    assert resp.status_code == 400


async def test_coordinator_flavor_drives_model_listing(client, monkeypatch):
    await login(client)
    await client.post(
        "/admin/proxy/coord",
        data={"proxy_url": "https://p/v1", "proxy_key": "k", "model": "", "flavor": "openai"},
    )
    calls = []

    async def fake_list_models(base_url, api_key, *, flavor, **kw):
        calls.append((base_url, flavor))
        return ["gpt-5.5"] if flavor == "openai" else ["claude-x"]

    monkeypatch.setattr("app.admin.proxy.list_models", fake_list_models)
    page = await client.get("/admin/proxy")
    # The coordinator (its saved url) was listed with its saved flavor, openai.
    assert ("https://p/v1", "openai") in calls
    assert "gpt-5.5" in page.text


async def test_step2_model_dropdown_populates_after_key(client, monkeypatch):
    await login(client)
    await client.post(
        "/admin/proxy/claude",
        data={"proxy_url": "https://proxy.internal", "proxy_key": "k", "model": ""},
    )

    async def fake_list_models(base_url, api_key, *, flavor, **kw):
        if base_url and api_key and flavor == "anthropic":
            return ["claude-sonnet-4-6", "claude-opus-4-7"]
        return []

    monkeypatch.setattr("app.admin.proxy.list_models", fake_list_models)
    page = await client.get("/admin/proxy")
    assert "<select" in page.text
    assert "claude-sonnet-4-6" in page.text

    # Step 2: pick a model and save.
    resp = await client.post(
        "/admin/proxy/claude",
        data={"proxy_url": "https://proxy.internal", "proxy_key": "", "model": "claude-opus-4-7"},
    )
    assert resp.status_code == 303
