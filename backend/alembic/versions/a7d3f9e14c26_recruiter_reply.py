"""Recruiter reply: inbound mail detection + auto-reply

Adds ``recruiter_emails`` (one row per detected inbound message, with the
classification, the matched profile, the confidence band that fired and what was
done about it) and ``recruiter_reply_preferences`` (the two per-user switches and
the scan bookkeeping).

Purely additive: no existing table is touched, so the downgrade is a clean drop
and nothing else in the schema references either table.

Revision ID: a7d3f9e14c26
Revises: f5c2a7d3b810
Create Date: 2026-07-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d3f9e14c26"
down_revision: str | None = "f5c2a7d3b810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recruiter_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("gmail_account_id", sa.Integer(), nullable=True),
        sa.Column("gmail_message_id", sa.String(length=255), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=255), nullable=True),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("from_name", sa.String(length=200), nullable=True),
        sa.Column("subject", sa.String(length=998), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("snippet", sa.String(length=500), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(
                "RECRUITER_OUTREACH",
                "HIRING_MANAGER",
                "JOB_ALERT",
                "ATS_AUTOMATED",
                "NOT_RECRUITER",
                "UNKNOWN",
                name="recruiteremailkind",
                native_enum=False,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("classification_confidence", sa.Float(), nullable=False),
        sa.Column("classified_by", sa.String(length=120), nullable=True),
        sa.Column("extracted", sa.JSON(), nullable=True),
        sa.Column("matched_profile_id", sa.Integer(), nullable=True),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.Column("route_confidence", sa.Float(), nullable=True),
        sa.Column(
            "route",
            sa.Enum(
                "AUTO", "DRAFT", "FLAG", name="replyroute", native_enum=False, length=8
            ),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "DETECTED",
                "CLASSIFIED",
                "FLAGGED",
                "DRAFTED",
                "REPLY_QUEUED",
                "REPLIED",
                "IGNORED",
                "FAILED",
                name="recruiteremailstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("flag_reason", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("reply_email_id", sa.Integer(), nullable=True),
        sa.Column("application_id", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["gmail_account_id"], ["gmail_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["matched_profile_id"], ["profiles.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["reply_email_id"], ["emails.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        # The scanner's idempotency key: seeing a message twice must never
        # produce a second row, and so never a second reply.
        sa.UniqueConstraint(
            "user_id", "gmail_message_id", name="uq_recruiter_email_user_message"
        ),
    )
    op.create_index(
        "ix_recruiter_emails_user_id", "recruiter_emails", ["user_id"], unique=False
    )
    op.create_index(
        "ix_recruiter_emails_gmail_account_id",
        "recruiter_emails",
        ["gmail_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_recruiter_emails_gmail_message_id",
        "recruiter_emails",
        ["gmail_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_recruiter_emails_gmail_thread_id",
        "recruiter_emails",
        ["gmail_thread_id"],
        unique=False,
    )
    op.create_index(
        "ix_recruiter_emails_matched_profile_id",
        "recruiter_emails",
        ["matched_profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_recruiter_emails_reply_email_id",
        "recruiter_emails",
        ["reply_email_id"],
        unique=False,
    )
    op.create_index(
        "ix_recruiter_emails_application_id",
        "recruiter_emails",
        ["application_id"],
        unique=False,
    )
    # The list view's query: everything for one user in one status.
    op.create_index(
        "ix_recruiter_emails_user_status",
        "recruiter_emails",
        ["user_id", "status"],
        unique=False,
    )

    op.create_table(
        "recruiter_reply_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "auto_reply_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "detected_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("replied_count", sa.Integer(), server_default="0", nullable=False),
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
    )
    op.create_index(
        "ix_recruiter_reply_preferences_user_id",
        "recruiter_reply_preferences",
        ["user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recruiter_reply_preferences_user_id",
        table_name="recruiter_reply_preferences",
    )
    op.drop_table("recruiter_reply_preferences")

    for index in (
        "ix_recruiter_emails_user_status",
        "ix_recruiter_emails_application_id",
        "ix_recruiter_emails_reply_email_id",
        "ix_recruiter_emails_matched_profile_id",
        "ix_recruiter_emails_gmail_thread_id",
        "ix_recruiter_emails_gmail_message_id",
        "ix_recruiter_emails_gmail_account_id",
        "ix_recruiter_emails_user_id",
    ):
        op.drop_index(index, table_name="recruiter_emails")
    op.drop_table("recruiter_emails")
