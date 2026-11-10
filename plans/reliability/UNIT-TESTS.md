# Test map — protection-list delta v2 accepted 2026-11-10

All cases belong to our gate and call imported production code. Expected results
derive from the accepted behavior in IMPLEMENTATION.md.
Transport/container responses are the only scripted review boundary. Tests use
isolated SQLite fixtures; no operational database writes or model-output grading.

| Case | Protection | Real entry point | Inputs and expected result | Breaking source change |
| --- | --- | --- | --- | --- |
| Hints preserve verdicts | REL-P1 | FindingLedger.apply/result | Owner/opponent issue/agree/disagree/resolved with optional duplicate metadata; same explicit agreement/objection/withdrawal semantics without whole-run failure | apply reinstates permission exception for hints |
| Cycles remain answerable | REL-P1 | FindingLedger.apply/assigned/result | Reciprocal and multi-node loops; claims stay open and authors clarify; later valid withdrawal/confirmation converges without losing surviving claim | apply discards all cyclic entries or leaves no responder |
| Chains preserve survivor | REL-P1 | FindingLedger.apply/result | Valid duplicate chain and withdrawn endpoint; real survivor kept, invalid endpoints request clarification; unrelated settled defects preserved | duplicate resolution treats withdrawn endpoint as survivor |
| Recovery reaches final output | REL-P1 | run_review | Scripted duplicate loop followed by clarification/acceptance, and omission at cap; actual markdown includes survivor, agreement stays explicit, unresolved vote is wait | coordinator synthesizes empty/approved result while obligations remain |
| Ledger failure retains evidence | REL-P2 | run_and_record | Valid independent responses followed by duplicate response ID (still invalid); failed row contains exact completed raw outputs and measured usage, no final review | outer exception boundary omits attachment or runner omits failed persistence |
| Parse/transport failure retains evidence | REL-P2 | run_and_record | Malformed completed raw file or transport failure after earlier completion; completed evidence retained with failed status | batch cancellation/outer boundary drops prior records |

Existing normal author withdrawal, severity acceptance, unassigned/duplicate ID
validation, parallel scheduling and cancellation cases remain within accepted
directed-review P-01/P-02. This document is a plan, not executed test evidence.
