"""Per-role proxy configs: replace the single proxy connection with three.

Drops the generic `proxy_base_url`/`proxy_api_key` (migration 0002) and adds a
base URL, API key, and model for each of the two harnesses and the coordinator
(see app/orm.py::Settings, configured at /admin/proxy). The old generic columns
were never used by a working review run, so no data migration is needed.

Revision ID: 0004_per_harness_proxy
Revises: 0003_add_users
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_per_harness_proxy"
down_revision = "0003_add_users"
branch_labels = None
depends_on = None

_ROLES = ("claude", "codex", "coord")


def upgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        for role in _ROLES:
            batch.add_column(sa.Column(f"{role}_proxy_url", sa.Unicode(512), nullable=True))
            batch.add_column(sa.Column(f"{role}_proxy_key", sa.Unicode(2048), nullable=True))
            batch.add_column(sa.Column(f"{role}_model", sa.Unicode(128), nullable=True))
        batch.drop_column("proxy_api_key")
        batch.drop_column("proxy_base_url")


def downgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        batch.add_column(sa.Column("proxy_base_url", sa.Unicode(512), nullable=True))
        batch.add_column(sa.Column("proxy_api_key", sa.Unicode(2048), nullable=True))
        for role in _ROLES:
            batch.drop_column(f"{role}_model")
            batch.drop_column(f"{role}_proxy_key")
            batch.drop_column(f"{role}_proxy_url")
