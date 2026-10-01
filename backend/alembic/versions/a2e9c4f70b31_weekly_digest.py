"""weekly digest preferences

One table. The row is created lazily on the first read of ``/digest``, so
nothing is backfilled here — an existing user gets their row the moment the app
asks for it, with the defaults baked into the model (on, Monday, 08:00 UTC).

``unsubscribe_token`` is unique and indexed because the public opt-out route
looks a row up by it, and a duplicate would make one user's link switch off
another user's digest.

Revision ID: a2e9c4f70b31
Revises: e6b2d9a41f70
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a2e9c4f70b31"
down_revision = "e6b2d9a41f70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digest_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("weekday", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hour", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("unsubscribe_token", sa.String(length=64), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_digest_preferences_user_id",
        "digest_preferences",
        ["user_id"],
        unique=True,
    )
    op.create_index(
        "ix_digest_preferences_unsubscribe_token",
        "digest_preferences",
        ["unsubscribe_token"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_digest_preferences_unsubscribe_token", "digest_preferences")
    op.drop_index("ix_digest_preferences_user_id", "digest_preferences")
    op.drop_table("digest_preferences")
