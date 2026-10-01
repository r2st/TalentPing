"""Keeping the dead-letter table a place an operator can read.

:mod:`app.tasks.dead_letter` writes a row every time a task fails for the last
time, and it collapses repeats — but the collapse is per *distinct* failure, so
the row count still grows with the number of different things that have ever
broken. Nothing deletes from it. A year in, the screen that exists to answer
"what is broken now?" opens on a scroll of failures from releases that no longer
exist.

So this prunes, on two clocks, because open and resolved rows are not the same
kind of record:

**Resolved rows** — replayed or ignored — are kept for
``DEAD_LETTER_RETENTION_DAYS``. An administrator has looked at them and decided;
what remains is the audit trail of that decision.

**Open rows** are kept three times as long, and the multiplier is the point.
Nobody has looked at an open row. Deleting it on the resolved clock would
quietly discard the record of a bug that was never triaged — which is the exact
failure mode the table was built to end. Three months without a single
recurrence is the point at which it stops being "now".

Both clocks run on ``last_failed_at`` rather than ``created_at``: a row that has
been collapsing hits for six months is six months old and *current*, and pruning
it by age would delete the most active failure on the deployment.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.dead_letter import STATUS_NEW, DeadLetterJob
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

#: How much longer an untriaged failure is kept than a resolved one. See the
#: module docstring — an open row is a record nobody has read yet.
OPEN_RETENTION_MULTIPLIER = 3


@celery_app.task(name="app.tasks.dead_letter_tasks.prune_dead_letters")
def prune_dead_letters() -> dict:
    """Delete dead letters that have aged out. Returns what it removed.

    The counts are in the return value *and* the log line on purpose: this is
    the only task here that destroys data, and a silent one would leave nothing
    to check the retention against short of counting rows by hand.
    """
    days = max(1, int(settings.dead_letter_retention_days))
    now = datetime.now(UTC)
    resolved_before = now - timedelta(days=days)
    open_before = now - timedelta(days=days * OPEN_RETENTION_MULTIPLIER)

    db = SessionLocal()
    try:
        resolved = db.execute(
            delete(DeadLetterJob).where(
                DeadLetterJob.status != STATUS_NEW,
                DeadLetterJob.last_failed_at < resolved_before,
            )
        ).rowcount
        stale_open = db.execute(
            delete(DeadLetterJob).where(
                DeadLetterJob.status == STATUS_NEW,
                DeadLetterJob.last_failed_at < open_before,
            )
        ).rowcount
        db.commit()

        remaining = db.scalar(select(func.count()).select_from(DeadLetterJob)) or 0
    finally:
        db.close()

    if resolved or stale_open:
        logger.info(
            "pruned dead letters: %s resolved older than %sd, %s open older than "
            "%sd, %s remaining",
            resolved, days, stale_open, days * OPEN_RETENTION_MULTIPLIER, remaining,
        )
    return {
        "resolved_deleted": int(resolved or 0),
        "open_deleted": int(stale_open or 0),
        "remaining": int(remaining),
        "retention_days": days,
    }


__all__ = ["OPEN_RETENTION_MULTIPLIER", "prune_dead_letters"]
