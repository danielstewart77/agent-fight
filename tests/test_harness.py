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
    argv = build_argv(
        HarnessType.CLAUDE, workdir="/work/repo", prompt_path="/p", findings_path="/f", model="m"
    )
    assert argv[0] == "claude"
    assert "stream-json" in argv
    assert "--model" in argv and "m" in argv


def test_build_argv_codex():
    argv = build_argv(HarnessType.CODEX, workdir="/work/repo", prompt_path="/p", findings_path="/f")
    assert argv[0] == "codex"
    assert "--json" in argv


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
    s.proxy_base_url = "https://proxy.internal"
    s.proxy_api_key = "key-abc"
    return s


def test_harness_env_claude():
    env = harness_env(HarnessType.CLAUDE, _settings_with_proxy())
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.internal"
    assert env["ANTHROPIC_API_KEY"] == "key-abc"


def test_harness_env_codex():
    env = harness_env(HarnessType.CODEX, _settings_with_proxy())
    assert env["OPENAI_BASE_URL"] == "https://proxy.internal"
    assert env["OPENAI_API_KEY"] == "key-abc"


def test_harness_env_empty_when_proxy_unset():
    s = Settings(id=1, round_cap=3, clone_credential_kind="token")
    assert harness_env(HarnessType.CLAUDE, s) == {}
