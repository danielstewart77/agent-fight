"""Client for the inference proxy the pipeline routes model calls through.

The proxy is an API-key-authed service that forwards each request to the
underlying provider (see plans/code-review-consensus.md). Three independent
connections are configured at /admin/proxy — one per harness and one for the
coordinator — because they differ in practice (see app/orm.py::Settings):

  - The Claude harness and the coordinator speak the Anthropic Messages API and
    use a bare-root base URL (e.g. https://proxy.example).
  - Codex is OpenAI-compatible and uses a base URL with /v1.

This module serves the coordinator's own direct calls (`ProxyClient`, Anthropic
Messages) and the /admin/proxy model dropdown (`list_models`). The harnesses'
own proxy wiring is env/files injected into the container (see app/harness.py).

Endpoints on the proxy root (confirmed against the proxy source):
  POST {root}/v1/messages           Anthropic Messages
  GET  {root}/v1/anthropic/models   Anthropic-compatible deployments
  GET  {root}/v1/models             OpenAI-compatible deployments
  GET  {root}/health                health
Model listing requires a valid key (Authorization: Bearer).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import Settings

logger = logging.getLogger("agent_fight.proxy")

SETTINGS_ID = 1
ANTHROPIC_VERSION = "2023-06-01"


class ProxyNotConfigured(RuntimeError):
    """Raised when a proxy call is attempted but the URL/key are unset."""


class ProxyError(RuntimeError):
    """A non-2xx response or malformed body from the proxy."""


def proxy_root(base_url: str) -> str:
    """Normalize a configured base URL to the proxy root.

    Users may enter the bare root (Anthropic style) or a `/v1`-suffixed URL
    (Codex style); both should resolve to the same root so we can build the
    right absolute endpoint paths.
    """
    root = base_url.strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


class ProxyClient:
    """Thin async wrapper over the coordinator's inference proxy connection.

    The underlying `httpx.AsyncClient` is injectable so tests can supply an
    `httpx.MockTransport`; when omitted, one is created lazily and owned by the
    client (close it with `aclose()` or use it as an async context manager).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        model: Optional[str] = None,
        flavor: str = "anthropic",
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 120.0,
    ) -> None:
        self._root = proxy_root(base_url)
        self._api_key = api_key
        self._model = model
        self._flavor = flavor
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def ping(self) -> bool:
        """Best-effort health check. Returns False on any error rather than
        raising, so a startup ping never takes the app down."""
        try:
            resp = await self._http().get(f"{self._root}/health", headers=self._headers())
            return resp.status_code == 200
        except httpx.HTTPError as exc:
            logger.warning("proxy ping failed: %s", exc)
            return False

    async def complete(
        self,
        *,
        messages: list[dict],
        system: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
    ) -> str:
        """One completion in the configured flavor; returns the text.

        - anthropic: POST /v1/messages, top-level `system`, parse content[].text.
        - openai:    POST /v1/chat/completions, `system` as a system-role
                     message, parse choices[0].message.content.
        """
        use_model = model or self._model
        if not use_model:
            raise ProxyError("no model configured for the coordinator proxy")

        if self._flavor == "openai":
            msgs = ([{"role": "system", "content": system}] if system else []) + messages
            body = {"model": use_model, "max_tokens": max_tokens, "messages": msgs}
            resp = await self._http().post(
                f"{self._root}/v1/chat/completions", headers=self._headers(), json=body
            )
            if resp.status_code // 100 != 2:
                raise ProxyError(f"proxy returned {resp.status_code}: {resp.text[:500]}")
            try:
                data = resp.json()
                return data["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ProxyError(f"malformed proxy response: {exc}") from exc

        body = {"model": use_model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            body["system"] = system
        resp = await self._http().post(
            f"{self._root}/v1/messages", headers=self._headers(), json=body
        )
        if resp.status_code // 100 != 2:
            raise ProxyError(f"proxy returned {resp.status_code}: {resp.text[:500]}")
        try:
            data = resp.json()
            parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        except (ValueError, AttributeError) as exc:
            raise ProxyError(f"malformed proxy response: {exc}") from exc
        return "".join(parts)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "ProxyClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


async def list_models(
    base_url: str,
    api_key: str,
    *,
    flavor: str,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 8.0,
) -> list[str]:
    """List deployment ids from the proxy, for the /admin/proxy dropdown.

    `flavor` selects the endpoint: 'anthropic' -> /v1/anthropic/models (Claude /
    coordinator), 'openai' -> /v1/models (Codex). Best-effort: returns [] on any
    error (missing key, unreachable proxy, unexpected body) so the page can fall
    back to a free-text model field.
    """
    if not base_url or not api_key:
        return []
    root = proxy_root(base_url)
    path = "/v1/anthropic/models" if flavor == "anthropic" else "/v1/models"
    owns = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await http.get(
            f"{root}{path}", headers={"Authorization": f"Bearer {api_key}"}
        )
        if resp.status_code // 100 != 2:
            logger.warning("model listing %s returned %s", path, resp.status_code)
            return []
        data = resp.json()
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    except (httpx.HTTPError, ValueError, KeyError, AttributeError) as exc:
        logger.warning("model listing failed for %s: %s", path, exc)
        return []
    finally:
        if owns:
            await http.aclose()


async def _settings(session: AsyncSession) -> Optional[Settings]:
    return (
        await session.execute(select(Settings).where(Settings.id == SETTINGS_ID))
    ).scalar_one_or_none()


async def load_coordinator_proxy(session: AsyncSession) -> tuple[str, str, Optional[str], str]:
    """Return (base_url, api_key, model, flavor) for the coordinator proxy or raise."""
    row = await _settings(session)
    if row is None or not row.coord_proxy_url or not row.coord_proxy_key:
        raise ProxyNotConfigured(
            "coordinator proxy is not configured — set it at /admin/proxy"
        )
    return row.coord_proxy_url, row.coord_proxy_key, row.coord_model, row.coord_flavor or "anthropic"


async def coordinator_client_from_settings(
    session: AsyncSession, *, client: Optional[httpx.AsyncClient] = None
) -> ProxyClient:
    base_url, api_key, model, flavor = await load_coordinator_proxy(session)
    return ProxyClient(base_url, api_key, model=model, flavor=flavor, client=client)
