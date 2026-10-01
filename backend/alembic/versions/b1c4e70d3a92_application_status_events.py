"""Application status history — every stage a card has held, and who put it there

The board lets a user drag an application between columns, which makes the
status column two different things at once: what the product observed, and what
a person asserted. ``application_status_events`` separates them — one immutable
row per transition, carrying ``source`` (AUTOMATIC | MANUAL) so an interview in
the funnel can be traced back either to the reply that was classified that way
or to the human who said so.

Nothing backfills. Applications that existed before this deploy start with no
history: their transitions happened, but no timestamp for them was ever kept,
and a history view that invents its own dates is worse than one that admits it
starts here.

Revision ID: b1c4e70d3a92
Revises: a4f7c93e18b2
Create Date: 2026-08-01
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b1c4e70d3a92"
down_revision: str | None = "a4f7c93e18b2"
branch_labels = None
depends_on = None

_TABLE = "application_status_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Null only on a row recording entry into QUEUED.
        sa.Column("from_status", sa.String(length=24), nullable=True),
        sa.Column("to_status", sa.String(length=24), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_application_status_events_application_id", _TABLE, ["application_id"]
    )
    op.create_index("ix_application_status_events_user_id", _TABLE, ["user_id"])
    # The board's read: latest event per application, for a page of cards.
    op.create_index(
        "ix_status_events_application_created", _TABLE, ["application_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_status_events_application_created", table_name=_TABLE)
    op.drop_index("ix_application_status_events_user_id", table_name=_TABLE)
    op.drop_index("ix_application_status_events_application_id", table_name=_TABLE)
    op.drop_table(_TABLE)
