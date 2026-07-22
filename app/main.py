"""FastAPI application factory: mounts the admin console and the ADO webhook."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.admin import account as admin_account
from app.admin import login as admin_login
from app.admin import proxy as admin_proxy
from app.admin import settings as admin_settings
from app.admin import users as admin_users
from app.auth import is_authenticated, is_redirect_exception, normalize_username
from app.config import config
from app.db import SessionLocal
from app.orm import User
from app.proxy import ProxyNotConfigured, load_coordinator_proxy, ProxyClient
from app.runner import ReviewDispatcher
from app.security import hash_password
from sqlalchemy import func, select
from app.webhooks import router as webhooks_router

logger = logging.getLogger("agent_fight.main")


async def _proxy_status() -> str:
    """'up' | 'down' | 'unconfigured' — non-fatal diagnostic of the coordinator proxy."""
    async with SessionLocal() as session:
        try:
            base_url, api_key, model = await load_coordinator_proxy(session)
        except ProxyNotConfigured:
            return "unconfigured"
    async with ProxyClient(base_url, api_key, model=model) as pc:
        return "up" if await pc.ping() else "down"


async def _seed_first_user() -> None:
    """Create the first operator from env when the users table is empty.

    Idempotent (only fires when there are zero users) and non-fatal — a failure
    here must not stop the app from booting. Once any user exists, this is a
    no-op and env credentials are irrelevant to login.
    """
    if not config.admin_username or not config.admin_password:
        return
    try:
        async with SessionLocal() as session:
            count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            if count:
                return
            session.add(
                User(
                    username=normalize_username(config.admin_username),
                    password_hash=hash_password(config.admin_password),
                )
            )
            await session.commit()
            logger.info("seeded first user %r from env", normalize_username(config.admin_username))
    except Exception:  # noqa: BLE001 - seeding must never block startup
        logger.exception("failed to seed first user from env")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Seed the first operator (no-op once any user exists), then a non-fatal
    # proxy reachability ping. Neither blocks boot — the admin still needs to
    # log in and configure settings even if the proxy is down.
    await _seed_first_user()
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
        if not is_authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/admin/", status_code=303)

    @app.get("/admin")
    async def admin_no_slash():
        return RedirectResponse("/admin/", status_code=308)

    # ---- Admin console (session-authed) --------------------------------------
    app.include_router(admin_login.router)
    app.include_router(admin_settings.router)
    app.include_router(admin_proxy.router)
    app.include_router(admin_account.router)
    app.include_router(admin_users.router)

    # ---- Inbound Azure DevOps webhook (shared-secret authed) -----------------
    app.include_router(webhooks_router)

    return app


app = create_app()
