# Agent🥊🤖🥊Fight

A FastAPI web service that runs a multi-agent code-review consensus loop:
independent harnesses (Claude, Codex, ...) review the same Azure DevOps pull
request and debate their findings through a coordinator until they converge
or hit a round cap. See `plans/code-review-consensus.md` for the full design.

Run configuration is entered through the admin console rather than hardcoded.
`/admin/settings` holds the round cap, the ADO account/org/project connection
and its PAT, the webhook shared secret, and the clone credential (token or SSH
key). `/admin/proxy` holds the **three inference-proxy connections** — one each
for the Claude harness, the Codex harness, and the coordinator — as base URL +
API key + model, with the model chosen from a dropdown populated live from the
proxy's `/models` listing. `/admin/skills` clones the `claude-skills` /
`codex-skills` repos to `SKILLS_ROOT` and lets you pick, per harness, which
skills are bind-mounted read-only into its review container (`~/.claude/skills`
/ `~/.codex/skills`) — the `agent-fight-review` skill instructs each harness on
the review + findings format. Operator accounts are managed at `/admin/users`.

The harnesses connect to the proxy the way the proxy's own installers do
(mirrored from `install-claude.sh` / `install-codex.sh`): Claude via
`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` env (never `ANTHROPIC_AUTH_TOKEN`);
Codex via `~/.codex/auth.json` + `~/.codex/config.toml` (a custom
`azure_proxy` provider) written into the review container.

## On-demand PR reviews

Post this as the entire text of a new PR comment (or reply):

```text
/agent-fight review
```

The same command requests both initial reviews and re-reviews. Developers use
their existing ADO accounts; no Agent Fight login or new Entra app
registration is needed. Existing review voting behavior is preserved. Anyone permitted to comment on
a PR covered by the subscription can request a review. An explicit command on
an active draft PR is also allowed.

Agent Fight replies to the command with queued/running/completed/failed status
and posts a new numbered review thread, preserving production review history. Each accepted request pins
the source and target commit IDs read from ADO when the request is prepared;
the acknowledgment and results identify that revision. Later pushes do not
start reviews or change a queued review's commits. Post a **new comment** for
another review, even if the code has not changed. Editing or redelivering an
already accepted command never requests a second run. Quoted commands, code
blocks, normal discussion, and Agent Fight's own replies are ignored.

### ADO service hook setup

In **Project settings → Service hooks**, create a **Web Hooks** subscription:

- Event: **Pull request commented on** (`ms.vss-code.git-pullrequest-comment-event`).
- Repository/target branch: select the intended scope (all repositories if desired).
- URL: the existing externally reachable Agent Fight URL plus `/webhooks/ado`.
- HTTP header: `X-Webhook-Secret: <the secret configured in /admin/settings>`.
- Send full resource details (resource version `2.0` when creating through the API).

Keep the existing ADO organization/PAT and clone credentials. The PAT needs
repository/PR read access and PR comment create/update permission. Outbound
comments appear under the PAT owner's identity. Commands from that same person
are allowed; self-trigger prevention uses command syntax, not author identity.

Deploy the new code and run the database migration **before** enabling the new
subscription. Then disable the old **Pull request created** and **Pull request
updated** subscriptions. The handler also ignores these old events, so stale
subscriptions cannot cause automatic reviews after deployment. ADO's canned
test payload may be ignored because it doesn't contain the command; verify
delivery with a real command on a test PR.

Alternatively, `scripts/switch_comment_hook.py` can copy the existing endpoint
options and shared secret into the new subscription. Set `AZURE_DEVOPS_EXT_PAT`
to a PAT with service-hook management permission and run:

```powershell
python scripts/switch_comment_hook.py --organization ORG --project-id PROJECT_GUID --webhook-url https://HOST/webhooks/ado
```

This previews the change. Add `--apply` after deployment to enable the comment
subscription and disable the matching legacy subscriptions. The script checks
the new service's health capability before changing anything and doesn't print
credentials. It preserves repository/branch scope. The old PR-author filter is
not available on comment events: any permitted commenter in that scope can
request a review.

### Queue and recovery

The webhook commits a durable request to SQLite before returning `202`.
Duplicates are identified by organization/repository/PR/thread/comment, not
commit or event ID. The worker verifies the live comment and PR through ADO
before acknowledging. Requests for the same PR run in order, bounded by the existing `REVIEW_CONCURRENCY` setting. Admin retries use this same queue. Waiting requests are acknowledged independently.
Closed PRs are skipped, including PRs closed while a review is running.

Use **one application process/replica per database**, as the supplied entrypoint
does. The scheduling and concurrency limit are process-local. Preserve the
`data` volume. Pending and queued requests resume after restart; interrupted
running reviews are marked failed and require a fresh command, avoiding an
automatic replay after a result might already have been posted. Failures are
logged server-side and reported on the request when ADO is reachable. An ADO
outage can prevent that notification; the durable request status remains in
`review_requests`. Completed requests remain as the deduplication history.

The review pipeline consists of:

- `app/ado.py` — parse the PR webhook payload; `fetch_pr_context` (description,
  work items, and every comment thread + reply, rendered for the reviewers);
  post/update the review thread and cast the reviewer vote.
- `app/proxy.py` — coordinator's inference-proxy client (`ping`,
  Anthropic-Messages `complete`), `list_models` for the admin dropdown, and
  base-URL root normalization; a non-fatal startup health-ping and a
  `GET /health/proxy` diagnostic.
- `app/harness.py` — per-harness `build_argv` / `parse_line` (normalizing
  stream-json into one event vocabulary) / `harness_env` / `ado_env` (the PR
  REST endpoint + PAT header the reviewers curl) + the Codex
  `auth.json`/`config.toml` builders.
- `app/container.py` — the per-review `docker run` lifecycle behind a
  `ContainerRunner` interface (`DockerRunner` real, `FakeRunner` for tests);
  single clone + fetch target/PR refs + checkout PR branch, and the
  `/work/pr.diff` + `/work/pr-context.md` the harnesses read.
- `app/coordinator.py` — the fight loop (below).

### The fight

Round 0 runs Claude and Codex independently **in parallel**, using the pinned
checkout, diff, PR context and configured models. Valid empty reviews from both
agents finish immediately. Failed or malformed outputs never count as clean.

Round 1 runs both cross-reviews **in parallel** against the same immutable
round-0 snapshot. The coordinator assigns stable finding IDs and original
owners. Each reviewer explicitly responds to the other's findings.

Rounds 2 and later run **only the agent that owes the next response**. If Claude
challenges a Codex finding, Codex either withdraws it or defends/revises it. A
withdrawal closes that authored claim. A defense goes back to Claude for
acceptance or continued objection. Reciprocal obligations run concurrently,
batched by reviewer. Later prompts include only assigned open items; settled
findings stay in the coordinator's ledger.

Completion and reviewer votes come from explicit ledger state, with no LLM judge
call. A revision of a claim or severity requires acknowledgment. Missing replies
remain open. The round cap is a maximum, not a requirement to run every round;
`3` permits independent round 0 and fight rounds 1, 2, 3. At the cap, surviving
concerns and both arguments of unresolved disputes are posted without a tiebreak.
The saved coordinator proxy connection remains available for legacy diagnostics;
it is not used by this loop.

Findings retain evidence, fixes and per-round raw JSON. An author's replies to
previous reviews arrive in the PR context. Agents must verify arguments against
the code, and must not re-raise a refuted finding without answering that evidence.

**Not exercised in this environment.** The Docker driver, the real harness
CLIs, the live proxy, and real ADO payloads/REST calls cannot run here, so the
constants they depend on are marked as assumptions in each module's docstring
(CLI flags and stream-json schemas, provider env-var names, the proxy auth
header/routes, ADO field paths, REST api-version, and clone-credential
embedding). Confirm each against the live services on first deploy — every one
is isolated to a single module. The pure core (payload parsing, convergence,
argv/stream parsing, the proxy/ADO clients against a mock transport, and the
full coordinator loop against `FakeRunner`) is covered by the test suite.

Structured like the sibling `inference-proxy` project: `app/{main.py,
config.py, db.py, orm.py, auth.py}`, async SQLAlchemy 2.0 with SQLite,
Alembic migrations, session-cookie admin auth, Jinja2 templates.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then edit ADMIN_USERNAME/ADMIN_PASSWORD/SESSION_SECRET
alembic upgrade head
uvicorn app.main:app --reload --port 8890
```

Then log in at `/login` and configure the ADO connection, webhook secret, and
clone credential at `/admin/settings`, and the three inference-proxy
connections at `/admin/proxy`.

## Review container image

Each PR review runs in an ephemeral container built from `Dockerfile.review`
(git + Node + the Claude and Codex CLIs). Build it once:

```bash
docker build -f Dockerfile.review -t agent-fight-review:latest .
```

The image tag is overridable with `REVIEW_IMAGE`. The app drives it via the
host Docker socket (Docker-out-of-Docker — see `docker-compose.yml`).

## Tests

```bash
pytest
```
