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
from app.runner import describe_review_failure, run_and_record
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
        runner=FakeRunner(handle), ado_client=ado, judge=None,
    )

    # Row finalized to converged with metrics (battle rounds only; prep uncounted).
    assert review.status == "converged"
    assert review.rounds_used == 1
    assert review.converged_count == 1
    assert review.final_review_md and "bug" in review.final_review_md
    assert review.created_by == "Ada Lovelace"
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


def test_proxy_judge_none_when_coord_proxy_unconfigured():
    # No coord proxy in the bare _settings() -> run_review uses the fallback.
    from app.runner import _proxy_judge
    assert _proxy_judge(_settings()) is None


async def test_run_and_record_marks_failed_on_error(session):
    class BoomRunner:
        def start(self, job):
            raise RuntimeError("clone exploded")

    with pytest.raises(RuntimeError):
        await run_and_record(
            session, _job(), _settings(),
            runner=BoomRunner(), ado_client=None,
        )

    review = (await session.execute(select(Review))).scalars().one()
    assert review.status == "failed"
    assert "clone exploded" in review.error
    assert review.status_message.startswith("Source: Review worker")
    assert review.finished_at is not None


@pytest.mark.parametrize("exc,expected", [
    (TimeoutError(), "Source: Review worker"),
    (RuntimeError("fatal: couldn't find remote ref refs/heads/gone"), "Source: Repository checkout"),
])
def test_describe_review_failure_for_infrastructure_errors(exc, expected):
    message = describe_review_failure(exc)
    assert expected in message
    assert "What happened:" in message and "Next step:" in message


def test_describe_review_failure_for_agent_timeout():
    from app.review.models import ReviewFailure
    exc = ReviewFailure("claude inactive", harness="claude", round_no=1, reason="inactive")
    message = describe_review_failure(exc)
    assert "Source: Claude review agent" in message
    assert "fight round 1" in message
    assert "produced no output for 30 minutes" in message
    assert "model connection may be stalled" in message


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
            runner=FakeRunner(handle), ado_client=ado,
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


async def test_run_and_record_persists_every_agents_raw_findings(session):
    """The audit trail: what each agent actually wrote, kept beside the comment
    it produced. Added after a review published an approving note as a
    'concern' and the only evidence left was the judge's one-line summary."""
    from app.container import ExecResult
    from app.orm import ReviewFinding

    same = [{"file": "a.py", "line": 10, "claim": "bug", "verdict": "issue"}]
    handle = FakeHandle(
        files=_seed(indep=same, rounds={1: same}),
        exec_results={"claude": ExecResult(rc=0, stdout=""),
                      "codex": ExecResult(rc=0, stdout="")},
    )
    review = await run_and_record(
        session, _job(), _settings(round_cap=3),
        runner=FakeRunner(handle), ado_client=_RecordingAdo(), judge=None,
    )

    rows = (
        await session.execute(
            select(ReviewFinding).where(ReviewFinding.review_id == review.id)
        )
    ).scalars().all()
    # Both harnesses, round 0 (independent) and round 1 (fight).
    assert {(r.role, r.round_no) for r in rows} == {
        ("claude", 0), ("codex", 0), ("claude", 1), ("codex", 1),
    }
    # Stored verbatim, not re-serialized from the parse.
    assert all('"claim": "bug"' in r.raw for r in rows)
