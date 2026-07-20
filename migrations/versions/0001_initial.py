"""Initial schema: settings.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Schema only — the singleton id=1 row is created lazily by the app on
    # first GET/POST /admin/settings, not inserted here.
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("round_cap", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("ado_organization", sa.Unicode(256), nullable=True),
        sa.Column("ado_project", sa.Unicode(256), nullable=True),
        sa.Column("ado_pat", sa.Unicode(2048), nullable=True),
        sa.Column("webhook_secret", sa.Unicode(512), nullable=True),
        sa.Column("clone_credential_kind", sa.Unicode(16), nullable=False, server_default="token"),
        sa.Column("clone_credential_secret", sa.UnicodeText(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "clone_credential_kind IN ('token','ssh_key')",
            name="CK_settings_clone_credential_kind",
        ),
    )


def downgrade() -> None:
    op.drop_table("settings")
