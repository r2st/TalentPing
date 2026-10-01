"""Drafts: remember that a person took one in hand

Adds ``emails.user_owned_at``, stamped when the user asks for a reply to be
written (``POST /recruiter-inbox/{id}/generate-reply``) or edits a draft
(``PATCH /tracker/emails/{id}``).

Two sweeps walk old drafts and re-decide them. ``retry_degraded`` rewrites the
body from a corrected classification and, on a confident re-read, routes the
result ``AUTO``; ``reevaluate_pending_replies`` queues one that would have sent
itself. Neither could tell a draft the pipeline is still deciding about from one
a human had already decided about, so the reply the candidate pressed "write me
one" for — and the paragraph they rewrote in their own words — could both be
replaced by freshly generated text and sent unread in their name.

The counterpart to ``recruiter_emails.draft_discarded_at``: that records the
user refusing a draft, this records them keeping one. Both exist because a
decision about *this message* has to outlive whatever a later classification
thinks of it.

Not backfilled. Nothing on file distinguishes an edited draft from an untouched
one — that is the gap this column closes — and a wrong guess in either direction
is worse than a null, which every sweep already reads as "the pipeline's".

Purely additive; the downgrade is a clean reversal.

Revision ID: c8a4f2b7d051
Revises: b3d7e15c9a24
Create Date: 2026-08-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8a4f2b7d051"
down_revision: str | None = "b3d7e15c9a24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "emails",
        sa.Column("user_owned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("emails", "user_owned_at")
