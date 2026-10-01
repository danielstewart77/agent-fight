# On-demand reviews: production deployment, 2026-10-01

Production: `daniel@ulhgpuhamster:/home/daniel/agent-fight`.

## Source preservation

The initial local implementation used an older checkout. Production was at
`d485879` with additional uncommitted review progress, author, retry, UI, and
coordinator changes. Those files were imported into the local
`feature/on-demand-production` branch and preserved in commit `15798c0` before
porting the command feature. The initial implementation remains on
`feature/on-demand-pr-reviews` (`15230c5`).

The deployed implementation is `9894698`, including the shutdown correction.
Production's existing working changes were preserved and the feature applied
as a patch, without resetting the deployment checkout or changing `.env`.

## Behavior

- Post `/agent-fight review` as an entire new PR comment or reply.
- Each new comment requests a review, even when the commit is unchanged.
- Duplicate deliveries/edits of an accepted comment do not request another run.
- PR creation, pushes, ordinary discussion and bot status replies do not trigger reviews.
- Existing production behavior is retained: numbered review threads, reviewer
  votes, admin review history, per-round usage/findings and live progress.
- The admin Retry action uses the same durable per-PR queue.
- Source and target commits are pinned during request preparation.
- One application process per database; overall concurrency remains controlled
  by `REVIEW_CONCURRENCY`.

## Deployment and rollback assets

Backups are on the server in
`/home/daniel/agent-fight-backups/on-demand-20261001/`:

- `repository.bundle`, `source.tar.gz`, `working-tree.patch`: original code,
  history, and uncommitted changes.
- `production.db`: consistent SQLite backup containing 773 pre-deployment reviews.
- `candidate.tar.gz`, `on-demand.patch`: deployed candidate and feature patch.
- `migration-check.db`: copy used to verify the migration, separate from production.

Images: `agent-fight:on-demand-20261001` (new),
`agent-fight:before-on-demand-20261001` (rollback), and `agent-fight:latest`
(current). Migration `0012_review_requests` adds a table/index without changing
existing review data. The previous image can operate with this additive schema;
do not restore the old database over newer review history for an image rollback.

For an image rollback, retag the `before-on-demand` image as `agent-fight:latest`
and recreate only the `agent-fight` Compose service with `--no-build`. Switch the
service hooks back as a separate step; old code does not handle comment commands.
Restore source from the backup or reverse the feature patch only after checking
for subsequent edits.

## ADO subscriptions

Organization: `ulandco`; project ID: `46ad0b6b-e752-4dc6-8805-e888b09f843d`.
Existing endpoint settings and secret were reused.

| Event | Subscription | State after deployment |
|---|---|---|
| PR commented on | `af1c48e6-91b6-4ea8-b9a6-e0658711e57f` | Enabled |
| PR updated | `8ebd3876-1c29-4502-b65f-547b58de7913` | Disabled by user |
| PR created | `e9ce3dbf-27b4-408d-a5e8-ea3dd2236057` | Disabled by user |

The old subscriptions restricted PR authors to a group. Comment events do not
have that filter; anyone able to comment in the existing project/repository/
branch scope can request a review.

## Verification

- 241 automated tests passed on the production host in an isolated directory.
- A shutdown warning from that run was corrected; the six affected queue and
  HTTP integration checks passed again on both Windows and Linux without warnings.
- Docker image built successfully; migration against a copy of the live database
  retained all 773 records.
- Local and public health report `review_trigger: pr_comment` after deployment.
- Live verification PR: CodeReview !4924, temporary documentation-only branch.
- PR creation produced no request; a real command created a request and status reply.
- Replaying that command returned `duplicate`; a legacy update returned `ignored`.
- Two distinct command comments on the same source commit produced exactly two
  completed requests (1 and 2) and converged reviews (774 and 775), serialized
  for the PR. Both status replies reported completion.
- Result threads 17683 and 17684 retained numbered review behavior; the latter
  was labeled `re-review #2`. Bot replies created no additional requests.
- The temporary PR was abandoned and its branch deleted after verification;
  nothing was merged.
- Final checks confirmed public health, the enabled comment subscription, and
  disabled creation/update subscriptions. Production application and migration
  sources match the tested candidate after normalizing line endings.
