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
from typing import Awaitable, Callable, Optional

from app.container import (
    FINDINGS_DIR,
    WORKDIR,
    ContainerRunner,
    compute_diff,
    prepare_repo,
    provision_harnesses,
)
from app.harness import build_argv, harness_env, harness_model
from app.orm import Settings
from app.review.models import (
    ConvergenceResult,
    Disagreement,
    Finding,
    HarnessType,
    ReviewJob,
)

logger = logging.getLogger("agent_fight.coordinator")

# Per-harness wall-clock cap for a single review round (a hung CLI must not
# block the run forever).
HARNESS_TIMEOUT = 900.0

# A reasoner turns a structured cross-exposure input into conversational prose.
Reasoner = Callable[[str], Awaitable[str]]


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


# Verdicts that mean "this side no longer considers it an open issue".
_CLOSED_VERDICTS = {"resolved", "agree"}


def convergence(left: list[Finding], right: list[Finding]) -> ConvergenceResult:
    """Diff two harnesses' latest findings by (file, line).

    - Both raised the same location with the same verdict -> converged.
    - Both raised it with differing verdicts -> disagreement (both kept).
    - Only one side raised it -> unmatched (an unresolved concern).
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
            if lf.verdict == rf.verdict:
                converged.append(lf)
            else:
                disagreements.append(
                    Disagreement(file=key[0], line=key[1], left=lf, right=rf)
                )
        else:
            unmatched.append(lf or rf)  # type: ignore[arg-type]

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


def _round1_prompt(diff: str, findings_path: str) -> str:
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
    post_back: bool = True,
) -> str:
    """Run the full consensus loop for one PR and return the final review.

    `runner` provides the container; `reasoner` reframes cross-exposure prompts
    conversationally (defaults to passthrough); `ado_client.post_pr_review` is
    called with the result unless `post_back` is False.
    """
    harnesses = harnesses or [HarnessType.CLAUDE, HarnessType.CODEX]
    if len(harnesses) != 2:
        raise ValueError("the consensus loop expects exactly two harnesses")
    reasoner = reasoner or _default_framing
    round_cap = max(1, int(settings.round_cap))
    h1, h2 = harnesses

    async with runner.start(job) as handle:
        await prepare_repo(
            handle,
            job,
            clone_credential_kind=settings.clone_credential_kind,
            clone_credential_secret=settings.clone_credential_secret,
        )
        await provision_harnesses(handle, settings)
        diff = await compute_diff(handle, job)

        prev: dict[HarnessType, list[Finding]] = {}
        consensus: list[Finding] = []
        result = ConvergenceResult()

        for round_no in range(1, round_cap + 1):
            disagreements = result.disagreements

            for h in harnesses:
                fpath = _findings_path(h, round_no)
                if round_no == 1:
                    prompt = _round1_prompt(diff, fpath)
                else:
                    other = h2 if h is h1 else h1
                    structured = build_cross_exposure_input(
                        own=prev.get(h, []),
                        other=prev.get(other, []),
                        open_disagreements=disagreements,
                    )
                    framed = await reasoner(structured)
                    prompt = (
                        "This is a follow-up round of a code-review debate. "
                        "Below are your own prior findings, the other reviewer's "
                        "findings, and the items still open. For each, respond by "
                        "writing an updated findings entry marking it "
                        '"agree", "disagree" (with reasoning in claim), '
                        '"revised", or "resolved".\n\n'
                        f"Write the result as JSON to {fpath}.\n\n"
                        f"{framed}\n"
                    )

                argv = build_argv(h, workdir=WORKDIR, model=harness_model(h, settings))
                exec_res = await handle.exec(
                    argv,
                    env=harness_env(h, settings),
                    stdin=prompt,
                    timeout=HARNESS_TIMEOUT,
                )
                if not exec_res.ok:
                    logger.warning(
                        "harness %s exited rc=%s in round %s", h.value, exec_res.rc, round_no
                    )
                try:
                    prev[h] = parse_findings(await handle.read_file(fpath))
                except FileNotFoundError:
                    logger.warning("harness %s wrote no findings file in round %s", h.value, round_no)
                    prev[h] = []

            result = convergence(prev[h1], prev[h2])
            consensus = _dedupe(consensus + result.converged)

            if result.fully_converged:
                logger.info("converged after round %s", round_no)
                break

        review_md = synthesize_final_review(
            consensus, result.disagreements, result.unmatched,
            reviewed=f"`{job.source_branch}` → `{job.target_branch}`",
        )

    if post_back and ado_client is not None:
        await ado_client.post_pr_review(settings, job, review_md)

    return review_md
