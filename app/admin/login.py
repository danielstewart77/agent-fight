"""/login, /logout, /admin/ landing.

Credentials are checked against the `users` table (see app/auth.py); the
session carries the authenticated user's id and username.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    authenticate,
    is_authenticated,
    login_session,
    logout_session,
    require_login,
)
from app.db import get_session
from app.orm import Settings
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
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {"settings_configured": settings_configured},
    )
