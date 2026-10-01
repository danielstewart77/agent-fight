"""Authenticate and durably accept explicit ADO PR comment commands."""

from __future__ import annotations

import hmac
import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ado import MalformedPayload, UnsupportedEvent, parse_comment_event, request_key
from app.db import get_session
from app.orm import ReviewRequest, Settings

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

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any non-JSON body is a client error
        raise HTTPException(status_code=400, detail="request body is not valid JSON")

    try:
        job = parse_comment_event(payload, row)
    except UnsupportedEvent as exc:
        # Well-formed but not an event we act on — acknowledge without dispatching.
        return {"status": "ignored", "detail": str(exc)}
    except MalformedPayload as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not row.ado_organization or not row.ado_pat:
        raise HTTPException(status_code=503, detail="Configure the ADO organization and PAT first")

    result = await session.execute(insert(ReviewRequest).values(
        request_key=request_key(job), job_json=json.dumps(asdict(job)), status="pending",
    ).on_conflict_do_nothing(index_elements=["request_key"]))
    await session.commit()
    # Commit before acknowledging: a restart between this response and dispatch
    # cannot lose the command. The worker polls this inbox independently.
    return {"status": "accepted" if result.rowcount else "duplicate", "pr_id": job.pr_id}
