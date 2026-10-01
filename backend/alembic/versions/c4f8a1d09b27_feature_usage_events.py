"""Feature-usage events

The product had every outcome recorded and no record of *use*. Which features
people actually open, how many come back to one a second time, and which of them
carry code nobody exercises were all unanswerable — not hard to answer, but
unanswerable, because nothing was written down. This is the table that answers
them. See ``app/models/feature_event.py`` for why it is shaped the way it is.

Two things are worth noticing about this migration rather than the model.

``user_id`` is ``ON DELETE SET NULL``, which is the only such column on a
per-user table in this schema. It is deliberate: every other table describes
work done for one account and is meaningless without it, while this one
describes the product. Deleting an account must not retroactively change how
many people used Smart Apply last March.

``day`` duplicates the date part of ``occurred_at``. Both indexes are on it
rather than on the timestamp, because every read groups by day and there is no
spelling of "the date part of a timestamptz" that is both indexable here and
runnable on the SQLite the test suite builds its schema with.

Revision ID: c4f8a1d09b27
Revises: b3d7c1f92e64
Create Date: 2026-08-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4f8a1d09b27"
down_revision: str | None = "b3d7c1f92e64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("feature", sa.String(length=48), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("props", sa.JSON(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feature_events_user_id", "feature_events", ["user_id"])
    op.create_index("ix_feature_events_day", "feature_events", ["day"])
    op.create_index(
        "ix_feature_events_feature_day", "feature_events", ["feature", "day"]
    )


def downgrade() -> None:
    op.drop_index("ix_feature_events_feature_day", table_name="feature_events")
    op.drop_index("ix_feature_events_day", table_name="feature_events")
    op.drop_index("ix_feature_events_user_id", table_name="feature_events")
    op.drop_table("feature_events")
