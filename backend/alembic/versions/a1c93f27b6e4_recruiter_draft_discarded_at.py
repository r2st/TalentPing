"""Recruiter inbox: remember that the user discarded a draft

Adds ``recruiter_emails.draft_discarded_at``, set when the user throws away a
reply the product wrote for them.

Discarding left the row at ``FLAGGED`` with no draft attached, which is exactly
the shape ``retry_degraded_classifications`` sweeps up and re-answers — so a
reply the candidate had personally refused could be rewritten, and on a good
re-read routed to ``AUTO`` and sent unread in their name. The refusal was
recorded only in ``reply_feedback``, as a confidence nudge, and that table sits
behind a feature flag.

Backfilled from ``reply_feedback``: a ``review_dismiss`` row is exactly this
event, and production already holds them. ``created_at`` is when the discard
happened, so it is the right timestamp rather than an approximation.

Purely additive; the downgrade is a clean reversal.

Revision ID: a1c93f27b6e4
Revises: d5f1a9c37e28
Create Date: 2026-08-25
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c93f27b6e4"
down_revision: str | None = "d5f1a9c37e28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recruiter_emails",
        sa.Column("draft_discarded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The earliest discard, so a row discarded twice keeps the moment the user
    # first said no.
    op.execute(
        """
        UPDATE recruiter_emails
           SET draft_discarded_at = (
               SELECT MIN(rf.created_at)
                 FROM reply_feedback rf
                WHERE rf.recruiter_email_id = recruiter_emails.id
                  AND rf.source = 'review_dismiss'
           )
         WHERE EXISTS (
               SELECT 1
                 FROM reply_feedback rf
                WHERE rf.recruiter_email_id = recruiter_emails.id
                  AND rf.source = 'review_dismiss'
           )
        """
    )


def downgrade() -> None:
    op.drop_column("recruiter_emails", "draft_discarded_at")
