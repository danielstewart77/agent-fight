"""Harness adapter: argv, stream-json parsing, env injection."""

from __future__ import annotations

import pytest

from app.harness import build_argv, harness_env, parse_line, resolve_harness
from app.orm import Settings
from app.review.models import HarnessType


def test_resolve_harness():
    assert resolve_harness("claude") is HarnessType.CLAUDE
    assert resolve_harness(" CODEX ") is HarnessType.CODEX
    with pytest.raises(ValueError):
        resolve_harness("gpt")


def test_build_argv_claude():
    argv = build_argv(HarnessType.CLAUDE, workdir="/work/repo", model="m")
    assert argv[0] == "claude"
    assert "stream-json" in argv
    assert "--model" in argv and "m" in argv


def test_build_argv_codex():
    argv = build_argv(HarnessType.CODEX, workdir="/work/repo")
    assert argv[0] == "codex"
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_build_argv_claude_skips_permissions():
    argv = build_argv(HarnessType.CLAUDE, workdir="/w")
    assert "--dangerously-skip-permissions" in argv


def test_parse_line_blank_returns_none():
    assert parse_line(HarnessType.CLAUDE, "   ") is None


def test_parse_line_garbage_is_unknown_not_raise():
    ev = parse_line(HarnessType.CLAUDE, "not json at all")
    assert ev is not None and ev.kind == "unknown"


def test_parse_line_claude_assistant_text():
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}'
    ev = parse_line(HarnessType.CLAUDE, line)
    assert ev.kind == "assistant_text"
    assert ev.text == "hi"


def test_parse_line_claude_result():
    ev = parse_line(HarnessType.CLAUDE, '{"type":"result","result":"done"}')
    assert ev.kind == "result"
    assert ev.text == "done"


def test_parse_line_codex_message_and_error():
    msg = parse_line(HarnessType.CODEX, '{"type":"agent_message","text":"yo"}')
    assert msg.kind == "assistant_text" and msg.text == "yo"
    err = parse_line(HarnessType.CODEX, '{"type":"error","message":"bad"}')
    assert err.kind == "error" and err.text == "bad"


def _settings_with_proxy() -> Settings:
    s = Settings(id=1, round_cap=3, clone_credential_kind="token")
    s.claude_proxy_url = "https://proxy.internal"
    s.claude_proxy_key = "claude-key"
    s.codex_proxy_url = "https://proxy.internal/v1"
    s.codex_proxy_key = "codex-key"
    return s


def test_harness_env_claude_has_no_auth_token():
    env = harness_env(HarnessType.CLAUDE, _settings_with_proxy())
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.internal"
    assert env["ANTHROPIC_API_KEY"] == "claude-key"
    # AUTH_TOKEN must never be set — it would route away from the proxy.
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_harness_env_codex_normalizes_to_v1():
    env = harness_env(HarnessType.CODEX, _settings_with_proxy())
    assert env["OPENAI_BASE_URL"] == "https://proxy.internal/v1"
    assert env["OPENAI_API_KEY"] == "codex-key"


def test_harness_env_codex_appends_v1_to_bare_url():
    from app.orm import Settings

    s = Settings(id=1, round_cap=3, clone_credential_kind="token")
    s.codex_proxy_url = "https://proxy.internal"  # bare, no /v1
    s.codex_proxy_key = "k"
    assert harness_env(HarnessType.CODEX, s)["OPENAI_BASE_URL"] == "https://proxy.internal/v1"


def test_codex_base_url_normalizes():
    from app.harness import codex_base_url

    assert codex_base_url("https://p.example") == "https://p.example/v1"
    assert codex_base_url("https://p.example/v1") == "https://p.example/v1"
    assert codex_base_url("https://p.example/") == "https://p.example/v1"


def test_harness_env_empty_when_proxy_unset():
    s = Settings(id=1, round_cap=3, clone_credential_kind="token")
    assert harness_env(HarnessType.CLAUDE, s) == {}
    assert harness_env(HarnessType.CODEX, s) == {}


def test_codex_auth_json():
    import json
    from app.harness import codex_auth_json

    data = json.loads(codex_auth_json("codex-key"))
    assert data == {"auth_mode": "apikey", "OPENAI_API_KEY": "codex-key"}


def test_codex_config_toml():
    from app.harness import codex_config_toml

    toml = codex_config_toml("https://proxy.internal/v1", "gpt-5.5")
    assert 'openai_base_url = "https://proxy.internal/v1"' in toml
    assert 'model = "gpt-5.5"' in toml
    assert 'model_provider = "azure_proxy"' in toml
    assert "[model_providers.azure_proxy]" in toml
    assert 'wire_api = "responses"' in toml
    assert 'env_key = "OPENAI_API_KEY"' in toml
