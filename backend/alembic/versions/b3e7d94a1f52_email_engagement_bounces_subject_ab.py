"""Email engagement: tracking events, bounce classification, subject A/B, timing

Six email features land together because they share a surface:

* ``email_events`` — open/click events on outbound mail, plus denormalized
  counters and a ``tracking_token`` on ``emails``.
* ``email_bounces`` + ``recruiters`` delivery columns — hard/soft bounce
  classification. ``opted_out`` is deliberately untouched: it means consent, and
  conflating it with deliverability is the bug this replaces.
* ``subject_variants`` + ``emails.subject_variant_id`` — the subject-line
  experiment. The winner is recorded on the variant itself rather than back on
  the campaign, which would make these two tables circularly dependent for no
  saved reads.
* ``recruiters.timezone`` — cached IANA zone for send-time optimization.
* ``campaigns.follow_up_step_days`` — explicit day offsets (``[3, 7]``) for the
  follow-up sequence.

The warm-up ramp reshape needs no DDL: it reads the ``gmail_accounts`` columns
that already exist.

The enum columns are non-native VARCHARs with no CHECK constraint, per house
style, so no type objects are created or dropped.

Purely additive; the downgrade is a clean reversal.

Revision ID: b3e7d94a1f52
Revises: a7d3f9e14c26
Create Date: 2026-07-27
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3e7d94a1f52"
down_revision: str | None = "a7d3f9e14c26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    # ---- Subject-line variants ----------------------------------------------
    # Created first: emails and campaigns both reference it below.
    op.create_table(
        "subject_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=2), nullable=False),
        sa.Column("text", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_winner", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sends", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replies", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "generated_with",
            sa.String(length=16),
            nullable=False,
            server_default="template",
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "campaign_id", "label", name="uq_subject_variant_campaign_label"
        ),
    )
    op.create_index(
        op.f("ix_subject_variants_user_id"), "subject_variants", ["user_id"]
    )
    op.create_index(
        op.f("ix_subject_variants_campaign_id"), "subject_variants", ["campaign_id"]
    )

    # ---- Open / click events -------------------------------------------------
    op.create_table(
        "email_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=8), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "is_prefetch", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_email_events_user_id"), "email_events", ["user_id"])
    op.create_index(op.f("ix_email_events_email_id"), "email_events", ["email_id"])
    op.create_index(
        op.f("ix_email_events_occurred_at"), "email_events", ["occurred_at"]
    )

    # ---- Bounces -------------------------------------------------------------
    op.create_table(
        "email_bounces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("recruiter_id", sa.Integer(), nullable=True),
        sa.Column("email_id", sa.Integer(), nullable=True),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["recruiter_id"], ["recruiters.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], ondelete="SET NULL"),
    )
    op.create_index(op.f("ix_email_bounces_user_id"), "email_bounces", ["user_id"])
    op.create_index(
        op.f("ix_email_bounces_recruiter_id"), "email_bounces", ["recruiter_id"]
    )
    op.create_index(op.f("ix_email_bounces_address"), "email_bounces", ["address"])
    op.create_index(op.f("ix_email_bounces_domain"), "email_bounces", ["domain"])
    op.create_index(
        op.f("ix_email_bounces_occurred_at"), "email_bounces", ["occurred_at"]
    )

    # ---- Tracking + experiment columns on emails -----------------------------
    op.add_column(
        "emails", sa.Column("tracking_token", sa.String(length=64), nullable=True)
    )
    op.create_index(
        op.f("ix_emails_tracking_token"), "emails", ["tracking_token"], unique=True
    )
    op.add_column(
        "emails", sa.Column("open_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "emails",
        sa.Column("click_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "emails", sa.Column("first_opened_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "emails", sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "emails",
        sa.Column("first_clicked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("emails", sa.Column("subject_variant_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_emails_subject_variant_id"), "emails", ["subject_variant_id"]
    )

    # ---- Deliverability + timezone on recruiters -----------------------------
    op.add_column(
        "recruiters",
        sa.Column(
            "delivery_state", sa.String(length=16), nullable=False, server_default="OK"
        ),
    )
    op.create_index(
        op.f("ix_recruiters_delivery_state"), "recruiters", ["delivery_state"]
    )
    op.add_column(
        "recruiters",
        sa.Column("soft_bounce_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "recruiters", sa.Column("last_bounce_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "recruiters", sa.Column("last_bounce_reason", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "recruiters", sa.Column("timezone", sa.String(length=64), nullable=True)
    )

    # ---- Campaign follow-up offsets ------------------------------------------
    op.add_column("campaigns", sa.Column("follow_up_step_days", sa.JSON(), nullable=True))

    # SQLite can't ALTER a table to add a constraint; production is Postgres,
    # where the real FK is created. (Tests build the schema with create_all, so
    # they get it from the models regardless.)
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_emails_subject_variant_id",
            "emails",
            "subject_variants",
            ["subject_variant_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_emails_subject_variant_id", "emails", type_="foreignkey")

    op.drop_column("campaigns", "follow_up_step_days")

    op.drop_column("recruiters", "timezone")
    op.drop_column("recruiters", "last_bounce_reason")
    op.drop_column("recruiters", "last_bounce_at")
    op.drop_column("recruiters", "soft_bounce_count")
    op.drop_index(op.f("ix_recruiters_delivery_state"), table_name="recruiters")
    op.drop_column("recruiters", "delivery_state")

    op.drop_index(op.f("ix_emails_subject_variant_id"), table_name="emails")
    op.drop_column("emails", "subject_variant_id")
    op.drop_column("emails", "first_clicked_at")
    op.drop_column("emails", "last_opened_at")
    op.drop_column("emails", "first_opened_at")
    op.drop_column("emails", "click_count")
    op.drop_column("emails", "open_count")
    op.drop_index(op.f("ix_emails_tracking_token"), table_name="emails")
    op.drop_column("emails", "tracking_token")

    for index in (
        "ix_email_bounces_occurred_at",
        "ix_email_bounces_domain",
        "ix_email_bounces_address",
        "ix_email_bounces_recruiter_id",
        "ix_email_bounces_user_id",
    ):
        op.drop_index(op.f(index), table_name="email_bounces")
    op.drop_table("email_bounces")

    for index in (
        "ix_email_events_occurred_at",
        "ix_email_events_email_id",
        "ix_email_events_user_id",
    ):
        op.drop_index(op.f(index), table_name="email_events")
    op.drop_table("email_events")

    op.drop_index(op.f("ix_subject_variants_campaign_id"), table_name="subject_variants")
    op.drop_index(op.f("ix_subject_variants_user_id"), table_name="subject_variants")
    op.drop_table("subject_variants")
