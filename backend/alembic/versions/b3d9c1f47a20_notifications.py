"""In-app notifications, and the per-kind mute that makes them bearable

The product could tell its user something in exactly two ways: a toast that
lasts four seconds and only fires for an action they just took, and a weekly
digest. Everything with a clock on it — a grant expiring Thursday, a follow-up
due Wednesday, a recruiter waiting since Tuesday — fell between the two and was
"visible in the app" on a page nobody had a reason to open.

Two tables.

``notifications`` carries a ``dedupe_key`` unique per user, and that constraint
is the entire design rather than a safety net: most of these rows are written by
an hourly sweep that re-derives the same conditions every tick, so the key is
what makes "your grant expires Thursday" one notification instead of a hundred
and sixty-eight. See ``app/models/notification.py`` for the two shapes a key
takes and why one of them has a date in it.

``notification_preferences`` stores *silenced* kinds rather than a boolean per
kind. A column per kind is a migration every time the product learns to notice
something new, and the release that adds the column is the release where every
existing row defaults to whatever the migration author typed — rather than to
"on", which is the only defensible default for a notification nobody has seen
yet. Absence means enabled, everywhere.

Revision ID: b3d9c1f47a20
Revises: a1c7f4e29d05
Create Date: 2026-08-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3d9c1f47a20"
down_revision: str | None = "a1c7f4e29d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column(
            "severity", sa.String(length=10), nullable=False, server_default="info"
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=True),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_user_dedupe"),
    )
    op.create_index(
        op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False
    )
    op.create_index(
        "ix_notifications_user_created",
        "notifications",
        ["user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("muted_kinds", sa.JSON(), nullable=False, server_default="[]"),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # A unique *constraint*, not a unique index, and no separate
        # `ix_..._user_id` beside it. `c5a9e63b17d4` dropped exactly that
        # duplicate pair off three other preference tables; this one is born
        # without it. See `tests/test_schema_drift.py`.
        sa.UniqueConstraint("user_id", name="uq_notification_preferences_user"),
    )


def downgrade() -> None:
    op.drop_table("notification_preferences")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_table("notifications")
