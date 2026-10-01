"""Outreach personalization options on autopilot preferences

Six columns letting the user say how their outreach should read — tone,
length, which single call to action it closes on, an optional sign-off,
phrases to work in, and free-text steering.

All six carry server defaults matching the composer's previous hard-coded
behaviour, so existing rows keep producing byte-identical emails until the
user actually changes something. ``outreach_highlights`` defaults to an empty
JSON array rather than NULL: the composer reads it as a list, and a NULL that
only some rows have is a second code path for no reason.

Purely additive; the downgrade is a clean reversal.

Revision ID: a4f7c93e18b2
Revises: a4f7d2e91b63
Create Date: 2026-08-01

Chained after ``a4f7d2e91b63`` rather than off ``e3b8c1a75d24`` alongside it.
The two were written in the same session against the same parent, which left
alembic with two heads and ``upgrade head`` refusing to run at all. They touch
different tables, so the order between them is arbitrary — only that there is
one matters.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4f7c93e18b2"
down_revision: str | None = "a4f7d2e91b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "autopilot_preferences"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "outreach_tone",
            sa.String(length=16),
            nullable=False,
            server_default="peer",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "outreach_length",
            sa.String(length=16),
            nullable=False,
            server_default="standard",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "outreach_cta",
            sa.String(length=16),
            nullable=False,
            server_default="call",
        ),
    )
    op.add_column(_TABLE, sa.Column("outreach_sign_off", sa.String(length=60), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column(
            "outreach_highlights",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        _TABLE, sa.Column("outreach_custom_instructions", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "outreach_custom_instructions")
    op.drop_column(_TABLE, "outreach_highlights")
    op.drop_column(_TABLE, "outreach_sign_off")
    op.drop_column(_TABLE, "outreach_cta")
    op.drop_column(_TABLE, "outreach_length")
    op.drop_column(_TABLE, "outreach_tone")
