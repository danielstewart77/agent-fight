"""Add users table.

Introduces operator accounts (see app/orm.py::User). Schema only — the first
row is seeded by the app from env on startup, and further rows are created
through /admin/users. Flat model: no role column.

Revision ID: 0003_add_users
Revises: 0002_add_inference_proxy
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_add_users"
down_revision = "0002_add_inference_proxy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.Unicode(256), nullable=False),
        sa.Column("password_hash", sa.Unicode(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="UQ_users_username"),
    )


def downgrade() -> None:
    op.drop_table("users")
