"""Add agent-skills settings.

Per-harness skills-repo URLs and the operator's per-harness skill selection
(JSON list of skill-folder names). See app/orm.py::Settings and app/skills.py.

Revision ID: 0006_skills
Revises: 0005_coord_flavor
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_skills"
down_revision = "0005_coord_flavor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        batch.add_column(sa.Column("claude_skills_repo", sa.Unicode(512), nullable=True))
        batch.add_column(sa.Column("codex_skills_repo", sa.Unicode(512), nullable=True))
        batch.add_column(sa.Column("claude_skills_enabled", sa.UnicodeText(), nullable=True))
        batch.add_column(sa.Column("codex_skills_enabled", sa.UnicodeText(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        batch.drop_column("codex_skills_enabled")
        batch.drop_column("claude_skills_enabled")
        batch.drop_column("codex_skills_repo")
        batch.drop_column("claude_skills_repo")
