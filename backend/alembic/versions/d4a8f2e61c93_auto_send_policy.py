"""Graduated auto-send: trial approvals, pause, daily ceiling, auto_sent flag.

``autopilot_preferences.auto_send`` was a bare on/off switch. This adds the three
columns that let it be held back without being turned off — an unfinished trial,
an active pause, a spent daily allowance — plus ``emails.auto_sent``, which
records whether a message went out unread so the ceiling has something to count.

Every default is the status quo: trial 0, no pause, no ceiling, and existing sent
mail marked as *not* auto-sent. That last one is a deliberate under-count for
history (some of it certainly was auto-sent) rather than a guess that would
retroactively spend allowances the user never spent.

Revision ID: d4a8f2e61c93
Revises: c1d5e8a02f47
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "d4a8f2e61c93"
down_revision: str | None = "c1d5e8a02f47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("autopilot_preferences") as batch:
        batch.add_column(
            sa.Column(
                "auto_send_trial_approvals",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "auto_send_approved_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column("auto_send_paused_until", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("auto_send_daily_limit", sa.Integer(), nullable=True)
        )

    with op.batch_alter_table("emails") as batch:
        batch.add_column(
            sa.Column(
                "auto_sent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("emails") as batch:
        batch.drop_column("auto_sent")

    with op.batch_alter_table("autopilot_preferences") as batch:
        batch.drop_column("auto_send_daily_limit")
        batch.drop_column("auto_send_paused_until")
        batch.drop_column("auto_send_approved_count")
        batch.drop_column("auto_send_trial_approvals")
