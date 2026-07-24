"""/admin/reviews — the persisted history of review runs and their metrics.

List view (newest first) with status, rounds, finding counts, token/cost totals;
detail view with the posted markdown, per-round convergence, the role×round token
breakdown, and the error for a failed run. Read-only — rows are written by the
runner (see app/runner.py::run_and_record)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import require_login
from app.db import get_session
from app.orm import Review
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
            .options(selectinload(Review.token_usage))
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
        },
    )
