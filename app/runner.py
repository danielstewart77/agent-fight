"""Execute reviews with production history, live progress, usage and findings.

Request scheduling lives in app.review_queue.
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

from app.review.models import ReviewFailure, ReviewJob

logger = logging.getLogger("agent_fight.runner")


def describe_review_failure(exc: Exception) -> str:
    """Turn an internal exception into a safe, actionable operator message.

    The full traceback is still stored in ``Review.error``.  This text is the
    deliberately short explanation shown first in the admin UI.
    """
    raw = str(exc)
    text = raw.lower()
    source = "Review worker"
    happened = "The review stopped because of an unexpected internal error."
    action = "Open technical details below, then retry after correcting the reported cause."

    if isinstance(exc, ReviewFailure):
        source = f"{exc.harness.title()} review agent" if exc.harness else "Review coordinator"
        stage = "independent review" if exc.round_no == 0 else (
            f"fight round {exc.round_no}" if exc.round_no is not None else "review"
        )
        if exc.reason == "inactive":
            happened = f"The {source} produced no output for 30 minutes during {stage}."
            action = "The agent or model connection may be stalled. Check the model service before retrying."
        elif "upstream connection dropped" in text or "connection reset" in text or "connection closed" in text:
            happened = f"The model service connection for the {source} dropped during {stage}, including its retry."
            action = "This is usually transient. Check the model proxy/upstream status, then retry the review."
        elif any(marker in text for marker in ("401", "403", "unauthorized", "forbidden", "authentication")):
            happened = f"The {source} was rejected by its model service during {stage}."
            action = "Verify that agent's proxy URL, API key, and model access in Settings, then retry."
        elif exc.reason == "wrote no findings file":
            happened = f"The {source} exited without producing its required findings file during {stage}."
            action = "Check the agent logs and review image/skill compatibility, then retry."
        elif exc.reason and "unparseable" in exc.reason:
            happened = f"The {source} produced malformed findings during {stage}; nothing was posted."
            action = "Check the agent output in technical details and verify the review skill, then retry."
        elif exc.reason == "judge call failed":
            happened = f"The coordinator model could not judge {stage}; nothing was posted."
            action = "Check the coordinator proxy, key, and model configuration, then retry."
        else:
            happened = f"The {source} failed during {stage}; nothing was posted."
    elif isinstance(exc, TimeoutError):
        source = "Review worker"
        happened = "A review operation exceeded its time limit."
        action = "Retry the review. If it repeats, inspect technical details to identify the slow operation."
    elif any(marker in text for marker in ("couldn't find remote ref", "not our ref", "reference is not a tree")):
        source = "Repository checkout"
        happened = "Azure DevOps no longer exposes the source branch or commit from this webhook event."
        action = "Push or restore the source commit and post a new /agent-fight review command; an old event cannot be reviewed."
    elif "repo prep step" in text or "git clone" in text or "git fetch" in text:
        source = "Repository checkout"
        happened = "The repository could not be cloned or prepared for review."
        action = "Check the clone credential, repository access, and ref shown in technical details, then retry."

    return f"Source: {source}\nWhat happened: {happened}\nNext step: {action}"


def _utcnow() -> datetime:
    """Naive UTC `now`, matching the naive DateTime columns whose server-side
    default is `func.now()`."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


RunFn = Callable[[ReviewJob], Awaitable[None]]


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

        ado_client = AdoClient()
        mounts = skills_mod.selected_skill_mounts(settings)
        try:
            await run_and_record(
                session,
                job,
                settings,
                runner=DockerRunner(config.review_image, mounts=mounts),
                ado_client=ado_client,
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
):
    """Run one review to completion and persist it as a `Review` row.

    A `running` row is committed up front (so an in-flight or crashed run is
    visible), then finalized to `converged`/`capped` with metrics + per-role,
    per-round `ReviewTokenUsage` rows on success. On any failure the row is
    marked `failed` with the traceback in `error` and the exception re-raised
    (the dispatcher's guard still logs it). This is what makes a failed run
    visible without shelling into the process log.

    `runner`/`ado_client` are injected so the whole lifecycle is
    exercisable against `FakeRunner` without Docker or a live proxy.
    """
    from app.coordinator import run_review
    from app.orm import Review, ReviewEvent, ReviewFinding, ReviewTokenUsage

    review = Review(
        pr_id=job.pr_id,
        repo_name=job.repo_name,
        organization=job.organization or None,
        project=job.project or None,
        source_branch=job.source_branch,
        target_branch=job.target_branch,
        pr_web_url=job.pr_web_url or None,
        created_by=job.created_by or None,
        event_type=job.event_type,
        source_commit=job.source_commit or None,
        status="running",
        current_phase="queued",
        status_message="Review accepted",
        last_activity_at=_utcnow(),
        started_at=_utcnow(),
    )
    session.add(review)
    await session.commit()
    await session.refresh(review)

    async def progress(kind, phase, message, *, role=None, round_no=None):
        now = _utcnow()
        review.current_phase, review.status_message, review.last_activity_at = phase, message, now
        session.add(ReviewEvent(review_id=review.id, kind=kind, phase=phase,
                                message=message, role=role, round_no=round_no, created_at=now))
        await session.commit()

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
            job, settings, runner=runner, ado_client=ado_client, progress=progress,
        )
    except (Exception, asyncio.CancelledError) as exc:  # record interrupted runs as well
        # Cancellation can interrupt the other agent's progress-event commit.
        # Recover the session before persisting the failed review and its usage.
        await session.rollback()
        await session.refresh(review)
        # A failed run is recorded as `failed` and NOTHING is posted to the PR
        # (run_review raises before its post-back). This is the guarantee that a
        # broken review can never look successful. See coordinator.ReviewFailure.
        review.status = "failed"
        review.current_phase = "failed"
        review.status_message = describe_review_failure(exc)[:512]
        review.last_activity_at = _utcnow()
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
    review.current_phase = "completed"
    review.status_message = "Review completed"
    review.last_activity_at = _utcnow()
    review.rounds_used = outcome.rounds_used
    review.converged_count = outcome.converged_count
    review.disagreement_count = outcome.disagreement_count
    review.unmatched_count = outcome.unmatched_count
    review.final_review_md = outcome.review_md
    review.finished_at = _utcnow()
    review.duration_ms = int((time.monotonic() - started) * 1000)
    _persist_usage(outcome.usage)
    # Keep every agent's raw output beside the comment it produced, so a review
    # that reads wrong can be explained from the source rather than guessed at.
    for rf in outcome.raw_findings:
        session.add(
            ReviewFinding(
                review_id=review.id, role=rf.role, round_no=rf.round_no, raw=rf.raw
            )
        )
    await session.commit()
    logger.info(
        "PR %s review #%s complete: status=%s rounds=%s findings=%s",
        job.pr_id, review.id, outcome.status, outcome.rounds_used, outcome.converged_count,
    )
    return review


_JUDGE_SYSTEM = """\
You are the coordinator of an agent fight: two independent code reviewers argue
over one pull request until they agree or the round cap is reached. You have just
been handed both reviewers' latest reviews.

You do NOT review the code, and you do NOT rewrite, merge, or improve anyone's
findings. Their words go to the pull request as written. Your only decision is
whether the fight is over.

CONVERGED = nothing is left for the two of them to argue about. Work through
every item in both reviews and settle it:

  both assert it (issue / agree / revised)      -> SETTLED
  one asserts it, the other withdrew (resolved) -> SETTLED
  one asserts it, the other marked "disagree"   -> NOT SETTLED, they are still
                                                   fighting over it
  one asserts it, and the other filed findings
    this round but never addressed this item    -> NOT SETTLED. They engaged
                                                   with the review and ducked
                                                   this one; make them answer.
  one asserts it, and the other filed NO
    findings at all this round                  -> SETTLED. They reviewed and
                                                   had nothing; there is no
                                                   dispute to resolve, and
                                                   another round buys nothing.
  neither asserts anything                      -> SETTLED

Judge the substance, not the anchor. The two reviewers may describe the same
issue at different lines or in different words — that is ONE item, and if they
agree on it, it is settled. Do not call a fight unsettled over wording.

Also cast the reviewer's vote on the pull request:
  10   approve                   no findings survive
   5   approve with suggestions  only minor/nit findings survive
  -5   wait for author           any surviving critical or major finding, or the
                                 round cap was reached with the fight unsettled
 -10   reject                    egregious and clearly unmergeable; prefer -5

Respond with ONLY this JSON object. No prose, no code fences:
{"converged": <bool>,
 "reason": "<one line: what is still contested, or that nothing is>",
 "vote": <10|5|-5|-10>,
 "vote_reason": "<one line>"}
"""


def _proxy_judge(settings):
    """The coordinator LLM judge, or None when the coord proxy is unconfigured
    (run_review then uses the deterministic fallback). Returns a callable
    judge(left, right, round_no, round_cap) -> (CoordinatorDecision, Usage) that
    fails closed: any proxy error or unparseable ruling raises ReviewFailure, so
    a garbled ruling can never produce a posted review."""
    if not settings.coord_proxy_url or not settings.coord_proxy_key:
        return None

    from app.coordinator import parse_coordinator_decision
    from app.proxy import ProxyClient
    from app.review.models import ReviewFailure

    def _finding_payload(f) -> dict:
        return {
            "file": f.file, "line": f.line, "severity": f.severity,
            "verdict": f.verdict, "claim": f.claim, "suggestion": f.suggestion,
            "evidence": f.evidence, "response": f.response,
        }

    async def judge(left, right, round_no: int, round_cap: int):
        payload = json.dumps(
            {
                "round": round_no,
                "round_cap": round_cap,
                "agent_1_findings": [_finding_payload(f) for f in left],
                "agent_2_findings": [_finding_payload(f) for f in right],
            },
            indent=2,
            ensure_ascii=False,
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
                f"judge call failed in round {round_no}: {exc}",
                round_no=round_no, reason="judge call failed",
            ) from exc
        try:
            decision = parse_coordinator_decision(text)
        except ValueError as exc:
            raise ReviewFailure(
                f"judge returned unparseable output in round {round_no}: {exc}",
                round_no=round_no, reason="judge output unparseable",
            ) from exc
        return decision, usage

    return judge
