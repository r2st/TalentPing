"""Record the resume sent with an outbound email

Adds ``emails.attachment_filename``, written by the sender with the name of the
resume PDF that actually travelled with the message. Null on a SENT row means
the message went out with no attachment — until now every message did, and the
schema had no way to say so.

Left null for existing rows on purpose: backfilling a filename would claim those
historical sends carried a resume, which they did not.

Purely additive; the downgrade is a clean reversal.

Revision ID: a4c9f2e18d63
Revises: e2b8f4a19c63
Create Date: 2026-07-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4c9f2e18d63"
down_revision: str | None = "e2b8f4a19c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "emails", sa.Column("attachment_filename", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("emails", "attachment_filename")
