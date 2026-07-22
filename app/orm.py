"""SQLAlchemy ORM models for agent-fight.

There is exactly one row of run configuration: `Settings`, a singleton at
id=1 managed entirely through the admin console (see app/admin/settings.py).
It holds the Azure DevOps account/org/project connection, the webhook shared
secret used to authenticate inbound ADO calls, and the clone credential the
per-review container will use to pull the target repo. There is no user
table and no self-service portal: the admin is a single operator identity
held in environment config (see app/config.py), same as inference-proxy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Integer, Unicode, UnicodeText, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models. UTC datetimes throughout."""


class User(Base):
    """An operator account. Flat model — every user is a full operator: they
    can log in, edit settings, and manage other users. There is no role column.

    `username` is stored normalized (lowercase, trimmed) and is unique.
    `password_hash` is a bcrypt hash (see app/security.py) — never a plaintext
    password. The first user is seeded from the ADMIN_USERNAME/ADMIN_PASSWORD
    env vars on startup when the table is empty (see app/main.py); after that,
    accounts are managed entirely in-app and env is break-glass only.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Unicode(256), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Unicode(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Settings(Base):
    """Singleton row (id is always 1) holding the admin-configured run settings.

    Secrets (`ado_pat`, `webhook_secret`, `clone_credential_secret`,
    `proxy_api_key`) are stored the same way inference-proxy stores API keys:
    DB-backed and off the filesystem, plaintext-in-DB (not a repo file) — not
    full encryption-at-rest. The admin console never echoes a stored secret
    back; a blank field on save leaves it untouched (see app/admin/settings.py).
    """

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Round cap for the code-review consensus loop. Configurable through the
    # admin page rather than hardcoded; validated at the application layer
    # (must be a positive int) — no DB-level bound.
    round_cap: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")

    # Azure DevOps account/org/project connection.
    ado_organization: Mapped[Optional[str]] = mapped_column(Unicode(256), nullable=True)
    ado_project: Mapped[Optional[str]] = mapped_column(Unicode(256), nullable=True)
    # PAT with PR-comment permission, used to post the final review back.
    ado_pat: Mapped[Optional[str]] = mapped_column(Unicode(2048), nullable=True)

    # Shared secret used to authenticate inbound ADO webhook calls.
    webhook_secret: Mapped[Optional[str]] = mapped_column(Unicode(512), nullable=True)

    # Inference-proxy connection. Harnesses inside the review container route
    # their provider calls through this proxy (base URL + key injected into
    # their env), and the coordinator calls it directly for its own reasoning.
    # `proxy_base_url` is non-secret; `proxy_api_key` is a write-only secret.
    proxy_base_url: Mapped[Optional[str]] = mapped_column(Unicode(512), nullable=True)
    proxy_api_key: Mapped[Optional[str]] = mapped_column(Unicode(2048), nullable=True)

    # Clone credential the per-review container uses to pull the target
    # repo: a PAT/token or an SSH private key.
    clone_credential_kind: Mapped[str] = mapped_column(
        Unicode(16), nullable=False, server_default="token"
    )
    clone_credential_secret: Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "clone_credential_kind IN ('token','ssh_key')",
            name="CK_settings_clone_credential_kind",
        ),
    )
