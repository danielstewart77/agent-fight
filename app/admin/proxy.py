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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
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

# role -> (label, fixed flavor or None). The two harnesses have a fixed provider
# protocol; the coordinator's is user-selectable (Settings.coord_flavor).
ROLES = {
    "claude": ("Claude harness", "anthropic"),
    "codex": ("Codex harness", "openai"),
    "coord": ("Coordinator", None),
}
VALID_FLAVORS = ("anthropic", "openai")


def _role_flavor(role: str, row) -> str:
    fixed = ROLES[role][1]
    return fixed if fixed is not None else (row.coord_flavor or "anthropic")


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
    async def one(role: str) -> tuple[str, list[str]]:
        url = getattr(row, f"{role}_proxy_url")
        key = getattr(row, f"{role}_proxy_key")
        return role, await list_models(url or "", key or "", flavor=_role_flavor(role, row))

    results = await asyncio.gather(*(one(r) for r in ROLES))
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
            # Only the coordinator exposes a provider selector.
            "selectable_flavor": ROLES[role][1] is None,
            "flavor": _role_flavor(role, row),
        }
        for role, (label, _flavor) in ROLES.items()
    ]
    return templates.TemplateResponse(
        request, "admin/proxy.html", {"blocks": blocks, "flavors": VALID_FLAVORS}
    )


@router.post("/admin/proxy/{role}")
async def update_proxy_role(
    role: str,
    request: Request,
    proxy_url: str = Form(""),
    proxy_key: str = Form(""),
    model: str = Form(""),
    flavor: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Save one harness/coordinator block. Independent per role so the two-step
    flow (save URL+key, then reload and pick a model) never disturbs the others."""
    await require_login(request)
    if role not in ROLES:
        raise HTTPException(status_code=404, detail=f"unknown proxy role: {role!r}")
    row = await _get_or_create(session)

    # Only the coordinator has a selectable provider flavor.
    if ROLES[role][1] is None and flavor:
        if flavor not in VALID_FLAVORS:
            raise HTTPException(status_code=400, detail=f"flavor must be one of {VALID_FLAVORS}")
        row.coord_flavor = flavor

    setattr(row, f"{role}_proxy_url", _validate_url(proxy_url))

    model_val = model.strip() or None
    if model_val and len(model_val) > 128:
        raise HTTPException(status_code=400, detail="model must be 128 characters or fewer")
    setattr(row, f"{role}_model", model_val)

    # Secret: only overwrite when a non-blank value is submitted.
    new_key = proxy_key.strip()
    if new_key:
        if len(new_key) > 2048:
            raise HTTPException(status_code=400, detail="API key must be 2048 characters or fewer")
        setattr(row, f"{role}_proxy_key", new_key)

    await session.commit()
    return RedirectResponse("/admin/proxy", status_code=303)
