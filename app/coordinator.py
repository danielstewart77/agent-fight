"""The round-loop coordinator: independent review, cross-exposure debate,
convergence tracking, and inclusive final synthesis.

Implements the core loop from plans/code-review-consensus.md:

  Round 1  — each harness reviews the diff blind, writing its own findings file.
  Round 2+ — the coordinator builds a per-harness prompt containing that
             harness's own prior findings plus the other's, delivered
             conversationally (reframed by the coordinator, a reasoning agent),
             and each harness writes its next findings file.
  Each round — diff the two latest files item by item (same file, same line).
             Matching verdicts get pulled into a running consensus the
             coordinator alone owns; disagreements carry forward.
  Terminate — converged, or the configurable round cap. No human tiebreak: the
             final review is the inclusive union of every agreed item plus every
             distinct unresolved concern, with each disagreement surfaced.

The pure functions (`parse_findings`, `convergence`, `build_cross_exposure_input`,
`synthesize_final_review`) hold the logic and are fully unit-testable. Only the
natural-language reframing of a cross-exposure prompt goes through `reasoner`
(backed by the proxy in production, a stub in tests), and only `run_review`
touches the container.
"""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Optional, Union

from app.container import (
    FINDINGS_DIR,
    WORKDIR,
    ContainerRunner,
    compute_diff,
    prepare_repo,
    provision_harnesses,
)
from app.harness import (
    build_argv,
    extract_usage,
    harness_env,
    harness_error_line,
    harness_model,
)
from app.orm import Settings
from app.review.models import (
    ConvergenceResult,
    CoordinatorDecision,
    Disagreement,
    Finding,
    HarnessType,
    ReviewFailure,
    ReviewJob,
    ReviewOutcome,
    RoleRoundUsage,
    Usage,
)

# How much harness stderr to keep in logs / the failure record (chars).
_STDERR_EXCERPT = 2000

logger = logging.getLogger("agent_fight.coordinator")

# Per-harness wall-clock cap for a single review round (a hung CLI must not
# block the run forever).
HARNESS_TIMEOUT = 900.0

# A reasoner turns a structured cross-exposure input into conversational prose.
# It may return just the prose (tests / passthrough) or, in production, a
# (prose, Usage) pair so the coordinator's own token spend is accounted for.
Reasoner = Callable[[str], Awaitable[Union[str, "tuple[str, Optional[Usage]]"]]]

# A judge reads both agents' latest findings after a battle round and returns a
# `CoordinatorDecision` (optionally paired with the coord LLM's `Usage`). The
# production judge is an LLM (see runner._proxy_judge); the default is a
# deterministic fallback (see `_fallback_judge`).
Judge = Callable[
    [list["Finding"], list["Finding"], int],
    Awaitable[Union["CoordinatorDecision", "tuple[CoordinatorDecision, Optional[Usage]]"]],
]


def _norm_file(path: str) -> str:
    return path.strip().lstrip("./").replace("\\", "/")


def _key(f: Finding) -> tuple[str, int]:
    return (_norm_file(f.file), f.line)


def parse_findings(raw: str) -> list[Finding]:
    """Tolerant parse of a harness findings file.

    Accepts either a bare list or a `{"findings": [...]}` envelope. Rows missing
    `file`/`claim` or otherwise malformed are dropped with a warning — a
    misbehaving harness must never crash the loop.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("findings file is not valid JSON; treating as empty")
        return []

    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list):
        return []

    out: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        file = item.get("file")
        claim = item.get("claim")
        if not file or not claim:
            continue
        try:
            line = int(item.get("line", 0) or 0)
        except (TypeError, ValueError):
            line = 0
        try:
            confidence = int(item.get("confidence", 100) or 100)
        except (TypeError, ValueError):
            confidence = 100
        out.append(
            Finding(
                file=str(file),
                line=line,
                claim=str(claim),
                severity=str(item.get("severity", "medium")),
                verdict=str(item.get("verdict", "issue")),
                confidence=max(0, min(100, confidence)),
                suggestion=str(item.get("suggestion", "") or ""),
            )
        )
    return out


def parse_coordinator_decision(text: str) -> CoordinatorDecision:
    """Parse the coordinator LLM's JSON judgment into a `CoordinatorDecision`.

    Tolerant of prose/markdown fences around the JSON object, but raises
    ValueError if no usable object is found (the caller turns that into a
    ReviewFailure so a garbled judgment never yields a posted review). Expected
    shape: {"converged": bool, "agreed": [<finding>...],
    "disagreed": [{file,line,claim,agent1,agent2}...], "summary": str}.
    """
    obj = _extract_json_object(text)
    if not isinstance(obj, dict):
        raise ValueError("coordinator judgment is not a JSON object")

    agreed = parse_findings(json.dumps({"findings": obj.get("agreed") or []}))

    disagreed: list[Disagreement] = []
    for d in obj.get("disagreed") or []:
        if not isinstance(d, dict):
            continue
        file = str(d.get("file") or "").strip()
        claim = str(d.get("claim") or "").strip()
        if not file or not claim:
            continue
        try:
            line = int(d.get("line", 0) or 0)
        except (TypeError, ValueError):
            line = 0
        a1 = str(d.get("agent1") or d.get("agent_1") or "issue")[:60]
        a2 = str(d.get("agent2") or d.get("agent_2") or "issue")[:60]
        sev = str(d.get("severity", "medium") or "medium")
        left = Finding(file=file, line=line, claim=claim, severity=sev, verdict=a1)
        right = Finding(file=file, line=line, claim=claim, severity=sev, verdict=a2)
        disagreed.append(Disagreement(file=file, line=line, left=left, right=right))

    converged = bool(obj.get("converged", not disagreed))
    vote = None
    try:
        v = int(obj["vote"])
        vote = v if v in _ALLOWED_VOTES else None
    except (KeyError, TypeError, ValueError):
        vote = None
    return CoordinatorDecision(
        converged=converged,
        agreed=agreed,
        disagreed=disagreed,
        summary=str(obj.get("summary") or ""),
        vote=vote,
        vote_reason=str(obj.get("vote_reason") or ""),
    )


# Azure DevOps reviewer-vote values. We never auto-reject (-10) by default — the
# strongest automated vote is "wait for author"; -10 is available but not emitted
# by _derive_vote.
VOTE_APPROVE = 10
VOTE_APPROVE_SUGGESTIONS = 5
VOTE_WAIT = -5
VOTE_REJECT = -10
_ALLOWED_VOTES = {VOTE_APPROVE, VOTE_APPROVE_SUGGESTIONS, 0, VOTE_WAIT, VOTE_REJECT}
_VOTE_LABELS = {
    10: "Approve", 5: "Approve with suggestions", 0: "No vote",
    -5: "Wait for author", -10: "Reject",
}


def vote_label(vote: Optional[int]) -> str:
    return _VOTE_LABELS.get(vote if vote is not None else 0, "No vote")


def _derive_vote(agreed: list[Finding], disagreed: list[Disagreement]) -> tuple[int, str]:
    """Deterministic vote from the merged findings — the fallback when the
    coordinator LLM did not supply a valid `vote`. Conservative: worst case is
    'wait for author', never an automated reject."""
    if disagreed:
        return VOTE_WAIT, "reviewers did not resolve all disagreements"
    severities = {(f.severity or "").strip().lower() for f in agreed}
    if severities & {"critical", "major"}:
        return VOTE_WAIT, "major or critical issue(s) found"
    if agreed:
        return VOTE_APPROVE_SUGGESTIONS, "only minor/nit suggestions"
    return VOTE_APPROVE, "no issues found"


def _extract_json_object(text: str):
    """json.loads(text), or the first {...} block if the model wrapped it in
    prose / ```json fences. Raises ValueError if nothing parses."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            pass
    raise ValueError("no JSON object found in coordinator output")


# Each latest-round verdict falls into one of three classes. The debate protocol
# (see the round 2+ prompt) asks reviewers to mark each item agree / disagree /
# revised / resolved; harnesses also emit round-1 `issue` and severity-flavored
# labels like `nit`. We classify, rather than string-match, so an open-ended
# vocabulary does not produce false disagreements:
#
#   DISPUTE  — an EXPLICIT, still-open objection. This is the ONLY thing that
#              makes an item an "unresolved disagreement".
#   DISMISS  — the reviewer settled it as a non-issue / closed it (`resolved`,
#              `wontfix`, ...). Not an open dispute — the debate reached closure.
#   AFFIRM   — the reviewer still stands on a concern at some severity (`issue`,
#              `agree`, `revised`, `nit`, a bare severity, or any unknown label).
#
# Crucially `resolved` is NOT a dispute: when one side affirms and the other
# marks it `resolved`, the reviewers converged (one raised it, the other
# acknowledged/closed it) — it must be reported as an agreed finding, never as an
# unresolved disagreement. Only `disagree` keeps an item open.
_DISPUTE_VERDICTS = {"disagree", "dispute", "contest", "contested", "reject", "object"}
_DISMISS_VERDICTS = {
    "resolved", "resolve", "dismiss", "dismissed", "wontfix", "won't fix", "wont fix",
    "invalid", "false-positive", "false positive", "falsepositive", "not-an-issue",
    "not an issue", "nonissue", "non-issue", "no-issue", "duplicate", "withdrawn",
    "retracted", "obsolete",
}


def _verdict_class(f: Finding) -> str:
    v = (f.verdict or "").strip().lower()
    if v in _DISPUTE_VERDICTS:
        return "dispute"
    if v in _DISMISS_VERDICTS:
        return "dismiss"
    return "affirm"


def _richer(a: Finding, b: Finding) -> Finding:
    """Pick the more useful of two findings at the same location for display —
    prefer one with a concrete fix, else the longer claim."""
    if bool(a.suggestion) != bool(b.suggestion):
        return a if a.suggestion else b
    return a if len(a.claim) >= len(b.claim) else b


def _pick_display(lf: Finding, rf: Finding) -> Finding:
    """The finding to show for a converged pair — keep the substantive side.

    If exactly one side affirms (the other dismissed/closed it), show the
    affirming one; otherwise show the richer of the two."""
    lc, rc = _verdict_class(lf), _verdict_class(rf)
    if lc == "affirm" and rc != "affirm":
        return lf
    if rc == "affirm" and lc != "affirm":
        return rf
    return _richer(lf, rf)


def convergence(left: list[Finding], right: list[Finding]) -> ConvergenceResult:
    """Diff two harnesses' latest findings by (file, line), classifying each
    side's verdict (see `_verdict_class`):

    - either side DISPUTES (`disagree`) -> disagreement (both kept). This is the
      only path to an unresolved disagreement;
    - both DISMISS (`resolved`/etc.) -> agreed non-issue, surfaced nowhere;
    - otherwise (both affirm, or one affirms and the other closed it) ->
      converged, an agreed finding;
    - only one side raised it and affirms -> unmatched (a one-reviewer concern).
    """
    left_by = {_key(f): f for f in left}
    right_by = {_key(f): f for f in right}

    converged: list[Finding] = []
    disagreements: list[Disagreement] = []
    unmatched: list[Finding] = []

    for key in left_by.keys() | right_by.keys():
        lf = left_by.get(key)
        rf = right_by.get(key)
        if lf and rf:
            lc, rc = _verdict_class(lf), _verdict_class(rf)
            if lc == "dispute" or rc == "dispute":
                disagreements.append(
                    Disagreement(file=key[0], line=key[1], left=lf, right=rf)
                )
            elif lc == "dismiss" and rc == "dismiss":
                continue  # both settled it as a non-issue — nothing to report
            else:
                converged.append(_pick_display(lf, rf))
        else:
            only = lf or rf
            if _verdict_class(only) == "affirm":  # a closed/disputed one-sider drops
                unmatched.append(only)

    return ConvergenceResult(
        converged=converged, disagreements=disagreements, unmatched=unmatched
    )


def build_cross_exposure_input(
    *,
    own: list[Finding],
    other: list[Finding],
    open_disagreements: list[Disagreement],
) -> str:
    """Deterministically assemble the material for a round 2+ prompt.

    Kept separate from the natural-language framing so the assembly is testable;
    the string it returns is what the `reasoner` reframes conversationally.
    """
    def fmt(fs: list[Finding]) -> list[dict]:
        return [
            {"file": f.file, "line": f.line, "severity": f.severity,
             "verdict": f.verdict, "claim": f.claim}
            for f in fs
        ]

    payload = {
        "your_prior_findings": fmt(own),
        "other_agents_findings": fmt(other),
        "still_open": [
            {
                "file": d.file,
                "line": d.line,
                "your_verdict": d.left.verdict,
                "their_verdict": d.right.verdict,
                "claim": d.left.claim,
            }
            for d in open_disagreements
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: dict[tuple[str, int], Finding] = {}
    for f in findings:
        seen[_key(f)] = f
    return list(seen.values())


# Severity buckets in the order they render (unknown severities fall to the
# end). Matches the org's house vocabulary (see the pr-code-review skill).
_SEVERITY_ORDER = ["critical", "major", "minor", "nit"]


def _sev_rank(severity: str) -> int:
    s = (severity or "").strip().lower()
    return _SEVERITY_ORDER.index(s) if s in _SEVERITY_ORDER else len(_SEVERITY_ORDER)


def _loc(f: Finding) -> str:
    return f"{f.file}:{f.line}" if f.line else f.file


def _render_grouped(findings: list[Finding]) -> list[str]:
    """Findings grouped by severity (Critical→Low), each with confidence + fix."""
    lines: list[str] = []
    order = sorted(findings, key=lambda f: (_sev_rank(f.severity), _key(f)))
    current: str | None = None
    for f in order:
        sev = (f.severity or "medium").strip().lower()
        if sev != current:
            current = sev
            lines.append("")
            lines.append(f"### {sev.capitalize()}")
        lines.append(f"- **{_loc(f)}** _(confidence {f.confidence})_ — {f.claim}")
        if f.suggestion:
            lines.append(f"  - **Fix:** {f.suggestion}")
    return lines


def synthesize_final_review(
    consensus: list[Finding],
    disagreements: list[Disagreement],
    unmatched: list[Finding],
    *,
    reviewed: str | None = None,
) -> str:
    """Inclusive union write-up in the pr-review format: a "reviewed" header,
    findings grouped by severity with confidence + a concrete fix, agreed items
    first, then disagreements surfaced explicitly, then one-reviewer concerns.
    No tiebreak."""
    lines: list[str] = ["# Code Review"]
    if reviewed:
        lines.append(f"_Reviewed {reviewed}._")

    agreed = _dedupe(consensus)
    if agreed:
        lines.append("")
        lines.append("## Agreed findings")
        lines += _render_grouped(agreed)

    if disagreements:
        lines.append("")
        lines.append("## Unresolved disagreements")
        lines.append("")
        for d in sorted(disagreements, key=lambda x: (x.file, x.line)):
            lines.append(
                f"- **{_loc(d.left)}** — {d.left.claim} "
                f"(agent 1: `{d.left.verdict}`, agent 2: `{d.right.verdict}`)"
            )

    if unmatched:
        lines.append("")
        lines.append("## Additional concerns (raised by one reviewer)")
        lines += _render_grouped(unmatched)

    if not (agreed or disagreements or unmatched):
        lines.append("")
        lines.append("_No findings — both reviewers reported the change as clean._")

    return "\n".join(lines).rstrip() + "\n"


def _findings_path(harness: HarnessType, round_no: int) -> str:
    return f"{FINDINGS_DIR}/findings-{harness.value}-round{round_no}.json"


def _independent_path(harness: HarnessType) -> str:
    """Findings file for the blind independent-review prep pass (not a round)."""
    return f"{FINDINGS_DIR}/findings-{harness.value}-independent.json"


def _independent_prompt(diff: str, findings_path: str) -> str:
    return (
        "You are reviewing a pull request. Review the diff below independently "
        "and thoroughly. If you have the agent-fight-review skill, follow it.\n\n"
        f"Write your findings as JSON to {findings_path} — a list of objects, each "
        '{"file","line","claim","severity","confidence","suggestion","verdict":"issue"}. '
        "severity is one of critical|major|minor|nit; confidence is 0-100 (only "
        "report issues you are >=80 confident are real); suggestion is a concrete "
        "fix. Review across the nine dimensions (correctness, readability, "
        "simplicity, consistency, maintainability, test coverage, error handling, "
        "performance, security). Prefer few high-signal findings over nitpicks.\n\n"
        f"Full repo is at {WORKDIR}; read any file you need for context.\n\n"
        "--- DIFF ---\n"
        f"{diff}\n"
    )


async def _default_framing(structured: str) -> str:
    """Fallback reasoner: pass the structured material through unframed."""
    return structured


async def run_review(
    job: ReviewJob,
    settings: Settings,
    *,
    runner: ContainerRunner,
    ado_client,
    harnesses: Optional[list[HarnessType]] = None,
    reasoner: Optional[Reasoner] = None,
    judge: Optional[Judge] = None,
    post_back: bool = True,
) -> ReviewOutcome:
    """Run the full review for one PR and return a `ReviewOutcome`.

    Structure: an unnumbered independent-review prep phase (both agents review
    the diff blind), then up to `settings.round_cap` battle rounds in which each
    agent sees the other's latest findings and the coordinator `judge` decides —
    semantically — what they agree on, what remains disputed, and whether to
    continue. The final review is authored from the last judgment.

    `runner` provides the container; `reasoner` reframes cross-exposure prompts
    conversationally (defaults to passthrough); `judge` is the coordinator LLM
    (defaults to the deterministic `_fallback_judge`); `ado_client` posts the
    result unless `post_back` is False.

    The returned `ReviewOutcome` carries the posted markdown plus the metrics the
    runner persists. Failures raise `ReviewFailure` (recorded as `failed`) — they
    never return an outcome, and nothing is posted.
    """
    harnesses = harnesses or [HarnessType.CLAUDE, HarnessType.CODEX]
    if len(harnesses) != 2:
        raise ValueError("the review expects exactly two harnesses")
    reasoner = reasoner or _default_framing
    judge = judge or _fallback_judge
    round_cap = max(1, int(settings.round_cap))
    h1, h2 = harnesses

    usage_records: list[RoleRoundUsage] = []
    rounds_used = 0

    logger.info(
        "PR %s review starting: repo=%s %s -> %s harnesses=%s battle_rounds<=%s",
        job.pr_id, job.repo_name, job.source_branch, job.target_branch,
        [h.value for h in harnesses], round_cap,
    )

    async def _run_harness(h: HarnessType, prompt: str, fpath: str, round_no: int, phase: str):
        """Run one harness, capture usage, and fail-closed on any fault."""
        argv = build_argv(h, workdir=WORKDIR, model=harness_model(h, settings))
        exec_res = await handle.exec(
            argv, env=harness_env(h, settings), stdin=prompt, timeout=HARNESS_TIMEOUT
        )
        # Capture usage even on failure so a failed run still accounts for spend.
        h_usage = extract_usage(h, exec_res.stdout)
        if not h_usage.is_empty:
            usage_records.append(RoleRoundUsage(h.value, round_no, h_usage))
        # A broken harness must NEVER be synthesized into a clean/consensus review
        # — this raises BEFORE post-back, so nothing reaches the PR.
        return _read_findings_or_fail(
            h, round_no, exec_res, await _read_or_none(handle, fpath),
            usage_records, phase=phase,
        )

    async with runner.start(job) as handle:
        await prepare_repo(
            handle,
            job,
            clone_credential_kind=settings.clone_credential_kind,
            clone_credential_secret=settings.clone_credential_secret,
        )
        await provision_harnesses(handle, settings)
        diff = await compute_diff(handle, job)
        if not diff.strip():
            # Not a failure (the branch may genuinely add nothing), but log it —
            # an empty diff is also the fingerprint of a bad ref/checkout.
            logger.warning("PR %s: computed diff is EMPTY — reviewers will see no changes", job.pr_id)

        prev: dict[HarnessType, list[Finding]] = {}

        # ---- Independent review (prep phase — NOT a round) ------------------ #
        # Both agents review the diff blind. This builds the prerequisites for a
        # debate; it is not a battle round and no coordinator judgment runs yet.
        logger.info("PR %s: independent review (both agents, blind)", job.pr_id)
        for h in harnesses:
            fpath = _independent_path(h)
            prev[h] = await _run_harness(
                h, _independent_prompt(diff, fpath), fpath, round_no=0,
                phase="independent review",
            )

        # ---- Battle rounds -------------------------------------------------- #
        decision: Optional[CoordinatorDecision] = None
        for round_no in range(1, round_cap + 1):
            rounds_used = round_no
            open_disagreements = list(decision.disagreed) if decision else []

            for h in harnesses:
                other = h2 if h is h1 else h1
                fpath = _findings_path(h, round_no)
                structured = build_cross_exposure_input(
                    own=prev.get(h, []),
                    other=prev.get(other, []),
                    open_disagreements=open_disagreements,
                )
                framed, coord_usage = _unpack_framing(await reasoner(structured))
                if coord_usage is not None and not coord_usage.is_empty:
                    usage_records.append(RoleRoundUsage("coordinator", round_no, coord_usage))
                prompt = (
                    "This is a round of a code-review debate. Below are your own "
                    "prior findings, the other reviewer's findings, and the items "
                    "still open. Defend, concede, or revise each — respond by "
                    "writing an updated findings entry marking it "
                    '"agree", "disagree" (with reasoning in claim), "revised", or '
                    '"resolved".\n\n'
                    f"Write the result as JSON to {fpath}.\n\n"
                    f"{framed}\n"
                )
                prev[h] = await _run_harness(
                    h, prompt, fpath, round_no=round_no, phase=f"round {round_no}"
                )

            # The coordinator LLM judges agreement and decides whether to continue.
            decision, judge_usage = _unpack_judgment(
                await judge(prev[h1], prev[h2], round_no)
            )
            if judge_usage is not None and not judge_usage.is_empty:
                usage_records.append(RoleRoundUsage("coordinator", round_no, judge_usage))

            logger.info(
                "PR %s round %s of %s: agreed=%s disagreed=%s converged=%s",
                job.pr_id, round_no, round_cap,
                len(decision.agreed), len(decision.disagreed), decision.converged,
            )
            if decision.converged:
                logger.info("PR %s converged after round %s", job.pr_id, round_no)
                break

        agreed = list(decision.agreed) if decision else []
        disagreed = list(decision.disagreed) if decision else []

        # Reviewer vote: the coordinator's recommendation, else derived from the
        # findings. Included in the comment and cast on the PR below.
        if decision is not None and decision.vote in _ALLOWED_VOTES and decision.vote != 0:
            vote, vote_reason = decision.vote, (decision.vote_reason or vote_label(decision.vote))
        else:
            vote, vote_reason = _derive_vote(agreed, disagreed)

        review_md = synthesize_final_review(
            agreed, disagreed, [],
            reviewed=f"`{job.source_branch}` → `{job.target_branch}`",
        )
        review_md += f"\n\n---\n**Reviewer vote:** {vote_label(vote)} — {vote_reason}\n"

    # Reaching here means every harness in every phase succeeded (any fault
    # raised ReviewFailure above), so this review is safe to post.
    status = "converged" if (decision and decision.converged) else "capped"
    if post_back and ado_client is not None:
        logger.info(
            "PR %s posting review: status=%s rounds=%s agreed=%s disagreed=%s vote=%s(%s)",
            job.pr_id, status, rounds_used, len(agreed), len(disagreed), vote, vote_label(vote),
        )
        await ado_client.post_or_update_pr_review(settings, job, review_md)
        # Voting is best-effort: a failed/rejected vote (e.g. on a completed PR)
        # must not fail an otherwise-successful review.
        try:
            await ado_client.set_pr_vote(settings, job, vote)
            logger.info("PR %s vote cast: %s (%s)", job.pr_id, vote, vote_label(vote))
        except Exception as exc:  # noqa: BLE001 - vote is secondary to the posted review
            logger.warning("PR %s vote NOT cast (%s): %s", job.pr_id, vote_label(vote), exc)
    else:
        logger.info("PR %s review complete (post_back disabled)", job.pr_id)

    return ReviewOutcome(
        review_md=review_md,
        status=status,
        rounds_used=rounds_used,
        converged_count=len(agreed),
        disagreement_count=len(disagreed),
        unmatched_count=0,
        usage=usage_records,
        vote=vote,
        vote_reason=vote_reason,
    )


async def _read_or_none(handle, path: str) -> Optional[str]:
    """Read a findings file, or None if the harness never wrote it."""
    try:
        return await handle.read_file(path)
    except FileNotFoundError:
        return None


def _read_findings_or_fail(
    harness: HarnessType,
    round_no: int,
    exec_res,
    raw: Optional[str],
    usage_records: list[RoleRoundUsage],
    *,
    phase: str = "",
) -> list[Finding]:
    """Validate one harness's output for a phase; raise ReviewFailure on a fault.

    This is the fail-closed gate. Three faults, each of which historically could
    have been silently synthesized into a false "clean" review:
      1. non-zero exit  -> the CLI errored (auth/proxy/crash);
      2. no findings file -> the harness never completed the protocol;
      3. file present but not valid JSON -> garbled output.
    A present, valid, EMPTY findings file is NOT a fault — it is a genuine clean.
    `phase` is the human label ("independent review" or "round N"); `round_no` is
    0 for the independent prep pass.
    """
    label = phase or f"round {round_no}"

    def _fail(reason: str) -> "ReviewFailure":
        stderr = (exec_res.stderr or "")[-_STDERR_EXCERPT:]
        # Codex (and Claude stream-json) report the real error on STDOUT, not
        # stderr — surface the tail of both so the failure is diagnosable.
        stdout_tail = (exec_res.stdout or "")[-_STDERR_EXCERPT:]
        err_line = harness_error_line(harness, exec_res.stdout)
        logger.error(
            "PR review ABORTED — harness %s FAILED in %s (%s), rc=%s. Refusing to "
            "post.\n  harness error: %s\n  stderr tail: %s\n  stdout tail: %s",
            harness.value, label, reason, exec_res.rc,
            err_line or "<none parsed>", stderr or "<empty>", stdout_tail or "<empty>",
        )
        detail = f" — {err_line}" if err_line else ""
        return ReviewFailure(
            f"harness {harness.value} {reason} in {label}{detail}",
            harness=harness.value,
            round_no=round_no,
            rc=exec_res.rc,
            reason=reason,
            usage=list(usage_records),
        )

    if not exec_res.ok:
        raise _fail("exited non-zero")
    if raw is None:
        raise _fail("wrote no findings file")
    try:
        json.loads(raw)
    except (ValueError, TypeError):
        raise _fail("wrote an unparseable (non-JSON) findings file")
    return parse_findings(raw)


def _unpack_framing(
    framed: Union[str, "tuple[str, Optional[Usage]]"]
) -> tuple[str, Optional[Usage]]:
    """Normalize a reasoner's return into (prose, usage-or-None).

    A passthrough/test reasoner returns a bare string; the production proxy
    reasoner returns (prose, Usage) so coordinator tokens are counted."""
    if isinstance(framed, tuple):
        text, usage = framed
        return text, usage
    return framed, None


def _unpack_judgment(
    res: Union[CoordinatorDecision, "tuple[CoordinatorDecision, Optional[Usage]]"]
) -> tuple[CoordinatorDecision, Optional[Usage]]:
    """Normalize a judge's return into (decision, usage-or-None). A test/fallback
    judge may return a bare decision; the LLM judge returns (decision, Usage)."""
    if isinstance(res, tuple):
        return res[0], res[1]
    return res, None


async def _fallback_judge(
    left: list[Finding], right: list[Finding], round_no: int
) -> CoordinatorDecision:
    """Deterministic judge used when no coordinator LLM is configured (degraded).

    Maps the string-diff `convergence()` into a `CoordinatorDecision`: agreed +
    one-sided concerns become `agreed` (surfaced), clashes become `disagreed`,
    and `converged` mirrors `fully_converged`. This is a fallback only — the
    production path is the LLM judge (see runner._proxy_judge)."""
    r = convergence(left, right)
    return CoordinatorDecision(
        converged=r.fully_converged,
        agreed=list(r.converged) + list(r.unmatched),
        disagreed=list(r.disagreements),
        summary="",
    )
