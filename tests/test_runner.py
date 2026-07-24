"""Runner persistence: `run_and_record` writes a Review row (+ per-round,
per-role token usage) on success and a `failed` row with a traceback on error.

Driven against FakeRunner + the in-memory `session` fixture — no Docker, no proxy.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.container import FakeHandle, FakeRunner
from app.orm import Review, ReviewTokenUsage
from app.runner import ReviewDispatcher, run_and_record
from tests.test_coordinator import (
    _indep,
    _seed,
    _job,
    _settings,
    _RecordingAdo,
)


def _claude_usage_stdout() -> str:
    return json.dumps(
        {
            "type": "result",
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 100, "output_tokens": 10,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20},
        }
    )


def _codex_usage_stdout() -> str:
    return json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 5}}
    )


async def test_run_and_record_persists_success_with_usage(session):
    same = [{"file": "a.py", "line": 10, "claim": "bug", "verdict": "issue"}]
    from app.container import ExecResult

    # Prep (independent) + battle round 1 findings; both agree -> fallback judge
    # converges after round 1.
    handle = FakeHandle(
        files=_seed(indep=same, rounds={1: same}),
        exec_results={
            "claude": ExecResult(rc=0, stdout=_claude_usage_stdout()),
            "codex": ExecResult(rc=0, stdout=_codex_usage_stdout()),
        },
    )
    ado = _RecordingAdo()

    review = await run_and_record(
        session, _job(), _settings(round_cap=3),
        runner=FakeRunner(handle), ado_client=ado, reasoner=None, judge=None,
    )

    # Row finalized to converged with metrics (battle rounds only; prep uncounted).
    assert review.status == "converged"
    assert review.rounds_used == 1
    assert review.converged_count == 1
    assert review.final_review_md and "bug" in review.final_review_md
    assert review.finished_at is not None and review.duration_ms is not None

    # Usage captured for the prep pass (round 0) AND battle round 1, per harness.
    rows = (
        await session.execute(
            select(ReviewTokenUsage).where(ReviewTokenUsage.review_id == review.id)
        )
    ).scalars().all()
    assert {r.round_no for r in rows} == {0, 1}
    assert {r.role for r in rows} == {"claude", "codex"}
    claude_rows = [r for r in rows if r.role == "claude"]
    assert all(r.input_tokens == 100 and r.cache_creation_tokens == 20 for r in claude_rows)
    assert all(r.cost_usd == 0.05 for r in claude_rows)
    assert any(r.role == "codex" and r.input_tokens == 200 for r in rows)


async def test_dispatcher_bounds_concurrency():
    import asyncio
    active = 0
    peak = 0
    gate = asyncio.Event()

    async def fake_run(job):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await gate.wait()
        active -= 1

    d = ReviewDispatcher(run=fake_run, concurrency=2)
    for _ in range(5):
        d.dispatch(_job())
    await asyncio.sleep(0.05)          # let the loop start tasks up to the cap
    assert active == 2 and peak == 2   # only 2 run; the other 3 are queued
    gate.set()
    await asyncio.sleep(0.05)          # drain the queue
    assert peak == 2                   # never exceeded the cap


def test_proxy_judge_none_when_coord_proxy_unconfigured():
    # No coord proxy in the bare _settings() -> run_review uses the fallback.
    from app.runner import _proxy_judge
    assert _proxy_judge(_settings()) is None


async def test_loop_guard_already_reviewed(session):
    """The webhook loop-guard: a non-failed review of the same (pr, commit)
    blocks a re-review; a failed one doesn't; new commit / unknown / other PR
    all pass through."""
    from app.webhooks import _already_reviewed
    session.add(Review(pr_id=42, repo_name="r", status="converged", source_commit="c1"))
    session.add(Review(pr_id=42, repo_name="r", status="failed", source_commit="c2"))
    await session.commit()

    assert await _already_reviewed(session, 42, "c1") is True    # already reviewed -> skip
    assert await _already_reviewed(session, 42, "c2") is False   # prior FAILED -> allow retry
    assert await _already_reviewed(session, 42, "c3") is False   # new commit -> review
    assert await _already_reviewed(session, 42, "") is False     # unknown commit -> best-effort pass
    assert await _already_reviewed(session, 99, "c1") is False   # different PR


async def test_run_and_record_marks_failed_on_error(session):
    class BoomRunner:
        def start(self, job):
            raise RuntimeError("clone exploded")

    with pytest.raises(RuntimeError):
        await run_and_record(
            session, _job(), _settings(),
            runner=BoomRunner(), ado_client=None, reasoner=None,
        )

    review = (await session.execute(select(Review))).scalars().one()
    assert review.status == "failed"
    assert "clone exploded" in review.error
    assert review.finished_at is not None


async def test_run_and_record_harness_failure_never_posts_but_records(session):
    """The core guarantee: a harness crash -> review marked failed, NOTHING
    posted to the PR, and partial token spend still recorded."""
    from app.container import ExecResult
    from app.review.models import ReviewFailure

    # claude crashes rc=1 but its stdout already reported usage for the round.
    handle = FakeHandle(
        exec_results={
            "claude": ExecResult(rc=1, stdout=_claude_usage_stdout(), stderr="401 Unauthorized"),
            "codex": ExecResult(rc=1, stdout="", stderr="boom"),
        }
    )
    ado = _RecordingAdo()

    with pytest.raises(ReviewFailure):
        await run_and_record(
            session, _job(), _settings(round_cap=3),
            runner=FakeRunner(handle), ado_client=ado, reasoner=None,
        )

    review = (await session.execute(select(Review))).scalars().one()
    assert review.status == "failed"
    assert review.final_review_md is None          # no review synthesized/stored
    assert review.error and "exited non-zero" in review.error
    assert ado.posted is None                       # <-- the guarantee: nothing posted

    # Partial usage (claude round 1) was still captured before the abort.
    rows = (
        await session.execute(
            select(ReviewTokenUsage).where(ReviewTokenUsage.review_id == review.id)
        )
    ).scalars().all()
    assert any(r.role == "claude" and r.input_tokens == 100 for r in rows)
