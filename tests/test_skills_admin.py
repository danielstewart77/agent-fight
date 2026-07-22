"""/admin/skills — repos, sync, per-harness picker."""

from __future__ import annotations

from tests.conftest import login


async def test_requires_login(client):
    resp = await client.get("/admin/skills", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_renders_both_repos(client):
    await login(client)
    resp = await client.get("/admin/skills")
    assert resp.status_code == 200
    assert "claude-skills" in resp.text
    assert "codex-skills" in resp.text


async def test_save_urls_and_selection(client):
    await login(client)
    resp = await client.post(
        "/admin/skills",
        data={
            "claude_skills_repo": "https://dev.azure.com/o/p/_git/claude-skills",
            "codex_skills_repo": "https://dev.azure.com/o/p/_git/codex-skills",
            "claude_skill": ["agent-fight-review", "other"],
            "codex_skill": "agent-fight-review",
        },
    )
    assert resp.status_code == 303
    page = await client.get("/admin/skills")
    assert "https://dev.azure.com/o/p/_git/claude-skills" in page.text
    assert "_git/codex-skills" in page.text


async def test_sync_button_invokes_sync(client, monkeypatch):
    await login(client)
    called = {}

    def fake_sync(settings):
        called["yes"] = True
        return [{"repo": "claude-skills", "ok": True, "action": "clone", "detail": "abc ok"}]

    monkeypatch.setattr("app.admin.skills.skills_mod.sync", fake_sync)
    resp = await client.post("/admin/skills/sync", follow_redirects=False)
    assert resp.status_code == 303
    assert called.get("yes") is True
