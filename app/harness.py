"""Harness adapter: build each CLI's invocation, normalize its output, and
point it at the inference proxy.

This is the "harness-type resolver" the design calls for, generalized from
inference-proxy's name-based provider resolution. Three pure pieces:

  - `build_argv`   : the argv to run a harness non-interactively in the container
  - `parse_line`   : one stream-json line -> a normalized `Event`
  - `harness_env`  : env that points the CLI's provider calls at the proxy

Keeping these pure means the whole adapter is unit-testable without the CLIs
installed. The findings each harness produces are read from the JSON file it
writes (see the coordinator), NOT scraped from the event stream — the stream is
only for progress/logging and completion/error detection.

ASSUMPTIONS (isolated here — confirm against the installed CLIs): the exact
flags, the per-line JSON schemas, and the provider env-var names each CLI
honors for a base-URL/key override.
"""

from __future__ import annotations

import json

from app.orm import Settings
from app.review.models import Event, HarnessType


def resolve_harness(name: str) -> HarnessType:
    """Map a free-form name to a HarnessType (raises ValueError if unknown)."""
    return HarnessType(name.strip().lower())


def build_argv(
    harness: HarnessType,
    *,
    workdir: str,
    prompt_path: str,
    findings_path: str,
    model: str | None = None,
) -> list[str]:
    """The command to run `harness` once, non-interactively, in the container.

    The prompt (read from `prompt_path`) instructs the harness to write its
    structured findings to `findings_path`; we surface both paths to the
    harness via the prompt text rather than a dedicated flag, since neither CLI
    has a "write findings here" option.
    """
    if harness is HarnessType.CLAUDE:
        argv = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--add-dir",
            workdir,
        ]
        if model:
            argv += ["--model", model]
        return argv
    if harness is HarnessType.CODEX:
        argv = ["codex", "exec", "--json", "--cd", workdir]
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
    """Env that routes the harness's provider calls through the proxy.

    Returns an empty dict when the proxy is unconfigured, so the caller can
    decide whether that is fatal (it is, for a real run — checked upstream).
    """
    base_url = settings.proxy_base_url or ""
    api_key = settings.proxy_api_key or ""
    if not base_url or not api_key:
        return {}

    if harness is HarnessType.CLAUDE:
        return {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": api_key,
            "ANTHROPIC_AUTH_TOKEN": api_key,
        }
    if harness is HarnessType.CODEX:
        return {
            "OPENAI_BASE_URL": base_url,
            "OPENAI_API_KEY": api_key,
        }
    raise ValueError(f"unknown harness: {harness!r}")
