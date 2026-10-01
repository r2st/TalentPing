"""Inbox: per-message read state

Adds ``emails.read_at``, set when the user opens an inbound message in the
Inbox. Null means unread, which is what the inbox unread badge counts. Outbound
rows keep it null forever — they are read by definition and never counted.

Purely additive; the downgrade is a clean reversal.

Revision ID: f3a6b1c8d920
Revises: e7a1b9c2d4f5
Create Date: 2026-07-24
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a6b1c8d920"
down_revision: str | None = "e7a1b9c2d4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "emails", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Everything that predates the inbox has already been seen in the tracker;
    # marking it read avoids greeting existing users with a huge unread badge.
    op.execute(
        "UPDATE emails SET read_at = COALESCE(sent_at, created_at) "
        "WHERE direction = 'RECEIVED'"
    )


def downgrade() -> None:
    op.drop_column("emails", "read_at")
