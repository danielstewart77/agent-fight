"""Admin console session auth.

The admin is a single operator identity held in environment config — there
is no user table and no self-service portal. Login is a constant-time
comparison and the session carries nothing but an "is admin" flag. There is
no per-caller proxy-key model here (unlike inference-proxy's Client/ApiKey
machinery): the only other principal is Azure DevOps itself, authenticated
on the webhook route via the shared secret stored in `Settings.webhook_secret`
(see app/webhooks.py), not via a session.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from app.config import config

SESSION_ADMIN_KEY = "admin"


def verify_admin_credentials(username: str, password: str) -> bool:
    """Constant-time check against the configured admin identity.

    Returns False when either config value is unset, so an unconfigured
    deploy cannot be signed into at all.
    """
    if not config.admin_username or not config.admin_password:
        return False
    ok_user = hmac.compare_digest(username, config.admin_username)
    ok_pass = hmac.compare_digest(password, config.admin_password)
    return ok_user and ok_pass


def login_session(request: Request) -> None:
    request.session[SESSION_ADMIN_KEY] = True


def logout_session(request: Request) -> None:
    request.session.pop(SESSION_ADMIN_KEY, None)


def is_admin_session(request: Request) -> bool:
    return bool(request.session.get(SESSION_ADMIN_KEY))


async def require_html_admin(request: Request) -> None:
    """Guard for admin console HTML routes. Raises a 303 redirect to /login."""
    if not is_admin_session(request):
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
