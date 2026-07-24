"""Coordinator: convergence diff, inclusive synthesis, and the full round loop
driven against a FakeRunner (no Docker, no CLIs, no proxy)."""

from __future__ import annotations

import json

import pytest

from app.container import ExecResult, FINDINGS_DIR, FakeHandle, FakeRunner
from app.coordinator import (
    convergence,
    parse_coordinator_decision,
    parse_findings,
    run_review,
    synthesize_final_review,
)
from app.orm import Settings
from app.review.models import (
    CoordinatorDecision,
    Disagreement,
    Finding,
    HarnessType,
    ReviewFailure,
)
from tests.conftest import sample_ado_payload
from app.ado import parse_pull_request_event


# --- pure helpers ---------------------------------------------------------- #


def test_parse_findings_tolerant():
    raw = json.dumps(
        {
            "findings": [
                {"file": "a.py", "line": 10, "claim": "bug", "severity": "high", "verdict": "issue"},
                {"file": "b.py", "claim": "no line ok"},
                {"line": 5, "claim": "no file dropped"},
                "garbage",
                {"file": "c.py", "line": "notint", "claim": "bad line coerced to 0"},
            ]
        }
    )
    findings = parse_findings(raw)
    assert len(findings) == 3
    assert findings[0].file == "a.py" and findings[0].line == 10
    assert findings[2].line == 0


def test_parse_findings_bad_json_is_empty():
    assert parse_findings("not json") == []


def test_convergence_agree_disagree_unmatched():
    left = [
        Finding("a.py", 10, "bug", verdict="issue"),
        Finding("b.py", 20, "smell", verdict="issue"),
        Finding("c.py", 30, "only-left", verdict="issue"),
    ]
    right = [
        Finding("a.py", 10, "bug", verdict="issue"),        # agree
        Finding("b.py", 20, "smell", verdict="disagree"),   # explicit open dispute
        Finding("d.py", 40, "only-right", verdict="issue"), # unmatched
    ]
    result = convergence(left, right)
    assert {f.file for f in result.converged} == {"a.py"}
    assert {d.file for d in result.disagreements} == {"b.py"}
    assert {f.file for f in result.unmatched} == {"c.py", "d.py"}
    assert not result.fully_converged


def test_convergence_fully_converged():
    f = [Finding("a.py", 1, "x", verdict="issue")]
    assert convergence(f, list(f)).fully_converged


def test_convergence_agree_and_revised_count_as_agreement():
    left = [
        Finding("a.py", 10, "bug", verdict="issue"),
        Finding("b.py", 20, "smell", verdict="issue"),
    ]
    right = [
        Finding("a.py", 10, "bug", verdict="agree"),      # agreement, not a clash
        Finding("b.py", 20, "smell", verdict="revised"),  # also affirming
    ]
    r = convergence(left, right)
    assert {f.file for f in r.converged} == {"a.py", "b.py"}
    assert r.disagreements == []


def test_convergence_both_resolved_reports_nothing():
    left = [Finding("a.py", 1, "x", verdict="resolved")]
    right = [Finding("a.py", 1, "x", verdict="resolved")]
    r = convergence(left, right)
    assert r.converged == [] and r.disagreements == [] and r.unmatched == []


def test_convergence_issue_vs_resolved_is_agreement_not_disagreement():
    # One side still calls it an issue, the other marked it resolved (settled in
    # the debate). That is NOT an open disagreement — the reviewers converged;
    # only an explicit `disagree` keeps an item open. (Regression: these used to
    # be surfaced under "Unresolved disagreements".)
    left = [Finding("a.py", 1, "misleading coverage", verdict="issue")]
    right = [Finding("a.py", 1, "misleading coverage", verdict="resolved")]
    r = convergence(left, right)
    assert r.disagreements == []
    assert {f.file for f in r.converged} == {"a.py"}
    assert r.converged[0].claim == "misleading coverage"  # substantive side kept


def test_convergence_nit_and_agree_is_agreement():
    # (agent 1: nit, agent 2: agree) — both affirm a minor concern -> converged.
    left = [Finding("h.ts", 276, "duplication nit", severity="nit", verdict="nit")]
    right = [Finding("h.ts", 276, "duplication nit", severity="nit", verdict="agree")]
    r = convergence(left, right)
    assert r.disagreements == []
    assert {f.file for f in r.converged} == {"h.ts"}


def test_convergence_explicit_disagree_is_the_only_open_dispute():
    left = [Finding("a.py", 1, "x", verdict="issue")]
    right = [Finding("a.py", 1, "x", verdict="disagree")]
    r = convergence(left, right)
    assert len(r.disagreements) == 1
    assert not r.fully_converged


def test_convergence_one_sided_resolved_drops():
    left = [Finding("a.py", 1, "x", verdict="resolved")]
    r = convergence(left, [])
    assert r.unmatched == []  # a withdrawn one-sided item is not surfaced


def test_convergence_richer_finding_kept():
    left = [Finding("a.py", 1, "short", verdict="issue")]
    right = [Finding("a.py", 1, "much longer explanation", verdict="agree",
                     suggestion="do the thing")]
    conv = convergence(left, right).converged
    assert conv[0].suggestion == "do the thing"


def test_synthesize_surfaces_all_sections():
    consensus = [Finding("a.py", 10, "agreed bug", severity="high", verdict="issue")]
    disagreements = convergence(
        [Finding("b.py", 20, "contested", verdict="issue")],
        [Finding("b.py", 20, "contested", verdict="disagree")],
    ).disagreements
    unmatched = [Finding("c.py", 30, "one-sided concern", verdict="issue")]
    md = synthesize_final_review(consensus, disagreements, unmatched)
    assert "Agreed findings" in md
    assert "agreed bug" in md
    assert "Unresolved disagreements" in md
    assert "issue" in md and "resolved" in md  # both verdicts surfaced
    assert "Additional concerns" in md
    assert "one-sided concern" in md


def test_synthesize_clean_when_empty():
    assert "clean" in synthesize_final_review([], [], [])


def test_parse_findings_reads_confidence_and_suggestion():
    raw = json.dumps({"findings": [
        {"file": "a.py", "line": 5, "claim": "bug", "severity": "critical",
         "confidence": 92, "suggestion": "guard the null"},
        {"file": "b.py", "line": 1, "claim": "x"},  # defaults
    ]})
    fs = parse_findings(raw)
    assert fs[0].confidence == 92 and fs[0].suggestion == "guard the null"
    assert fs[1].confidence == 100 and fs[1].suggestion == ""


def test_synthesize_pr_review_format():
    consensus = [
        Finding("a.py", 10, "leak", severity="critical", verdict="issue",
                confidence=95, suggestion="dispose it"),
        Finding("b.py", 20, "nit", severity="nit", verdict="issue", confidence=80),
    ]
    md = synthesize_final_review(consensus, [], [], reviewed="`feat` → `main`")
    assert "_Reviewed `feat` → `main`._" in md
    assert "### Critical" in md and "### Nit" in md
    # Critical group renders before Nit group.
    assert md.index("### Critical") < md.index("### Nit")
    assert "_(confidence 95)_" in md
    assert "**Fix:** dispose it" in md


# --- coordinator judgment parsing ------------------------------------------ #


def test_parse_coordinator_decision_basic():
    d = parse_coordinator_decision(json.dumps({
        "converged": False,
        "agreed": [{"file": "a.py", "line": 10, "claim": "bug",
                    "severity": "major", "suggestion": "fix it"}],
        "disagreed": [{"file": "b.py", "line": 5, "claim": "maybe",
                       "agent1": "issue", "agent2": "not a bug"}],
        "summary": "one open item",
    }))
    assert d.converged is False
    assert len(d.agreed) == 1 and d.agreed[0].file == "a.py"
    assert d.agreed[0].suggestion == "fix it"
    assert len(d.disagreed) == 1
    assert d.disagreed[0].left.verdict == "issue"
    assert d.disagreed[0].right.verdict == "not a bug"


def test_parse_coordinator_decision_tolerates_prose_and_fences():
    text = 'Sure:\n```json\n{"converged": true, "agreed": [], "disagreed": []}\n```'
    d = parse_coordinator_decision(text)
    assert d.converged is True and d.agreed == [] and d.disagreed == []


def test_parse_coordinator_decision_invalid_raises():
    with pytest.raises(ValueError):
        parse_coordinator_decision("no json here at all")


def test_parse_coordinator_decision_vote_parsed_and_clamped():
    d = parse_coordinator_decision(json.dumps({
        "converged": True, "agreed": [], "disagreed": [],
        "vote": 5, "vote_reason": "minor only",
    }))
    assert d.vote == 5 and d.vote_reason == "minor only"
    # An out-of-range vote is dropped (None -> the loop derives one).
    d2 = parse_coordinator_decision(json.dumps({"converged": True, "vote": 3}))
    assert d2.vote is None


def test_derive_vote_mapping():
    from app.coordinator import _derive_vote
    from app.review.models import Disagreement
    # clean
    assert _derive_vote([], [])[0] == 10
    # minor/nit only
    assert _derive_vote([Finding("a", 1, "x", severity="nit")], [])[0] == 5
    # major/critical
    assert _derive_vote([Finding("a", 1, "x", severity="critical")], [])[0] == -5
    # any unresolved disagreement -> wait for author, regardless of severities
    dis = [Disagreement("a", 1, Finding("a", 1, "x"), Finding("a", 1, "x", verdict="disagree"))]
    assert _derive_vote([Finding("a", 2, "y", severity="nit")], dis)[0] == -5


# --- full loop against FakeRunner ------------------------------------------ #


def _settings(round_cap: int = 3) -> Settings:
    return Settings(id=1, round_cap=round_cap, clone_credential_kind="token")


def _job():
    return parse_pull_request_event(sample_ado_payload(), _settings())


def _fpath(h: HarnessType, rnd: int) -> str:
    return f"{FINDINGS_DIR}/findings-{h.value}-round{rnd}.json"


def _indep(h: HarnessType) -> str:
    return f"{FINDINGS_DIR}/findings-{h.value}-independent.json"


def _findings_json(items):
    return json.dumps({"findings": items})


def _judge(converged, agreed=(), disagreed=()):
    """A fake coordinator judge that ignores inputs and returns a canned
    decision — lets loop tests exercise the new structure without an LLM."""
    async def j(left, right, round_no):
        return CoordinatorDecision(
            converged=converged, agreed=list(agreed), disagreed=list(disagreed)
        )
    return j


def _seed(indep=None, rounds=None):
    """Build a FakeHandle.files dict seeding the independent (prep) findings and
    each battle round for BOTH harnesses. `indep`/each round value is the items
    list posted by both harnesses (same for both, for brevity)."""
    files = {}
    if indep is not None:
        files[_indep(HarnessType.CLAUDE)] = _findings_json(indep)
        files[_indep(HarnessType.CODEX)] = _findings_json(indep)
    for rnd, items in (rounds or {}).items():
        files[_fpath(HarnessType.CLAUDE, rnd)] = _findings_json(items)
        files[_fpath(HarnessType.CODEX, rnd)] = _findings_json(items)
    return files


class _RecordingAdo:
    def __init__(self):
        self.posted = None
        self.voted = None

    async def post_or_update_pr_review(self, settings, job, review_md):
        self.posted = review_md
        return "created"

    async def set_pr_vote(self, settings, job, vote):
        self.voted = vote


async def test_prep_phase_then_judge_converges_round1():
    bug = [{"file": "a.py", "line": 10, "claim": "bug", "severity": "major", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=bug, rounds={1: bug}))
    ado = _RecordingAdo()
    agreed = [Finding("a.py", 10, "bug", severity="major", verdict="issue")]
    outcome = await run_review(
        _job(), _settings(round_cap=5), runner=FakeRunner(handle),
        ado_client=ado, judge=_judge(True, agreed=agreed),
    )
    assert outcome.status == "converged"
    assert outcome.rounds_used == 1           # battle rounds; prep is not counted
    assert outcome.converged_count == 1
    assert "Agreed findings" in outcome.review_md and "bug" in outcome.review_md
    assert ado.posted == outcome.review_md
    assert handle.stopped is True
    # The independent prep pass ran (its findings file was written to/read).
    assert _indep(HarnessType.CLAUDE) in handle.files
    # A major agreed finding -> "wait for author" (-5), cast on the PR.
    assert outcome.vote == -5
    assert ado.voted == -5
    assert "Reviewer vote:" in outcome.review_md


async def test_cap_hit_surfaces_disagreement():
    leak = [{"file": "a.py", "line": 10, "claim": "leak", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=leak, rounds={1: leak, 2: leak}))
    ado = _RecordingAdo()
    dis = [Disagreement(
        "a.py", 10,
        Finding("a.py", 10, "leak", verdict="issue"),
        Finding("a.py", 10, "leak", verdict="disagree"),
    )]
    outcome = await run_review(
        _job(), _settings(round_cap=2), runner=FakeRunner(handle),
        ado_client=ado, judge=_judge(False, disagreed=dis),
    )
    assert outcome.status == "capped"
    assert outcome.rounds_used == 2
    assert outcome.disagreement_count == 1
    assert "Unresolved disagreements" in outcome.review_md
    assert "a.py:10" in outcome.review_md
    assert ado.posted is not None
    # Unresolved disagreement -> wait for author (-5).
    assert outcome.vote == -5 and ado.voted == -5


async def test_llm_supplied_vote_is_honored():
    bug = [{"file": "a.py", "line": 1, "claim": "bug", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=bug, rounds={1: bug}))
    ado = _RecordingAdo()

    async def judge(left, right, round_no):
        # Coordinator explicitly approves-with-suggestions despite a finding.
        return CoordinatorDecision(
            converged=True,
            agreed=[Finding("a.py", 1, "bug", severity="minor", verdict="issue")],
            vote=5, vote_reason="minor only, safe to merge",
        )

    outcome = await run_review(
        _job(), _settings(round_cap=2), runner=FakeRunner(handle),
        ado_client=ado, judge=judge,
    )
    assert outcome.vote == 5           # LLM's vote used, not re-derived
    assert ado.voted == 5
    assert "safe to merge" in outcome.review_md


async def test_default_fallback_judge_converges_on_agreement():
    # No judge injected -> deterministic fallback (convergence()); both agree.
    bug = [{"file": "a.py", "line": 1, "claim": "bug", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=bug, rounds={1: bug}))
    ado = _RecordingAdo()
    outcome = await run_review(
        _job(), _settings(round_cap=3), runner=FakeRunner(handle), ado_client=ado
    )
    assert outcome.status == "converged"
    assert outcome.rounds_used == 1
    assert "bug" in outcome.review_md
    assert ado.posted == outcome.review_md


# --- fail-closed: a broken run must NEVER post a (false) clean/consensus ---- #


async def test_harness_nonzero_exit_aborts_and_does_not_post():
    # Both harnesses crash (the 4623 case) in the prep phase. Must raise, post nothing.
    handle = FakeHandle(
        exec_results={
            "claude": ExecResult(rc=1, stdout="", stderr="401 Unauthorized"),
            "codex": ExecResult(rc=1, stdout="", stderr="boom"),
        }
    )
    ado = _RecordingAdo()
    with pytest.raises(ReviewFailure) as ei:
        await run_review(_job(), _settings(round_cap=3), runner=FakeRunner(handle), ado_client=ado)
    assert ei.value.rc == 1
    assert ei.value.reason == "exited non-zero"
    assert ado.posted is None          # <-- nothing posted to the PR
    assert handle.stopped is True       # teardown still ran


async def test_missing_findings_file_aborts_and_does_not_post():
    # Harness exits 0 but writes no findings file -> protocol failure, not clean.
    handle = FakeHandle(files={})       # exec defaults to rc=0, stdout=""
    ado = _RecordingAdo()
    with pytest.raises(ReviewFailure) as ei:
        await run_review(_job(), _settings(round_cap=2), runner=FakeRunner(handle), ado_client=ado)
    assert ei.value.reason == "wrote no findings file"
    assert ado.posted is None
    assert handle.stopped is True


async def test_unparseable_findings_file_aborts_and_does_not_post():
    # Bad JSON in the independent (prep) pass -> abort before any battle round.
    handle = FakeHandle(files={_indep(HarnessType.CLAUDE): "this is not json"})
    ado = _RecordingAdo()
    with pytest.raises(ReviewFailure) as ei:
        await run_review(_job(), _settings(round_cap=2), runner=FakeRunner(handle), ado_client=ado)
    assert "unparseable" in ei.value.reason
    assert ado.posted is None


async def test_judge_error_aborts_and_does_not_post():
    # The coordinator judge itself fails -> fail closed, nothing posted.
    bug = [{"file": "a.py", "line": 1, "claim": "bug", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=bug, rounds={1: bug}))
    ado = _RecordingAdo()

    async def boom_judge(left, right, round_no):
        raise ReviewFailure("coordinator judge blew up", round_no=round_no, reason="judge call failed")

    with pytest.raises(ReviewFailure) as ei:
        await run_review(
            _job(), _settings(round_cap=2), runner=FakeRunner(handle),
            ado_client=ado, judge=boom_judge,
        )
    assert ei.value.reason == "judge call failed"
    assert ado.posted is None


async def test_genuine_empty_findings_is_clean_and_posts():
    # BOTH harnesses ran fine and wrote present, valid, EMPTY findings files
    # (prep + round 1). Fallback judge converges -> a real clean, which posts.
    handle = FakeHandle(files=_seed(indep=[], rounds={1: []}))
    ado = _RecordingAdo()
    outcome = await run_review(
        _job(), _settings(round_cap=2), runner=FakeRunner(handle), ado_client=ado
    )
    assert "clean" in outcome.review_md
    assert outcome.status == "converged"
    assert ado.posted == outcome.review_md   # a real clean DOES post
    assert handle.stopped is True
    # Clean review -> Approve (+10) cast on the PR.
    assert outcome.vote == 10
    assert ado.voted == 10


async def test_reasoner_frames_each_battle_round():
    calls = []

    async def reasoner(structured: str) -> str:
        calls.append(structured)
        return "FRAMED"

    bug = [{"file": "a.py", "line": 1, "claim": "x", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=bug, rounds={1: bug}))
    await run_review(
        _job(), _settings(round_cap=1), runner=FakeRunner(handle),
        ado_client=None, reasoner=reasoner, judge=_judge(False), post_back=False,
    )
    # One battle round, two harnesses -> reasoner invoked once per harness.
    assert len(calls) == 2
