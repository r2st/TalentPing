"""Celery tasks for the follow-up sequence.

The beat task sweeps for follow-ups that have come due and turns each into a
queued email. The actual delivery is handed to the existing throttled sender, so
follow-ups obey the same per-mailbox daily limit and randomized spacing as the
initial outreach — a nudge that bursts is worse than no nudge.

*And the same slot rule.* The spacing is the input to
``email_tasks.send_countdown`` rather than the countdown itself, exactly as it is
for a campaign's sends and for the stranded-send sweep. Handed to the broker raw
— which is what this did — the accumulated spacing quietly undid the one thing
:mod:`app.services.send_time` exists to get right. ``schedule_for_application``
resolves the recruiter's timezone and puts every step in their local 09:00-11:00
weekday morning; the sweep then added 90-600s *per due row* on top of that, and
the window is only two hours wide. Twenty due follow-ups pushed the last of them
two hours past it, and a full sweep of 200 pushed the tail most of a day — into
the recipient's evening and, for anyone the sweep reached late in their morning,
into their night. The rows the scheduler had been most careful about (Sydney,
Bangalore, anywhere far from the beat's own clock) were the ones it hurt most.
"""
from __future__ import annotations

import logging
import random
from datetime import UTC, datetime

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.email import EmailStatus
from app.models.follow_up import FollowUpStatus
from app.services.follow_up_service import due_follow_ups, prepare_follow_up_email
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.follow_up_tasks.process_due_follow_ups")
def process_due_follow_ups() -> dict:
    """Beat entrypoint: action every follow-up whose scheduled time has passed.

    Each one becomes a queued email, is cancelled with a reason — a recruiter who
    replied, opted out, or an application that closed — or is *deferred*, which
    is the one outcome that leaves the row SCHEDULED for a later sweep to look at
    again. Only a paused campaign defers, and only until it is resumed.
    """
    db = SessionLocal()
    try:
        # Claimed, not merely read — see `due_follow_ups`. Two sweeps overlapping
        # (a backed-up beat queue, or `task_acks_late` replaying a killed worker)
        # both actioned the same due rows and mailed the recruiter twice.
        due = due_follow_ups(db, for_update=True)
        queued = 0
        drafted = 0
        cancelled = 0
        deferred = 0
        failed = 0
        dispatch_error: str | None = None
        cumulative = 0
        # One clock for the whole sweep, so the spacing below accumulates against
        # the moment the sweep started rather than drifting with each row's own
        # composition time.
        now = datetime.now(UTC)

        for follow_up in due:
            try:
                # A savepoint per row, so that "one bad row must not stall the
                # sweep" is true of the rows that fail *partway*, which is most
                # of the ways this can fail. `prepare_follow_up_email` adds the
                # email, flushes it for its id, bumps the thread's message count
                # and records a pipeline change — anything raising after that
                # leaves the session holding a half-built message that the
                # commit below would write out, attached to a follow-up this
                # loop is about to mark FAILED.
                #
                # A database error is worse still: without a rollback the
                # session is unusable, so every remaining follow-up raises on
                # contact and the final commit takes the whole sweep down with
                # it. The savepoint is what keeps the outer transaction alive.
                with db.begin_nested():
                    email, skip_reason = prepare_follow_up_email(db, follow_up)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stall the sweep
                logger.exception("follow-up %s failed to prepare", follow_up.id)
                failed += 1
                # Reachable because the savepoint rolled back cleanly. The
                # attributes were expired by that rollback, so this writes onto
                # the row's committed state rather than onto half an update.
                follow_up.status = FollowUpStatus.FAILED
                follow_up.note = str(exc)[:500]
                continue

            if email is None:
                if follow_up.status is FollowUpStatus.SCHEDULED:
                    # Not actioned and not retired: a paused campaign. The row
                    # keeps its slot and this sweep leaves it alone, so counting
                    # it as cancelled would report a sequence being retired when
                    # it is being held.
                    deferred += 1
                    continue
                cancelled += 1
                logger.info("follow-up %s cancelled: %s", follow_up.id, skip_reason)
                continue

            if email.status != EmailStatus.QUEUED:
                # Parked as a draft for review by send policy. It is a real
                # follow-up and it is not a send, so it is counted as neither.
                drafted += 1
                continue

            queued += 1
            if dispatch_error is not None:
                # The broker already refused a publish this sweep; don't ask it
                # again for every remaining row.
                continue

            # Spread the sweep's sends out the same way a campaign's are, and
            # land them where a campaign's land: the recipient's business
            # morning. See the module docstring for what the raw spacing cost.
            cumulative += random.randint(
                settings.min_send_interval_seconds, settings.max_send_interval_seconds
            )
            try:
                from app.tasks.email_tasks import send_countdown, send_outreach_email

                countdown = send_countdown(db, email, cumulative, now=now)
                send_outreach_email.apply_async(args=[email.id], countdown=countdown)
            except Exception as exc:  # noqa: BLE001 - broker down
                logger.warning("follow-up dispatch failed: %s", exc)
                dispatch_error = str(exc)[:200]

        db.commit()
        return {
            "due": len(due),
            "queued": queued,
            "drafted": drafted,
            "cancelled": cancelled,
            "deferred": deferred,
            "failed": failed,
            "dispatch_error": dispatch_error,
            "at": datetime.now(UTC).isoformat(),
        }
    finally:
        db.close()
