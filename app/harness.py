"""Harness adapter: build each CLI's invocation, normalize its output, and
wire it to its inference proxy — exactly the way the proxy's own installers do.

Three pure pieces plus the Codex config builders:

  - `build_argv`   : the argv to run a harness non-interactively in the container
  - `parse_line`   : one stream-json line -> a normalized `Event`
  - `harness_env`  : provider env vars for the harness
  - `codex_auth_json` / `codex_config_toml` : the files Codex reads

Provider wiring mirrors /home/daniel/azure-openai-proxy/install-{claude,codex}.sh:
  - Claude: ANTHROPIC_BASE_URL (bare root) + ANTHROPIC_API_KEY. ANTHROPIC_AUTH_TOKEN
    is deliberately NOT set — it overrides the key and routes back to
    api.anthropic.com.
  - Codex: reads ~/.codex/auth.json and ~/.codex/config.toml (a custom
    model_provider with wire_api="responses"), base URL WITH /v1. Env vars are
    written too as a fallback, but the files are authoritative.

The findings each harness produces are read from the JSON file it writes (see
the coordinator), NOT scraped from the event stream.
"""

from __future__ import annotations

import json

from app.orm import Settings
from app.proxy import proxy_root
from app.review.models import Event, HarnessType

CODEX_PROVIDER_ID = "azure_proxy"


def codex_base_url(url: str) -> str:
    """Codex talks to the OpenAI-compatible surface at the proxy's /v1 root.

    Normalize whatever the operator entered (bare root or /v1-suffixed) to a
    single canonical `<root>/v1`, so a bare URL doesn't route Codex at the wrong
    path (see install-codex.sh, which sets PROXY_URL with /v1)."""
    return proxy_root(url) + "/v1"


def resolve_harness(name: str) -> HarnessType:
    """Map a free-form name to a HarnessType (raises ValueError if unknown)."""
    return HarnessType(name.strip().lower())


def harness_model(harness: HarnessType, settings: Settings) -> str | None:
    return settings.claude_model if harness is HarnessType.CLAUDE else settings.codex_model


def build_argv(
    harness: HarnessType,
    *,
    workdir: str,
    model: str | None = None,
) -> list[str]:
    """The command to run `harness` once, non-interactively, in the container.

    The prompt is piped to the process on stdin (see coordinator.run_review); it
    tells the harness where to write its findings JSON. Both run inside the
    sealed, ephemeral review container, so Claude skips permission prompts.
    """
    if harness is HarnessType.CLAUDE:
        argv = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--add-dir",
            workdir,
        ]
        if model:
            argv += ["--model", model]
        return argv
    if harness is HarnessType.CODEX:
        # The review runs in a sealed, ephemeral container, so let Codex act
        # autonomously (the analog of Claude's --dangerously-skip-permissions):
        # its default read-only sandbox otherwise blocks writing the findings
        # file, and approvals would block non-interactively. --skip-git-repo-check
        # keeps its trusted-directory guard from aborting the run.
        argv = [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--cd",
            workdir,
        ]
        if model:
            argv += ["--model", model]
        return argv
    raise ValueError(f"unknown harness: {harness!r}")


def _claude_event(obj: dict) -> Event:
    kind = obj.get("type")
    if kind == "assistant":
        message = obj.get("message", {}) or {}
        parts = [
            b.get("text", "")
            for b in message.get("content", []) or []
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return Event(kind="assistant_text", text="".join(parts) or None, raw=obj)
    if kind == "result":
        return Event(kind="result", text=obj.get("result"), raw=obj)
    if kind == "system":
        return Event(kind="usage", text=None, raw=obj)
    return Event(kind="unknown", raw=obj)


def _codex_event(obj: dict) -> Event:
    kind = obj.get("type") or obj.get("msg", {}).get("type")
    if kind in ("assistant_message", "agent_message", "message"):
        text = obj.get("text") or obj.get("message") or (obj.get("msg", {}) or {}).get("text")
        return Event(kind="assistant_text", text=text, raw=obj)
    if kind in ("task_complete", "result", "turn_complete"):
        return Event(kind="result", text=obj.get("text"), raw=obj)
    if kind in ("error", "stream_error"):
        return Event(kind="error", text=obj.get("message") or obj.get("error"), raw=obj)
    return Event(kind="unknown", raw=obj)


def parse_line(harness: HarnessType, line: str) -> Event | None:
    """Normalize one stream-json line into an `Event`.

    Blank lines return None. Non-JSON or unrecognized lines return an
    `unknown` event rather than raising — a harness printing a stray log line
    must never crash the run.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return Event(kind="unknown", text=line, raw={"line": line})
    if not isinstance(obj, dict):
        return Event(kind="unknown", raw={"value": obj})

    if harness is HarnessType.CLAUDE:
        return _claude_event(obj)
    if harness is HarnessType.CODEX:
        return _codex_event(obj)
    return Event(kind="unknown", raw=obj)


def harness_env(harness: HarnessType, settings: Settings) -> dict[str, str]:
    """Provider env for the harness. Empty dict when its proxy is unconfigured
    (the caller decides whether that is fatal)."""
    if harness is HarnessType.CLAUDE:
        url, key = settings.claude_proxy_url, settings.claude_proxy_key
        if not url or not key:
            return {}
        # NOTE: ANTHROPIC_AUTH_TOKEN is intentionally absent — it would override
        # ANTHROPIC_API_KEY and route away from the proxy (see install-claude.sh).
        return {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": key}
    if harness is HarnessType.CODEX:
        url, key = settings.codex_proxy_url, settings.codex_proxy_key
        if not url or not key:
            return {}
        return {"OPENAI_BASE_URL": codex_base_url(url), "OPENAI_API_KEY": key}
    raise ValueError(f"unknown harness: {harness!r}")


def codex_auth_json(api_key: str) -> str:
    """~/.codex/auth.json — Codex prefers this over the env var on every call."""
    return json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": api_key})


def codex_config_toml(base_url: str, model: str | None) -> str:
    """~/.codex/config.toml — a custom provider routed at the proxy.

    Mirrors install-codex.sh: top-level openai_base_url/model/model_provider
    plus a [model_providers.azure_proxy] block with wire_api="responses". Using
    a custom provider (not the built-in openai one) keeps Codex from enabling
    OpenAI-only wire experiments the proxy can't forward.
    """
    lines = [
        f'openai_base_url = "{base_url}"',
    ]
    if model:
        lines.append(f'model = "{model}"')
    lines += [
        f'model_provider = "{CODEX_PROVIDER_ID}"',
        "",
        f"[model_providers.{CODEX_PROVIDER_ID}]",
        'name = "Azure OpenAI Proxy"',
        f'base_url = "{base_url}"',
        'env_key = "OPENAI_API_KEY"',
        'wire_api = "responses"',
    ]
    return "\n".join(lines) + "\n"
