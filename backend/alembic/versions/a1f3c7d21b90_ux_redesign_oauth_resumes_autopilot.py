"""UX redesign: per-user Gmail OAuth, multi-resume profiles, autopilot campaigns

Replaces the single-profile / manual-recruiter model with:

* ``gmail_accounts``  — per-user OAuth sending identities (encrypted tokens),
  superseding the single shared ``keys/token.json`` and ``users.gmail_connected``.
* ``resumes``         — many per user, fully machine-extracted; replaces
  ``candidate_profiles`` (which held one hand-filled profile per user).
* ``recruiter_cache`` — globally shared careers-page crawl results.
* ``campaigns``       — reshaped around autopilot: a resume, a target list, and
  live progress counters.
* ``recruiters``      — provenance columns for scraped contacts.

The ``candidate_profiles`` drop is destructive. Its contents were hand-entered
profile fields that the resume parser now derives from the uploaded PDF, and no
column maps cleanly onto a ``resumes`` row without the source file — so the
downgrade recreates the table empty rather than pretending to restore it.

Revision ID: a1f3c7d21b90
Revises: 18bc0ebb5d75
Create Date: 2026-07-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1f3c7d21b90"
down_revision: str | None = "18bc0ebb5d75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- Connected Gmail accounts -------------------------------------------
    op.create_table(
        "gmail_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("google_sub", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("daily_send_count", sa.Integer(), nullable=False),
        sa.Column("daily_count_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_gmail_accounts_email"),
        sa.UniqueConstraint("google_sub", name="uq_gmail_accounts_google_sub"),
    )
    op.create_index("ix_gmail_accounts_user_id", "gmail_accounts", ["user_id"])
    op.create_index("ix_gmail_accounts_email", "gmail_accounts", ["email"])

    # ---- Resumes -------------------------------------------------------------
    op.create_table(
        "resumes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("headline", sa.String(length=255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("years_experience", sa.Integer(), nullable=True),
        sa.Column("seniority", sa.String(length=50), nullable=True),
        sa.Column("skills", sa.JSON(), nullable=True),
        sa.Column("target_roles", sa.JSON(), nullable=True),
        sa.Column("target_industries", sa.JSON(), nullable=True),
        sa.Column("experience", sa.JSON(), nullable=True),
        sa.Column("education", sa.JSON(), nullable=True),
        sa.Column("links", sa.JSON(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("parsed_with", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_resumes_user_id", "resumes", ["user_id"])

    # ---- Global recruiter-contact cache -------------------------------------
    op.create_table(
        "recruiter_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("emails", sa.JSON(), nullable=True),
        sa.Column("contacts", sa.JSON(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("careers_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain", name="uq_recruiter_cache_domain"),
    )
    op.create_index("ix_recruiter_cache_company", "recruiter_cache", ["company"])
    op.create_index("ix_recruiter_cache_domain", "recruiter_cache", ["domain"])

    # ---- Recruiter provenance ------------------------------------------------
    with op.batch_alter_table("recruiters") as batch:
        batch.add_column(sa.Column("source_url", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")
            )
        )

    # ---- Campaigns reshaped around the autopilot -----------------------------
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("resume_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("target_companies", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column(
                "auto_send", sa.Boolean(), nullable=False, server_default=sa.text("true")
            )
        )
        batch.add_column(
            sa.Column(
                "companies_processed",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "contacts_found", sa.Integer(), nullable=False, server_default=sa.text("0")
            )
        )
        batch.add_column(
            sa.Column(
                "emails_generated",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
        )
        # ``description`` and ``scheduled_at`` had no place in a one-click flow.
        batch.drop_column("description")
        batch.drop_column("scheduled_at")
        batch.create_foreign_key(
            "fk_campaigns_resume_id", "resumes", ["resume_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index("ix_campaigns_resume_id", "campaigns", ["resume_id"])

    # ---- Retire the single-profile model ------------------------------------
    # Gmail connection state now lives on gmail_accounts.status.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("gmail_connected")
    op.drop_table("candidate_profiles")


def downgrade() -> None:
    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("headline", sa.String(length=255), nullable=True),
        sa.Column("years_experience", sa.Integer(), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("resume_text", sa.Text(), nullable=True),
        sa.Column("resume_filename", sa.String(length=255), nullable=True),
        sa.Column("skills", sa.JSON(), nullable=True),
        sa.Column("target_roles", sa.JSON(), nullable=True),
        sa.Column("target_industries", sa.JSON(), nullable=True),
        sa.Column("preferences", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "gmail_connected",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )

    op.drop_index("ix_campaigns_resume_id", table_name="campaigns")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_constraint("fk_campaigns_resume_id", type_="foreignkey")
        batch.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch.add_column(sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
        for column in (
            "completed_at", "started_at", "last_error", "emails_generated",
            "contacts_found", "companies_processed", "auto_send",
            "target_companies", "resume_id",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("recruiters") as batch:
        batch.drop_column("confidence")
        batch.drop_column("source_url")

    op.drop_index("ix_recruiter_cache_domain", table_name="recruiter_cache")
    op.drop_index("ix_recruiter_cache_company", table_name="recruiter_cache")
    op.drop_table("recruiter_cache")

    op.drop_index("ix_resumes_user_id", table_name="resumes")
    op.drop_table("resumes")

    op.drop_index("ix_gmail_accounts_email", table_name="gmail_accounts")
    op.drop_index("ix_gmail_accounts_user_id", table_name="gmail_accounts")
    op.drop_table("gmail_accounts")
