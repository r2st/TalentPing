"""Record when each Gmail refresh token was issued

A Google OAuth client whose consent screen is still in "Testing" hands out
refresh tokens that Google expires seven days after issue. The mailbox then
stops sending and stops fetching replies, and the product goes quiet in a way
that reads as "no recruiter wrote back". Production has been through this
repeatedly.

The app could already say so afterwards: ``gmail_accounts.mark_revoked`` flips
the row the first time a call comes back ``invalid_grant``, and every page
carries the banner. What it could not do is say so *beforehand*, because
nothing on the row recorded how old the live grant was.

``created_at`` is not that number. A re-consent keeps the existing row — the
callback matches on ``google_sub`` — so ``created_at`` is when the mailbox was
first connected, and on a weekly-expiry deployment the two diverge immediately.

Nullable, and not backfilled. Every existing row's grant was issued at a time
this schema never recorded, and inventing one would produce exactly the
confident wrong answer the column exists to avoid: a mailbox reported as fresh
on the morning it stops working. Null reads as "we cannot say", the warning
stays silent, and the reactive banner still fires the moment Google refuses.
The first reconnection stamps it for real.

Revision ID: e7c31a5d9b40
Revises: d4b8e1f70c93
Create Date: 2026-08-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7c31a5d9b40"
down_revision: str | None = "d4b8e1f70c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gmail_accounts",
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("gmail_accounts", "granted_at")
