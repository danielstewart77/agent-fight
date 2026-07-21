"""Client for the inference proxy the pipeline routes model calls through.

The proxy is an API-key-authed service that forwards each request to the
underlying provider (see plans/code-review-consensus.md). It is used two ways:

  1. The harnesses inside the review container are pointed at it via env vars
     (see app/harness.py::harness_env) — the proxy is their provider endpoint.
  2. The coordinator calls it *directly* through this `ProxyClient` for its own
     reasoning (paraphrasing one agent's findings for the other, synthesizing
     the final review).

For (2) we speak the Anthropic Messages API (`POST {base}/v1/messages`): the
primary harness family is Claude, the coordinator's job is a single-turn text
completion that maps cleanly onto it, and the proxy forwards the native request
to Anthropic. Auth is `Authorization: Bearer <proxy_api_key>`.

ASSUMPTIONS (isolated here — confirm against the deployed proxy): the bearer
auth header, the `anthropic-version` value, the `/v1/messages` route, and the
`/health` ping path.
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
DEFAULT_MODEL = "claude-opus-4-8"


class ProxyNotConfigured(RuntimeError):
    """Raised when a proxy call is attempted but the URL/key are unset."""


class ProxyError(RuntimeError):
    """A non-2xx response or malformed body from the proxy."""


class ProxyClient:
    """Thin async wrapper over the inference proxy.

    The underlying `httpx.AsyncClient` is injectable so tests can supply an
    `httpx.MockTransport`; when omitted, one is created lazily and owned by the
    client (close it with `aclose()` or use it as an async context manager).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
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
            resp = await self._http().get(
                f"{self._base_url}/health", headers=self._headers()
            )
            return resp.status_code == 200
        except httpx.HTTPError as exc:
            logger.warning("proxy ping failed: %s", exc)
            return False

    async def complete(
        self,
        *,
        messages: list[dict],
        system: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
    ) -> str:
        """One Anthropic Messages call; returns the concatenated text blocks."""
        body: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            body["system"] = system
        resp = await self._http().post(
            f"{self._base_url}/v1/messages", headers=self._headers(), json=body
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


async def load_proxy_config(session: AsyncSession) -> tuple[str, str]:
    """Return (base_url, api_key) from the settings singleton or raise."""
    row = (
        await session.execute(select(Settings).where(Settings.id == SETTINGS_ID))
    ).scalar_one_or_none()
    if row is None or not row.proxy_base_url or not row.proxy_api_key:
        raise ProxyNotConfigured(
            "inference proxy is not configured — set base URL and API key at /admin/settings"
        )
    return row.proxy_base_url, row.proxy_api_key


async def proxy_client_from_settings(
    session: AsyncSession, *, client: Optional[httpx.AsyncClient] = None
) -> ProxyClient:
    base_url, api_key = await load_proxy_config(session)
    return ProxyClient(base_url, api_key, client=client)
