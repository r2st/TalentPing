"""The beat tick that turns database state into things the user is told.

Two tasks, both cheap and both idempotent.

:func:`sweep_notifications` re-derives every condition for every active user.
Idempotence comes from ``dedupe_key`` rather than from bookkeeping here — see
:mod:`app.services.notification_sweep` for why the sweep is stateless by design
— which is what lets this run every fifteen minutes without accumulating
duplicates, and lets a doubled tick (``task_acks_late`` redelivers a killed
worker's message while the original may still be running) be a no-op instead of
a second copy of every notification.

:func:`prune_notifications` is the only thing that deletes them. Without it the
table grows one row per user per event forever, and the failure is not disk —
it is a notification list that opens on last quarter.
"""
from __future__ import annotations

import logging

from app.core.database import SessionLocal
from app.services import notification_sweep, notifications
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.notification_tasks.sweep_notifications")
def sweep_notifications() -> dict:
    """Emit everything every active user is owed. Returns a per-kind tally.

    Outcomes are counted rather than raised, for the same reason the digest
    sweep counts them: a tally is what tells a deployment "nobody was owed
    anything" apart from "every user failed", and those look identical in a log
    that only records exceptions.
    """
    db = SessionLocal()
    try:
        return notification_sweep.sweep_all(db)
    finally:
        db.close()


@celery_app.task(name="app.tasks.notification_tasks.prune_notifications")
def prune_notifications() -> dict:
    """Delete notifications past their retention window."""
    db = SessionLocal()
    try:
        removed = notifications.prune(db)
        if removed:
            logger.info("pruned %s expired notifications", removed)
        return {"deleted": removed, "retention_days": notifications.RETENTION_DAYS}
    finally:
        db.close()
