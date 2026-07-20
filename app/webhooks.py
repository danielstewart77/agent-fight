"""POST /webhooks/ado — inbound Azure DevOps PR-submission webhook.

Authenticates the inbound call against `Settings.webhook_secret` (configured
through /admin/settings — see app/admin/settings.py) via a simple shared-secret
header check. Azure DevOps service hooks can be configured with Basic auth or
a custom header depending on setup; the plan leaves the exact payload shape
and route list as an open question, so this deliberately does not parse or
act on the ADO payload yet. It only authenticates the call and reports that
the review pipeline (container lifecycle, harness adapter, round-loop
coordinator — see plans/code-review-consensus.md) is not yet implemented.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.orm import Settings

router = APIRouter()

SETTINGS_ID = 1


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

    return {"status": "accepted", "detail": "review pipeline not yet implemented"}
