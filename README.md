# agent-fight

A FastAPI web service that runs a multi-agent code-review consensus loop:
independent harnesses (Claude, Codex, ...) review the same Azure DevOps pull
request and debate their findings through a coordinator until they converge
or hit a round cap. See `plans/code-review-consensus.md` for the full design.

All run configuration is entered through `/admin/settings` rather than
hardcoded: the round cap, the ADO account/org/project connection and its PAT,
the webhook shared secret used to authenticate inbound ADO calls, the clone
credential (token or SSH key), and the **inference-proxy connection** (base URL
+ API key) that both the harnesses and the coordinator route model calls
through.

The webhook receiver at `/webhooks/ado` authenticates the inbound call, parses
the ADO PR service-hook payload into a `ReviewJob`, and dispatches the review
run in the background (returning `202` immediately). The pipeline itself is
built:

- `app/ado.py` — parse the PR webhook payload; post the final review back as a
  PR thread comment.
- `app/proxy.py` — inference-proxy client (`ping`, Anthropic-Messages
  `complete`) used by the coordinator; a non-fatal startup health-ping and a
  `GET /health/proxy` diagnostic.
- `app/harness.py` — per-harness `build_argv` / `parse_line` (normalizing
  stream-json into one event vocabulary) / `harness_env` (points each CLI at
  the proxy).
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

Then open `http://localhost:8890/admin/settings` (after logging in at
`/login`) to configure the ADO connection, webhook secret, clone credential,
and the inference-proxy base URL + API key.

## Tests

```bash
pytest
```
