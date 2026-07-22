"""Add coordinator provider flavor.

The coordinator can speak either the Anthropic Messages API (Claude-style) or
the OpenAI Chat Completions API (Codex-style); this column records which, so it
is no longer assumed to be Claude. See app/orm.py::Settings and app/proxy.py.

Revision ID: 0005_coord_flavor
Revises: 0004_per_harness_proxy
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_coord_flavor"
down_revision = "0004_per_harness_proxy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        batch.add_column(
            sa.Column("coord_flavor", sa.Unicode(16), nullable=False, server_default="anthropic")
        )


def downgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        batch.drop_column("coord_flavor")
