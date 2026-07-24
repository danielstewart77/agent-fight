"""/login, /logout, /admin/ landing.

Credentials are checked against the `users` table (see app/auth.py); the
session carries the authenticated user's id and username.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    authenticate,
    is_authenticated,
    login_session,
    logout_session,
    require_login,
)
from app.db import get_session
from app.orm import Review, ReviewTokenUsage, Settings
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/admin/", status_code=303)
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = await authenticate(session, username, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Invalid username or password"},
            status_code=401,
        )
    login_session(request, user)
    return RedirectResponse("/admin/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/admin/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    await require_login(request)
    settings_configured = (
        await session.execute(select(Settings).where(Settings.id == 1))
    ).scalar_one_or_none() is not None

    total = await session.scalar(select(func.count(Review.id))) or 0
    converged = await session.scalar(
        select(func.count(Review.id)).where(Review.status == "converged")
    ) or 0
    failed = await session.scalar(
        select(func.count(Review.id)).where(Review.status == "failed")
    ) or 0
    avg_rounds = await session.scalar(
        select(func.avg(Review.rounds_used)).where(
            Review.status.in_(("converged", "capped"))
        )
    )
    total_tokens = await session.scalar(
        select(func.sum(ReviewTokenUsage.input_tokens + ReviewTokenUsage.output_tokens))
    ) or 0
    total_cost = await session.scalar(select(func.sum(ReviewTokenUsage.cost_usd)))

    metrics = {
        "total": total,
        "converged": converged,
        "failed": failed,
        "converged_pct": round(100 * converged / total) if total else None,
        "avg_rounds": round(float(avg_rounds), 1) if avg_rounds is not None else None,
        "total_tokens": int(total_tokens),
        "total_cost": float(total_cost) if total_cost is not None else None,
    }
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {"settings_configured": settings_configured, "metrics": metrics},
    )
