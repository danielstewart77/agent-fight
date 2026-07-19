# Multi-Agent Code Review Consensus

## Goal

Prototype a feature where independent agent harnesses (Codex CLI and Claude
CLI to start) review the same code independently, then debate their
findings back and forth through a coordinator, converging on a shared code
review. Reusable pattern beyond this repo — later expandable to harnesses
proposing competing feature implementations and going through the same
consensus process.

## Core loop

**Round 1 — independent review.** Each harness reviews the diff/code blind
to the other. Each writes its own findings file:
`findings-agent1-round1.json`, `findings-agent2-round1.json`. Structured
per finding: file, line, claim, severity, verdict. No shared write target —
each agent only ever writes its own file, so there is never a race between
agents writing the same document.

**Round 2+ — conversational cross-exposure.** The coordinator reads both
latest round files and builds a fresh prompt per agent containing that
agent's own prior findings plus the other agent's findings, delivered
conversationally (paraphrased/framed by the coordinator, not a raw dump).
Each agent responds by writing its next round file
(`findings-agent1-round2.json`, etc.), marking each item agree, disagree
with reasoning, or revised. Still two separate files per round — agents
never write to a shared document.

**Convergence check.** After each round, the coordinator diffs the two
latest round files item by item (same file, same line). Items where both
sides land on the same verdict get pulled into a running consensus file
that only the coordinator writes. Remaining disagreements carry into the
next round's prompts.

**Termination.** Round cap is configurable (default range 3-5) via the
project's web management interface — not hardcoded. When the cap is hit,
there is no human-tiebreak step. The coordinator synthesizes the final
review by unioning all unique feedback across every agent's final
position: agreed items plus every distinct unresolved concern. No blocking
on Daniel.

## Key design decisions

- Agents never share a mutable document. Every write target is
  single-writer: each agent owns its own per-round file, the coordinator
  owns the merged/consensus file.
- The coordinator is a reasoning agent itself, not just a round-tracker —
  paraphrasing and reframing one side's argument for the other is a
  judgment call, not mechanical relay.
- Structured findings (file, line, claim, verdict) remain the source of
  truth for convergence tracking even though delivery to agents is
  conversational.
- Round cap is a live-configurable value, not a code constant.
- Deadlock resolution is inclusive synthesis, not escalation — the final
  code review is designed to surface all unique perspectives rather than
  force a single winner.

## Open questions / not yet decided

- Web service infrastructure for the management interface (round cap
  config, run history, etc.) — next topic to design.
- Harness adapter shape for Codex CLI and Claude CLI (spawn/send/kill
  pattern reused from hive_mind's `minds/<name>/implementation.py`?).
- What happens when an agent revises a finding the other side already
  marked resolved in an earlier round.
