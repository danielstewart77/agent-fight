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


@dataclass(frozen=True)
class Finding:
    """One structured review finding — the source of truth for convergence.

    `verdict` is `issue` in round 1; in round 2+ an agent re-states each item it
    is responding to as `agree` / `disagree` / `revised` / `resolved`. `line`
    is 1-indexed; 0 means file-level with no specific line.
    """

    file: str
    line: int
    claim: str
    severity: str = "medium"
    verdict: str = "issue"


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
