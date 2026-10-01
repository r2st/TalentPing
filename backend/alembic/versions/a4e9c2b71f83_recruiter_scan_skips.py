"""Remember the mail the scanner fetched and dismissed on sight

Adds ``recruiter_scan_skips``, and the ``skipped_blocked_sender`` counter that
``recruiter_scan_runs`` has always been handed and always dropped.

``inbound_scanner`` runs its cheap filter — "do we have a row for this id?" —
before fetching anything, and every filter after it on the fetched message,
because a Gmail list call returns ids and nothing else. A message dismissed by
one of those later filters left no row, so the cheap filter could not see it and
the next pass fetched the same body again. In production one mailbox listed 394
messages every five minutes, fetched 152, dismissed 145 of them as blocked
senders, and detected nothing — about 43,000 wasted ``messages.get`` calls a day
against a quota the whole deployment shares, and a scan that took 25 seconds
instead of a third of one.

Only the two filters that decide on the ``From:`` header write a row here, so a
verdict that could change on a re-read is never made permanent. The reasoning is
in :mod:`app.models.recruiter_scan_skip`.

Its own table rather than a ``recruiter_emails`` row like a bounce gets: that
table means "one row per detected inbound message" and is what the Recruiter
Inbox lists, so 145 newsletters a mailbox would bury the mail the user came to
read.

``uq_recruiter_scan_skip_user_message`` is what makes the write idempotent and
settles the race between two overlapping scans. The insert is savepointed, so
losing that race costs one row rather than the scan.

``server_default`` on ``skipped_blocked_sender`` because the column is NOT NULL
and ``recruiter_scan_runs`` is append-only with rows already in it.

Revision ID: a4e9c2b71f83
Revises: f2c7a9e4b310
Create Date: 2026-08-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4e9c2b71f83"
down_revision: str | None = "f2c7a9e4b310"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recruiter_scan_skips",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("gmail_account_id", sa.Integer(), nullable=True),
        sa.Column("gmail_message_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # SET NULL, matching ``recruiter_emails`` and ``recruiter_scan_runs``:
        # disconnecting a mailbox must not forget which of its mail was already
        # dismissed, or the next scan re-fetches every bit of it.
        sa.ForeignKeyConstraint(
            ["gmail_account_id"], ["gmail_accounts.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "gmail_message_id", name="uq_recruiter_scan_skip_user_message"
        ),
    )
    op.create_index(
        "ix_recruiter_scan_skips_user_id", "recruiter_scan_skips", ["user_id"]
    )
    op.create_index(
        "ix_recruiter_scan_skips_gmail_account_id",
        "recruiter_scan_skips",
        ["gmail_account_id"],
    )
    op.create_index(
        "ix_recruiter_scan_skips_user_created",
        "recruiter_scan_skips",
        ["user_id", "created_at"],
    )

    op.add_column(
        "recruiter_scan_runs",
        sa.Column(
            "skipped_blocked_sender",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("recruiter_scan_runs", "skipped_blocked_sender")
    op.drop_index("ix_recruiter_scan_skips_user_created", "recruiter_scan_skips")
    op.drop_index("ix_recruiter_scan_skips_gmail_account_id", "recruiter_scan_skips")
    op.drop_index("ix_recruiter_scan_skips_user_id", "recruiter_scan_skips")
    op.drop_table("recruiter_scan_skips")
