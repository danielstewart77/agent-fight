# Directed review rounds

Accepted scope: Daniel's 2026-11-05 request to implement the assessment's protocol on a feature branch and replay a recent round-3 review. Base: d5060ad. Branch: feature/directed-review-rounds. Separate worktree; existing investigation files preserved.

REQ-01: independent and cross-review stages run concurrently against an immutable snapshot.
REQ-02: later stages run only reviewers owing an objection/rebuttal/revision response, batching assigned items per reviewer.
REQ-03: stable finding identity and coordinator-owned state preserve settled findings and close authored withdrawals without stale disagreements.
REQ-04: complete immediately when obligations are settled; cap remaining objections and derive the vote from surviving findings.
REQ-05: concurrent failure never posts a successful review; progress persistence is serialized and both tasks are joined before teardown.
REQ-06: rerun a recent round-3 PR revision in isolation and compare wall time, tokens, and substantive outcome with saved original evidence.

Protection list v1: previously presented in ASSESSMENT.md and accepted by the user's instruction to implement it. P-01 protects directed scheduling and closure through real run_review calls against the container boundary, including reciprocal challenges, withdrawals, revisions, unanswered items and partial rounds. P-02 protects faithful final output and failure handling through real ledger/coordinator/persistence calls, including shared anchors, duplicate IDs, malformed outputs, preserved settled findings and concurrent cleanup. These cover REQ-01–05; REQ-06 is live verification, judged by reading model output.

Implementation: add a pure finding ledger, stable IDs in exchanged JSON, focused response prompts after round 1, bounded concurrent task batches and serialized progress callbacks. Replace production judge scheduling with ledger completion. Retain historical judge parsing helpers for compatibility/history, but do not invoke an LLM judge in the new flow. No schema migration is required; raw per-round JSON persists IDs and responses.

Test interpreter: existing agent-fight/.venv/Scripts/python.exe; pytest from this worktree, isolated fixtures only. No shared operational database test writes.
