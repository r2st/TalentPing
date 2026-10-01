"""Celery tasks for job-board monitoring.

The beat task sweeps for saved searches that are due (each search sets its own
``interval_hours``) and fans one task out per search. Scans are network-bound and
hit several third-party APIs, so they belong on a worker and not in a request.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.job import JobSearch
from app.models.user import User
from app.services import fit_refresh
from app.services.job_search_service import run_search
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _is_due(search: JobSearch, now: datetime) -> bool:
    """A search is due when it has never run, or its interval has elapsed."""
    if search.last_run_at is None:
        return True
    last = search.last_run_at
    if last.tzinfo is None:  # SQLite round-trips naive datetimes
        last = last.replace(tzinfo=UTC)
    return now - last >= timedelta(hours=max(1, search.interval_hours))


@celery_app.task(name="app.tasks.job_tasks.scan_due_searches")
def scan_due_searches() -> dict:
    """Beat entrypoint: enqueue a scan per active saved search that is due.

    A publish failure is counted, not raised — the same rule every other beat
    fan-out in this package follows, and for the same reason. Celery is
    configured here to fail fast on an unreachable broker, so ``.delay`` raises
    on a blip; raised out of this loop it ended the sweep, and every search
    behind the one that failed was never enqueued. The order is the fixed one
    the query returns, so it is the same tail of searches every time.

    ``enqueued`` counts tasks that actually went out, not rows that were due.
    Those were the same number until a publish could fail silently, and the
    difference is the whole signal a deployment needs to tell "nothing was due"
    apart from "the broker was down".
    """
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        searches = db.scalars(
            select(JobSearch).where(JobSearch.is_active.is_(True))
        ).all()
        due = [s for s in searches if _is_due(s, now)]
        enqueued = 0
        dispatch_error: str | None = None
        for search in due:
            if dispatch_error is not None:
                # The broker already refused a publish this sweep; asking again
                # per search costs a connect timeout apiece for an answer we
                # have. Nothing is stamped here, so the next tick retries them
                # all — `_is_due` reads `last_run_at`, which only a finished
                # scan writes.
                continue
            try:
                scan_job_search.delay(search.id)
            except Exception as exc:  # noqa: BLE001 - broker down; the next tick retries
                logger.warning("job search %s not dispatched: %s", search.id, exc)
                dispatch_error = str(exc)[:200]
                continue
            enqueued += 1
        return {
            "active": len(searches),
            "due": len(due),
            "enqueued": enqueued,
            "dispatch_error": dispatch_error,
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.job_tasks.scan_job_search")
def scan_job_search(search_id: int) -> dict:
    """Run one saved search: discover, filter, score, store.

    A failure is recorded on the row rather than only raised, because
    ``last_run_at`` is what ``_is_due`` reads and ``run_search`` only stamps it
    on the paths it finishes. Anything raising past the provider call — a board
    crawl, the scorer, a save — left the column at its old value, so the beat
    found the search due again on its very next tick and re-ran the scan that
    had just failed. A search that fails deterministically therefore ran every
    tick forever instead of every ``interval_hours``, hammering the third-party
    job boards on the way to the same exception, and its ``last_error`` never
    said so.
    """
    db = SessionLocal()
    try:
        search = db.get(JobSearch, search_id)
        if search is None:
            return {"search_id": search_id, "status": "missing"}
        try:
            result = run_search(db, search)
        except Exception as exc:  # noqa: BLE001 - the interval must still apply
            logger.exception("job search %s failed", search_id)
            db.rollback()
            search = db.get(JobSearch, search_id)
            if search is not None:
                search.last_error = str(exc)[:500]
                search.last_run_at = datetime.now(UTC)
                db.commit()
            return {
                "search_id": search_id,
                "status": "failed",
                "detail": str(exc)[:200],
            }
        return {
            "search_id": search_id,
            "scanned": result.scanned,
            "added": result.added,
            "below_threshold": result.below_threshold,
            "duplicates": result.duplicates,
            "detail": result.detail,
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.job_tasks.rescore_user_feed")
def rescore_user_feed(user_id: int) -> dict:
    """Re-score one user's live feed after they changed what they are looking for.

    Enqueued from the profile and preferences endpoints rather than run on the
    request thread: the work is a heuristic parse plus a score per posting, which
    is cheap per row and unbounded in rows. A candidate with a few hundred saved
    postings would have paid for all of them inside a PATCH that, from the
    screen, was a checkbox.

    A missing user is not an error. The row can be deleted between the edit and
    the worker picking this up, and an erased account whose feed we then rescored
    would be the worse outcome of the two.
    """
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return {"user_id": user_id, "status": "missing"}
        return {"user_id": user_id, **fit_refresh.rescore_user(db, user)}
    finally:
        db.close()


def dispatch_rescore(db, user) -> bool:
    """Queue a rescore for *user*, or run it here when there is no broker.

    Returns True when a worker took it. The same shape as every other "run now"
    in this codebase (``form_apply._dispatch``, ``autopilot._dispatch_cycle``)
    and for the same reason: a single-process deployment and the test suite have
    no worker, and a feature that silently does nothing there is a feature that
    is never exercised until production.

    Best effort in both directions. A rescore that fails must not fail the edit
    that asked for it — the user's profile change is saved either way, and the
    next scan re-scores what it touches regardless.
    """
    if settings.celery_enabled:
        try:
            rescore_user_feed.delay(user.id)
            return True
        except Exception:  # noqa: BLE001 - broker down; do it here instead
            logger.warning("rescore dispatch failed, running inline", exc_info=True)
    try:
        fit_refresh.rescore_user(db, user)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("inline rescore failed for user %s", user.id)
    return False
