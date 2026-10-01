"""Persist on-demand PR review requests."""
from alembic import op
import sqlalchemy as sa

revision = "0012_review_requests"
down_revision = "0011_review_created_by"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "review_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_key", sa.Unicode(64), nullable=False, unique=True),
        sa.Column("job_json", sa.UnicodeText(), nullable=False),
        sa.Column("status", sa.Unicode(24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_review_requests_status", "review_requests", ["status"])


def downgrade():
    op.drop_table("review_requests")
