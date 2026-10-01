"""/admin/reviews — the persisted history of review runs and their metrics.

List view (newest first) with status, rounds, finding counts, token/cost totals;
detail view with the posted markdown, per-round convergence, the role×round token
breakdown, and the error for a failed run. Read-only — rows are written by the
runner (see app/runner.py::run_and_record)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
import asyncio
import json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import require_login
from app.db import get_session
from app.ado import AdoClient, parse_pull_request_event
from app.orm import Review, ReviewEvent, Settings
from app.db import SessionLocal
from app.templating import templates

router = APIRouter()


def _totals(usage) -> dict:
    """Sum a review's per-role/per-round usage rows into review-level totals."""
    inp = sum(u.input_tokens for u in usage)
    out = sum(u.output_tokens for u in usage)
    cache = sum(u.cache_read_tokens + u.cache_creation_tokens for u in usage)
    costs = [u.cost_usd for u in usage if u.cost_usd is not None]
    return {
        "input": inp,
        "output": out,
        "cache": cache,
        "total": inp + out,
        "cost": sum(costs) if costs else None,
    }


def _chat_findings(records) -> list[dict]:
    """Turn verbatim findings files into a safe, template-friendly chat model."""
    messages = []
    for record in records:
        try:
            payload = json.loads(record.raw)
            items = payload.get("findings", []) if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                raise ValueError("findings payload is not a list")
            parsed = [item for item in items if isinstance(item, dict)]
            fallback = None
        except (json.JSONDecodeError, ValueError):
            parsed = []
            fallback = record.raw
        messages.append(
            {
                "role": record.role,
                "round_no": record.round_no,
                "items": parsed,
                "fallback": fallback,
            }
        )
    return messages


@router.get("/admin/reviews", response_class=HTMLResponse)
async def list_reviews(request: Request, session: AsyncSession = Depends(get_session)):
    await require_login(request)
    rows = (
        await session.execute(
            select(Review)
            .options(selectinload(Review.token_usage))
            .order_by(Review.id.desc())
            .limit(200)
        )
    ).scalars().all()
    reviews = [{"r": r, "tokens": _totals(r.token_usage)} for r in rows]
    return templates.TemplateResponse(
        request, "admin/reviews.html", {"reviews": reviews}
    )


@router.get("/admin/reviews/{review_id}", response_class=HTMLResponse)
async def review_detail(
    review_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    await require_login(request)
    review = (
        await session.execute(
            select(Review)
            .options(selectinload(Review.token_usage), selectinload(Review.findings), selectinload(Review.events))
            .where(Review.id == review_id)
        )
    ).scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    return templates.TemplateResponse(
        request,
        "admin/reviews_detail.html",
        {
            "r": review,
            "usage": review.token_usage,   # already ordered by round_no, role
            "tokens": _totals(review.token_usage),
            "findings": review.findings,   # ordered by round_no, role
            "finding_messages": _chat_findings(review.findings),
            "events": review.events,
        },
    )


@router.post("/admin/reviews/{review_id}/retry")
async def retry_review(
    review_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    """Manually retry a failed review against the PR's current commit."""
    await require_login(request)
    review = await session.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    if review.status != "failed":
        return RedirectResponse(f"/admin/reviews/{review_id}?retry=not-failed", status_code=303)
    running = await session.scalar(
        select(Review.id).where(Review.pr_id == review.pr_id, Review.status == "running").limit(1)
    )
    if running is not None:
        return RedirectResponse(f"/admin/reviews/{review_id}?retry=already-running", status_code=303)
    settings = await session.get(Settings, 1)
    if settings is None:
        return RedirectResponse(f"/admin/reviews/{review_id}?retry=not-configured", status_code=303)

    client = AdoClient()
    try:
        pr = await client.fetch_pull_request(settings, review.pr_id)
    except Exception:  # noqa: BLE001 - show a safe message; server log has detail
        return RedirectResponse(f"/admin/reviews/{review_id}?retry=lookup-failed", status_code=303)
    finally:
        await client.aclose()
    if str(pr.get("status") or "").lower() != "active":
        return RedirectResponse(f"/admin/reviews/{review_id}?retry=completed", status_code=303)

    job = parse_pull_request_event(
        {"eventType": "git.pullrequest.updated", "resource": pr}, settings
    )
    request.app.state.review_dispatcher.dispatch(job)
    return RedirectResponse(f"/admin/reviews/{review_id}?retry=queued", status_code=303)

def _event_json(e):
    return {"id": e.id, "kind": e.kind, "phase": e.phase, "message": e.message,
            "role": e.role, "round_no": e.round_no, "created_at": e.created_at.isoformat()}

@router.get("/admin/reviews/{review_id}/progress")
async def review_progress(review_id: int, request: Request, after: int = 0):
    await require_login(request)
    async with SessionLocal() as session:
        r = await session.get(Review, review_id)
        if not r: raise HTTPException(404, "review not found")
        es = (await session.execute(select(ReviewEvent).where(ReviewEvent.review_id == review_id, ReviewEvent.id > after).order_by(ReviewEvent.id))).scalars().all()
        return JSONResponse({"status": r.status, "phase": r.current_phase, "message": r.status_message,
                             "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
                             "events": [_event_json(e) for e in es]})

@router.get("/admin/reviews/{review_id}/events")
async def review_events(review_id: int, request: Request):
    await require_login(request)
    async def stream():
        last = int(request.headers.get("last-event-id") or 0)
        while True:
            async with SessionLocal() as session:
                r = await session.get(Review, review_id)
                if not r: return
                es = (await session.execute(select(ReviewEvent).where(ReviewEvent.review_id == review_id, ReviewEvent.id > last).order_by(ReviewEvent.id))).scalars().all()
                for e in es:
                    last = e.id
                    yield f"id: {e.id}\ndata: {json.dumps(_event_json(e))}\n\n"
                if r.status != "running":
                    yield f"event: done\ndata: {json.dumps({'status': r.status})}\n\n"; return
            yield ": heartbeat\n\n"
            await asyncio.sleep(3)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache"})
