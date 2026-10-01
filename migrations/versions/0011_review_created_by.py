"""Store the pull request creator on review records.

Revision ID: 0011_review_created_by
Revises: 0010_review_progress
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_review_created_by"
down_revision = "0010_review_progress"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reviews", sa.Column("created_by", sa.Unicode(256), nullable=True))


def downgrade():
    op.drop_column("reviews", "created_by")
