"""Add review_findings — each harness's raw findings file, per round.

The audit trail behind a posted review. `reviews.final_review_md` records what
the service said; this records what each agent actually wrote, verbatim, so a
comment that reads wrong can be traced to the verdict/severity/claim that
produced it instead of inferred from the judge's summary after the review
container is gone. See app/orm.py::ReviewFinding.

Revision ID: 0009_review_findings
Revises: 0008_review_source_commit
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_review_findings"
down_revision = "0008_review_source_commit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Unicode(16), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("raw", sa.UnicodeText(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("role IN ('claude','codex')", name="CK_review_findings_role"),
    )
    op.create_index(
        "ix_review_findings_review_id", "review_findings", ["review_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_review_findings_review_id", table_name="review_findings")
    op.drop_table("review_findings")
