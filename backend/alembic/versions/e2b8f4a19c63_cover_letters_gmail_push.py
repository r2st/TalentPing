"""Cover letters, Gmail push watches, Scout's reply drafts, rendered resumes

Four additive changes:

* ``cover_letters`` — the letter becomes its own artifact rather than a text
  column on ``tailored_resumes``. It has its own life cycle (regenerated,
  edited, downloaded, attached or inlined) and its own provenance: the verified
  company facts it was allowed to draw on are stored beside it, so a reader can
  check the basis of a personalized claim instead of trusting it. The legacy
  ``tailored_resumes.cover_letter`` column is left in place — old rows keep
  their letters, and nothing new writes there.
* ``gmail_watches`` — one push subscription per connected mailbox. ``history_id``
  is the processed-up-to cursor, advanced only after the messages it covers are
  stored, so a crash replays a range rather than skipping the replies inside it.
* ``emails.draft_template`` / ``draft_note`` / ``cover_letter_id`` — which brief
  Scout wrote a draft to, and why. Null for anything a human composed, which is
  what keeps "Scout suggests" and "you wrote this" distinguishable in the inbox.
* ``tailored_resumes`` PDF columns and ``autopilot_preferences`` cover-letter
  settings.

Purely additive; the downgrade is a clean reversal.

Revision ID: e2b8f4a19c63
Revises: d9e4a2c71b58
Create Date: 2026-07-25
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2b8f4a19c63"
down_revision: str | None = "d9e4a2c71b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- Cover letters -------------------------------------------------------
    op.create_table(
        "cover_letters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("resume_id", sa.Integer(), nullable=False),
        sa.Column("job_posting_id", sa.Integer(), nullable=True),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("tailored_resume_id", sa.Integer(), nullable=True),
        sa.Column("job_title", sa.String(length=500), nullable=True),
        sa.Column("job_company", sa.String(length=255), nullable=True),
        sa.Column("greeting", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("sign_off", sa.String(length=255), nullable=True),
        sa.Column("company_research", sa.JSON(), nullable=True, server_default="[]"),
        sa.Column("highlights", sa.JSON(), nullable=True, server_default="[]"),
        sa.Column("missing_keywords", sa.JSON(), nullable=True, server_default="[]"),
        sa.Column("delivery", sa.String(length=16), nullable=False, server_default="inline"),
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("generated_with", sa.String(length=20), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
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
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="CASCADE"),
        # SET NULL rather than CASCADE on all three: pruning the job feed, or an
        # application, must never delete prose the candidate might still want.
        sa.ForeignKeyConstraint(["job_posting_id"], ["job_postings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["tailored_resume_id"], ["tailored_resumes.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_cover_letters_user_id", "cover_letters", ["user_id"])
    op.create_index("ix_cover_letters_resume_id", "cover_letters", ["resume_id"])
    op.create_index("ix_cover_letters_job_posting_id", "cover_letters", ["job_posting_id"])
    op.create_index("ix_cover_letters_application_id", "cover_letters", ["application_id"])
    op.create_index(
        "ix_cover_letters_tailored_resume_id", "cover_letters", ["tailored_resume_id"]
    )

    # ---- Gmail push subscriptions -------------------------------------------
    op.create_table(
        "gmail_watches",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Unique: calling watch() again replaces the subscription at Google's
        # end, so a second row for one mailbox could only ever be stale.
        sa.Column("gmail_account_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("topic", sa.String(length=500), nullable=True),
        sa.Column("history_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "notifications_received", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("messages_ingested", sa.Integer(), nullable=False, server_default="0"),
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
        sa.ForeignKeyConstraint(
            ["gmail_account_id"], ["gmail_accounts.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_gmail_watches_gmail_account_id", "gmail_watches", ["gmail_account_id"])

    # ---- Scout's reply drafts ------------------------------------------------
    op.add_column("emails", sa.Column("draft_template", sa.String(length=24), nullable=True))
    op.add_column("emails", sa.Column("draft_note", sa.Text(), nullable=True))
    op.add_column("emails", sa.Column("cover_letter_id", sa.Integer(), nullable=True))
    op.create_index("ix_emails_cover_letter_id", "emails", ["cover_letter_id"])
    op.create_foreign_key(
        "fk_emails_cover_letter_id",
        "emails",
        "cover_letters",
        ["cover_letter_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- Rendered tailored resumes ------------------------------------------
    op.add_column("tailored_resumes", sa.Column("pdf_bytes", sa.LargeBinary(), nullable=True))
    op.add_column(
        "tailored_resumes", sa.Column("pdf_filename", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "tailored_resumes",
        sa.Column("pdf_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tailored_resumes",
        sa.Column("bullets_generated_with", sa.String(length=20), nullable=True),
    )

    # ---- Autopilot cover-letter settings ------------------------------------
    op.add_column(
        "autopilot_preferences",
        sa.Column(
            "cover_letter_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "autopilot_preferences",
        sa.Column(
            "cover_letter_delivery",
            sa.String(length=16),
            nullable=False,
            server_default="inline",
        ),
    )


def downgrade() -> None:
    op.drop_column("autopilot_preferences", "cover_letter_delivery")
    op.drop_column("autopilot_preferences", "cover_letter_enabled")

    op.drop_column("tailored_resumes", "bullets_generated_with")
    op.drop_column("tailored_resumes", "pdf_generated_at")
    op.drop_column("tailored_resumes", "pdf_filename")
    op.drop_column("tailored_resumes", "pdf_bytes")

    op.drop_constraint("fk_emails_cover_letter_id", "emails", type_="foreignkey")
    op.drop_index("ix_emails_cover_letter_id", table_name="emails")
    op.drop_column("emails", "cover_letter_id")
    op.drop_column("emails", "draft_note")
    op.drop_column("emails", "draft_template")

    op.drop_index("ix_gmail_watches_gmail_account_id", table_name="gmail_watches")
    op.drop_table("gmail_watches")

    op.drop_index("ix_cover_letters_tailored_resume_id", table_name="cover_letters")
    op.drop_index("ix_cover_letters_application_id", table_name="cover_letters")
    op.drop_index("ix_cover_letters_job_posting_id", table_name="cover_letters")
    op.drop_index("ix_cover_letters_resume_id", table_name="cover_letters")
    op.drop_index("ix_cover_letters_user_id", table_name="cover_letters")
    op.drop_table("cover_letters")
