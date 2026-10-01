"""V2 Auto-Apply: autopilot preferences, sender reputation, form-apply tracking

Adds the moving parts the end-to-end autopilot runs on:

* ``autopilot_preferences`` — one row per user: the targeting, selectivity and
  send-behaviour knobs the auto-apply beat task reads.
* ``gmail_accounts`` reputation columns — warm-up start, lifetime send/bounce/
  complaint counters and a cool-down window, so sending from a personal mailbox
  ramps gradually and self-pauses if deliverability drops.
* ``applications.job_posting_id`` — links an auto-applied outreach to the exact
  posting it targets.
* ``job_postings`` form-apply columns — the outcome of the Playwright form filler.

The enum columns (``applicationstatus``, ``replyintent``) are non-native VARCHARs
with no CHECK constraint, so the new ``OFFER`` members need no DDL here.

Purely additive; the downgrade is a clean reversal.

Revision ID: e7a1b9c2d4f5
Revises: c4d8e2f10a37
Create Date: 2026-07-24
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7a1b9c2d4f5"
down_revision: str | None = "c4d8e2f10a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- Autopilot preferences ----------------------------------------------
    op.create_table(
        "autopilot_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("resume_id", sa.Integer(), nullable=True),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("target_roles", sa.JSON(), nullable=True),
        sa.Column("target_industries", sa.JSON(), nullable=True),
        sa.Column("locations", sa.JSON(), nullable=True),
        sa.Column(
            "remote_only", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("min_fit_score", sa.Integer(), nullable=False, server_default="70"),
        sa.Column(
            "daily_application_limit",
            sa.Integer(),
            nullable=False,
            server_default="10",
        ),
        sa.Column("auto_send", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "form_autofill_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("follow_up_count", sa.Integer(), nullable=False, server_default="2"),
        sa.Column(
            "follow_up_interval_days",
            sa.Integer(),
            nullable=False,
            server_default="4",
        ),
        sa.Column(
            "follow_up_stop_on_reply",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        # Stamped the first time the user saves preferences themselves. The row
        # itself is created lazily on any read, so its mere existence says
        # nothing about whether setup step 3 is done — this column does.
        sa.Column("configured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "applications_created", sa.Integer(), nullable=False, server_default="0"
        ),
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
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("user_id", name="uq_autopilot_user"),
    )
    op.create_index(
        op.f("ix_autopilot_preferences_user_id"),
        "autopilot_preferences",
        ["user_id"],
        unique=False,
    )

    # ---- Sender reputation on gmail_accounts --------------------------------
    op.add_column(
        "gmail_accounts",
        sa.Column("warmup_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "gmail_accounts",
        sa.Column("sent_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "gmail_accounts",
        sa.Column("bounce_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "gmail_accounts",
        sa.Column("complaint_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "gmail_accounts",
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "gmail_accounts",
        sa.Column("pause_reason", sa.String(length=255), nullable=True),
    )

    # ---- Application → job posting link -------------------------------------
    op.add_column(
        "applications",
        sa.Column("job_posting_id", sa.Integer(), nullable=True),
    )
    # SQLite can't ALTER a table to add a constraint; production is Postgres,
    # where the real FK is created. (Tests build the schema with create_all, so
    # they get the FK from the model regardless.)
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_applications_job_posting_id",
            "applications",
            "job_postings",
            ["job_posting_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        op.f("ix_applications_job_posting_id"),
        "applications",
        ["job_posting_id"],
        unique=False,
    )

    # ---- Form-apply tracking on job_postings --------------------------------
    op.add_column(
        "job_postings",
        sa.Column("form_apply_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "job_postings", sa.Column("form_apply_note", sa.Text(), nullable=True)
    )
    op.add_column(
        "job_postings",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_postings", "applied_at")
    op.drop_column("job_postings", "form_apply_note")
    op.drop_column("job_postings", "form_apply_status")

    op.drop_index(op.f("ix_applications_job_posting_id"), table_name="applications")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint(
            "fk_applications_job_posting_id", "applications", type_="foreignkey"
        )
    op.drop_column("applications", "job_posting_id")

    for column in (
        "pause_reason",
        "paused_until",
        "complaint_count",
        "bounce_count",
        "sent_total",
        "warmup_started_at",
    ):
        op.drop_column("gmail_accounts", column)

    op.drop_index(
        op.f("ix_autopilot_preferences_user_id"), table_name="autopilot_preferences"
    )
    op.drop_table("autopilot_preferences")
