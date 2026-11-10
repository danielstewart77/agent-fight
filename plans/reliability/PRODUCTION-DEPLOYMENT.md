# Reliability deployment — November 10, 2026

Deployed 2026-11-10 15:06:43 UTC (09:06:43 CST) on daniel@ulhgpuhamster,
/home/daniel/agent-fight. Runtime commit e422f56c4c87a5c890f096fdc93baffe68a30459,
branch fix/duplicate-review-reliability. Later documentation/helper commits do
not change the application in this image.

Image: agent-fight:reliability-e422f56.
Image ID: sha256:4d97b2e46d4d6edf0cd982a79c31597cf37e7402cf28ceb0c2302ba4bb1c5f02.
Backup: /home/daniel/agent-fight-backups/reliability-20261110T150309Z/.
Rollback tag: agent-fight:before-reliability-20261110T150309Z.
Previous image: sha256:e41c577a5e7177b648be803f7757d9561f634ceb5e13016d881f0c2372b2ff48.

## Delivered behavior

Duplicate relationship hints preserve explicit verdict semantics. Only authored
resolved responses can withdraw a finding. Unsafe duplicate withdrawals remain
open for author clarification, keeping the original claims; the round cap
reports remaining concerns with a wait-for-author vote. No human runtime gate.
Failed reviews retain completed raw responses and measured token accounting,
including ledger/parse/transport failures and cancellation. Progress commits
are not interrupted by double cancellation during sibling cleanup.

## Verification and preservation

- Windows gate: 271 passed in 138.39 seconds.
- Staged production source on Linux: 271 passed in 47.89 seconds.
- Actual candidate image: 40 focused production ledger/coordinator/runner
  checks passed in 0.63 seconds, including automatic recovery and failed-run
  persistence through isolated SQLite fixtures and scripted external CLIs.
- Candidate bootstrapped an isolated consistent DB copy with networking disabled:
  schema 0012_review_requests, integrity OK, all 1,171 reviews retained.
- Python dependency inventories identical to the patched deployed image;
  libbrotli1 remains 1.1.0-2+deb13u1.
- Four deployed app hashes match pinned candidate: coordinator, ledger, review
  models and runner. No unrelated source/skills/security configuration changed.
- A paused final check found zero running reviews and zero queued/active
  requests. Only agent-fight was recreated; no active review was interrupted.
- Counts before/after identical: 1,171 reviews, 398 requests, 5,593 raw findings,
  7,568 token-usage records, 3 users. Integrity passed.
- Startup completed without traceback. Health passed on the host and workstation
  at http://ulhgpuhamster:8890/health; trigger remains pr_comment.

The protected backup contains source originals, Compose, dirty-tree patch,
consistent preparation/pre-switch databases, candidate/base files, release
recipe, metadata, Linux/image/schema logs and startup log. Failed historical
raw responses/tokens were absent before this repair and cannot be recovered.
No additional live model review was requested or PR comment/vote posted by
verification. The future observed failure rate remains to be measured.

## Rollback

Drain active/queued reviews first. Confirm no subsequent source/config edits
would be overwritten. Restore only the four app files and docker-compose.yml
from backup/originals; recreate only agent-fight using Compose with no build:

```sh
cd /home/daniel/agent-fight
docker compose up -d --no-build --no-deps --force-recreate agent-fight
curl --fail http://localhost:8890/health
```

The original Compose file names agent-fight:directed-6105be5. The additional
rollback tag preserves its exact image. Keep the current operational database;
there was no schema migration, and restoring an old DB would lose newer history.
Mounted skills were unchanged and do not need restoring for this repair.
