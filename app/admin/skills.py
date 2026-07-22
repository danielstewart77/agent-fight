"""/admin/skills — skills repos, sync, and the per-harness skill picker.

Repo URLs are saved to Settings; the host clones live under config.skills_root
(see app/skills.py). "Sync" clones/pulls each repo with the host git
credentials. Per harness, the operator ticks which skills to bind-mount into
that harness's review container.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import skills as skills_mod
from app.auth import require_login
from app.config import config
from app.db import get_session
from app.orm import Settings
from app.templating import templates

router = APIRouter()

SETTINGS_ID = 1


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


@router.get("/admin/skills", response_class=HTMLResponse)
async def show_skills(request: Request, session: AsyncSession = Depends(get_session)):
    await require_login(request)
    row = await _get_or_create(session)
    return templates.TemplateResponse(
        request,
        "admin/skills.html",
        {"repos": skills_mod.status(row), "skills_root": config.skills_root},
    )


@router.post("/admin/skills")
async def update_skills(request: Request, session: AsyncSession = Depends(get_session)):
    await require_login(request)
    row = await _get_or_create(session)
    form = await request.form()

    for role, _repo_dir, _mount, _prefix in skills_mod.REPOS:
        url = (form.get(f"{role}_skills_repo") or "").strip() or None
        setattr(row, f"{role}_skills_repo", url)
        # Checkboxes: one form field per selected skill, name="<role>_skill".
        selected = form.getlist(f"{role}_skill")
        setattr(row, f"{role}_skills_enabled", json.dumps(selected))

    await session.commit()
    return RedirectResponse("/admin/skills", status_code=303)


@router.post("/admin/skills/sync")
async def sync_skills(request: Request, session: AsyncSession = Depends(get_session)):
    await require_login(request)
    row = await _get_or_create(session)
    skills_mod.sync(row)
    return RedirectResponse("/admin/skills", status_code=303)
