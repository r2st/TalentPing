"""Proof of submission: the employer's acknowledgement, kept verbatim

A submitted form application already carried screenshots. What it could not
carry was the *sentence* — "Thank you for applying, we have received your
application" — because :func:`ats_adapters.confirmed` only ever answered yes or
no and the yes went into a note that read the same for everyone.

``form_applications.confirmation`` gives those words somewhere to live. It is
nullable and stays null on every existing row: the runs that submitted before
this deploy were confirmed at the time, but the text was never kept and
inventing one would be the one thing a receipt must never do. Their receipts
report ``confirmed: false`` and lean on the screenshots, which they do have.

Revision ID: a4f7d2e91b63
Revises: e3b8c1a75d24
Create Date: 2026-08-01
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a4f7d2e91b63"
down_revision: str | None = "e3b8c1a75d24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "form_applications", sa.Column("confirmation", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("form_applications", "confirmation")
