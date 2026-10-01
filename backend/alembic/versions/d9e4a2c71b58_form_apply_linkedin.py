"""Form applications, the answer bank, and LinkedIn credentials

The browser agent's own tables, added alongside the email pipeline rather than
folded into it — a form submission is a *run* with attempts, steps and
screenshots, not a conversation with a recruiter:

* ``form_applications`` — one row per attempt at one ATS form, carrying the full
  audit trail: which profile fields were typed, which screening questions were
  answered and from where, every step's screenshot, and the exit status.
* ``form_apply_profiles`` — one row per user, holding the answers a resume
  cannot supply (work authorization, sponsorship, notice period, salary
  expectations). These are legally significant, so they come from the candidate
  and are replayed verbatim rather than inferred.
* ``linkedin_accounts`` — one row per user: the Fernet-encrypted LinkedIn
  sign-in, the (also encrypted) session cookies that let us avoid retyping it,
  and the rolling-24h Easy Apply counter.

The status/platform columns are non-native VARCHAR enums, matching every other
enum in this schema, so adding a member later needs no DDL.

Purely additive; the downgrade is a clean reversal.

Revision ID: d9e4a2c71b58
Revises: b7c5d1e93f42
Create Date: 2026-07-25
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9e4a2c71b58"
down_revision: str | None = "b7c5d1e93f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- Form applications --------------------------------------------------
    op.create_table(
        "form_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("job_posting_id", sa.Integer(), nullable=True),
        sa.Column("resume_id", sa.Integer(), nullable=True),
        sa.Column(
            "platform",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("job_title", sa.String(length=500), nullable=True),
        sa.Column("company", sa.String(length=255), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="QUEUED"
        ),
        sa.Column(
            "submit_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("filled_fields", sa.JSON(), nullable=True),
        sa.Column("answers", sa.JSON(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=True),
        sa.Column(
            "resume_uploaded", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("unanswered", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
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
        # SET NULL, not CASCADE: pruning the job feed must never erase the record
        # that we submitted an application on the candidate's behalf.
        sa.ForeignKeyConstraint(
            ["job_posting_id"], ["job_postings.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_form_applications_user_id", "form_applications", ["user_id"])
    op.create_index(
        "ix_form_applications_job_posting_id", "form_applications", ["job_posting_id"]
    )
    op.create_index("ix_form_applications_status", "form_applications", ["status"])
    op.create_index("ix_form_applications_platform", "form_applications", ["platform"])

    # ---- The answer bank ----------------------------------------------------
    op.create_table(
        "form_apply_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_authorized", sa.Boolean(), nullable=True),
        sa.Column("requires_sponsorship", sa.Boolean(), nullable=True),
        sa.Column("willing_to_relocate", sa.Boolean(), nullable=True),
        sa.Column("earliest_start", sa.String(length=120), nullable=True),
        sa.Column("notice_period_days", sa.Integer(), nullable=True),
        sa.Column("desired_salary", sa.String(length=120), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("linkedin_url", sa.String(length=500), nullable=True),
        sa.Column("website_url", sa.String(length=500), nullable=True),
        sa.Column("github_url", sa.String(length=500), nullable=True),
        sa.Column("address_city", sa.String(length=120), nullable=True),
        sa.Column("address_country", sa.String(length=120), nullable=True),
        sa.Column("custom_answers", sa.JSON(), nullable=True),
        sa.Column(
            "llm_answers_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
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
        sa.UniqueConstraint("user_id", name="uq_form_apply_profile_user"),
    )
    op.create_index(
        "ix_form_apply_profiles_user_id", "form_apply_profiles", ["user_id"]
    )

    # ---- LinkedIn credentials ----------------------------------------------
    op.create_table(
        "linkedin_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        # Fernet ciphertext, never plaintext. See services/crypto.py.
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("session_state_encrypted", sa.Text(), nullable=True),
        sa.Column("session_saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="connected"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "daily_apply_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("daily_count_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_apply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "easy_apply_total", sa.Integer(), nullable=False, server_default="0"
        ),
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
        sa.UniqueConstraint("user_id", name="uq_linkedin_account_user"),
    )
    op.create_index("ix_linkedin_accounts_user_id", "linkedin_accounts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_linkedin_accounts_user_id", table_name="linkedin_accounts")
    op.drop_table("linkedin_accounts")

    op.drop_index("ix_form_apply_profiles_user_id", table_name="form_apply_profiles")
    op.drop_table("form_apply_profiles")

    op.drop_index("ix_form_applications_platform", table_name="form_applications")
    op.drop_index("ix_form_applications_status", table_name="form_applications")
    op.drop_index("ix_form_applications_job_posting_id", table_name="form_applications")
    op.drop_index("ix_form_applications_user_id", table_name="form_applications")
    op.drop_table("form_applications")
