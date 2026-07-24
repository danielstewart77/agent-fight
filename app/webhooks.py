"""POST /webhooks/ado — inbound Azure DevOps PR-submission webhook.

Authenticates the inbound call against `Settings.webhook_secret` (configured
through /admin/settings) via a shared-secret header check, then parses the ADO
service-hook payload into a `ReviewJob` and dispatches the review run in the
background, returning 202 immediately. The heavy lifting (container, harness
rounds, post-back) happens off the request path — see app/runner.py and
app/coordinator.py.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ado import MalformedPayload, UnsupportedEvent, parse_pull_request_event
from app.db import get_session
from app.orm import Review, Settings

router = APIRouter()

logger = logging.getLogger("agent_fight.webhooks")

SETTINGS_ID = 1


async def _already_reviewed(session: AsyncSession, pr_id: int, source_commit: str) -> bool:
    """True if this PR's source commit was already reviewed (or is being
    reviewed) and did NOT fail — the loop-guard so a `pullrequest.updated`
    event fired by our own comment/vote (same commit) doesn't re-trigger.

    A failed prior review is NOT a match (we want retries to go through), and a
    missing/unknown commit never matches (guard is best-effort; the subscription
    filter is the primary control)."""
    if not source_commit:
        return False
    row = (
        await session.execute(
            select(Review.id)
            .where(Review.pr_id == pr_id)
            .where(Review.source_commit == source_commit)
            .where(Review.status != "failed")
            .limit(1)
        )
    ).first()
    return row is not None


@router.post("/webhooks/ado", status_code=202)
async def receive_ado_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(None, alias="X-Webhook-Secret"),
    session: AsyncSession = Depends(get_session),
):
    row = (
        await session.execute(select(Settings).where(Settings.id == SETTINGS_ID))
    ).scalar_one_or_none()

    if row is None or not row.webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="webhook_secret is not configured yet — set it at /admin/settings",
        )

    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, row.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Webhook-Secret header")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any non-JSON body is a client error
        raise HTTPException(status_code=400, detail="request body is not valid JSON")

    try:
        job = parse_pull_request_event(payload, row)
    except UnsupportedEvent as exc:
        # Well-formed but not an event we act on — acknowledge without dispatching.
        return {"status": "ignored", "detail": str(exc)}
    except MalformedPayload as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Loop-guard: don't re-review a source commit we've already reviewed (an
    # updated-event fired by our own comment/vote reports the same commit).
    if await _already_reviewed(session, job.pr_id, job.source_commit):
        logger.info(
            "PR %s: skipping — commit %s already reviewed",
            job.pr_id, job.source_commit[:12],
        )
        return {"status": "skipped", "detail": "commit already reviewed", "pr_id": job.pr_id}

    request.app.state.review_dispatcher.dispatch(job)
    return {"status": "accepted", "pr_id": job.pr_id}
