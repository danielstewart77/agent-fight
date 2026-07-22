"""/admin/users — manage operator accounts (flat model: any operator can).

List users, add a user, reset a user's password, and delete a user. Deleting
the last remaining user is refused so the console can't be locked out.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import normalize_username, require_login
from app.db import get_session
from app.orm import User
from app.security import MAX_PASSWORD_BYTES, hash_password
from app.templating import templates

router = APIRouter()

MIN_PASSWORD_LEN = 8
USERNAME_RE = re.compile(r"^[a-z0-9._-]{1,256}$")


def _password_error(password: str, confirm: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"Password must be at most {MAX_PASSWORD_BYTES} bytes."
    if password != confirm:
        return "Password and confirmation do not match."
    return None


async def _list_users(session: AsyncSession) -> list[User]:
    return list(
        (await session.execute(select(User).order_by(User.username))).scalars().all()
    )


async def _render(request: Request, session: AsyncSession, *, error=None, status_code=200):
    users = await _list_users(session)
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {"users": users, "error": error},
        status_code=status_code,
    )


@router.get("/admin/users", response_class=HTMLResponse)
async def show_users(request: Request, session: AsyncSession = Depends(get_session)):
    await require_login(request)
    return await _render(request, session)


@router.post("/admin/users", response_class=HTMLResponse)
async def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    await require_login(request)
    uname = normalize_username(username)
    if not USERNAME_RE.match(uname):
        return await _render(
            request, session,
            error="Username may contain only lowercase letters, digits, dot, dash, underscore.",
            status_code=400,
        )
    pwd_err = _password_error(password, confirm)
    if pwd_err:
        return await _render(request, session, error=pwd_err, status_code=400)

    exists = (
        await session.execute(select(User).where(User.username == uname))
    ).scalar_one_or_none()
    if exists is not None:
        return await _render(request, session, error=f"User {uname!r} already exists.", status_code=400)

    session.add(User(username=uname, password_hash=hash_password(password)))
    await session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/password", response_class=HTMLResponse)
async def reset_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    confirm: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    await require_login(request)
    pwd_err = _password_error(password, confirm)
    if pwd_err:
        return await _render(request, session, error=pwd_err, status_code=400)

    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        return await _render(request, session, error="No such user.", status_code=404)

    user.password_hash = hash_password(password)
    await session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(
    request: Request,
    user_id: int,
    session: AsyncSession = Depends(get_session),
):
    await require_login(request)
    count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if count <= 1:
        return await _render(
            request, session,
            error="Cannot delete the last remaining user — the console would be locked out.",
            status_code=400,
        )

    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        return await _render(request, session, error="No such user.", status_code=404)

    await session.delete(user)
    await session.commit()
    return RedirectResponse("/admin/users", status_code=303)
