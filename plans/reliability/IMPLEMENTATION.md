# Reliability repair proposal

Source: 2026-11-10 user request to investigate frequent failures, BUGREPORT.md,
and directed-review REQ-01 through REQ-05 / accepted protections P-01 and P-02.
Worktree fix/duplicate-review-reliability at base 00ef657; no starting dirty files.
Ownership: ledger, coordinator, review models, runner, affected tests, this plan.
Preserve production's uncommitted work, patched image and active reviews.

Protection-list delta v2 accepted by Daniel's "proceed" on 2026-11-10 after the
unattended recovery and evidence-retention outcomes were presented. Approval
carries through implementation and repairs; no runtime human gate is introduced.

- REL-P1: Duplicate hints or loops must not abort a review or silently erase
  findings; agreement still requires an explicit response. Production ledger
  and coordinator calls establish preserved claims, pending obligations, final
  output and vote. Extends directed-review P-01/P-02, REQ-03/04.
- REL-P2: Failed reviews retain completed agents' exact responses and measured
  token usage while remaining failed, without publishing a misleading result.
  Real runner/coordinator calls against an isolated database establish stored
  evidence and status. Extends P-02, REQ-05.

## Proposed behavior

Duplicate metadata alone never grants withdrawal permission. Non-author and
affirmative responses retain their explicit verdict; optional relationship hints
do not fail the run. An ordinary explicit authored resolved response still ends
the claim, subject to the existing independently shared-origin behavior.

For an authored duplicate withdrawal, validate the complete simultaneous batch
against surviving claims. If its link cannot reach a surviving claim (a loop,
missing/self reference or withdrawn endpoint), retain that finding as open and
assign its author to clarify the duplicate withdrawal. Keep the original claim
and raw response. Do not choose a representative or invent an agreement. The
author can explicitly withdraw the claim without a duplicate link or identify
a surviving finding. Silence remains an obligation; the round cap yields a
capped review and wait vote. Clarification responses must work in later rounds.

Only effective authored withdrawal links participate in relationship checking;
stale/opponent/affirmative duplicate metadata cannot create graph cycles.
Check chains, concurrent withdrawals, shared origins, new findings, one-sided
duplicate hints, and revisions. Keep required envelope/ID/verdict validation.

Attach accumulated raw responses and measured usage to exceptions at the
outer review boundary, after cancellation/join and teardown. The runner stores
them on failure using the same tables as success. Do not send raw output to
logs or public comments. Preserve original exception type and traceback, and
serialized cancellation-shielded progress writes.

## Validation and deployment

Existing interpreter: agent-fight/.venv/Scripts/python.exe. Run focused pytest
red/green and full pytest gate. Use only disposable fixture databases; model
transport/container boundaries may be scripted, ledger/coordinator/runner may not.
Historical raw outputs are unavailable, so exact historical replay is impossible.

Production rollout must derive from the current security-patched image, validate
candidate files and image first, then wait for no active/queued review before
switching. Back up selected source and DB, preserve dirty source, verify health,
image/file hashes and live review evidence. Existing directed-review deployment
scripts/documentation provide the rollback pattern. Deployment remains pending.
