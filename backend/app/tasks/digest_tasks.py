"""Celery task for the weekly digest.

The beat tick runs hourly rather than weekly, and every user's row decides for
itself whether it is due. A weekly beat would mean a worker that happened to be
down at 08:00 Monday cost every user their digest for that week, and there is no
catch-up in Celery beat for a missed interval — the schedule simply moves on.
An hourly sweep costs one cheap query per hour and cannot miss.

:func:`app.services.digest_service.is_due` decides "have we already done this",
which is what stops an hourly tick sending twenty-four digests on Monday, and
:func:`app.services.digest_service._claim` is what makes that decision hold when
two sweeps overlap — ``task_acks_late`` redelivers a killed worker's message
while the original may still be running, and this entry carries no ``expires``,
so a backed-up queue runs the ticks it accumulated back to back. Read rather
than claimed, both sweeps saw the same un-sent row and mailed the same user.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.digest import DigestPreference
from app.models.user import User
from app.services import digest_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.digest_tasks.send_weekly_digests")
def send_weekly_digests() -> dict:
    """Beat entrypoint: send the digest to every user whose turn it is.

    Only users who already have a preference row are considered. That row is
    created on the first read of ``/digest`` and by the digest settings step of
    onboarding — so the sweep never mails someone who has not yet finished
    setting the product up, without needing a second flag to say so.

    Outcomes are tallied by status rather than raised: one unreachable mailbox
    must not stop the other ninety-nine digests, and the tally is what a
    deployment reads to tell "nobody was due" apart from "everybody failed".
    """
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        user_ids = list(
            db.scalars(
                select(DigestPreference.user_id).where(
                    DigestPreference.enabled.is_(True)
                )
            )
        )
        tally: dict[str, int] = {}
        for user_id in user_ids:
            user = db.get(User, user_id)
            if user is None or not user.is_active:
                continue
            try:
                result = digest_service.send(db, user, now=now)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stall the sweep
                logger.exception("digest failed for user %s", user_id)
                db.rollback()
                result = {"status": "failed", "error": str(exc)[:200]}
            tally[result["status"]] = tally.get(result["status"], 0) + 1

        return {"considered": len(user_ids), "outcomes": tally, "at": now.isoformat()}
    finally:
        db.close()


@celery_app.task(name="app.tasks.digest_tasks.send_digest_for_user")
def send_digest_for_user(user_id: int) -> dict:
    """Send one user's digest now — the "email it to me" button's worker path."""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return {"status": "unknown_user", "user_id": user_id}
        return digest_service.send(db, user, force=True, skip_quiet=False)
    finally:
        db.close()
