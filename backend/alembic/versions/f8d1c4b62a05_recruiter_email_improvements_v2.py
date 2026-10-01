"""Recruiter email improvements V2: threading, feedback, follow-ups, stats, resume choice

Five of the six changes in ``docs/features/recruiter-email-improvements-v2.md``
need storage, and four of them exist because a number the code already computed
was never written down:

* **Threading.** The scanner reads the recruiter's ``Message-ID`` header and
  throws it away, so a reply can carry Gmail's ``threadId`` and nothing else —
  which threads in Gmail and in no other client. ``recruiter_emails`` keeps the
  headers; ``emails`` keeps what the reply must send back.
* **Feedback.** Approving or discarding a draft was a judgement nobody recorded.
  ``reply_feedback`` is the audit trail, ``classifier_priors`` the rolled-up
  counters read while classifying.
* **Follow-ups.** Nothing linked a second message from a recruiter to the first
  one we answered, so it could be auto-answered again as though it were first
  contact.
* **Stats.** ``ScanResult`` counts everything a dashboard needs and
  ``inbound_scanner`` logs it once and drops it. ``recruiter_scan_runs`` is that
  same dict, persisted.
* **Resume choice.** Which document a reply carries was re-derived at send time
  with no record of why.

``emails.email_references`` is deliberately not called ``references``: it is a
reserved word in both SQLite and Postgres and would need quoting forever.

Every column added here is nullable or carries a server default, and no row is
rewritten — an existing deployment upgrades without a table scan and behaves
exactly as it did until the corresponding feature switch is turned on.

Revision ID: f8d1c4b62a05
Revises: d2b6e8f04a71
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "f8d1c4b62a05"
down_revision: str | None = "d2b6e8f04a71"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ---- RFC 5322 threading -------------------------------------------------
    op.add_column(
        "recruiter_emails",
        sa.Column("rfc_message_id", sa.String(length=998), nullable=True),
    )
    op.add_column(
        "recruiter_emails", sa.Column("rfc_references", sa.Text(), nullable=True)
    )
    op.add_column("emails", sa.Column("in_reply_to", sa.String(length=998), nullable=True))
    op.add_column("emails", sa.Column("email_references", sa.Text(), nullable=True))

    # ---- Feedback -----------------------------------------------------------
    op.add_column(
        "recruiter_emails",
        sa.Column(
            "confidence_adjustment",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )

    # ---- Follow-ups ---------------------------------------------------------
    op.add_column(
        "recruiter_emails",
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "recruiter_emails", sa.Column("escalation_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "recruiter_emails",
        sa.Column("follow_up_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "recruiter_emails",
        sa.Column("last_follow_up_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "recruiter_emails",
        sa.Column("previous_recruiter_email_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_recruiter_emails_previous_recruiter_email_id",
        "recruiter_emails",
        ["previous_recruiter_email_id"],
    )

    # ---- Resume choice ------------------------------------------------------
    op.add_column(
        "recruiter_emails", sa.Column("selected_resume_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "recruiter_emails", sa.Column("resume_choice_reason", sa.Text(), nullable=True)
    )
    op.create_index(
        "ix_recruiter_emails_selected_resume_id",
        "recruiter_emails",
        ["selected_resume_id"],
    )

    # The two self/cross-table foreign keys are created as named constraints in a
    # batch block so SQLite — which cannot ALTER a constraint into an existing
    # table — rebuilds rather than failing. Both are SET NULL: deleting a resume
    # or tidying an old message must never cascade into losing a follow-up chain.
    with op.batch_alter_table("recruiter_emails") as batch:
        batch.create_foreign_key(
            "fk_recruiter_emails_previous",
            "recruiter_emails",
            ["previous_recruiter_email_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_recruiter_emails_selected_resume",
            "resumes",
            ["selected_resume_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # ---- New tables ---------------------------------------------------------
    op.create_table(
        "reply_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recruiter_email_id",
            sa.Integer(),
            sa.ForeignKey("recruiter_emails.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("signal", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=True),
        sa.Column("classification_confidence", sa.Float(), nullable=True),
        sa.Column("sender_address", sa.String(length=320), nullable=True),
        sa.Column("sender_domain", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reply_feedback_user_id", "reply_feedback", ["user_id"])
    op.create_index(
        "ix_reply_feedback_recruiter_email_id", "reply_feedback", ["recruiter_email_id"]
    )
    op.create_index(
        "ix_reply_feedback_sender_address", "reply_feedback", ["sender_address"]
    )
    op.create_index(
        "ix_reply_feedback_sender_domain", "reply_feedback", ["sender_domain"]
    )
    op.create_index(
        "ix_reply_feedback_user_created", "reply_feedback", ["user_id", "created_at"]
    )

    op.create_table(
        "classifier_priors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("value", sa.String(length=320), nullable=False),
        sa.Column("approvals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_signal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id", "scope", "value", name="uq_classifier_prior_user_scope_value"
        ),
    )
    op.create_index("ix_classifier_priors_user_id", "classifier_priors", ["user_id"])

    op.create_table(
        "recruiter_scan_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "gmail_account_id",
            sa.Integer(),
            sa.ForeignKey("gmail_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="beat"),
        sa.Column("listed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("examined", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_known", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_own_thread", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_from_self", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_bounce", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_opt_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_unfetchable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deferred", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_recruiter_scan_runs_user_id", "recruiter_scan_runs", ["user_id"])
    op.create_index(
        "ix_recruiter_scan_runs_gmail_account_id",
        "recruiter_scan_runs",
        ["gmail_account_id"],
    )
    op.create_index(
        "ix_recruiter_scan_runs_user_created",
        "recruiter_scan_runs",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_recruiter_scan_runs_user_created", table_name="recruiter_scan_runs")
    op.drop_index(
        "ix_recruiter_scan_runs_gmail_account_id", table_name="recruiter_scan_runs"
    )
    op.drop_index("ix_recruiter_scan_runs_user_id", table_name="recruiter_scan_runs")
    op.drop_table("recruiter_scan_runs")

    op.drop_index("ix_classifier_priors_user_id", table_name="classifier_priors")
    op.drop_table("classifier_priors")

    op.drop_index("ix_reply_feedback_user_created", table_name="reply_feedback")
    op.drop_index("ix_reply_feedback_sender_domain", table_name="reply_feedback")
    op.drop_index("ix_reply_feedback_sender_address", table_name="reply_feedback")
    op.drop_index("ix_reply_feedback_recruiter_email_id", table_name="reply_feedback")
    op.drop_index("ix_reply_feedback_user_id", table_name="reply_feedback")
    op.drop_table("reply_feedback")

    with op.batch_alter_table("recruiter_emails") as batch:
        batch.drop_constraint("fk_recruiter_emails_selected_resume", type_="foreignkey")
        batch.drop_constraint("fk_recruiter_emails_previous", type_="foreignkey")

    op.drop_index(
        "ix_recruiter_emails_selected_resume_id", table_name="recruiter_emails"
    )
    op.drop_column("recruiter_emails", "resume_choice_reason")
    op.drop_column("recruiter_emails", "selected_resume_id")

    op.drop_index(
        "ix_recruiter_emails_previous_recruiter_email_id", table_name="recruiter_emails"
    )
    op.drop_column("recruiter_emails", "previous_recruiter_email_id")
    op.drop_column("recruiter_emails", "last_follow_up_at")
    op.drop_column("recruiter_emails", "follow_up_count")
    op.drop_column("recruiter_emails", "escalation_reason")
    op.drop_column("recruiter_emails", "escalated")

    op.drop_column("recruiter_emails", "confidence_adjustment")

    op.drop_column("emails", "email_references")
    op.drop_column("emails", "in_reply_to")
    op.drop_column("recruiter_emails", "rfc_references")
    op.drop_column("recruiter_emails", "rfc_message_id")
