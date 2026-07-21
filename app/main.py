"""FastAPI application factory: mounts the admin console and the ADO webhook."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.admin import login as admin_login
from app.admin import settings as admin_settings
from app.auth import is_admin_session, is_redirect_exception
from app.config import config
from app.db import SessionLocal
from app.proxy import ProxyNotConfigured, load_proxy_config, ProxyClient
from app.runner import ReviewDispatcher
from app.webhooks import router as webhooks_router

logger = logging.getLogger("agent_fight.main")


async def _proxy_status() -> str:
    """'up' | 'down' | 'unconfigured' — a non-fatal diagnostic of the proxy."""
    async with SessionLocal() as session:
        try:
            base_url, api_key = await load_proxy_config(session)
        except ProxyNotConfigured:
            return "unconfigured"
    async with ProxyClient(base_url, api_key) as pc:
        return "up" if await pc.ping() else "down"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Non-fatal startup ping: report the proxy's reachability but never block
    # boot on it — the admin still needs to log in and configure settings.
    status = await _proxy_status()
    logger.info("inference proxy status at startup: %s", status)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Fight", version="0.1.0", lifespan=_lifespan)

    # Fire-and-forget review runs are dispatched through this. Held on
    # app.state so tests can swap in a recording fake.
    app.state.review_dispatcher = ReviewDispatcher()

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

    @app.get("/health/proxy")
    async def health_proxy():
        return {"proxy": await _proxy_status()}

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
