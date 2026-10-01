"""Record when a send was last handed to the broker

Adds ``emails.send_dispatched_at``, the stamp ``sweep_stranded_sends`` needs to
tell "nobody is going to send this" from "somebody already is".

The sweep's age bound was ``Email.created_at < now - MAX_SEND_DELAY_SECONDS``,
and its stated justification was that past that horizon no live task can be
aiming at the row. That is true of a *fresh* send — ``enqueue_campaign_sends``
publishes it with an ETA clamped to that same horizon measured from the moment
the row is written — and it is false of a re-dispatched one, because the sweep
itself publishes an ETA up to seven days from *now* while ``created_at`` stays
where it was. ``created_at`` never moves, so the bound it feeds can never
re-close once it has opened.

That turns any row the send path declines to retire into a permanent hourly
publisher. The reputation gate is the ordinary way to get one: it leaves the
message QUEUED on purpose so a temporary hold delays rather than drops it. On
production a warm-up ramp reading the wrong start date held 54 approved messages
that way, the hourly sweep re-dispatched every one of them every hour, and two
days later the worker was holding 2,502 unacked copies of 53 emails. Nothing
double-sent — ``_claim_for_send`` takes the row ``FOR UPDATE SKIP LOCKED`` and
re-checks the status — so the only visible symptom was a worker slowly filling
with tasks that could not do anything, which reads as the broker storm the
visibility timeout was raised to end and is a different bug entirely.

Nullable with no backfill. NULL means "never dispatched, or last dispatched
before this column existed", and the sweep reads that as "no live task known",
which is exactly the pre-migration behaviour for every existing row — the fix
takes effect on the first dispatch after deploy rather than retroactively
suppressing a sweep that might be the only thing keeping a genuinely stranded
message alive.

Deliberately not ``updated_at``: that column moves for any write to the row —
a tracking pixel, an attachment resolution, a review edit — and a bound built on
it would silence the sweep for a row nothing ever dispatched.

Revision ID: c5f2a9d84e17
Revises: b7d3e8f4c210
Create Date: 2026-08-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5f2a9d84e17"
down_revision: str | None = "b7d3e8f4c210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "emails",
        sa.Column("send_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("emails", "send_dispatched_at")
