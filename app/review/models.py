"""Shared, side-effect-free dataclasses for the review pipeline.

These are the vocabulary every other module speaks in: the `ReviewJob` parsed
from an ADO webhook, the structured `Finding` each harness emits, the
normalized `Event` a harness's stream-json is parsed into, and the
`ConvergenceResult` the coordinator computes each round. Nothing here does
I/O — keeping the data model pure is what makes the coordinator, parser, and
harness adapter unit-testable without Docker, a live proxy, or the CLIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HarnessType(str, Enum):
    """The agent harnesses that review a PR. String-valued so a value doubles
    as the agent's stable label in findings-file names and prompts."""

    CLAUDE = "claude"
    CODEX = "codex"


@dataclass(frozen=True)
class ReviewJob:
    """Everything a review run needs, parsed from the ADO PR webhook payload.

    Shape follows function (see plans/code-review-consensus.md): the repo to
    clone, the two refs that bound the diff, and the identifiers needed to post
    the final review back to the right pull request.
    """

    event_type: str
    pr_id: int
    repo_id: str
    repo_name: str
    repo_clone_url: str
    source_ref: str  # full ref, e.g. refs/heads/feature-x
    target_ref: str  # full ref, e.g. refs/heads/main
    source_branch: str  # short name, e.g. feature-x
    target_branch: str  # short name, e.g. main
    organization: str
    project: str
    pr_web_url: str = ""
    # Tip commit of the source branch at webhook time (resource.lastMergeSource
    # Commit). Used to skip re-reviewing a commit we've already reviewed — the
    # loop-guard against `pullrequest.updated` events fired by our own comment/
    # vote. Empty when the payload omits it.
    source_commit: str = ""


@dataclass(frozen=True)
class Finding:
    """One structured review finding — the source of truth for convergence.

    `verdict` is `issue` in the independent round; in a fight round an agent
    re-states each item it is responding to as `agree` / `disagree` / `revised`
    / `resolved`. `line` is 1-indexed; 0 means file-level with no specific line.

    There is deliberately NO confidence score. A self-reported number nobody can
    check is decoration — every finding in the false-positive that motivated this
    design shipped at "confidence 100". `evidence` replaces it: the agent must
    name the code it actually read to support the consequence it is claiming, and
    the rival gets to attack that.
    """

    file: str
    line: int
    claim: str
    severity: str = "medium"
    verdict: str = "issue"
    # A concrete fix suggestion, shown in the final comment.
    suggestion: str = ""
    # The files/lines the agent read that prove the claimed consequence.
    evidence: str = ""
    # The agent's argument in a fight round: why it agrees, or why the rival is
    # wrong. Never dropped — a rebuttal is the whole point of the fight, and it
    # is rendered verbatim for any item that stays contested.
    response: str = ""


@dataclass(frozen=True)
class Event:
    """A harness stream-json line normalized into one vocabulary across CLIs.

    Used for progress/logging and to detect completion or failure. The findings
    themselves are read from the JSON file each harness writes, never scraped
    from this stream.
    """

    kind: str  # assistant_text|tool_use|tool_result|result|usage|error|unknown
    text: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Disagreement:
    """A finding location where the two harnesses landed on different verdicts.

    Both sides are kept verbatim so the coordinator can surface the split in the
    final write-up rather than silently picking a winner."""

    file: str
    line: int
    left: Finding
    right: Finding


@dataclass(frozen=True)
class ConvergenceResult:
    """The item-by-item diff of two harnesses' latest round files.

    - `converged`: same (file, line) and same verdict on both sides.
    - `disagreements`: same (file, line), differing verdict.
    - `unmatched`: a (file, line) only one side raised — an unresolved concern.
    """

    converged: list[Finding] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    unmatched: list[Finding] = field(default_factory=list)

    @property
    def fully_converged(self) -> bool:
        return not self.disagreements and not self.unmatched


@dataclass(frozen=True)
class Usage:
    """Token (and, when the harness reports it, cost) accounting for one model
    invocation. Cache tokens are Anthropic-specific; `cost_usd` is only known
    for the Claude harness (its `result` event carries `total_cost_usd`) — None
    means "not reported", not "free"."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: Optional[float] = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.input_tokens
            or self.output_tokens
            or self.cache_read_tokens
            or self.cache_creation_tokens
            or self.cost_usd
        )


@dataclass(frozen=True)
class RawFindings:
    """The exact bytes one harness wrote in one round, kept verbatim.

    Persisted per review so a wrong or baffling posted comment can be traced to
    what the agents actually emitted — verdicts, severities and all. Without it,
    diagnosing bad output means inferring it from the judge's prose after the
    container is gone.
    """

    role: str
    round_no: int
    raw: str


@dataclass(frozen=True)
class RoleRoundUsage:
    """`Usage` attributed to one role in one round of the consensus loop.

    `role` is a harness value (`claude`/`codex`) or `coordinator` for the judge's
    per-round ruling. `round_no` is 1-indexed (0 = the independent prep pass).
    """

    role: str
    round_no: int
    usage: Usage


@dataclass(frozen=True)
class CoordinatorDecision:
    """The judge's ruling after one fight round.

    The judge referees; it does NOT review. It never sees the code and never
    writes, merges, or rewrites a finding — the agents' own words go to the pull
    request verbatim. Its only calls are whether the fight is settled
    (`converged`, which gates the loop; the round cap bounds it) and what vote to
    cast. The review's content is derived from the agents' findings by
    `coordinator.convergence`.
    """

    converged: bool
    summary: str = ""
    # Recommended Azure DevOps reviewer vote based on the findings — one of
    # 10 (approve), 5 (approve w/ suggestions), -5 (wait for author),
    # -10 (reject). None = the coordinator did not supply one; the loop then
    # derives it deterministically from severities (see coordinator._derive_vote).
    vote: Optional[int] = None
    vote_reason: str = ""


class ReviewFailure(RuntimeError):
    """A review could not complete correctly and therefore MUST NOT be posted.

    Raised by the coordinator the moment any harness faults (non-zero exit, no
    findings file, or unparseable findings) in any round. It is the single
    tripwire that guarantees a broken run never masquerades as a clean/consensus
    review: because it is raised *before* the post-back step, nothing is ever
    written to the pull request on failure. The runner catches it, records the
    review as `failed` with this detail, and persists whatever token usage was
    captured before the fault.
    """

    def __init__(
        self,
        message: str,
        *,
        harness: Optional[str] = None,
        round_no: Optional[int] = None,
        rc: Optional[int] = None,
        reason: Optional[str] = None,
        usage: Optional[list["RoleRoundUsage"]] = None,
    ) -> None:
        super().__init__(message)
        self.harness = harness
        self.round_no = round_no
        self.rc = rc
        self.reason = reason
        self.usage = usage or []


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of a full `run_review`: the posted markdown plus the metrics
    the runner persists to the `reviews` / `review_token_usage` tables.

    `status` is `converged` when the loop terminated early on agreement, else
    `capped` (hit the round cap with items still open). Failures never produce a
    ReviewOutcome — they raise, and the runner records them as `failed`.
    """

    review_md: str
    status: str
    rounds_used: int
    converged_count: int
    disagreement_count: int
    unmatched_count: int
    usage: list[RoleRoundUsage] = field(default_factory=list)
    vote: Optional[int] = None       # ADO reviewer vote cast for this review
    vote_reason: str = ""
    # Every harness's raw findings file, every round — the audit trail behind
    # the posted comment.
    raw_findings: list[RawFindings] = field(default_factory=list)
