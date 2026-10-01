"""Keep the uploaded resume, and let the user change what an email carries

Two changes, one subject: which files reach a recruiter.

``resumes.file_bytes``/``file_content_type``/``file_size`` keep the document the
candidate actually uploaded. Until now the bytes were parsed and dropped, and
every outbound "resume" was a PDF re-rendered from the extracted fields under a
filename derived from the candidate's name — the same filename for every resume
one person owns. That is what "the wrong CV was attached" looked like from the
receiving end. Existing rows stay null and keep getting the re-render, which is
the only thing that can be produced for them; re-uploading is what fills the
column.

``emails.attachment_resume_id`` and ``emails.suppressed_attachments`` give the
user the final say: pin a specific document, or take a resolved one off. Both
default to "no opinion", which is what every existing row means.

``email_attachments`` holds files the user attaches by hand. Nothing else can
produce them, so the bytes are stored rather than resolved, and they persist
past the send so a sent message can still show what it carried.

Revision ID: c1d5e8a02f47
Revises: b7e4c9a15f30
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c1d5e8a02f47"
down_revision: str | None = "b7e4c9a15f30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("file_bytes", sa.LargeBinary(), nullable=True))
    op.add_column(
        "resumes", sa.Column("file_content_type", sa.String(length=120), nullable=True)
    )
    op.add_column("resumes", sa.Column("file_size", sa.Integer(), nullable=True))

    op.add_column(
        "emails", sa.Column("attachment_resume_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_emails_attachment_resume_id", "emails", ["attachment_resume_id"]
    )
    # SQLite can't ALTER a table to add a constraint; production is Postgres,
    # where the real FK is created. (Tests build the schema with create_all, so
    # they get it from the models regardless.)
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_emails_attachment_resume_id",
            "emails",
            "resumes",
            ["attachment_resume_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.add_column(
        "emails",
        sa.Column(
            "suppressed_attachments",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )

    op.create_table(
        "email_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "email_id",
            sa.Integer(),
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
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
    )
    op.create_index("ix_email_attachments_email_id", "email_attachments", ["email_id"])


def downgrade() -> None:
    op.drop_index("ix_email_attachments_email_id", table_name="email_attachments")
    op.drop_table("email_attachments")

    op.drop_column("emails", "suppressed_attachments")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint(
            "fk_emails_attachment_resume_id", "emails", type_="foreignkey"
        )
    op.drop_index("ix_emails_attachment_resume_id", table_name="emails")
    op.drop_column("emails", "attachment_resume_id")

    op.drop_column("resumes", "file_size")
    op.drop_column("resumes", "file_content_type")
    op.drop_column("resumes", "file_bytes")
