"""Coordinator: convergence diff, inclusive synthesis, and the full round loop
driven against a FakeRunner (no Docker, no CLIs, no proxy)."""

from __future__ import annotations

import json

import pytest

from app.container import (
    DIFF_PATH,
    ExecResult,
    FINDINGS_DIR,
    PR_CONTEXT_PATH,
    WORKDIR,
    FakeHandle,
    FakeRunner,
)
from app.coordinator import (
    HARNESS_INACTIVITY_TIMEOUT,
    build_fight_prompt,
    build_independent_prompt,
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


def test_parse_findings_reads_evidence_suggestion_response():
    raw = json.dumps({"findings": [
        {"file": "a.py", "line": 5, "claim": "bug", "severity": "critical",
         "suggestion": "guard the null", "evidence": "read Foo.cs:40-80",
         "verdict": "disagree", "response": "the service resolves it server-side"},
        {"file": "b.py", "line": 1, "claim": "x"},  # defaults
    ]})
    fs = parse_findings(raw)
    assert fs[0].suggestion == "guard the null"
    assert fs[0].evidence == "read Foo.cs:40-80"
    assert fs[0].response == "the service resolves it server-side"
    assert fs[1].suggestion == "" and fs[1].evidence == "" and fs[1].response == ""


def test_parse_findings_ignores_confidence():
    """Confidence was removed: a harness still emitting it must not break, and
    the number must not resurface anywhere."""
    fs = parse_findings(json.dumps([{"file": "a.py", "line": 1, "claim": "x",
                                     "confidence": 100}]))
    assert len(fs) == 1 and not hasattr(fs[0], "confidence")


def test_synthesize_pr_review_format():
    consensus = [
        Finding("a.py", 10, "leak", severity="critical", verdict="issue",
                suggestion="dispose it", evidence="Foo.cs:12 opens it, never closed"),
        Finding("b.py", 20, "nit", severity="nit", verdict="issue"),
    ]
    md = synthesize_final_review(consensus, [], [], reviewed="`feat` → `main`")
    assert "_Reviewed `feat` → `main`._" in md
    assert "### Critical" in md and "### Nit" in md
    # Critical group renders before Nit group.
    assert md.index("### Critical") < md.index("### Nit")
    assert "confidence" not in md
    assert "**Fix:** dispose it" in md
    # The author can see what the reviewer actually read.
    assert "**Checked:** Foo.cs:12 opens it, never closed" in md


def test_synthesize_quotes_both_sides_of_a_disagreement():
    """A rebuttal is the point of the fight — it must reach the pull request."""
    dis = [Disagreement(
        "a.py", 10,
        Finding("a.py", 10, "creates a duplicate row", verdict="issue",
                response="the POST omits the id"),
        Finding("a.py", 10, "creates a duplicate row", verdict="disagree",
                response="Service.SubmitAsync:22 resolves the existing row first"),
    )]
    md = synthesize_final_review([], dis, [])
    assert "Unresolved disagreements" in md
    assert "the POST omits the id" in md
    assert "Service.SubmitAsync:22 resolves the existing row first" in md
    assert "`disagree`" in md


# --- judge ruling parsing ---------------------------------------------------- #


def test_parse_judge_ruling_basic():
    d = parse_coordinator_decision(json.dumps({
        "converged": False,
        "reason": "reviewer 2 still disputes a.py:10",
        "vote": -5,
        "vote_reason": "unsettled",
    }))
    assert d.converged is False
    assert d.summary == "reviewer 2 still disputes a.py:10"
    assert d.vote == -5


def test_parse_judge_ruling_tolerates_prose_and_fences():
    text = 'Sure:\n```json\n{"converged": true, "reason": "nothing contested"}\n```'
    assert parse_coordinator_decision(text).converged is True


def test_parse_judge_ruling_invalid_raises():
    with pytest.raises(ValueError):
        parse_coordinator_decision("no json here at all")


def test_parse_judge_ruling_without_converged_raises():
    """A ruling that does not answer the one question it was asked is garbage,
    and fails the review rather than silently ending the fight."""
    with pytest.raises(ValueError):
        parse_coordinator_decision(json.dumps({"vote": 10, "reason": "looks fine"}))


def test_parse_judge_ruling_vote_parsed_and_clamped():
    d = parse_coordinator_decision(json.dumps({
        "converged": True, "vote": 5, "vote_reason": "minor only",
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


def _judge(converged, vote=None):
    """A fake judge: rules on the fight, supplies no findings (the real one
    can't either — the review's content comes from the agents)."""
    async def j(left, right, round_no, round_cap):
        return CoordinatorDecision(converged=converged, vote=vote)
    return j


def _seed(indep=None, rounds=None):
    """Build a FakeHandle.files dict seeding the independent (prep) findings and
    each fight round. A value may be one items list (both harnesses wrote the
    same thing) or a (claude_items, codex_items) tuple when they differ."""
    def put(files, path_for, items):
        left, right = items if isinstance(items, tuple) else (items, items)
        files[path_for(HarnessType.CLAUDE)] = _findings_json(left)
        files[path_for(HarnessType.CODEX)] = _findings_json(right)

    files: dict[str, str] = {}
    if indep is not None:
        put(files, _indep, indep)
    for rnd, items in (rounds or {}).items():
        put(files, lambda h, r=rnd: _fpath(h, r), items)
    return files


class _RecordingAdo:
    def __init__(self):
        self.posted = None
        self.voted = None
        self.context_fetched = False

    async def fetch_pr_context(self, settings, job):
        self.context_fetched = True
        return f"# Pull request !{job.pr_id}\n\n## Comment threads (0)\n"

    async def post_pr_review(self, settings, job, review_md):
        self.posted = review_md
        return 1

    async def set_pr_vote(self, settings, job, vote):
        self.voted = vote


async def test_prep_phase_then_judge_converges_round1():
    bug = [{"file": "a.py", "line": 10, "claim": "bug", "severity": "major", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=bug, rounds={1: bug}))
    ado = _RecordingAdo()
    outcome = await run_review(
        _job(), _settings(round_cap=5), runner=FakeRunner(handle),
        ado_client=ado, judge=_judge(True),
    )
    assert outcome.status == "converged"
    assert outcome.rounds_used == 1           # fight rounds; prep is not counted
    assert outcome.converged_count == 1
    assert "Agreed findings" in outcome.review_md and "bug" in outcome.review_md
    assert ado.posted == outcome.review_md
    assert handle.stopped is True
    # The independent prep pass ran (its findings file was written to/read).
    assert _indep(HarnessType.CLAUDE) in handle.files
    # The pull request (description + every reply) was pulled and handed over.
    assert ado.context_fetched is True
    assert PR_CONTEXT_PATH in handle.files and DIFF_PATH in handle.files
    # A major agreed finding -> "wait for author" (-5), cast on the PR.
    assert outcome.vote == -5
    assert ado.voted == -5
    assert "Reviewer vote:" in outcome.review_md


async def test_cap_hit_surfaces_disagreement():
    """Two reviewers who never settle: the cap ends it and BOTH arguments ship."""
    holds = [{"file": "a.py", "line": 10, "claim": "leak", "verdict": "issue",
              "response": "nothing closes the handle"}]
    rejects = [{"file": "a.py", "line": 10, "claim": "leak", "verdict": "disagree",
                "response": "Foo.cs:88 disposes it in the finally"}]
    fight = (holds, rejects)
    handle = FakeHandle(files=_seed(indep=fight, rounds={1: fight, 2: fight}))
    ado = _RecordingAdo()
    outcome = await run_review(
        _job(), _settings(round_cap=2), runner=FakeRunner(handle),
        ado_client=ado, judge=_judge(False),
    )
    assert outcome.status == "capped"
    assert outcome.rounds_used == 2
    assert outcome.disagreement_count == 1
    assert "Unresolved disagreements" in outcome.review_md
    assert "a.py:10" in outcome.review_md
    assert "nothing closes the handle" in outcome.review_md
    assert "Foo.cs:88 disposes it in the finally" in outcome.review_md
    assert ado.posted is not None
    # Unresolved disagreement -> wait for author (-5).
    assert outcome.vote == -5 and ado.voted == -5


async def test_judge_supplied_vote_is_honored():
    bug = [{"file": "a.py", "line": 1, "claim": "bug", "severity": "minor",
            "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=bug, rounds={1: bug}))
    ado = _RecordingAdo()

    async def judge(left, right, round_no, round_cap):
        # Judge explicitly approves-with-suggestions despite a finding.
        return CoordinatorDecision(
            converged=True, vote=5, vote_reason="minor only, safe to merge",
        )

    outcome = await run_review(
        _job(), _settings(round_cap=2), runner=FakeRunner(handle),
        ado_client=ado, judge=judge,
    )
    assert outcome.vote == 5           # judge's vote used, not re-derived
    assert ado.voted == 5
    assert "safe to merge" in outcome.review_md
    # The judge supplied no findings, and could not have: the review is the
    # agents' own text.
    assert "bug" in outcome.review_md


async def test_review_content_comes_from_agents_not_the_judge():
    """The judge cannot add, drop, or reword a finding — even if it tries."""
    real = [{"file": "a.py", "line": 7, "claim": "agent wrote this", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=real, rounds={1: real}))
    ado = _RecordingAdo()

    async def meddling_judge(left, right, round_no, round_cap):
        return CoordinatorDecision(converged=True, summary="judge wrote this")

    outcome = await run_review(
        _job(), _settings(round_cap=1), runner=FakeRunner(handle),
        ado_client=ado, judge=meddling_judge,
    )
    assert "agent wrote this" in outcome.review_md
    assert "judge wrote this" not in outcome.review_md


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


async def test_harness_inactivity_timeout_is_30_minutes_and_is_not_automatically_retried():
    class TimeoutHandle(FakeHandle):
        calls = 0

        async def exec(self, argv, **kwargs):
            if argv and argv[0] == "claude":
                self.calls += 1
                raise TimeoutError
            return await super().exec(argv, **kwargs)

    handle = TimeoutHandle()
    with pytest.raises(ReviewFailure) as ei:
        await run_review(_job(), _settings(), runner=FakeRunner(handle), ado_client=_RecordingAdo())
    assert HARNESS_INACTIVITY_TIMEOUT == 30 * 60
    assert handle.calls == 1
    assert "no output for 30 minutes" in str(ei.value)


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

    async def boom_judge(left, right, round_no, round_cap):
        raise ReviewFailure("judge blew up", round_no=round_no, reason="judge call failed")

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


# --- what actually reaches each fighter ------------------------------------- #


def test_independent_prompt_hands_over_the_whole_pull_request():
    prompt = build_independent_prompt(_job(), "diff --git a/x b/x", "/f.json", round_cap=3)
    assert WORKDIR in prompt          # the repo, checked out
    assert DIFF_PATH in prompt        # the diff on disk
    assert PR_CONTEXT_PATH in prompt  # description + every comment and reply
    assert "$ADO_PR_API" in prompt    # and the live PR API
    assert "diff --git a/x b/x" in prompt
    assert '"evidence"' in prompt and "confidence" not in prompt


def test_independent_prompt_truncates_a_huge_diff_to_the_file():
    from app.coordinator import MAX_INLINE_DIFF
    prompt = build_independent_prompt(_job(), "x" * (MAX_INLINE_DIFF + 5000), "/f.json",
                                      round_cap=3)
    assert "diff truncated here" in prompt
    assert len(prompt) < MAX_INLINE_DIFF + 5000


def test_fight_prompt_carries_the_rival_verbatim_and_the_code():
    own = [Finding("a.py", 1, "my claim", verdict="issue", evidence="I read Foo.cs")]
    other = [Finding("b.py", 2, "their claim", verdict="issue",
                     response="their exact words, unparaphrased")]
    dis = [Disagreement("c.py", 3, Finding("c.py", 3, "contested", verdict="issue"),
                        Finding("c.py", 3, "contested", verdict="disagree"))]
    prompt = build_fight_prompt(
        _job(), own=own, other=other, disagreements=dis,
        unanswered=[Finding("d.py", 4, "ignored by them", verdict="issue")],
        round_no=2, round_cap=3, findings_path="/f.json",
    )
    # The rival's argument crosses intact — no coordinator paraphrase in between.
    assert "their claim" in prompt and "their exact words, unparaphrased" in prompt
    assert "my claim" in prompt
    assert "contested" in prompt and "ignored by them" in prompt
    # And the fighter still has the code to settle it with.
    assert WORKDIR in prompt and DIFF_PATH in prompt and PR_CONTEXT_PATH in prompt
    assert "round 2 of 3" in prompt
    assert "re-read the code" in prompt.lower()


async def test_both_fighters_see_the_same_opponent_state():
    """No first-mover advantage: within a round, each agent argues against the
    other's PREVIOUS findings, not against whatever the other just wrote."""
    indep = ([{"file": "a.py", "line": 1, "claim": "claude-round0", "verdict": "issue"}],
             [{"file": "b.py", "line": 2, "claim": "codex-round0", "verdict": "issue"}])
    rnd1 = ([{"file": "a.py", "line": 1, "claim": "claude-round1", "verdict": "issue"}],
            [{"file": "b.py", "line": 2, "claim": "codex-round1", "verdict": "issue"}])
    handle = FakeHandle(files=_seed(indep=indep, rounds={1: rnd1}))
    await run_review(
        _job(), _settings(round_cap=1), runner=FakeRunner(handle),
        ado_client=None, judge=_judge(True), post_back=False,
    )
    fight_prompts = [p for p in handle.stdin_log if p and "agent fight over this" in p]
    assert len(fight_prompts) == 2
    # Codex ran second, but must NOT have been shown Claude's fresh round-1 text.
    assert "claude-round1" not in fight_prompts[1]
    assert "claude-round0" in fight_prompts[1]


async def test_harnesses_get_ado_credentials_to_read_the_pr():
    bug = [{"file": "a.py", "line": 1, "claim": "x", "verdict": "issue"}]
    handle = FakeHandle(files=_seed(indep=bug, rounds={1: bug}))
    settings = _settings(round_cap=1)
    settings.ado_pat = "pat-123"
    await run_review(
        _job(), settings, runner=FakeRunner(handle),
        ado_client=None, judge=_judge(True), post_back=False,
    )
    env = handle.env_log[-1]
    assert env["ADO_AUTH_HEADER"].startswith("Basic ")
    assert env["ADO_PR_API"].endswith("/pullRequests/42")


def test_fight_prompt_orients_open_items_to_the_recipient():
    """The same Disagreement is 'you vs them' for one fighter and the reverse
    for the other — mislabel it and an agent argues against its own position."""
    mine = Finding("c.py", 3, "contested", verdict="issue", response="I hold")
    theirs = Finding("c.py", 3, "contested", verdict="disagree", response="they hold")
    dis = [Disagreement("c.py", 3, mine, theirs)]
    unanswered = [Finding("d.py", 4, "mine, ignored", verdict="issue")]

    left = build_fight_prompt(
        _job(), own=[mine, unanswered[0]], other=[theirs], disagreements=dis,
        unanswered=unanswered, round_no=1, round_cap=3, findings_path="/f.json",
        own_is_left=True,
    )
    right = build_fight_prompt(
        _job(), own=[theirs], other=[mine, unanswered[0]], disagreements=dis,
        unanswered=unanswered, round_no=1, round_cap=3, findings_path="/f.json",
        own_is_left=False,
    )
    assert '"your_response": "I hold"' in left
    assert '"their_response": "they hold"' in left
    # Flipped for the other fighter.
    assert '"your_response": "they hold"' in right
    assert '"their_response": "I hold"' in right
    # And an unanswered item is labelled by who raised it.
    assert "yours (they never responded)" in left
    assert "theirs (you never responded)" in right


def test_prompts_forbid_filing_praise_as_a_finding():
    """PR 4650 shipped 'the core fix is correct and well-scoped' as a Nit with
    'No change required' — an approval note published as a concern."""
    prompt = build_independent_prompt(_job(), "diff", "/f.json", round_cap=3)
    assert "ASSERTS A DEFECT" in prompt
    assert "no change required" in prompt.lower()
    assert "empty list" in prompt.lower()


def test_fight_prompt_pins_withdrawal_to_the_resolved_verdict():
    """The same run withdrew an item by rewriting `claim` into an explanation of
    why the code was fine, while leaving an affirming verdict — so it rendered
    as a live concern."""
    prompt = build_fight_prompt(
        _job(), own=[], other=[], disagreements=[], unanswered=[],
        round_no=1, round_cap=3, findings_path="/f.json",
    )
    assert "WITHDRAWING IS `resolved`" in prompt
    assert "everything except \"resolved\" is read as you still" in prompt
