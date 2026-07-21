"""Fire-and-forget dispatch of review runs off the webhook request path.

A review run is minutes long (clone, multiple harness rounds, post-back), so
the webhook must return its 202 immediately and let the run proceed in the
background. `ReviewDispatcher.dispatch` schedules an asyncio task, retains a
reference so it is not garbage-collected mid-flight, and logs any exception via
a done-callback.

This is the prototype-grade seam: a production deployment would swap the
in-process task for a real queue/worker, but the `dispatch(job)` interface
stays the same. The actual run is behind `self._run`, injectable so tests use a
recording fake instead of spinning up Docker.

ASSUMPTION: a single uvicorn worker, so in-process background tasks are an
acceptable execution model for now.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from sqlalchemy import select

from app.review.models import ReviewJob

logger = logging.getLogger("agent_fight.runner")

RunFn = Callable[[ReviewJob], Awaitable[None]]


class ReviewDispatcher:
    def __init__(self, run: Optional[RunFn] = None) -> None:
        self._run = run or _default_run
        self._tasks: set[asyncio.Task] = set()

    def dispatch(self, job: ReviewJob) -> None:
        task = asyncio.create_task(self._guarded(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, job: ReviewJob) -> None:
        try:
            await self._run(job)
        except Exception:  # noqa: BLE001 - background task; log, don't crash the loop
            logger.exception("review run failed for PR %s", job.pr_id)


async def _default_run(job: ReviewJob) -> None:
    """Production run: load settings, build real deps, run the loop, post back.

    Imported lazily so the module graph (and tests that inject a fake run) do
    not drag in Docker/proxy wiring unless a real run actually happens.
    """
    from app.ado import AdoClient
    from app.config import config
    from app.container import DockerRunner
    from app.coordinator import run_review
    from app.db import SessionLocal
    from app.orm import Settings

    async with SessionLocal() as session:
        settings = (
            await session.execute(select(Settings).where(Settings.id == 1))
        ).scalar_one_or_none()
        if settings is None:
            logger.error("no settings row; cannot run review for PR %s", job.pr_id)
            return

        reasoner = _proxy_reasoner(settings)
        ado_client = AdoClient()
        try:
            await run_review(
                job,
                settings,
                runner=DockerRunner(config.review_image),
                ado_client=ado_client,
                reasoner=reasoner,
            )
        finally:
            await ado_client.aclose()


def _proxy_reasoner(settings) -> Optional[Callable[[str], Awaitable[str]]]:
    """A reasoner that reframes cross-exposure material via the proxy, or None
    (coordinator falls back to passthrough) when the proxy is unconfigured."""
    if not settings.proxy_base_url or not settings.proxy_api_key:
        return None

    from app.proxy import ProxyClient

    async def reason(structured: str) -> str:
        async with ProxyClient(settings.proxy_base_url, settings.proxy_api_key) as pc:
            return await pc.complete(
                system=(
                    "You are the coordinator in a multi-agent code review. Reframe "
                    "the structured findings below into a concise, conversational "
                    "briefing for one reviewer — summarize their own prior points, "
                    "then present the other reviewer's points and the open "
                    "disagreements fairly, inviting them to agree, disagree, or revise."
                ),
                messages=[{"role": "user", "content": structured}],
            )

    return reason
