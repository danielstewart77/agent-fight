# Candidate inspection

Candidate code: 1d4e4e3 (same application code as archived replay candidate 4191906; the amended commit excludes private PR evidence and adds ignore rules).

Inspected locally by the implementer; this is not an independent reviewer approval.

- Round 0 and round 1 create both tasks before awaiting results. The gathered response order remains the configured harness order, giving stable IDs regardless of completion order. Round 1 prompts are built before the batch begins.
- Partial response batches update only assigned ledger items. Missing responses retain their obligation, and settled findings survive later partial files. Authored withdrawal closes an old opposing objection. A separate original co-author's surviving concern is preserved.
- Author defense cannot impersonate acceptance from the challenger. Defenses/revisions change the next responder; reciprocal obligations create both tasks. Matched agreement on the current proposed severity closes a response obligation.
- Stable IDs preserve distinct defects at one anchor. Exact original claim/anchor/severity duplicates share an identity; explicit duplicate withdrawal supports different anchors. Unknown IDs, duplicate response IDs, and cyclic duplicate withdrawals fail closed.
- Output validation rejects malformed envelopes/rows, invalid verdicts, and independent responses masquerading as objections or withdrawals. A real empty pair can finish after round 0; empty later responses remain pending.
- Progress callbacks serialize the shared AsyncSession and finish an in-flight commit before task cancellation. The batch joins cancelled siblings before container teardown; the runner rolls back/refreshes before recording failure. Failure persistence and partial reported usage passed the real ORM fixture checks.
- The production path does not call the LLM judge. Votes derive from surviving findings; a cap with any pending obligation waits for the author. Historical judge parsing/diagnostic helpers and proxy settings remain available.
- Replay runs from an isolated runner container with a read-only settings database and no posting client. Both PR commit IDs are pinned. Current PR metadata and unrecoverable historical edits limit comparison with the saved original run.

Validation: 254 tests passed in 43.65 seconds on Windows, plus diff whitespace and compilation checks. The replay records live model output for manual comparison; no automated assertion grades model conclusions.
