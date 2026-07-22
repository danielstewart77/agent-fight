"""Add inference-proxy connection to settings.

Adds `proxy_base_url` (non-secret) and `proxy_api_key` (write-only secret) to
the settings singleton. Harnesses in the review container route provider calls
through this proxy, and the coordinator calls it directly — see app/proxy.py.

Revision ID: 0002_add_inference_proxy
Revises: 0001_initial
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_add_inference_proxy"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        batch.add_column(sa.Column("proxy_base_url", sa.Unicode(512), nullable=True))
        batch.add_column(sa.Column("proxy_api_key", sa.Unicode(2048), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        batch.drop_column("proxy_api_key")
        batch.drop_column("proxy_base_url")
