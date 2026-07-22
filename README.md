# agent-fight

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
proxy's `/models` listing. Operator accounts are managed at `/admin/users`.

The harnesses connect to the proxy the way the proxy's own installers do
(mirrored from `install-claude.sh` / `install-codex.sh`): Claude via
`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` env (never `ANTHROPIC_AUTH_TOKEN`);
Codex via `~/.codex/auth.json` + `~/.codex/config.toml` (a custom
`azure_proxy` provider) written into the review container.

The webhook receiver at `/webhooks/ado` authenticates the inbound call, parses
the ADO PR service-hook payload into a `ReviewJob`, and dispatches the review
run in the background (returning `202` immediately). The pipeline itself is
built:

- `app/ado.py` — parse the PR webhook payload; post the final review back as a
  PR thread comment.
- `app/proxy.py` — coordinator's inference-proxy client (`ping`,
  Anthropic-Messages `complete`), `list_models` for the admin dropdown, and
  base-URL root normalization; a non-fatal startup health-ping and a
  `GET /health/proxy` diagnostic.
- `app/harness.py` — per-harness `build_argv` / `parse_line` (normalizing
  stream-json into one event vocabulary) / `harness_env` + the Codex
  `auth.json`/`config.toml` builders.
- `app/container.py` — the per-review `docker run` lifecycle behind a
  `ContainerRunner` interface (`DockerRunner` real, `FakeRunner` for tests);
  single clone + fetch target/PR refs + checkout PR branch.
- `app/coordinator.py` — the round-loop consensus: independent round 1,
  conversational cross-exposure in round 2+, convergence diff by
  `(file, line, verdict)`, inclusive synthesis at the round cap with
  disagreements surfaced rather than tiebroken.

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
