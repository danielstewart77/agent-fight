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

## Infrastructure

**Trigger.** Azure DevOps pull request submission calls a webhook configured
in ADO's PR pipeline. The web service listens for that call and kicks off a
review run.

**Provider access.** Reuses the existing `inference-proxy` project
(`~/Storage/Dev/inference-proxy`) — an API-key-authed FastAPI proxy that
routes requests from any harness to that harness's underlying provider.
agent-fight's harnesses call through it rather than holding provider
credentials directly.

**Web service shape.** A new FastAPI server, structured like
inference-proxy's `app/{main.py, config.py, db.py, orm.py, auth.py}` split:
a webhook route to receive the ADO PR call, an admin surface for run
settings (round cap, whatever metrics we decide to track), generalizing
inference-proxy's `deployments.py` name-based provider resolution into a
harness-type resolver (`claude` / `codex` / future harnesses).

**Per-review container lifecycle.** Modeled on `frame-main`
(`~/Storage/Dev/frame-main`, see its `DESIGN.md`, `sandbox/provision.py`,
`sandbox/entrypoint.sh`, `sessions.py`): on webhook, create one ephemeral
container for the review (frame-main's `DockerProvisioner` shells out to
`docker run` per session — same pattern here, one container per PR review
rather than per chat thread). Inside the container: clone the repo once,
fetch both the target branch and the PR branch, checkout the PR branch as
the single working tree. No second worktree needed — `git diff target..pr`
works against a fetched-but-not-checked-out ref, so one clone plus one
checkout is enough for both the diff surface and full file content when a
harness needs more context than the diff hunks. Run the round loop from
Core loop above inside that container. Once the coordinator produces the
final review (converged or round-capped synthesis), post it back and tear
the container down — no idle persistence, a review is one-shot, not an
ongoing thread like frame-main's chat sessions.

**Harness adapter shape.** Still to design in detail, but frame-main's
`harness.py` (`build_argv()` per harness type, `parse_line()` normalizing
each harness's stream-json into one event vocabulary) is the closest
existing reference and likely reusable pattern.

**Webhook payload.** Not spec'd in advance — carries whatever the review
job actually needs to do its work (repo URL, PR branch, target branch,
credentials/access to clone). Shape follows function, not a predefined
contract.

**Admin-tracked metrics.** Deferred — will define once the service is
running in production and it's clear what's worth tracking. The one
setting known to be required now is the round cap (turn limit before
consolidation), configurable per run through the admin surface.

**Unresolved disagreement at round cap.** If the two agents never converge
on a finding, it is not forced to a winner — it stays an open item in the
final review. The coordinator makes the disagreement itself visible in the
write-up rather than silently picking a side, e.g. "Codex has marked this
issue as closed, but Claude still considers it an issue." This is the same
inclusive-synthesis behavior as the round-cap termination case, applied to
a single finding rather than the whole run.

## Open questions / not yet decided

- Exact FastAPI route list for the ADO webhook (payload shape resolved —
  whatever the job needs; route/method still TBD).

## Blockers before first launch

- Decide the reopen-after-resolved policy — resolved above.
- Deploy `inference-proxy` — agent-fight's harness calls route through it,
  can't run without it live.
- Azure DevOps specifics: target org/project, webhook auth so arbitrary
  calls aren't accepted, and how the final review posts back (PR comment
  via ADO REST API needs a service account/PAT with comment permission).
- Clone credentials: token or SSH key for the container to pull a private
  ADO repo, stored in the same keyring pattern as the rest of the minds.
- A real target repo to point the webhook at for the first test run.
