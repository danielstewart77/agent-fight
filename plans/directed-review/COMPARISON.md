# Directed review replay comparison

Completed 2026-11-05 on `feature/directed-review-rounds`. Application candidate: `1d4e4e3` (identical application/skill/test source to replay archive `4191906`; the amended commit excludes private PR evidence).

## Result

The isolated replay finished **20.4% faster**, processed **24.0% fewer tokens including cache traffic**, and used **five harness invocations instead of eight**. It ended after round 2, with only Codex running that round. The resulting concerns differed from the original; equivalent coverage is not established by this one sample.

| Measure | Original review 1131 | Directed replay | Reduction |
|---|---:|---:|---:|
| Recorded review duration | 27m 22.6s | 21m 47.8s | 20.4% |
| Tokens including cache traffic | 13,112,237 | 9,963,625 | 24.0% |
| App-reported input + output tokens | 1,118,775 | 915,175 | 18.2% |
| Output tokens | 117,500 | 93,828 | 20.1% |
| Harness invocations | 8 | 5 | 37.5% |
| LLM judge calls | 3 | 0 | 100% |
| Fight rounds used | 3 | 2 | — |
| Agreed findings | 7 | 6 | See outcome comparison |
| Final unresolved disagreements | 0 | 0 | — |
| Reviewer recommendation | Wait for author | Wait for author | Same |

The replay's timed pipeline excludes preparing its frozen context snapshot and posting/voting. The original duration includes live context retrieval and post-back. This small orchestration difference is included in the comparison limitation; the large reduction comes from the later review stages.

## What actually ran

Original: Claude, then Codex, in independent round 0 and each of fight rounds 1, 2, 3. A judge ran after each fight round.

Replay:

1. Round 0: both started within 2 ms. Claude took 831.48 seconds; Codex 42.47 seconds.
2. Round 1: both started within 1 ms. Claude took 432.08 seconds; Codex 56.62 seconds.
3. Claude withdrew its edit-retargeting claim in round 1 after checking the explicit legacy-parity test. Codex independently rejected that same claim. The withdrawal closed it immediately, without leaving the old disagreement active.
4. The remaining obligation was acknowledgment of Claude's updated dropdown claim, which removed unsupported reproduction evidence. Round 2 ran **only Codex**, took 26.70 seconds, and accepted that revision. Claude was explicitly skipped.
5. Six settled findings remained; no response was owed, so round 3 and the judge did not run.

The original independent stage took about 10m 38s; the replay's took about 13m 52s. Despite that slower first pass, elapsed fight stages fell from about **16m 32s to 7m 39s**, approximately **54% less time**. This is why end-to-end time improved by 20% rather than by the larger later-stage reduction.

## Token accounting

| Role | Original input | Replay input | Original output | Replay output | Original cached reads | Replay cached reads | Original cache writes | Replay cache writes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Claude | 17,767 | 6,174 | 104,977 | 84,604 | 11,606,126 | 8,784,221 | 387,336 | 264,229 |
| Codex | 955,941 | 815,173 | 11,998 | 9,224 | 802,922 | 691,776 | 152,935 | 123,328 |
| Judge | 27,567 | 0 | 525 | 0 | 0 | 0 | 0 | 0 |

These are the actual recorded CLI/API counters, summed across calls. Claude's cached input is additional to its `input_tokens`; Codex's cache counters are subsets of its input total. Therefore processed tokens include Claude input + cached reads + cache writes + output, and Codex input + output, without counting Codex's cache twice. The app's existing displayed total simply sums reported input + output and excludes Claude cache traffic. Cached tokens can be processed again on successive model turns; these totals are not unique source size or a dollar-cost calculation.

## Outcome comparison

The final output was read and compared manually, not graded through assertions on model text.

| Concern | Original | Replay |
|---|---|---|
| Edit dropdowns lose their saved selection | Major | Retained as major, with corrected evidence |
| Decimal amount entry clears during typing | Major | Not rediscovered |
| Edit changes a different credit/debit row | Major | Withdrawn: the cited test explicitly covers and preserves that legacy behavior |
| Journey test omits required type selections | Minor | Retained; severity increased to major |
| Modal lacks keyboard/focus management | Minor | Retained as minor |
| Remarks exceeding the database column length | Minor | Not rediscovered |
| Unused type-name helper | Nit | Retained as nit |
| Mandatory remark validation differs from legacy UI/required indicators | Absent | Added as minor |
| One-tick dirty marker loses SQL timestamp precision | Absent | Added as minor |

Thus four original findings survived, one was explicitly withdrawn, two were not rediscovered, and two additional concerns were raised. Both runs recommend waiting for the author, but they are **not equivalent reviews**. This single sample cannot distinguish ordinary model variation from coverage lost by removing repeated broad passes. The experiment supports a timing/token improvement and demonstrates correct selective routing/withdrawal; it does not prove unchanged recall or independently validate every surviving PR concern. No product fixes or SQL/browser verification were performed on the reviewed PR.

## Reproducibility and isolation

- PR: LandAdmin 5126; original review: 1131, started 2026-11-04 23:28:17 UTC and reached fight round 3.
- Source commit: `70954d46e29374a9fd73830dae2b42a3358aea02`.
- Target commit: `332d1a545e950db265f03e88745dfc535a621fbf`.
- Models: `claude-opus-5`, `gpt-5.6-sol`; cap: 3. The settings row was last updated 2026-08-21, before both runs. Provider execution/load and model nondeterminism were not controlled.
- Same current review-container image and host; a separate runner process used the candidate source and a read-only settings/database mount. Both pinned commits were confirmed in the checkout; a mid-run status check showed no working-tree changes.
- Five later/edited comments were excluded from materialized context, leaving only the original request. Current PR description/work items were used because historical versions were not archived. Live PR API credentials were not supplied through the harness environment during the replay.
- `post_back=False`; the frozen context client has no posting/voting methods. No PR comment, vote, request or production review row was created by the replay.
- Runner exited 0; its review container was removed by the real DockerRunner. The production coordinator's normalized hash stayed `efb7f03702d5036e707d56ad8b18c8fe8b2e87655fadd321547966e96f46ecef` throughout. No deployment or configuration change occurred.

Local private evidence, ignored by Git: `baseline-1131.json` and `replay-results/` beside this report. That directory contains the full outcome, per-agent streams/timings, metadata, events, rendered review, context, diff, and `comparison.json`. Five completed streams were checked for configured credentials; none required redaction. Host evidence: `/home/daniel/agent-fight-replays/1131-4191906/` on ulhgpuhamster.

Use `scripts/compare_replay.py <baseline.json> <outcome.json> --output <comparison.json>` to reproduce numeric totals. `scripts/replay_review.py` records the actual CLI run and uses the explicitly pinned saved job; its Docker runner must have the required read-only settings/skills mounts and Docker socket, as recorded above.
