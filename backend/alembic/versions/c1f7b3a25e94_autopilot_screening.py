"""Record which postings the autopilot passed over, and why

The autopilot judges a posting against the candidate's target roles and
locations, and refuses the ones that don't belong to them. Until now that verdict
went nowhere: the posting stayed ``NEW``, so the next hourly run fetched it,
judged it and refused it again, forever.

In production that produced a run report reading ``scanned: 42, applied: 0,
skipped_irrelevant: 42`` — the same forty-two rows, every hour, for as long as
they existed. The budget was never the constraint and the gates were never wrong;
the run simply had nothing new to look at and no way to say so.

``screened_out_at`` retires a posting from consideration, and
``screened_out_reason`` keeps the gate's own sentence so the feed can answer "why
didn't you apply to that one?" in the words the user would have used.

These are columns rather than a new ``JobStatus`` on purpose. Status is the
*candidate's* pipeline — new, interested, applied — and being passed over by an
automated gate is a fact about our judgement, not a stage they moved the job to.
Keeping them apart is also what makes reopening cheap: clearing the pair is how
changed criteria put a posting back in play, which a status transition could not
express without lying about where the candidate had put it.

Both nullable with no backfill, so every existing posting stays exactly as
eligible as it is today and the first run after this migration is the one that
starts recording.

Revision ID: c1f7b3a25e94
Revises: b3e7d94a1f52
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c1f7b3a25e94"
down_revision: str | None = "b3e7d94a1f52"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column("screened_out_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_postings",
        sa.Column("screened_out_reason", sa.Text(), nullable=True),
    )
    # The autopilot's candidate query filters on this every run; without the
    # index it degrades to a scan of the whole feed as the screened-out set grows.
    op.create_index(
        "ix_job_postings_screened_out_at",
        "job_postings",
        ["screened_out_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_postings_screened_out_at", table_name="job_postings")
    op.drop_column("job_postings", "screened_out_reason")
    op.drop_column("job_postings", "screened_out_at")
