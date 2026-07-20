# agent-fight

A FastAPI web service that runs a multi-agent code-review consensus loop:
independent harnesses (Claude, Codex, ...) review the same Azure DevOps pull
request and debate their findings through a coordinator until they converge
or hit a round cap. See `plans/code-review-consensus.md` for the full design.

This first slice is the FastAPI skeleton plus the admin-configured settings
surface: the round cap, the ADO account/org/project connection and its PAT,
the webhook shared secret used to authenticate inbound ADO calls, and the
clone credential (token or SSH key) are all entered through `/admin/settings`
rather than hardcoded. The webhook receiver at `/webhooks/ado` authenticates
the inbound call and reports the review pipeline is not yet implemented —
the per-review Docker container lifecycle, harness adapter, and round-loop
coordinator are future work.

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
`/login`) to configure the ADO connection, webhook secret, and clone
credential.

## Tests

```bash
pytest
```
