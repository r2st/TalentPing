"""Smart inbox reply routing: confidence, held drafts, and the user's switches

Three columns on ``emails`` and three on ``autopilot_preferences``.

``emails.intent_confidence`` is how sure the classifier was, 0..1. Left null on
every existing row rather than backfilled with a guess: the policy reads a null
as "unknown" and holds, so the worst an old row can do is wait for its owner —
which is what it was already doing.

``emails.needs_attention`` defaults false for the same reason it is safe to add
to a live table: the 83 drafts already sitting in production are not flagged by
this migration. The re-evaluation task
(``app.tasks.inbox_tasks.reevaluate_pending_replies``) is what routes them, and
it is a separate, reversible step on purpose — a migration that decided to send
eighty-three emails would be a migration nobody could review.

Purely additive; the downgrade is a clean reversal.

Revision ID: c9f4a1e70d38
Revises: b7e3f1a90c42
Create Date: 2026-08-05
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9f4a1e70d38"
down_revision: str | None = "b7e3f1a90c42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("emails", sa.Column("intent_confidence", sa.Float(), nullable=True))
    op.add_column(
        "emails",
        sa.Column(
            "needs_attention",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("emails", sa.Column("attention_reason", sa.String(160), nullable=True))

    # The defaults here are the *shipped* behaviour, so a user who never opens
    # the settings page gets exactly what the release notes describe. They are
    # server defaults as well as ORM defaults because every existing row is
    # backfilled by them — an existing user has consented to autopilot sending
    # mail on their behalf, and inbox replies are the same bargain applied to
    # conversations they are already in.
    op.add_column(
        "autopilot_preferences",
        sa.Column(
            "inbox_auto_reply",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "autopilot_preferences",
        sa.Column(
            "inbox_auto_reply_min_confidence",
            sa.Integer(),
            nullable=False,
            server_default="85",
        ),
    )
    # Added nullable, filled, then tightened — the only safe order on a
    # populated table, and the one ``b2d7f4c81a95`` established for every other
    # JSON list here. The model declares ``Mapped[list[str]]``, so the column has
    # to end up NOT NULL or ``tests/test_schema_drift.py`` fails, which is
    # exactly the check that exists to catch a column left half-added.
    #
    # The fill is bound through ``sa.JSON()`` rather than written as a string
    # literal: Postgres will not implicitly cast a ``varchar`` into a ``json``
    # column, and binding the Python value lets the driver do it.
    op.add_column(
        "autopilot_preferences",
        sa.Column("inbox_auto_reply_intents", sa.JSON(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE autopilot_preferences SET inbox_auto_reply_intents = :value "
            "WHERE inbox_auto_reply_intents IS NULL"
        ).bindparams(
            sa.bindparam(
                "value",
                value=["INTERESTED", "QUESTION", "SCHEDULING"],
                type_=sa.JSON(),
            )
        )
    )
    op.alter_column(
        "autopilot_preferences", "inbox_auto_reply_intents", nullable=False
    )


def downgrade() -> None:
    op.drop_column("autopilot_preferences", "inbox_auto_reply_intents")
    op.drop_column("autopilot_preferences", "inbox_auto_reply_min_confidence")
    op.drop_column("autopilot_preferences", "inbox_auto_reply")
    op.drop_column("emails", "attention_reason")
    op.drop_column("emails", "needs_attention")
    op.drop_column("emails", "intent_confidence")
