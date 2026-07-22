"""Coordinator: convergence diff, inclusive synthesis, and the full round loop
driven against a FakeRunner (no Docker, no CLIs, no proxy)."""

from __future__ import annotations

import json

from app.container import FINDINGS_DIR, FakeHandle, FakeRunner
from app.coordinator import (
    convergence,
    parse_findings,
    run_review,
    synthesize_final_review,
)
from app.orm import Settings
from app.review.models import Finding, HarnessType
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
        Finding("b.py", 20, "smell", verdict="resolved"),   # disagree
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


def test_synthesize_surfaces_all_sections():
    consensus = [Finding("a.py", 10, "agreed bug", severity="high", verdict="issue")]
    disagreements = convergence(
        [Finding("b.py", 20, "contested", verdict="issue")],
        [Finding("b.py", 20, "contested", verdict="resolved")],
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


# --- full loop against FakeRunner ------------------------------------------ #


def _settings(round_cap: int = 3) -> Settings:
    return Settings(id=1, round_cap=round_cap, clone_credential_kind="token")


def _job():
    return parse_pull_request_event(sample_ado_payload(), _settings())


def _fpath(h: HarnessType, rnd: int) -> str:
    return f"{FINDINGS_DIR}/findings-{h.value}-round{rnd}.json"


def _findings_json(items):
    return json.dumps({"findings": items})


class _RecordingAdo:
    def __init__(self):
        self.posted = None

    async def post_pr_review(self, settings, job, review_md):
        self.posted = review_md


async def test_run_review_early_convergence_round1():
    same = [{"file": "a.py", "line": 10, "claim": "bug", "severity": "high", "verdict": "issue"}]
    handle = FakeHandle(
        files={
            _fpath(HarnessType.CLAUDE, 1): _findings_json(same),
            _fpath(HarnessType.CODEX, 1): _findings_json(same),
        }
    )
    ado = _RecordingAdo()
    review = await run_review(
        _job(), _settings(round_cap=5), runner=FakeRunner(handle), ado_client=ado
    )
    assert "Agreed findings" in review
    assert "bug" in review
    # Converged in round 1 — no round-2 files were ever read.
    assert handle.stopped is True          # teardown ran
    assert ado.posted == review            # posted back
    # The prompt reached each harness via stdin (not a dropped file).
    piped = [s for s in handle.stdin_log if s]
    assert piped and all("findings" in s.lower() for s in piped)


async def test_run_review_cap_hit_surfaces_disagreement():
    claude_r1 = [{"file": "a.py", "line": 10, "claim": "leak", "verdict": "issue"}]
    codex_r1 = [{"file": "b.py", "line": 20, "claim": "naming", "verdict": "issue"}]
    # Round 2: they now collide on a.py:10 with different verdicts.
    claude_r2 = [{"file": "a.py", "line": 10, "claim": "leak", "verdict": "issue"}]
    codex_r2 = [{"file": "a.py", "line": 10, "claim": "leak", "verdict": "resolved"}]

    handle = FakeHandle(
        files={
            _fpath(HarnessType.CLAUDE, 1): _findings_json(claude_r1),
            _fpath(HarnessType.CODEX, 1): _findings_json(codex_r1),
            _fpath(HarnessType.CLAUDE, 2): _findings_json(claude_r2),
            _fpath(HarnessType.CODEX, 2): _findings_json(codex_r2),
        }
    )
    ado = _RecordingAdo()
    review = await run_review(
        _job(), _settings(round_cap=2), runner=FakeRunner(handle), ado_client=ado
    )
    assert "Unresolved disagreements" in review
    assert "a.py:10" in review
    assert "issue" in review and "resolved" in review
    assert handle.stopped is True
    assert ado.posted is not None


async def test_run_review_missing_findings_file_does_not_crash():
    # No findings files seeded at all -> harness "wrote nothing" every round.
    handle = FakeHandle(files={})
    review = await run_review(
        _job(), _settings(round_cap=2), runner=FakeRunner(handle), ado_client=None, post_back=False
    )
    assert "clean" in review
    assert handle.stopped is True


async def test_run_review_uses_reasoner_in_round2():
    calls = []

    async def reasoner(structured: str) -> str:
        calls.append(structured)
        return "FRAMED"

    r1a = [{"file": "a.py", "line": 1, "claim": "x", "verdict": "issue"}]
    r1b = [{"file": "z.py", "line": 9, "claim": "y", "verdict": "issue"}]
    handle = FakeHandle(
        files={
            _fpath(HarnessType.CLAUDE, 1): _findings_json(r1a),
            _fpath(HarnessType.CODEX, 1): _findings_json(r1b),
            _fpath(HarnessType.CLAUDE, 2): _findings_json(r1a),
            _fpath(HarnessType.CODEX, 2): _findings_json(r1b),
        }
    )
    await run_review(
        _job(),
        _settings(round_cap=2),
        runner=FakeRunner(handle),
        ado_client=None,
        reasoner=reasoner,
        post_back=False,
    )
    # Round 2 ran for both harnesses -> reasoner invoked twice.
    assert len(calls) == 2
