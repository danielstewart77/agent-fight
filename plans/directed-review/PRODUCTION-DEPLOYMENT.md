# Production deployment — November 5, 2026

Deployed at **17:31:42 UTC (11:31:42 CST)** to
`daniel@ulhgpuhamster:/home/daniel/agent-fight`.

Runtime candidate: `6105be521f8b947b9c878554e82cb378f5c2a3bc`
(application change `1d4e4e3`), branch `feature/directed-review-rounds`.
Running image: `agent-fight:directed-6105be5`, image ID
`sha256:e41c577a5e7177b648be803f7757d9561f634ceb5e13016d881f0c2372b2ff48`.

## Preservation

The production checkout had extensive uncommitted changes and security patches.
Existing changed runtime files matched the feature baseline after normalizing
line endings. Only the five feature application files were replaced; existing
deployment configuration was preserved except for its image tag. Both mounted
Agent Fight review skills matched the bundled baseline and were updated.

The release image derives from the previous patched production image rather
than rebuilding the candidate's older Dockerfile. Python dependency inventories
are identical; `libbrotli1` remains `1.1.0-2+deb13u1`. Production Dockerfile,
requirements, credentials, hook settings, review harness image, and unrelated
source changes were preserved. No schema changes were required.

Private backup/evidence directory:
`/home/daniel/agent-fight-backups/directed-20261105T172919Z/`.
It contains the Git bundle, original source/configuration archive, working-tree
patch, skill archive, consistent SQLite backups, candidate and baseline source,
changed-file originals, release recipe, startup log, and deployment metadata.

## Verification

- 254 tests passed on Linux in 42.89 seconds against staged production source.
- All release application files matched the pinned tested candidate.
- The actual release image bootstrapped an isolated database copy with networking
  disabled; schema stayed at `0012_review_requests`, integrity passed, and all
  1,136 reviews remained.
- The service was paused for a final idle check before replacement, preventing
  a request from starting between the check and the restart. No active or queued
  reviews were interrupted.
- Exact deployed hashes matched all five candidate application files. Loaded
  production code uses parallel `gather` and ledger assignments without judge
  calls in `run_review`.
- Health passed both on the host and from the Windows workstation at
  `http://ulhgpuhamster:8890/health`; trigger remains `pr_comment`.
- Inference proxy reported `up`; startup completed normally.
- Counts before/after were identical: 1,136 reviews, 363 requests, 5,452 findings,
  7,427 token-usage records, and 3 users.

The previously completed isolated PR 5126 replay established behavior and cost
changes; see [COMPARISON.md](COMPARISON.md). Deployment verification did not
request another live review or post PR messages.

## Rollback

Previous image is retained as
`agent-fight:before-directed-20261105T172919Z` (original image
`sha256:d26bbfcdcf2693a0aa16ece0508909d43afd5896d0ad2deefea07fec087756e1`).

After draining active reviews, change only the Agent Fight Compose image to
that rollback tag and recreate only this service:

```sh
cd /home/daniel/agent-fight
docker compose up -d --no-build --no-deps --force-recreate agent-fight
curl --fail http://localhost:8890/health
```

Restore the two mounted review skills from the private `skills.tar.gz` archive
when rolling back. Source originals for the four replaced application files
and Compose file are under `changed-files/`; remove the newly added
`app/review/ledger.py` when restoring source. Check for subsequent edits before
restoring any file. **Keep the current production database**: image rollback
does not require restoring an older database over newer review history.
