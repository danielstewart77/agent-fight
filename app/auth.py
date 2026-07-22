"""Admin console session auth, backed by the `users` table.

Every operator has a `User` row (see app/orm.py); login authenticates the
submitted credentials against it and the session then carries that user's
identity. The model is flat — any logged-in user is a full operator — so the
only guard is "are you logged in" (`require_login`). The other principal is
Azure DevOps itself, authenticated on the webhook route via the shared secret
in `Settings.webhook_secret` (see app/webhooks.py), not via a session.

The very first account is seeded from the ADMIN_USERNAME/ADMIN_PASSWORD env
vars on startup when the table is empty (see app/main.py); after that, env is
break-glass only and all auth goes through the DB.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import User
from app.security import verify_password

SESSION_UID_KEY = "uid"
SESSION_UNAME_KEY = "uname"


def normalize_username(username: str) -> str:
    """Usernames are compared/stored case-insensitively and trimmed."""
    return (username or "").strip().lower()


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    """Return the matching user for valid credentials, else None.

    Runs the password check even when the user is missing would be ideal to
    equalize timing; bcrypt's cost already dominates, and we keep it simple by
    returning None on a missing user. No response difference distinguishes
    "unknown user" from "wrong password" at the route layer.
    """
    row = (
        await session.execute(
            select(User).where(User.username == normalize_username(username))
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if not verify_password(password, row.password_hash):
        return None
    return row


def login_session(request: Request, user: User) -> None:
    """Establish an authenticated session for `user`.

    The session is cleared first so a pre-login session id cannot be reused
    post-login (session-fixation hygiene)."""
    request.session.clear()
    request.session[SESSION_UID_KEY] = user.id
    request.session[SESSION_UNAME_KEY] = user.username


def logout_session(request: Request) -> None:
    request.session.clear()


def is_authenticated(request: Request) -> bool:
    return request.session.get(SESSION_UID_KEY) is not None


def current_uid(request: Request) -> int | None:
    return request.session.get(SESSION_UID_KEY)


def current_username(request: Request) -> str | None:
    return request.session.get(SESSION_UNAME_KEY)


async def require_login(request: Request) -> None:
    """Guard for admin console HTML routes. Raises a 303 redirect to /login."""
    if not is_authenticated(request):
        raise _redirect("/login")


def _redirect(location: str) -> HTTPException:
    """HTTPException that FastAPI's exception handler turns into a 303 redirect.

    We register a handler in app.main that converts this exception shape into
    an actual RedirectResponse.
    """
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail={"redirect": location},
        headers={"Location": location},
    )


def is_redirect_exception(exc: HTTPException) -> str | None:
    if exc.status_code == status.HTTP_303_SEE_OTHER and exc.headers:
        return exc.headers.get("Location")
    return None
