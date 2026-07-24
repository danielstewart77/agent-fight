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
import json
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.review.models import ReviewJob

logger = logging.getLogger("agent_fight.runner")


def _utcnow() -> datetime:
    """Naive UTC `now`, matching the naive DateTime columns whose server-side
    default is `func.now()`."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


RunFn = Callable[[ReviewJob], Awaitable[None]]


class ReviewDispatcher:
    """Fire-and-forget dispatch with a bounded concurrency cap.

    Every webhook still returns 202 immediately; the review task is created at
    once but blocks on a semaphore, so at most `concurrency` reviews actually run
    at a time and the rest queue (FIFO-ish) and start as slots free. This keeps a
    burst of PRs from spinning up unbounded containers / hammering the proxy."""

    def __init__(self, run: Optional[RunFn] = None, *, concurrency: int = 5) -> None:
        self._run = run or _default_run
        self._tasks: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._concurrency = max(1, concurrency)

    def dispatch(self, job: ReviewJob) -> None:
        task = asyncio.create_task(self._guarded(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, job: ReviewJob) -> None:
        # If all slots are busy, this waits here (the PR is queued) rather than
        # starting yet another concurrent review.
        if self._sem.locked():
            logger.info(
                "PR %s queued — %s review slots busy", job.pr_id, self._concurrency
            )
        async with self._sem:
            try:
                await self._run(job)
            except Exception:  # noqa: BLE001 - background task; log, don't crash the loop
                logger.exception("review run failed for PR %s", job.pr_id)


async def _default_run(job: ReviewJob) -> None:
    """Production run: load settings, build real deps, run the loop, post back.

    Imported lazily so the module graph (and tests that inject a fake run) do
    not drag in Docker/proxy wiring unless a real run actually happens. The
    Review-record lifecycle lives in `run_and_record`, which is injectable-runner
    friendly so it can be tested against `FakeRunner`.
    """
    from app.ado import AdoClient
    from app.config import config
    from app.container import DockerRunner
    from app.db import SessionLocal
    from app.orm import Settings

    async with SessionLocal() as session:
        settings = (
            await session.execute(select(Settings).where(Settings.id == 1))
        ).scalar_one_or_none()
        if settings is None:
            logger.error("no settings row; cannot run review for PR %s", job.pr_id)
            return

        from app import skills as skills_mod

        reasoner = _proxy_reasoner(settings)
        judge = _proxy_judge(settings)
        if judge is None:
            logger.warning(
                "PR %s: coordinator LLM (coord proxy) not configured — falling "
                "back to the deterministic diff for convergence", job.pr_id,
            )
        ado_client = AdoClient()
        mounts = skills_mod.selected_skill_mounts(settings)
        try:
            await run_and_record(
                session,
                job,
                settings,
                runner=DockerRunner(config.review_image, mounts=mounts),
                ado_client=ado_client,
                reasoner=reasoner,
                judge=judge,
            )
        finally:
            await ado_client.aclose()


async def run_and_record(
    session: AsyncSession,
    job: ReviewJob,
    settings,
    *,
    runner,
    ado_client,
    reasoner=None,
    judge=None,
):
    """Run one review to completion and persist it as a `Review` row.

    A `running` row is committed up front (so an in-flight or crashed run is
    visible), then finalized to `converged`/`capped` with metrics + per-role,
    per-round `ReviewTokenUsage` rows on success. On any failure the row is
    marked `failed` with the traceback in `error` and the exception re-raised
    (the dispatcher's guard still logs it). This is what makes a failed run
    visible without shelling into the process log.

    `runner`/`ado_client`/`reasoner` are injected so the whole lifecycle is
    exercisable against `FakeRunner` without Docker or a live proxy.
    """
    from app.coordinator import run_review
    from app.orm import Review, ReviewTokenUsage

    review = Review(
        pr_id=job.pr_id,
        repo_name=job.repo_name,
        organization=job.organization or None,
        project=job.project or None,
        source_branch=job.source_branch,
        target_branch=job.target_branch,
        pr_web_url=job.pr_web_url or None,
        event_type=job.event_type,
        source_commit=job.source_commit or None,
        status="running",
        started_at=_utcnow(),
    )
    session.add(review)
    await session.commit()
    await session.refresh(review)

    def _persist_usage(records) -> None:
        for rr in records:
            session.add(
                ReviewTokenUsage(
                    review_id=review.id,
                    role=rr.role,
                    round_no=rr.round_no,
                    input_tokens=rr.usage.input_tokens,
                    output_tokens=rr.usage.output_tokens,
                    cache_read_tokens=rr.usage.cache_read_tokens,
                    cache_creation_tokens=rr.usage.cache_creation_tokens,
                    cost_usd=rr.usage.cost_usd,
                )
            )

    started = time.monotonic()
    try:
        outcome = await run_review(
            job, settings, runner=runner, ado_client=ado_client,
            reasoner=reasoner, judge=judge,
        )
    except Exception as exc:  # noqa: BLE001 - record failure, DO NOT post, re-raise to log
        # A failed run is recorded as `failed` and NOTHING is posted to the PR
        # (run_review raises before its post-back). This is the guarantee that a
        # broken review can never look successful. See coordinator.ReviewFailure.
        review.status = "failed"
        review.error = traceback.format_exc()
        review.finished_at = _utcnow()
        review.duration_ms = int((time.monotonic() - started) * 1000)
        _persist_usage(getattr(exc, "usage", None) or [])  # partial spend, if any
        await session.commit()
        logger.error(
            "PR %s review FAILED — nothing posted to the PR (review #%s marked failed): %s",
            job.pr_id, review.id, exc,
        )
        raise

    review.status = outcome.status
    review.rounds_used = outcome.rounds_used
    review.converged_count = outcome.converged_count
    review.disagreement_count = outcome.disagreement_count
    review.unmatched_count = outcome.unmatched_count
    review.final_review_md = outcome.review_md
    review.finished_at = _utcnow()
    review.duration_ms = int((time.monotonic() - started) * 1000)
    _persist_usage(outcome.usage)
    await session.commit()
    logger.info(
        "PR %s review #%s complete: status=%s rounds=%s findings=%s",
        job.pr_id, review.id, outcome.status, outcome.rounds_used, outcome.converged_count,
    )
    return review


def _proxy_reasoner(settings) -> Optional[Callable[[str], Awaitable[str]]]:
    """A reasoner that reframes cross-exposure material via the coordinator
    proxy, or None (coordinator falls back to passthrough) when it's
    unconfigured."""
    if not settings.coord_proxy_url or not settings.coord_proxy_key:
        return None

    from app.proxy import ProxyClient

    async def reason(structured: str):
        """Returns (prose, Usage) so the coordinator's token spend is recorded."""
        async with ProxyClient(
            settings.coord_proxy_url,
            settings.coord_proxy_key,
            model=settings.coord_model,
            flavor=settings.coord_flavor or "anthropic",
        ) as pc:
            return await pc.complete_with_usage(
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


_JUDGE_SYSTEM = (
    "You are the coordinator of a two-agent code-review debate. You are given "
    "both reviewers' latest findings on the same pull-request diff. Decide, "
    "semantically, which findings they AGREE are real issues, which remain a "
    "genuine open dispute, and whether the debate has converged.\n\n"
    "Rules:\n"
    "- Match findings that describe the SAME underlying issue even if worded "
    "differently or off by a line — treat those as one item.\n"
    "- A finding one side raised and the other did not contradict, or that one "
    "side withdrew/resolved, is NOT a dispute.\n"
    "- Only a substantive disagreement about whether something is a real issue "
    "counts as disputed. If there are no such disputes, converged is true.\n"
    "- For agreed items, author the finding cleanly (keep the strongest claim "
    "and a concrete fix).\n\n"
    "Also cast a reviewer VOTE on the pull request based on the findings, using "
    "this policy:\n"
    "- 10  (approve): no issues.\n"
    "- 5   (approve with suggestions): only minor/nit issues, nothing unresolved.\n"
    "- -5  (wait for author): any major/critical agreed issue, OR any unresolved "
    "disagreement.\n"
    "- -10 (reject): reserve for egregious, clearly-unmergeable changes; prefer "
    "-5 when unsure.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    '{"converged": <bool>, '
    '"agreed": [{"file","line","severity","claim","confidence","suggestion"}], '
    '"disagreed": [{"file","line","claim","agent1","agent2"}], '
    '"vote": <10|5|-5|-10>, "vote_reason": "<one line>", '
    '"summary": "<one line>"}\n'
    "severity is critical|major|minor|nit. agent1/agent2 are each reviewer's "
    "short stance on the disputed item."
)


def _proxy_judge(settings):
    """The coordinator LLM judge, or None when the coord proxy is unconfigured
    (run_review then uses the deterministic fallback). Returns a callable
    judge(left, right, round_no) -> (CoordinatorDecision, Usage) that fails
    closed: any proxy error or unparseable judgment raises ReviewFailure, so a
    garbled judgment can never produce a posted review."""
    if not settings.coord_proxy_url or not settings.coord_proxy_key:
        return None

    from app.coordinator import parse_coordinator_decision
    from app.proxy import ProxyClient
    from app.review.models import ReviewFailure

    def _finding_payload(f) -> dict:
        return {
            "file": f.file, "line": f.line, "severity": f.severity,
            "verdict": f.verdict, "claim": f.claim, "suggestion": f.suggestion,
        }

    async def judge(left, right, round_no: int):
        payload = json.dumps(
            {
                "agent_1_findings": [_finding_payload(f) for f in left],
                "agent_2_findings": [_finding_payload(f) for f in right],
            },
            indent=2,
        )
        try:
            async with ProxyClient(
                settings.coord_proxy_url,
                settings.coord_proxy_key,
                model=settings.coord_model,
                flavor=settings.coord_flavor or "anthropic",
            ) as pc:
                text, usage = await pc.complete_with_usage(
                    system=_JUDGE_SYSTEM,
                    messages=[{"role": "user", "content": payload}],
                )
        except Exception as exc:  # noqa: BLE001 - fail closed on ANY judge error
            raise ReviewFailure(
                f"coordinator judge call failed in round {round_no}: {exc}",
                round_no=round_no, reason="judge call failed",
            ) from exc
        try:
            decision = parse_coordinator_decision(text)
        except ValueError as exc:
            raise ReviewFailure(
                f"coordinator judge returned unparseable output in round {round_no}: {exc}",
                round_no=round_no, reason="judge output unparseable",
            ) from exc
        return decision, usage

    return judge
