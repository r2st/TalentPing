"""Celery tasks for the auto-apply autopilot.

The beat task sweeps for users who have switched autopilot on and fans one task
out per user. Each per-user run is network- and LLM-bound (discovery, tailoring,
career-page crawling), so it belongs on a worker, one user at a time.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.autopilot import AutopilotPreference
from app.models.user import User
from app.services.auto_apply_service import run_user_autopilot
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.auto_apply_tasks.run_all_autopilots")
def run_all_autopilots() -> dict:
    """Beat entrypoint: enqueue an autopilot run per active user.

    A publish failure is counted, not raised. Celery here is configured to fail
    fast on an unreachable broker — one connection retry, no publish retry, a
    two-second socket timeout — precisely so callers are not blocked, and the
    cost of that is that ``.delay`` raises on a blip rather than riding it out.
    Raised out of this loop it took the whole sweep with it, so every user
    *after* the one that failed was never enqueued at all.

    That is worse than it sounds, because the order is the fixed one the
    ``user_id`` select returns: the same tail of users is dropped every time,
    and the sweep runs hourly, so a broker that is flaky for a few seconds each
    hour can starve the newest accounts on the deployment of their autopilot
    indefinitely while the oldest ones run every tick. Nothing recorded it
    either — the task result said nothing, and "enqueued" counted the rows the
    query returned rather than the tasks that actually went out.

    Once the broker has refused one publish, the rest are skipped rather than
    attempted: each costs a connect timeout apiece for an answer we already
    have. The next tick re-enqueues everyone, because nothing here is stamped.
    """
    db = SessionLocal()
    try:
        user_ids = db.scalars(
            select(AutopilotPreference.user_id).where(
                AutopilotPreference.is_active.is_(True)
            )
        ).all()
        enqueued = 0
        dispatch_error: str | None = None
        for user_id in user_ids:
            if dispatch_error is not None:
                continue
            try:
                run_autopilot_for_user.delay(user_id)
            except Exception as exc:  # noqa: BLE001 - broker down; the next tick retries
                logger.warning("autopilot for user %s not dispatched: %s", user_id, exc)
                dispatch_error = str(exc)[:200]
                continue
            enqueued += 1
        return {
            "active": len(user_ids),
            "enqueued": enqueued,
            # Named rather than only logged, so a sweep that covered part of its
            # list never reads as one that covered all of it.
            "dispatch_error": dispatch_error,
            "at": datetime.now(UTC).isoformat(),
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.auto_apply_tasks.run_autopilot_for_user")
def run_autopilot_for_user(user_id: int) -> dict:
    """Run one full autopilot cycle for a single user."""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return {"user_id": user_id, "status": "missing"}
        result = run_user_autopilot(db, user)
        return {"user_id": user_id, **result.as_dict()}
    except Exception as exc:  # noqa: BLE001 - record the failure on the pref row
        logger.exception("autopilot run failed for user %s", user_id)
        db.rollback()
        pref = db.scalar(
            select(AutopilotPreference).where(AutopilotPreference.user_id == user_id)
        )
        if pref is not None:
            pref.last_error = str(exc)[:500]
            pref.last_run_at = datetime.now(UTC)
            db.commit()
        return {"user_id": user_id, "status": "failed", "reason": str(exc)[:200]}
    finally:
        db.close()
