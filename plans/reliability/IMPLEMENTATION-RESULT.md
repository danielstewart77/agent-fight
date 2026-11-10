# Implementation evidence

Source: BUGREPORT.md and IMPLEMENTATION.md, REL-P1/REL-P2 v2 accepted by Daniel
on 2026-11-10 ("proceed"). Branch fix/duplicate-review-reliability, base 00ef657.

REL-P1: duplicate metadata on non-withdrawal responses is ignored by the ledger;
unsafe authored duplicate withdrawals remain open and route to their authors.
The original claim survives, including at the cap, with wait-for-author vote.
Chains can resolve to a live survivor; shared independently asserted findings
remain; later clarification requires the challenger to accept a defense.

REL-P2: the outer coordinator exception boundary preserves measured usage and
exact completed response files on ledger, parse, transport and cancellation
failures. Real runner persistence stores both with failed status. Concurrent
batch cleanup avoids double-cancelling a shielded progress write.

Red: existing virtual environment, `python -m pytest -q
tests/test_review_reliability.py tests/test_finding_ledger.py` against unmodified
production implementation: 13 failed, 8 passed in 0.56 seconds. Failures reproduced
both reported ledger error paths and absent failure-evidence persistence.

Focused green: same interpreter, `python -m pytest -q
tests/test_review_reliability.py tests/test_finding_ledger.py tests/test_runner.py
tests/test_directed_review.py --tb=short`: 40 passed in 0.64 seconds.

Additional cases cover multi-node cycles/feeders, shared origins, later-round
cycles, and cancellation after a completed independent round. The cancellation
case exposed a second task cancellation interrupting a progress commit; joining
already-cancelling children once repaired this path. The hint test's unrelated
omitted counterpart correctly owes a response; its assignment expectation was
corrected to the agreed silence-is-not-agreement semantics.

Local repair/recheck cycles used: 3 of 5. No external reviewer was assigned or
spawned. Coordinator self-review checked withdrawal authority, graph terminals,
shared origins, stale metadata, explicit acceptance, cap output, exception paths,
task cleanup, diagnostic privacy and deployment preservation.

Full Windows gate: 271 passed in 138.39 seconds using `python -m pytest -q`.
`git diff --check` passed. Staged Linux/actual-image verification and deployment
remain pending at this revision. Historical exact replay cannot
be performed because old failed-run outputs were discarded; synthetic protocol
regressions establish the repaired transitions without claiming historical model
output or cost reconstruction. No new live model invocation is required to
reproduce these deterministic ledger/persistence defects.
