# Frequent failed reviews — 2026-11-10

Host: ulhgpuhamster, account daniel, SSH public-key access. Service agent-fight,
image agent-fight:directed-6105be5. Investigation used read-only SQLite connections.
Repair workspace: agent-fight-reliability, branch fix/duplicate-review-reliability,
base 00ef657. These observations preceded the repair; deployment details are
recorded in PRODUCTION-DEPLOYMENT.md.

## Observed incidence

Deployment boundary: 2026-11-05 17:31:42 UTC. November 1 through the boundary:
50 converged, 6 capped, zero failed. Since the boundary: 27 converged, 6 failed,
1 running. Failures are 6/33 completed runs (18.2%), totaling 7,232,122 ms
(2 hours, 32 seconds) of elapsed review time.

| Review | PR | Error | Duration ms |
| --- | --- | --- | --- |
| 1139 | 5103 | cyclic duplicate references | 1224732 |
| 1141 | 5103 | cyclic duplicate references | 1271504 |
| 1142 | 5129 | only the author may withdraw a duplicate | 2101779 |
| 1152 | 5132 | only the author may withdraw a duplicate | 730719 |
| 1156 | 5136 | only the author may withdraw a duplicate | 972560 |
| 1160 | 5138 | cyclic duplicate references | 930828 |

All six traces end at run_and_record -> run_review -> FindingLedger.apply.
This identifies application protocol validation as the cause of these failures.
It does not establish that provider or infrastructure failures never occur.

## Defects and reproduction

1. ledger.apply treats every optional duplicate_of as an authored withdrawal,
   including affirmative issue/agree verdicts and opponent responses. These
   optional relationship hints cause a whole review to fail.
2. Reciprocal duplicate withdrawals form a cycle. The ledger correctly refuses
   to discard every finding, but currently aborts the entire run rather than
   retaining the claims for clarification.
3. Exceptions outside the concurrent harness batch do not receive accumulated
   usage. Raw findings only leave run_review on success, and the runner does
   not store them on failure. Each of the six failed rows has zero raw findings
   and zero token-usage records. Historical token cost cannot be reconstructed.

Run plans/reliability/reproduce.py with the repository on PYTHONPATH using the
existing agent-fight virtual environment. On unchanged base 00ef657 it produces
the author-withdrawal error for affirmative_hint and opponent_hint, and the
cycle error for mutual_withdrawal. These are synthetic reproductions of the
observed error paths, not recovered historical agent responses.

Read-only audit collectors are C:/Users/dastewart/agent-fight-failure-audit.py
and agent-fight-failure-counts.py; remote copies are under /home/daniel/.
Settings observed: Claude claude-opus-5, Codex gpt-5.6-sol, round cap 3.
Review 1170 / PR 5122 was running during the investigation and was left alone.

Baseline verification: existing virtual environment, `python -m pytest -q` in
the repair worktree: 254 passed in 149.97 seconds. This is unchanged-code baseline
evidence; no repair or new regression tests have been implemented.
