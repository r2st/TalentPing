"""Autopilot: explicit follow-up day offsets

Adds ``autopilot_preferences.follow_up_step_days``, the mirror of the column
``campaigns`` has carried since follow-up sequences shipped.

``Campaign.follow_up_step_days`` exists because the interval knob cannot express
"day 3 then day 7" — no integer interval yields that pair under the shape
``follow_up_service.default_offsets`` extends. It was reachable only from a test
or a psql prompt: nothing in the API accepted it and no screen showed it, so
every user was held to a sequence the deployment's ``FOLLOW_UP_STEP_DAYS``
picked, re-anchored by "first one after N days".

The column goes on the preferences row rather than only on the campaign because
the preferences form is where a user actually configures follow-ups —
``auto_apply_service`` copies the row's cadence onto the autopilot campaign on
every cycle, and a knob that only existed on the campaign would be overwritten
by the row that does not have it.

Nullable, with null meaning exactly what it means on ``campaigns``: fall back to
the generated sequence. So this is inert for every existing account until
somebody sets one, which is the only safe way to add a column that changes when
mail goes out.

Revision ID: d1e5f9a3c724
Revises: c8a4f2b7d051
Create Date: 2026-08-27
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1e5f9a3c724"
down_revision: str | None = "c8a4f2b7d051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "autopilot_preferences",
        sa.Column("follow_up_step_days", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("autopilot_preferences", "follow_up_step_days")
