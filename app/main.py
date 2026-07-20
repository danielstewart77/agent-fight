"""FastAPI application factory: mounts the admin console and the ADO webhook."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.admin import login as admin_login
from app.admin import settings as admin_settings
from app.auth import is_admin_session, is_redirect_exception
from app.config import config
from app.webhooks import router as webhooks_router


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Fight", version="0.1.0")

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        session_cookie="agent_fight_session",
        same_site="lax",
        https_only=False,  # A reverse proxy may terminate TLS upstream.
    )

    # ---- Redirect-exception handler -----------------------------------------
    @app.exception_handler(StarletteHTTPException)
    async def _handle_redirect_exc(request: Request, exc: StarletteHTTPException):
        loc = is_redirect_exception(exc) if isinstance(exc, HTTPException) else None
        if loc:
            return RedirectResponse(loc, status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # ---- Public / health endpoints ------------------------------------------
    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/")
    async def root(request: Request):
        if not is_admin_session(request):
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/admin/", status_code=303)

    @app.get("/admin")
    async def admin_no_slash():
        return RedirectResponse("/admin/", status_code=308)

    # ---- Admin console (session-authed) --------------------------------------
    app.include_router(admin_login.router)
    app.include_router(admin_settings.router)

    # ---- Inbound Azure DevOps webhook (shared-secret authed) -----------------
    app.include_router(webhooks_router)

    return app


app = create_app()
