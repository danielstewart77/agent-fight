"""/admin/proxy — per-role inference-proxy configuration.

Three independent blocks (Claude harness, Codex harness, Coordinator), each
with a base URL, a write-only API key, and a model. The model field is a
dropdown populated from the proxy's own /models listing (see app/proxy.py::
list_models) using that block's saved URL + key; on first setup, or if the
listing can't be fetched, it falls back to a free-text input. Secret handling
mirrors app/admin/settings.py: blank key on save leaves the stored value
untouched; URL/model are always overwritten.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_login
from app.db import get_session
from app.orm import Settings
from app.proxy import list_models
from app.templating import templates

router = APIRouter()

SETTINGS_ID = 1

# role -> (label, model-listing flavor)
ROLES = {
    "claude": ("Claude harness", "anthropic"),
    "codex": ("Codex harness", "openai"),
    "coord": ("Coordinator", "anthropic"),
}


async def _get_or_create(session: AsyncSession) -> Settings:
    row = (
        await session.execute(select(Settings).where(Settings.id == SETTINGS_ID))
    ).scalar_one_or_none()
    if row is None:
        row = Settings(id=SETTINGS_ID)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def _validate_url(raw: str | None) -> str | None:
    val = (raw or "").strip()
    if not val:
        return None
    if len(val) > 512:
        raise HTTPException(status_code=400, detail="proxy URL must be 512 characters or fewer")
    if not (val.startswith("http://") or val.startswith("https://")):
        raise HTTPException(status_code=400, detail="proxy URL must start with http:// or https://")
    return val


async def _model_options(row: Settings) -> dict[str, list[str]]:
    """Fetch the model list for each configured role concurrently (best-effort)."""
    async def one(role: str, flavor: str) -> tuple[str, list[str]]:
        url = getattr(row, f"{role}_proxy_url")
        key = getattr(row, f"{role}_proxy_key")
        return role, await list_models(url or "", key or "", flavor=flavor)

    results = await asyncio.gather(*(one(r, f) for r, (_, f) in ROLES.items()))
    return {role: models for role, models in results}


@router.get("/admin/proxy", response_class=HTMLResponse)
async def show_proxy(request: Request, session: AsyncSession = Depends(get_session)):
    await require_login(request)
    row = await _get_or_create(session)
    options = await _model_options(row)
    blocks = [
        {
            "role": role,
            "label": label,
            "url": getattr(row, f"{role}_proxy_url") or "",
            "key_set": bool(getattr(row, f"{role}_proxy_key")),
            "model": getattr(row, f"{role}_model") or "",
            "options": options.get(role, []),
        }
        for role, (label, _flavor) in ROLES.items()
    ]
    return templates.TemplateResponse(request, "admin/proxy.html", {"blocks": blocks})


@router.post("/admin/proxy")
async def update_proxy(request: Request, session: AsyncSession = Depends(get_session)):
    await require_login(request)
    row = await _get_or_create(session)
    form = await request.form()

    for role in ROLES:
        url = _validate_url(form.get(f"{role}_proxy_url"))
        setattr(row, f"{role}_proxy_url", url)

        model = (form.get(f"{role}_model") or "").strip() or None
        if model and len(model) > 128:
            raise HTTPException(status_code=400, detail="model must be 128 characters or fewer")
        setattr(row, f"{role}_model", model)

        # Secret: only overwrite when a non-blank value is submitted.
        new_key = (form.get(f"{role}_proxy_key") or "").strip()
        if new_key:
            if len(new_key) > 2048:
                raise HTTPException(status_code=400, detail="API key must be 2048 characters or fewer")
            setattr(row, f"{role}_proxy_key", new_key)

    await session.commit()
    return RedirectResponse("/admin/proxy", status_code=303)
