"""Record when a digest was last *attempted*, not only when it last succeeded

``digest_preferences.last_sent_at`` answered two questions that turn out to be
different, and the beat task only ever got to ask one of them.

The weekly digest is swept hourly on purpose — a worker that is down at 08:00
Monday should not cost anybody their digest — and ``is_due`` keeps that honest
by refusing to send twice inside ``MIN_GAP``. Both guards read ``last_sent_at``,
which a failed send deliberately does not move: the week's digest has not gone
out, and it still should. So a mailbox that cannot send at all — disconnected,
grant revoked, paused for reputation — came back due on every single tick. That
is twenty-four full digest builds (a dozen aggregate queries apiece) and
twenty-four rejected Gmail calls per broken account per day, indefinitely.

``last_attempt_at`` is the missing half: ``last_sent_at`` still means "the last
digest the user actually received", and the retry spacing now measures from the
last *try*. A broken account is retried daily instead of hourly, and still
recovers the moment sending works again.

Nullable with no backfill. A row that has never been attempted under the new
column reads as ``NULL``, which ``is_due`` treats exactly as it treated every
row before this existed — so the upgrade needs no table rewrite and changes
nobody's schedule.

Revision ID: a4f7c209d3e8
Revises: c5a9e63b17d4
Create Date: 2026-08-04
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4f7c209d3e8"
down_revision: str | None = "c5a9e63b17d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "digest_preferences",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("digest_preferences", "last_attempt_at")
