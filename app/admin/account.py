"""/admin/account — change your own password.

Requires the current password (so a hijacked-but-idle session can't silently
lock the real owner out by changing it) and a matching confirmation. Passwords
are stored bcrypt-hashed via app/security.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_uid, require_login
from app.db import get_session
from app.orm import User
from app.security import MAX_PASSWORD_BYTES, hash_password, verify_password
from app.templating import templates

router = APIRouter()

MIN_PASSWORD_LEN = 8


def validate_new_password(new_password: str, confirm: str) -> str | None:
    """Return an error message, or None if the new password is acceptable."""
    if len(new_password) < MIN_PASSWORD_LEN:
        return f"New password must be at least {MIN_PASSWORD_LEN} characters."
    if len(new_password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"New password must be at most {MAX_PASSWORD_BYTES} bytes."
    if new_password != confirm:
        return "New password and confirmation do not match."
    return None


async def _current_user(request: Request, session: AsyncSession) -> User | None:
    uid = current_uid(request)
    if uid is None:
        return None
    return (
        await session.execute(select(User).where(User.id == uid))
    ).scalar_one_or_none()


@router.get("/admin/account", response_class=HTMLResponse)
async def show_account(request: Request, session: AsyncSession = Depends(get_session)):
    await require_login(request)
    user = await _current_user(request, session)
    return templates.TemplateResponse(
        request, "admin/account.html", {"user": user, "error": None, "ok": None}
    )


@router.post("/admin/account/password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    await require_login(request)
    user = await _current_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    def render(error=None, ok=None, status_code=200):
        return templates.TemplateResponse(
            request,
            "admin/account.html",
            {"user": user, "error": error, "ok": ok},
            status_code=status_code,
        )

    if not verify_password(current_password, user.password_hash):
        return render(error="Current password is incorrect.", status_code=400)

    msg = validate_new_password(new_password, confirm)
    if msg:
        return render(error=msg, status_code=400)

    user.password_hash = hash_password(new_password)
    await session.commit()
    return render(ok="Password updated.")
