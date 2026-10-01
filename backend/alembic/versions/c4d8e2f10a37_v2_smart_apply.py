"""V2 Smart Apply: tailoring, fit scoring, follow-ups, job monitoring

Adds the five tables Phase 1 runs on, plus the campaign columns that configure a
follow-up sequence:

* ``job_searches``     — standing search criteria, re-run by the monitoring beat.
* ``job_postings``     — discovered/pasted roles, deduped per user on fingerprint.
* ``tailored_resumes`` — a tailored resume + cover letter for one job description.
* ``fit_scores``       — cached 0-100 verdicts with their per-dimension breakdown.
* ``follow_ups``       — scheduled nudges on an outreach thread.

Purely additive: nothing existing is dropped or rewritten, so the downgrade is a
clean reversal.

Revision ID: c4d8e2f10a37
Revises: a1f3c7d21b90
Create Date: 2026-07-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4d8e2f10a37"
down_revision: str | None = "a1f3c7d21b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- Campaign follow-up configuration -----------------------------------
    # Server defaults so existing campaigns keep working without a backfill;
    # the model-side defaults cover new rows.
    op.add_column(
        "campaigns",
        sa.Column(
            "follow_up_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column("follow_up_count", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "follow_up_interval_days", sa.Integer(), nullable=False, server_default="4"
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "follow_up_stop_on_reply",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )

    # ---- Saved job searches --------------------------------------------------
    op.create_table(
        "job_searches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("resume_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=True),
        sa.Column("keywords", sa.JSON(), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("remote_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("min_fit_score", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("interval_hours", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("jobs_found", sa.Integer(), nullable=False, server_default="0"),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_searches_user_id", "job_searches", ["user_id"])

    # ---- Discovered job postings --------------------------------------------
    op.create_table(
        "job_postings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("search_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("company", sa.String(length=255), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("salary_text", sa.String(length=255), nullable=True),
        sa.Column("remote", sa.Boolean(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="NEW"),
        sa.Column("fit_score", sa.Float(), nullable=True),
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
        sa.ForeignKeyConstraint(["search_id"], ["job_searches.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_postings_user_id", "job_postings", ["user_id"])
    op.create_index("ix_job_postings_search_id", "job_postings", ["search_id"])
    op.create_index("ix_job_postings_company", "job_postings", ["company"])
    op.create_index("ix_job_postings_fingerprint", "job_postings", ["fingerprint"])
    op.create_index("ix_job_postings_status", "job_postings", ["status"])
    # The dedupe guarantee the feed relies on: one row per role per user.
    op.create_unique_constraint(
        "uq_job_posting_user_fingerprint", "job_postings", ["user_id", "fingerprint"]
    )

    # ---- Tailored resumes ----------------------------------------------------
    op.create_table(
        "tailored_resumes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("resume_id", sa.Integer(), nullable=False),
        sa.Column("job_posting_id", sa.Integer(), nullable=True),
        sa.Column("job_title", sa.String(length=500), nullable=True),
        sa.Column("job_company", sa.String(length=255), nullable=True),
        sa.Column("job_url", sa.Text(), nullable=True),
        sa.Column("job_description", sa.Text(), nullable=True),
        sa.Column("tailored_summary", sa.Text(), nullable=True),
        sa.Column("ordered_skills", sa.JSON(), nullable=True),
        sa.Column("highlighted_experience", sa.JSON(), nullable=True),
        sa.Column("matched_keywords", sa.JSON(), nullable=True),
        sa.Column("missing_keywords", sa.JSON(), nullable=True),
        sa.Column("cover_letter", sa.Text(), nullable=True),
        sa.Column("generated_with", sa.String(length=20), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
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
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["job_posting_id"], ["job_postings.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tailored_resumes_user_id", "tailored_resumes", ["user_id"])
    op.create_index("ix_tailored_resumes_resume_id", "tailored_resumes", ["resume_id"])
    op.create_index(
        "ix_tailored_resumes_job_posting_id", "tailored_resumes", ["job_posting_id"]
    )

    # ---- Fit scores ----------------------------------------------------------
    op.create_table(
        "fit_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("resume_id", sa.Integer(), nullable=False),
        sa.Column("job_posting_id", sa.Integer(), nullable=True),
        sa.Column("jd_hash", sa.String(length=64), nullable=False),
        sa.Column("job_title", sa.String(length=500), nullable=True),
        sa.Column("job_company", sa.String(length=255), nullable=True),
        sa.Column("overall", sa.Float(), nullable=False),
        sa.Column("skills_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("experience_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("industry_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("location_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("salary_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("matched_skills", sa.JSON(), nullable=True),
        sa.Column("missing_skills", sa.JSON(), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("recommendation", sa.String(length=20), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["job_posting_id"], ["job_postings.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resume_id", "jd_hash", name="uq_fit_score_resume_jd"),
    )
    op.create_index("ix_fit_scores_user_id", "fit_scores", ["user_id"])
    op.create_index("ix_fit_scores_resume_id", "fit_scores", ["resume_id"])
    op.create_index("ix_fit_scores_job_posting_id", "fit_scores", ["job_posting_id"])
    op.create_index("ix_fit_scores_jd_hash", "fit_scores", ["jd_hash"])

    # ---- Scheduled follow-ups ------------------------------------------------
    op.create_table(
        "follow_ups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("email_id", sa.Integer(), nullable=True),
        sa.Column("step", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="SCHEDULED"
        ),
        sa.Column(
            "template", sa.String(length=24), nullable=False, server_default="NO_RESPONSE"
        ),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_follow_ups_application_id", "follow_ups", ["application_id"])
    op.create_index("ix_follow_ups_scheduled_at", "follow_ups", ["scheduled_at"])
    op.create_index("ix_follow_ups_status", "follow_ups", ["status"])


def downgrade() -> None:
    op.drop_table("follow_ups")
    op.drop_table("fit_scores")
    op.drop_table("tailored_resumes")
    op.drop_constraint(
        "uq_job_posting_user_fingerprint", "job_postings", type_="unique"
    )
    op.drop_table("job_postings")
    op.drop_table("job_searches")

    op.drop_column("campaigns", "follow_up_stop_on_reply")
    op.drop_column("campaigns", "follow_up_interval_days")
    op.drop_column("campaigns", "follow_up_count")
    op.drop_column("campaigns", "follow_up_enabled")
