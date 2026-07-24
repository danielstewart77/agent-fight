"""Add reviews and review_token_usage tables.

Persistent record of every review run (success or failure) plus per-round,
per-role token/cost accounting. See app/orm.py::Review / ReviewTokenUsage.

Revision ID: 0007_reviews_and_usage
Revises: 0006_skills
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_reviews_and_usage"
down_revision = "0006_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("repo_name", sa.Unicode(512), nullable=False),
        sa.Column("organization", sa.Unicode(256), nullable=True),
        sa.Column("project", sa.Unicode(256), nullable=True),
        sa.Column("source_branch", sa.Unicode(512), nullable=True),
        sa.Column("target_branch", sa.Unicode(512), nullable=True),
        sa.Column("pr_web_url", sa.Unicode(1024), nullable=True),
        sa.Column("event_type", sa.Unicode(128), nullable=True),
        sa.Column("status", sa.Unicode(16), nullable=False, server_default="running"),
        sa.Column("rounds_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("converged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disagreement_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unmatched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("final_review_md", sa.UnicodeText(), nullable=True),
        sa.Column("error", sa.UnicodeText(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('running','converged','capped','failed')",
            name="CK_reviews_status",
        ),
    )

    op.create_table(
        "review_token_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "review_id",
            sa.Integer(),
            sa.ForeignKey("reviews.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Unicode(16), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "role IN ('claude','codex','coordinator')",
            name="CK_review_token_usage_role",
        ),
    )
    op.create_index(
        "ix_review_token_usage_review_id", "review_token_usage", ["review_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_review_token_usage_review_id", table_name="review_token_usage")
    op.drop_table("review_token_usage")
    op.drop_table("reviews")
