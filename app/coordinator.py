"""Parallel independent/cross reviews followed by directed response batches.

A coordinator-owned FindingLedger keeps stable identities, original owners,
settled findings, and the next responder for each open item. Only pending agents
run after round 1. Completion and voting are deterministic; legacy convergence
and judge parsers remain available for historical integrations.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional, Union

from app.container import (
    DIFF_PATH,
    FINDINGS_DIR,
    PR_CONTEXT_PATH,
    WORKDIR,
    ContainerRunner,
    compute_diff,
    prepare_repo,
    provision_harnesses,
    write_pr_context,
)
from app.harness import (
    ado_env,
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
    RawFindings,
    ReviewOutcome,
    RoleRoundUsage,
    Usage,
)

# How much harness stderr to keep in logs / the failure record (chars).
_STDERR_EXCERPT = 2000

logger = logging.getLogger("agent_fight.coordinator")

# A harness may run as long as needed while it is producing output. Kill only
# after this much complete stdout/stderr silence.
HARNESS_INACTIVITY_TIMEOUT = 1800.0

# How much of the diff to inline in the round-0 prompt. Past this the prompt
# points at DIFF_PATH instead — the whole diff is always on disk in the
# container, and the agent has the repo besides.
MAX_INLINE_DIFF = 120_000

# A judge reads both agents' latest findings after a fight round and rules on
# whether the fight is over (plus the reviewer vote), optionally paired with the
# coord LLM's `Usage`. It is handed no code and authors no findings. The
# production judge is an LLM (see runner._proxy_judge); the default is a
# deterministic fallback (see `_fallback_judge`).
Judge = Callable[
    [list["Finding"], list["Finding"], int, int],
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
        out.append(
            Finding(
                file=str(file),
                line=line,
                claim=str(claim),
                severity=str(item.get("severity", "medium")),
                verdict=str(item.get("verdict", "issue")),
                suggestion=str(item.get("suggestion", "") or ""),
                evidence=str(item.get("evidence", "") or ""),
                response=str(item.get("response", "") or ""),
                finding_id=str(item.get("finding_id", "") or ""),
                duplicate_of=str(item.get("duplicate_of", "") or ""),
            )
        )
    return out


def parse_coordinator_decision(text: str) -> CoordinatorDecision:
    """Parse the judge's JSON ruling into a `CoordinatorDecision`.

    Expected shape: {"converged": bool, "reason": str, "vote": int,
    "vote_reason": str}. Tolerant of prose/markdown fences around the object,
    but raises ValueError if no usable object is found or `converged` is missing
    — the caller turns that into a ReviewFailure, so a garbled ruling never
    yields a posted review. The judge supplies no findings: the review's content
    comes from the agents via `convergence`.
    """
    obj = _extract_json_object(text)
    if not isinstance(obj, dict):
        raise ValueError("judge ruling is not a JSON object")
    if "converged" not in obj:
        raise ValueError("judge ruling has no 'converged' key")

    vote = None
    try:
        v = int(obj["vote"])
        vote = v if v in _ALLOWED_VOTES else None
    except (KeyError, TypeError, ValueError):
        vote = None
    return CoordinatorDecision(
        converged=bool(obj.get("converged")),
        summary=str(obj.get("reason") or obj.get("summary") or ""),
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


def findings_json(findings: list[Finding]) -> str:
    """A findings list as the JSON the agents exchange.

    Verbatim: claim, evidence and response are passed between the two reviewers
    exactly as written. Nothing summarizes or paraphrases a rival's argument on
    its way across — an agent argues with what the other agent actually said.
    """
    return json.dumps(
        [
            {
                "file": f.file, "line": f.line, "severity": f.severity,
                "verdict": f.verdict, "claim": f.claim,
                "evidence": f.evidence, "suggestion": f.suggestion,
                "response": f.response,
                "finding_id": f.finding_id, "duplicate_of": f.duplicate_of,
            }
            for f in findings
        ],
        indent=2,
        ensure_ascii=False,
    )


def open_items_json(
    disagreements: list[Disagreement],
    unanswered: list[Finding],
    *,
    own_is_left: bool,
    own_keys: set[tuple[str, int]],
) -> str:
    """The items blocking convergence, written from the recipient's point of view.

    Explicit clashes, plus findings one side raised that the other never
    responded to (silence is not a verdict). `own_is_left` orients each
    disagreement — the same `Disagreement` is "you vs them" for one fighter and
    "them vs you" for the other, and mislabelling it would have an agent argue
    against its own position.
    """
    disputed = []
    for d in disagreements:
        mine, theirs = (d.left, d.right) if own_is_left else (d.right, d.left)
        disputed.append({
            "file": d.file, "line": d.line, "claim": d.left.claim,
            "your_verdict": mine.verdict, "your_response": mine.response,
            "their_verdict": theirs.verdict, "their_response": theirs.response,
        })
    payload = {
        "disputed": disputed,
        "unanswered": [
            {
                "file": f.file, "line": f.line, "claim": f.claim, "raised_as": f.verdict,
                "whose": "yours (they never responded)" if _key(f) in own_keys
                         else "theirs (you never responded)",
            }
            for f in unanswered
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: dict[str | tuple[str, int], Finding] = {}
    for f in findings:
        seen[f.finding_id or _key(f)] = f
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
    """Findings grouped by severity (Critical→Nit), each with its fix and the
    code the reviewer checked.

    `Checked` is deliberately public: it lets the author see in one line whether
    the claim rests on code someone actually read, and call it out when it does
    not."""
    lines: list[str] = []
    order = sorted(findings, key=lambda f: (_sev_rank(f.severity), _key(f)))
    current: str | None = None
    for f in order:
        sev = (f.severity or "medium").strip().lower()
        if sev != current:
            current = sev
            lines.append("")
            lines.append(f"### {sev.capitalize()}")
        lines.append(f"- **{_loc(f)}** — {f.claim}")
        if f.suggestion:
            lines.append(f"  - **Fix:** {f.suggestion}")
        if f.evidence:
            lines.append(f"  - **Checked:** {f.evidence}")
    return lines


def synthesize_final_review(
    consensus: list[Finding],
    disagreements: list[Disagreement],
    unmatched: list[Finding],
    *,
    reviewed: str | None = None,
) -> str:
    """Inclusive union write-up in the pr-review format: a "reviewed" header,
    findings grouped by severity with a concrete fix, agreed items first, then
    disagreements with BOTH sides quoted, then one-reviewer concerns.

    No tiebreak, and nothing is dropped: where the reviewers never agreed, the
    author gets each side's actual argument and decides for themselves."""
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
        lines.append("_The two reviewers could not settle these. Both arguments verbatim:_")
        for d in sorted(disagreements, key=lambda x: (x.file, x.line)):
            lines.append("")
            lines.append(f"- **{_loc(d.left)}** — {d.left.claim}")
            for label, side in (("Reviewer 1", d.left), ("Reviewer 2", d.right)):
                argument = side.response or side.claim
                lines.append(f"  - **{label}** (`{side.verdict}`): {argument}")

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


def _pr_header(job: ReviewJob) -> str:
    """The identifying block both prompts open with."""
    lines = [
        f"  {job.organization}/{job.project}  repo {job.repo_name}  PR !{job.pr_id}",
        f"  {job.source_branch} -> {job.target_branch}",
    ]
    if job.pr_web_url:
        lines.append(f"  {job.pr_web_url}")
    return "\n".join(lines)


_MATERIAL = f"""\
WHAT YOU HAVE
  {WORKDIR}
      The full repository, PR branch checked out. Read anything in it.
  {DIFF_PATH}
      The exact diff under review.
  {PR_CONTEXT_PATH}
      The pull request itself: title, description, linked work items, and EVERY
      comment thread on it -- including any previous agent-fight review and the
      author's replies to it. Read it. If the author has already answered a
      point, their reply is evidence.
  the live PR API
      curl -s -H "Authorization: $ADO_AUTH_HEADER" \\
           "$ADO_PR_API/threads?api-version=$ADO_API_VERSION"
      Also /iterations, /workitems, /commits. Pull whatever you need."""

_STANDARDS = """\
SCOPE
  The diff is the review. Read as widely as you need to understand it, but only
  report what THIS change breaks, introduces, or newly exposes. If the problem
  is in code this PR did not touch, raise it only if the change makes it
  reachable or wrong -- and say which changed line does that.

BEFORE YOU CLAIM A DEFECT
  Follow the call path and read the code at every hop before you assert a
  consequence. "This creates a duplicate record", "this loads the wrong row",
  "this posts without the id" are claims about what the system actually does:
  open the caller, the controller, the handler, the service, and check. If you
  have not read the code that produces the consequence you are describing, you
  do not have a finding.
  The absence of a pattern you expected -- a missing input binding, parameter,
  guard, null check -- is a QUESTION, not a defect, until you have found the
  code that needs it. Two components differing is not a bug; it is a bug only
  when you can show the behavior it breaks.
  Put what you actually read in "evidence". A finding with no evidence is
  noise, and the rival reviewer will take it apart.

REVIEW ACROSS
  Correctness, Readability, Simplicity, Consistency, Maintainability, Test
  Coverage, Error Handling, Performance, Security. Real defects and clear
  convention violations -- not style preferences.

EVERY ENTRY IN THE FILE ASSERTS A DEFECT
  The findings file is a defect list, and every entry in it is published to the
  pull request as something the author must consider. It is NOT a report, a
  summary, or a place to show your work. Never file:
    - praise or approval ("the fix is correct and well-scoped")
    - a summary of what the change does, or of what you verified
    - an item whose suggestion is "no change required" / "none" / "looks good"
  If the change is clean, the answer is an empty list. That is a real verdict
  and it is the right one more often than not. An approving note filed as a
  finding publishes a "concern" that isn't one, and makes the whole review
  worthless."""


def build_independent_prompt(job: ReviewJob, diff: str, findings_path: str, *, round_cap: int) -> str:
    """Round 0: review the pull request blind, before seeing the rival."""
    if len(diff) > MAX_INLINE_DIFF:
        diff = (
            diff[:MAX_INLINE_DIFF]
            + f"\n\n... diff truncated here. Read the whole thing at {DIFF_PATH}.\n"
        )
    return f"""\
You are one of two independent code reviewers in an "agent fight". You and a
rival reviewer -- a different AI harness -- review this pull request separately,
then argue it out over up to {round_cap} rounds until you agree. This is round 0:
your independent review. Do not read any other reviewer's findings or session files.

PULL REQUEST
{_pr_header(job)}

{_MATERIAL}

{_STANDARDS}

OUTPUT -- write JSON to {findings_path}
[
  {{
    "file": "path/relative/to/repo/root.cs",
    "line": 191,
    "severity": "critical|major|minor|nit",
    "claim": "What is wrong and the concrete consequence.",
    "evidence": "The files and lines you read that prove that consequence.",
    "suggestion": "The specific fix.",
    "verdict": "issue"
  }}
]
An empty list [] is a valid and meaningful answer: the change is clean.
`line` is 1-indexed; 0 means file-level. Write the file -- console output is
ignored.

--- DIFF ---
{diff}
"""


def build_fight_prompt(
    job: ReviewJob,
    *,
    own: list[Finding],
    other: list[Finding],
    disagreements: list[Disagreement],
    unanswered: list[Finding],
    round_no: int,
    round_cap: int,
    findings_path: str,
    own_is_left: bool = True,
) -> str:
    """Round 1+: the fight. Both reviews, the open items, and the code.

    `own_is_left` says which side of a `Disagreement` belongs to the recipient
    (`convergence` always puts the first harness on the left)."""
    open_items = open_items_json(
        disagreements, unanswered,
        own_is_left=own_is_left, own_keys={_key(f) for f in own},
    )
    return f"""\
This is round {round_no} of {round_cap} of an agent fight over this pull request. You and
a rival reviewer reviewed it independently. Below is your review and theirs.

Your job this round: go back to the code and settle it.

  1. Re-read the code for every item -- yours AND theirs. You have the full repo,
     the diff, and the pull request; use them.
  2. For each of THEIR findings: agree or disagree, and mark it. If you disagree,
     prove it -- name the file and line that shows they are wrong. If you agree,
     say so plainly.
  3. For each of YOUR findings they challenged: re-read the code, then hold the
     line with evidence, revise it, or withdraw it. Withdrawing a finding you
     cannot support is a win. Conceding one you can support is a loss.
  4. Verify before you rule. Do not agree with a finding you have not checked in
     the code, and do not disagree without evidence. "Sounds plausible" is not a
     verdict.
  5. You may add a NEW finding if re-reading turned one up -- same evidence bar.

PULL REQUEST
{_pr_header(job)}

{_MATERIAL}

YOUR REVIEW SO FAR
{findings_json(own)}

THE RIVAL REVIEWER'S REVIEW
{findings_json(other)}

STILL OPEN AFTER THE LAST ROUND
{open_items}

OUTPUT -- write JSON to {findings_path}
The complete list for this round: every item still in play, yours and theirs.
[
  {{
    "file": "...", "line": 191, "severity": "critical|major|minor|nit",
    "claim": "...", "evidence": "...", "suggestion": "...",
    "verdict": "issue|agree|disagree|revised|resolved",
    "response": "Your argument this round: why you agree, or why they are wrong."
  }}
]
  issue     your own finding, unchallenged and unchanged
  agree     you accept this finding -- yours or theirs -- as real
  disagree  you reject it; put the refuting code in "response"
  revised   real, but the claim or severity was wrong; state the corrected claim
  resolved  you are withdrawing it; say why in "response"

WITHDRAWING IS `resolved` -- NOTHING ELSE
  If you are dropping an item, the verdict MUST be "resolved". Do not withdraw
  it by rewriting `claim` into an explanation of why the code is fine and
  leaving any other verdict: everything except "resolved" is read as you still
  asserting a defect, and it gets published to the pull request as a concern.
  `claim` always states the defect, even on the item you are giving up; your
  reason for dropping it goes in `response`.

Keep the rival's EXACT file and line when you respond to their item -- that
anchor is how the two reviews are matched, and drifting off it reads as two
separate findings rather than one you settled. Respond to every one of their
items, INCLUDING the ones you simply agree with; silence is not a verdict and
will cost another round. Write the file -- console output is ignored.
"""


def _response_contract(payload):
    return """
STABLE FINDING IDs AND RESPONSE CONTRACT (authoritative)
Use the supplied finding_id on every response, even if the line/wording changes.
The coordinator keeps settled findings. Only return responses to assigned IDs.
Do not create a second ID for the same finding or omit an opposing finding.
`agree` accepts the current supplied claim AND severity; leave those unchanged.
`disagree` rejects it, with code evidence in response. `revised` proposes a
materially different claim or severity and needs the other reviewer's acceptance.
An author uses `issue` to defend their claim, `resolved` to withdraw it.
If withdrawing your duplicate, use resolved and duplicate_of=<existing ID>.
If a concrete new defect is discovered, omit finding_id; it will get a new ID
and need the other reviewer's response. Do not add summaries or approvals.
ASSIGNED ITEMS (with original owner and latest arguments)
""" + json.dumps(payload, ensure_ascii=False, indent=2)


def build_response_prompt(job, payload, round_no, round_cap, findings_path):
    return f"""This is round {round_no} of {round_cap} of the agent fight.
Answer ONLY the assigned open items below. Each item names who originally
raised it and contains the last objection/rebuttal. You owe the next response.
Consult the relevant code to verify those specific arguments. Do not restart a
broad PR review or re-read settled findings. An author's withdrawal ends that
claim immediately. Defending it routes the rebuttal back to the challenger.

PULL REQUEST
{_pr_header(job)}
{_MATERIAL}
{_STANDARDS}

OUTPUT -- write JSON to {findings_path}
Write a JSON list of responses with finding_id, file, line, claim, severity,
evidence, suggestion, verdict, response, and optional duplicate_of.
An empty list leaves the assigned items unanswered; it is not agreement.
""" + _response_contract(payload)


async def run_review(
    job: ReviewJob,
    settings: Settings,
    *,
    runner: ContainerRunner,
    ado_client,
    harnesses: Optional[list[HarnessType]] = None,
    post_back: bool = True,
    progress=None,
) -> ReviewOutcome:
    """Run parallel rounds 0/1, then only agents owing a response.

    The ledger preserves settled findings across partial rounds and closes an
    author's withdrawal immediately. Failed/cancelled batches join every task
    before container teardown and never reach post-back. Progress callbacks are
    serialized so the runner's single database session is safe.
    """
    harnesses = harnesses or [HarnessType.CLAUDE, HarnessType.CODEX]
    if len(harnesses) != 2 or len(set(harnesses)) != 2:
        raise ValueError("the review expects exactly two harnesses")
    round_cap = max(1, int(settings.round_cap))
    h1 = harnesses[0]

    progress_lock = asyncio.Lock()

    async def emit(kind, phase, message, *, role=None, round_no=None):
        if progress is not None:
            # The runner owns one AsyncSession. Never commit it concurrently.
            async with progress_lock:
                write = asyncio.create_task(progress(kind, phase, message, role=role, round_no=round_no))
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    # Finish the database operation before cancelling this agent;
                    # interrupting commit can invalidate the shared connection.
                    await write
                    raise

    usage_records: list[RoleRoundUsage] = []
    raw_records: list[RawFindings] = []
    rounds_used = 0

    logger.info(
        "PR %s review starting: repo=%s %s -> %s harnesses=%s battle_rounds<=%s",
        job.pr_id, job.repo_name, job.source_branch, job.target_branch,
        [h.value for h in harnesses], round_cap,
    )

    async def _run_harness(h: HarnessType, prompt: str, fpath: str, round_no: int, phase: str):
        """Run one harness, capture usage, and fail-closed on any fault."""
        argv = build_argv(h, workdir=WORKDIR, model=harness_model(h, settings))
        env = {**harness_env(h, settings), **ado_env(job, settings)}
        try:
            exec_res = await handle.exec(
                argv, env=env, stdin=prompt, timeout=HARNESS_INACTIVITY_TIMEOUT
            )
        except TimeoutError:
            # Do not automatically repeat a long, expensive review. The failed
            # run is recorded and an operator can decide whether to retry it.
            raise ReviewFailure(
                f"harness {h.value} produced no output for {HARNESS_INACTIVITY_TIMEOUT / 60:g} minutes in {phase}",
                harness=h.value, round_no=round_no, reason="inactive",
                usage=list(usage_records),
            ) from None
        # Capture usage even on failure so a failed run still accounts for spend.
        h_usage = extract_usage(h, exec_res.stdout)
        if not h_usage.is_empty:
            usage_records.append(RoleRoundUsage(h.value, round_no, h_usage))
        raw = await _read_or_none(handle, fpath)
        if raw is not None:
            # Keep the bytes, not just the parse: a finding that reads wrong on
            # the PR is diagnosed from what the harness actually wrote.
            raw_records.append(RawFindings(h.value, round_no, raw))
        # A broken harness must NEVER be synthesized into a clean/consensus review
        # — this raises BEFORE post-back, so nothing reaches the PR.
        return _read_findings_or_fail(
            h, round_no, exec_res, raw, usage_records, phase=phase,
        )

    async with runner.start(job) as handle:
        await emit("phase_started", "preparing_repository", "Preparing repository")
        await prepare_repo(
            handle,
            job,
            clone_credential_kind=settings.clone_credential_kind,
            clone_credential_secret=settings.clone_credential_secret,
        )
        await provision_harnesses(handle, settings)
        diff = await compute_diff(handle, job)
        await emit("phase_completed", "preparing_repository", "Repository prepared")
        if not diff.strip():
            # Not a failure (the branch may genuinely add nothing), but log it —
            # an empty diff is also the fingerprint of a bad ref/checkout.
            logger.warning("PR %s: computed diff is EMPTY — reviewers will see no changes", job.pr_id)
        # The pull request itself — description, work items, and every comment
        # thread, including the author's replies to our last review. Reviewers
        # must never re-raise a finding the author already refuted.
        if ado_client is not None:
            await write_pr_context(handle, await ado_client.fetch_pr_context(settings, job))
        else:
            logger.warning("PR %s: no ADO client — reviewing without PR context", job.pr_id)

        from app.review.ledger import FindingLedger

        async def batch(prompts, round_no, phase):
            async def one(h, prompt, path):
                await emit("agent_started", phase, f"{h.value.title()} started round {round_no}",
                           role=h.value, round_no=round_no)
                result = await _run_harness(h, prompt, path, round_no, phase)
                await emit("agent_completed", phase, f"{h.value.title()} completed with {len(result)} response(s)",
                           role=h.value, round_no=round_no)
                return h.value, result

            tasks = [asyncio.create_task(one(h, prompt, path)) for h, prompt, path in prompts]
            try:
                return dict(await asyncio.gather(*tasks))
            except BaseException as exc:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if isinstance(exc, ReviewFailure):
                    exc.usage = list(usage_records)
                raise

        await emit("phase_started", "independent_review", "Independent reviews started in parallel")
        independent = await batch([
            (h, build_independent_prompt(job, diff, _independent_path(h), round_cap=round_cap),
             _independent_path(h)) for h in harnesses
        ], 0, "independent_review")
        ledger = FindingLedger(independent)
        for round_no in range(1, round_cap + 1):
            if ledger.complete:
                break
            rounds_used = round_no
            assignments = {h.value: ledger.assigned(h.value, round_no) for h in harnesses}
            await emit("round_started", "fight", f"Fight round {round_no} started", round_no=round_no)
            prompts = []
            for h in harnesses:
                ids = assignments[h.value]
                if round_no > 1 and not ids:
                    await emit("agent_skipped", "fight", f"{h.value.title()} owes no response",
                               role=h.value, round_no=round_no)
                    continue
                path = _findings_path(h, round_no)
                payload = ledger.payload(h.value, ids)
                if round_no == 1:
                    prompt = build_fight_prompt(
                        job, own=ledger.review(h.value), other=ledger.review(ledger.other(h.value)),
                        disagreements=[], unanswered=[], round_no=round_no, round_cap=round_cap,
                        findings_path=path, own_is_left=h is h1,
                    )
                    prompt += _response_contract(payload)
                else:
                    prompt = build_response_prompt(job, payload, round_no, round_cap, path)
                prompts.append((h, prompt, path))
            responses = await batch(prompts, round_no, "fight")
            ledger.apply(responses, round_no, assignments)
            result = ledger.result()
            logger.info("PR %s round %s of %s: agreed=%s disputed=%s unanswered=%s converged=%s",
                        job.pr_id, round_no, round_cap, len(result.converged),
                        len(result.disagreements), len(result.unmatched), ledger.complete)
            await emit("round_completed", "fight",
                       f"Round {round_no}: {len(result.converged)} agreed, "
                       f"{len(result.disagreements) + len(result.unmatched)} open",
                       round_no=round_no)

        result = ledger.result()
        agreed = list(result.converged)
        disagreed = list(result.disagreements)
        unmatched = list(result.unmatched)
        vote, vote_reason = _derive_vote(agreed + unmatched, disagreed)
        if not ledger.complete:
            vote, vote_reason = VOTE_WAIT, "round cap reached with responses still owed"

        review_md = synthesize_final_review(
            agreed, disagreed, unmatched,
            reviewed=(f"`{job.source_branch}` → `{job.target_branch}`"
                      + (f" at source `{job.source_commit}`, target `{job.target_commit}`"
                         if job.target_commit else "")),
        )
        review_md += f"\n\n---\n**Reviewer vote:** {vote_label(vote)} — {vote_reason}\n"

    # Reaching here means every harness in every phase succeeded (any fault
    # raised ReviewFailure above), so this review is safe to post.
    status = "converged" if ledger.complete else "capped"
    if post_back and ado_client is not None:
        logger.info(
            "PR %s posting review: status=%s rounds=%s agreed=%s disagreed=%s vote=%s(%s)",
            job.pr_id, status, rounds_used, len(agreed), len(disagreed), vote, vote_label(vote),
        )
        revision = await ado_client.post_pr_review(settings, job, review_md)
        logger.info("PR %s review posted as a new thread (revision %s)", job.pr_id, revision)
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
        unmatched_count=len(unmatched),
        usage=usage_records,
        raw_findings=raw_records,
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
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise _fail("wrote an unparseable (non-JSON) findings file")
    if isinstance(data, dict):
        data = data.get("findings")
    if not isinstance(data, list):
        raise _fail("wrote an invalid findings envelope")
    for row in data:
        if not isinstance(row, dict) or not row.get("file") or not row.get("claim"):
            raise _fail("wrote an invalid finding row")
        if not isinstance(row.get("line", 0), int) or isinstance(row.get("line"), bool):
            raise _fail("wrote an invalid finding line")
        if row.get("verdict", "issue") not in {"issue", "agree", "disagree", "revised", "resolved"}:
            raise _fail("wrote an invalid finding verdict")
        if round_no == 0 and row.get("verdict", "issue") != "issue":
            raise _fail("independent findings must assert an issue")
    return parse_findings(json.dumps(data))


def _unpack_judgment(
    res: Union[CoordinatorDecision, "tuple[CoordinatorDecision, Optional[Usage]]"]
) -> tuple[CoordinatorDecision, Optional[Usage]]:
    """Normalize a judge's return into (decision, usage-or-None). A test/fallback
    judge may return a bare decision; the LLM judge returns (decision, Usage)."""
    if isinstance(res, tuple):
        return res[0], res[1]
    return res, None


async def _fallback_judge(
    left: list[Finding], right: list[Finding], round_no: int, round_cap: int
) -> CoordinatorDecision:
    """Deterministic judge used when no coordinator LLM is configured (degraded).

    Rules on the same question the LLM judge is asked, structurally: the fight is
    over when nothing is disputed and nothing was left unanswered. Supplies no
    vote, so the loop derives one from the findings."""
    r = convergence(left, right)
    return CoordinatorDecision(converged=r.fully_converged, summary="")
