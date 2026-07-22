"""Skills: host clone/pull, skill listing, and per-harness mount selection."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import app.skills as skills_mod
from app.config import config


def _settings(**kw):
    base = dict(
        ado_pat=None,
        claude_skills_repo="",
        codex_skills_repo="",
        claude_skills_enabled=None,
        codex_skills_enabled=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _make_source_repo(path):
    path.mkdir(parents=True)
    (path / "agent-fight-review").mkdir()
    (path / "agent-fight-review" / "SKILL.md").write_text("# review\n")
    (path / "other-skill").mkdir()
    (path / "other-skill" / "SKILL.md").write_text("# other\n")
    env = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"}
    author = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,  # noqa: E731
                                    capture_output=True, env={**env, **author})
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")


def test_list_skills(tmp_path, monkeypatch):
    (tmp_path / "claude-skills" / "a").mkdir(parents=True)
    (tmp_path / "claude-skills" / "a" / "SKILL.md").write_text("x")
    (tmp_path / "claude-skills" / "notaskill").mkdir()  # no SKILL.md
    assert skills_mod.list_skills(tmp_path, "claude-skills") == ["a"]


def test_selected_skill_mounts_only_selected_and_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "skills_root", str(tmp_path))
    for name in ("agent-fight-review", "other-skill"):
        (tmp_path / "claude-skills" / name).mkdir(parents=True)
        (tmp_path / "claude-skills" / name / "SKILL.md").write_text("x")
    settings = _settings(claude_skills_enabled=json.dumps(["agent-fight-review", "missing"]))
    mounts = skills_mod.selected_skill_mounts(settings)
    assert mounts == [
        (str(tmp_path / "claude-skills" / "agent-fight-review"),
         "/home/node/.claude/skills/agent-fight-review")
    ]


def test_sync_clones_then_fast_forwards(tmp_path, monkeypatch):
    source = tmp_path / "src"
    _make_source_repo(source)
    monkeypatch.setattr(config, "skills_root", str(tmp_path / "skills"))
    settings = _settings(claude_skills_repo=str(source), codex_skills_repo="")

    first = {r["repo"]: r for r in skills_mod.sync(settings)}
    assert first["claude-skills"]["ok"] and first["claude-skills"]["action"] == "clone"
    assert first["codex-skills"]["action"] == "skip"
    assert (tmp_path / "skills" / "claude-skills" / "agent-fight-review" / "SKILL.md").exists()

    second = {r["repo"]: r for r in skills_mod.sync(settings)}
    assert second["claude-skills"]["ok"] and second["claude-skills"]["action"] == "pull"

    st = {r["repo"]: r for r in skills_mod.status(settings)}
    assert st["claude-skills"]["present"] and st["claude-skills"]["head"]
    assert set(st["claude-skills"]["available"]) == {"agent-fight-review", "other-skill"}
    assert st["codex-skills"]["configured"] is False
