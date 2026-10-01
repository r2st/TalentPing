"""Dismissal is a tombstone on notifications, not a delete

``DELETE /notifications/{id}`` removed the row, which handed its ``dedupe_key``
back. Most of what the sweep writes is state-scoped — "162 drafts are waiting",
"9 applications have gone quiet" — and that condition is still true at the next
tick, so the sweep re-derived it, found the key free, and wrote the identical
notification again fifteen minutes later. See
``app/services/notification_sweep.py``.

Nullable with no server default, so the column adds to a populated table with
no backfill: every existing row is not dismissed, which is exactly what it was.

Nothing is lost by keeping the rows. ``notifications.prune`` already deletes by
age, and a dismissed row ages out on the same clock as a read one.

Revision ID: d5f1a9c37e28
Revises: c4e8b2d61f93
Create Date: 2026-08-25
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5f1a9c37e28"
down_revision: str | None = "c4e8b2d61f93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notifications", "dismissed_at")
