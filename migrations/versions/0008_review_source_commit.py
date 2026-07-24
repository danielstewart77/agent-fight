"""Add reviews.source_commit for the re-review loop-guard.

Records the source-branch tip commit a review covered so an incoming
`git.pullrequest.updated` webhook for the SAME commit (e.g. one our own
comment/vote triggered) is skipped instead of looping. See app/orm.py::Review
and app/webhooks.py.

Revision ID: 0008_review_source_commit
Revises: 0007_reviews_and_usage
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_review_source_commit"
down_revision = "0007_reviews_and_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.add_column(sa.Column("source_commit", sa.Unicode(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.drop_column("source_commit")
