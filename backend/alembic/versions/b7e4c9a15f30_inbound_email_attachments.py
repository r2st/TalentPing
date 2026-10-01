"""Inbound email attachments

Adds ``emails.inbound_attachments``: what a message we *received* arrived
carrying, as ``[{filename, mime_type, attachment_id, size}]``.

``emails.attachment_filename`` only ever described outbound mail — the sender
writes it with the resume that travelled — so a recruiter's job spec or
contract had nowhere to live and the inbox could neither list nor open it.

Only the description is stored. The bytes stay in Gmail and are redeemed by
``attachment_id`` when the user opens the file, so a mailbox full of large
PDFs costs nothing here.

Backfills to ``[]`` rather than NULL: "we have not looked" and "there was
nothing attached" read identically to the API, and every existing row is the
second one as far as anything can tell.

Revision ID: b7e4c9a15f30
Revises: f8d1c4b62a05
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b7e4c9a15f30"
down_revision: str | None = "f8d1c4b62a05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "emails",
        sa.Column(
            "inbound_attachments",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )
    # The same description on the recruiter row, because the scan is the only
    # place the raw Gmail message is in hand and the conversation's ``emails``
    # row is built from this one long afterwards.
    op.add_column(
        "recruiter_emails",
        sa.Column(
            "attachments",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("recruiter_emails", "attachments")
    op.drop_column("emails", "inbound_attachments")
