"""Bank a mailbox's reputation holds in their own column

Adds ``gmail_accounts.warmup_hold_seconds``: the total time this mailbox has
spent under a bounce or complaint hold. The warm-up ramp now reads
``warmup_started_at + warmup_hold_seconds`` (``reputation_service.
effective_start``) rather than ``warmup_started_at`` alone.

The penalty was previously applied by pushing ``warmup_started_at`` forward,
which put two meanings in one column with two writers moving it in opposite
directions:

* ``_hold_until`` pushed it *later*, so days spent forbidden to send would not
  count as days of earned trust.
* ``adopt_send_history`` pulls it *earlier*, back to the first send this address
  really made, so a rebuilt row does not read as a brand-new mailbox.

The second runs on a schedule. ``reconcile_warmup_ramps`` sweeps every mailbox
every six hours, so a three-day complaint hold kept its ramp penalty for at most
six hours and then silently lost it — the mailbox served the pause and still
emerged promoted for the days it spent held, which is the exact outcome the
push existed to prevent.

Backfilled to 0, which is the honest value: nothing has been recorded until the
next hold fires. Rows that currently carry a pushed-forward
``warmup_started_at`` keep it until the next reconcile pulls it back to real
history — the same thing that would have happened without this migration, and
from then on the two columns mean what they say.

``server_default`` stays on the column: rows are created by fixtures and by
hand, and a NOT NULL column whose default lives only in the ORM is a column that
fails an INSERT nobody wrote in Python.

Revision ID: d1e5b8c3a927
Revises: c9f4a1e70d38
Create Date: 2026-08-08
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1e5b8c3a927"
down_revision: str | None = "c9f4a1e70d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gmail_accounts",
        sa.Column(
            "warmup_hold_seconds",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("gmail_accounts", "warmup_hold_seconds")
