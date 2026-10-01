"""Add review progress snapshot and timeline.

Revision ID: 0010_review_progress
Revises: 0009_review_findings
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_review_progress"
down_revision = "0009_review_findings"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("reviews", sa.Column("current_phase", sa.Unicode(64)))
    op.add_column("reviews", sa.Column("status_message", sa.Unicode(512)))
    op.add_column("reviews", sa.Column("last_activity_at", sa.DateTime()))
    op.create_table("review_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("review_id", sa.Integer(), sa.ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Unicode(64), nullable=False),
        sa.Column("phase", sa.Unicode(64), nullable=False),
        sa.Column("message", sa.Unicode(512), nullable=False),
        sa.Column("role", sa.Unicode(16)), sa.Column("round_no", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_review_events_review_id", "review_events", ["review_id"])

def downgrade():
    op.drop_index("ix_review_events_review_id", table_name="review_events")
    op.drop_table("review_events")
    op.drop_column("reviews", "last_activity_at")
    op.drop_column("reviews", "status_message")
    op.drop_column("reviews", "current_phase")
