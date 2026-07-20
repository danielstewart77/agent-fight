"""/admin/settings — the admin-configured run settings singleton.

Mirrors inference-proxy's app/admin/credentials.py CRUD pattern: secrets
(`ado_pat`, `webhook_secret`, `clone_credential_secret`) are write-only from
the console — the page shows whether a value is set, never the value
itself, and a blank field on save leaves the stored column untouched.
Non-secret fields (`round_cap`, `ado_organization`, `ado_project`,
`clone_credential_kind`) are always overwritten with the submitted value.

There is exactly one settings row, id=1, created lazily on first GET or POST
(get-or-create) rather than by the migration — see migrations/versions/0001_initial.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_html_admin
from app.db import get_session
from app.orm import Settings
from app.templating import templates

router = APIRouter()

VALID_CLONE_CREDENTIAL_KINDS = ("token", "ssh_key")

SETTINGS_ID = 1


async def _get_or_create_settings(session: AsyncSession) -> Settings:
    row = (
        await session.execute(select(Settings).where(Settings.id == SETTINGS_ID))
    ).scalar_one_or_none()
    if row is None:
        row = Settings(id=SETTINGS_ID)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def _validate_round_cap(raw: str) -> int:
    try:
        val = int((raw or "").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="round_cap must be a positive integer")
    if val <= 0:
        raise HTTPException(status_code=400, detail="round_cap must be a positive integer")
    return val


def _validate_clone_credential_kind(raw: str) -> str:
    val = (raw or "").strip()
    if val not in VALID_CLONE_CREDENTIAL_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"clone_credential_kind must be one of {VALID_CLONE_CREDENTIAL_KINDS}, got {val!r}",
        )
    return val


def _cleared_or_set(raw: str | None, *, max_len: int) -> str | None:
    """Non-secret optional field: always takes the submitted value.

    A blank submission clears the stored value (unlike the secret fields,
    which treat blank as "leave unchanged" — see `_optional_secret` below).
    """
    val = (raw or "").strip()
    if not val:
        return None
    if len(val) > max_len:
        raise HTTPException(status_code=400, detail=f"value must be {max_len} characters or fewer")
    return val


def _optional_secret(raw: str | None, *, max_len: int) -> str | None:
    """Secret field: a blank submission means "leave the stored value alone".

    The console never renders the current secret back, so it cannot be
    resubmitted. Returns None when the field was left blank (no-op for the
    caller); returns the new value otherwise.
    """
    val = (raw or "").strip()
    if not val:
        return None
    if len(val) > max_len:
        raise HTTPException(status_code=400, detail=f"value must be {max_len} characters or fewer")
    return val


@router.get("/admin/settings", response_class=HTMLResponse)
async def show_settings(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    await require_html_admin(request)
    row = await _get_or_create_settings(session)
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {"settings": row, "kinds": VALID_CLONE_CREDENTIAL_KINDS},
    )


@router.post("/admin/settings")
async def update_settings(
    request: Request,
    round_cap: str = Form(...),
    ado_organization: str = Form(""),
    ado_project: str = Form(""),
    ado_pat: str = Form(""),
    webhook_secret: str = Form(""),
    clone_credential_kind: str = Form("token"),
    clone_credential_secret: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    await require_html_admin(request)
    row = await _get_or_create_settings(session)

    row.round_cap = _validate_round_cap(round_cap)
    row.clone_credential_kind = _validate_clone_credential_kind(clone_credential_kind)

    # Non-secret fields: always overwritten with the submitted value (blank
    # clears them).
    row.ado_organization = _cleared_or_set(ado_organization, max_len=256)
    row.ado_project = _cleared_or_set(ado_project, max_len=256)

    # Secret fields: only overwritten when the submitted value is non-blank.
    new_ado_pat = _optional_secret(ado_pat, max_len=2048)
    if new_ado_pat is not None:
        row.ado_pat = new_ado_pat

    new_webhook_secret = _optional_secret(webhook_secret, max_len=512)
    if new_webhook_secret is not None:
        row.webhook_secret = new_webhook_secret

    new_clone_secret = _optional_secret(clone_credential_secret, max_len=8192)
    if new_clone_secret is not None:
        row.clone_credential_secret = new_clone_secret

    await session.commit()
    return RedirectResponse("/admin/settings", status_code=303)
