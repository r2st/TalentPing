"""Catching up on recruiter mail that arrived while nothing was reading it.

**The hole this fills.** ``inbound_scanner.build_query`` bounds every scan with
``newer_than:{RECRUITER_SCAN_WINDOW_DAYS}d`` — a week by default. That is the
right bound for a detector running every five minutes, and it has a consequence
nothing else in the pipeline compensates for: an outage longer than the window
does not *delay* the mail it covers, it makes it **permanently invisible**. The
next healthy scan looks back seven days, the messages are older than seven days,
and no later scan will ever look further.

Production proved it in August 2026. Gmail's OAuth grants died at the seven-day
Testing-mode ceiling, and separately the model chain answered 404 for a retired
model id; between them the pipeline went ten days without detecting a message.
Both were fixed, both mailboxes came back — and the ten days of recruiter mail in
between stayed unread, because the only thing that could have found it was a
query that had already moved past it.

**What runs here.**

``catch_up_all_backlogs``
    The beat entrypoint. Fans out per watched mailbox and does no work itself.

``catch_up_mailbox``
    One widened scan of one mailbox: the same scanner, the same filters, the
    same dedup, over a month instead of a week. Writes ``DETECTED`` rows and
    hands a *bounded* batch of them to the ordinary processing task.

``drain_detected``
    The other half, and the one that makes a large backlog safe: everything
    still sitting at ``DETECTED`` after the batch cap, oldest first. This is
    also what recovers rows stranded by the outage itself — a scan that stored a
    row and then failed to enqueue its processing task leaves exactly this
    shape, and nothing used to pick it back up.

**Why the two halves are paced differently.** Detection is cheap and idempotent:
one ``messages.list`` over a wider range, and bodies fetched only for ids that
have never been seen — after the first pass, none. Processing is neither: it
costs a model call apiece and can end in a reply going out. So the scan half gets a
budget five times the live one (``RECRUITER_BACKLOG_SCAN_MAX_PER_RUN``, with the
remainder reported as ``deferred`` for the next run), and the processing half is
rate limited by three things at once:

* ``RECRUITER_BACKLOG_BATCH_SIZE`` rows per user per run, with the remainder left
  ``DETECTED`` for the next run. A thousand-message backlog drains over days.
* ``RECRUITER_BACKLOG_SPACING_SECONDS`` between two of them, so a batch does not
  fire its model calls simultaneously and exhaust the free tiers that new mail
  also needs.
* :data:`~app.services.recruiter_reply_service.RETRY_AUTO_MAX_AGE_DAYS`, which
  holds an otherwise-auto reply for review once the recruiter's message is more
  than a week old. This is the one that actually answers "will it blast out
  hundreds of replies?": a backlog is old by definition, so nearly all of it
  drafts rather than sends, and what does send is still behind the reputation
  gate, the warm-up ramp and ``RECRUITER_AUTO_REPLY_DAILY_LIMIT``.

**Nothing is answered twice.** Three independent guarantees, none of them new:
``uq_recruiter_email_user_message`` refuses a second row for a message already on
file, :func:`recruiter_reply_service.process` claims its row with a conditional
``UPDATE`` and returns ``skipped`` for anything that has moved past ``DETECTED``,
and the scanner skips a known id before it is even fetched. A catch-up is
therefore safe to run as often as anyone likes; the debounce below exists to save
quota, not to protect correctness.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.gmail_account import GmailAccount
from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailStatus,
    RecruiterReplyPreference,
)
from app.models.recruiter_scan_run import TRIGGER_BACKLOG, RecruiterScanRun
from app.models.user import User
from app.services import (
    gmail_accounts,
    gmail_service,
    recruiter_reply_service,
    reputation_service,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

#: How settled a ``DETECTED`` row must be before the drain will claim it.
#:
#: The live path writes a row and enqueues its processing task moments later, and
#: the two are not in one transaction. A drain with no floor would pick up mail
#: that is about to be processed anyway and pay for a second dispatch. Nothing
#: breaks when it does — ``process`` claims the row in the database, so the loser
#: returns ``skipped`` — but a fifteen-minute floor makes the overlap rare
#: instead of routine, and fifteen minutes is far longer than the live path's
#: worst dispatch lag (``scan_mailbox`` staggers at ten seconds a message, capped
#: at forty a run).
DRAIN_MIN_AGE_SECONDS = 900


def backlog_window_days() -> int:
    """How far back a catch-up scan reads, never narrower than an ordinary one.

    The clamp is not decoration. Setting ``RECRUITER_BACKLOG_WINDOW_DAYS`` below
    ``RECRUITER_SCAN_WINDOW_DAYS`` would make the catch-up read *less* than the
    scan it is backstopping — a sweep that runs, reports success, and covers
    strictly less ground than the thing it exists to cover for.
    """
    return max(
        settings.recruiter_backlog_window_days, settings.recruiter_scan_window_days
    )


def last_backlog_scan(db, account: GmailAccount) -> datetime | None:
    """When this mailbox was last read by a catch-up, or ``None``.

    Read from ``recruiter_scan_runs`` rather than from ``GmailAccount``, and that
    is the whole reason this function exists rather than a column comparison:
    ``last_scan_at`` is bumped by the five-minute live scan, so a debounce keyed
    on it would find the mailbox "too recently scanned" every single time and the
    catch-up would never run at all. The scan-run row records *which caller* read
    the mailbox, which is exactly the question being asked.
    """
    run = db.scalars(
        select(RecruiterScanRun)
        .where(
            RecruiterScanRun.gmail_account_id == account.id,
            RecruiterScanRun.trigger == TRIGGER_BACKLOG,
        )
        .order_by(RecruiterScanRun.created_at.desc())
        .limit(1)
    ).first()
    if run is None or run.created_at is None:
        return None
    return run.created_at if run.created_at.tzinfo else run.created_at.replace(tzinfo=UTC)


def recently_caught_up(
    db, account: GmailAccount, *, now: datetime | None = None
) -> bool:
    """True when a catch-up read this mailbox too recently to read it again."""
    gap = settings.recruiter_backlog_interval_seconds
    if gap <= 0:
        return False
    last = last_backlog_scan(db, account)
    if last is None:
        return False
    return (now or datetime.now(UTC)) - last < timedelta(seconds=gap)


# --------------------------------------------------------------------------- #
# The beat entrypoint                                                          #
# --------------------------------------------------------------------------- #


@celery_app.task
def catch_up_all_backlogs(user_id: int | None = None, force: bool = False) -> dict:
    """Enqueue a catch-up per watched mailbox, and a drain per watched user.

    Guarded per user *and* per mailbox, for the reason
    ``scan_all_recruiter_inboxes`` learned the hard way: an unguarded fan-out
    ends at the first raise, the rows come back in the same order every tick, and
    so the same tail is starved forever while the sweep reports success.

    Every skip is counted and named. "Why did the catch-up find nothing?" should
    be answerable from the task result alone — the alternative is reading worker
    logs during the incident the catch-up exists to recover from.
    """
    if not settings.recruiter_reply_enabled:
        return {"status": "disabled"}
    if not settings.recruiter_backlog_enabled:
        return {"status": "backlog_disabled"}

    db = SessionLocal()
    try:
        stmt = select(RecruiterReplyPreference).where(
            RecruiterReplyPreference.enabled.is_(True)
        )
        if user_id is not None:
            stmt = stmt.where(RecruiterReplyPreference.user_id == user_id)
        prefs = db.scalars(stmt).all()

        tally = dict.fromkeys(
            ("enqueued", "no_mailbox", "paused", "too_soon", "drained", "failed"), 0
        )
        dispatch_error: str | None = None
        for pref in prefs:
            try:
                dispatch_error = _fan_out_for(db, pref, tally, dispatch_error, force)
            except Exception as exc:  # noqa: BLE001 - one user must not stall the sweep
                tally["failed"] += 1
                logger.exception("backlog fan-out failed for pref %s", pref.id)
                dispatch_error = dispatch_error or str(exc)[:200]

        return {
            "status": "ok",
            "watched": len(prefs),
            "mailboxes_enqueued": tally["enqueued"],
            "drains_enqueued": tally["drained"],
            "skipped_no_mailbox": tally["no_mailbox"],
            "skipped_paused": tally["paused"],
            "skipped_too_soon": tally["too_soon"],
            "failed": tally["failed"],
            "dispatch_error": dispatch_error,
        }
    finally:
        db.close()


def _fan_out_for(
    db,
    pref: RecruiterReplyPreference,
    tally: dict[str, int],
    dispatch_error: str | None,
    force: bool,
) -> str | None:
    """One user's worth of catch-up work: a scan per mailbox, then one drain."""
    user = db.get(User, pref.user_id)
    if user is None:
        return dispatch_error

    accounts = gmail_accounts.live_accounts(user)
    if not accounts:
        # The ordinary state while the candidate has not reconnected Gmail yet.
        # Counted rather than logged, because "the catch-up did nothing" and "the
        # catch-up has no mailbox to read" are different answers to the same
        # question and only one of them needs acting on.
        tally["no_mailbox"] += 1
        return dispatch_error

    for account in accounts:
        # A mailbox in a reputation pause is in trouble already, and a catch-up
        # is the largest burst of replies this product can produce. Same rule the
        # live sweep applies, and for a stronger reason.
        decision = reputation_service.evaluate(account, 0)
        if not decision.allowed and account.paused_until is not None:
            tally["paused"] += 1
            continue
        if not force and recently_caught_up(db, account):
            tally["too_soon"] += 1
            continue
        if dispatch_error is not None:
            # The broker has already refused a publish this sweep; asking again
            # per mailbox costs a connect timeout apiece for an answer we have.
            continue
        try:
            catch_up_mailbox.apply_async(
                args=[user.id, account.id],
                kwargs={"force": force},
                # A catch-up still queued when its replacement is due has nothing
                # left to contribute: the newer one reads the same window.
                expires=settings.recruiter_backlog_interval_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - broker down; the next tick retries
            logger.warning("backlog scan for %s not dispatched: %s", account.id, exc)
            dispatch_error = str(exc)[:200]
            continue
        tally["enqueued"] += 1

    # One drain per user rather than per mailbox: the batch size is a budget for
    # this user's share of the model rate limit, and handing each of their
    # mailboxes a full budget would multiply it by however many they connected.
    if dispatch_error is None:
        try:
            drain_detected.apply_async(
                args=[user.id],
                expires=settings.recruiter_backlog_interval_seconds,
            )
            tally["drained"] += 1
        except Exception as exc:  # noqa: BLE001 - broker down; the next tick retries
            logger.warning("backlog drain for user %s not dispatched: %s", user.id, exc)
            dispatch_error = str(exc)[:200]

    return dispatch_error


# --------------------------------------------------------------------------- #
# The scan half                                                                #
# --------------------------------------------------------------------------- #


@celery_app.task
def catch_up_mailbox(
    user_id: int,
    account_id: int,
    *,
    window_days: int | None = None,
    scan_limit: int | None = None,
    process_limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Read one mailbox over the wide window and record what was never seen.

    *dry_run* stops before anything is written **and before anything is
    fetched**: the scan is what costs Gmail quota, so a rehearsal that ran it
    would spend the thing it is rehearsing. What comes back is the window and the
    query that would have been used, which is the half worth checking — this
    pipeline's recurring failure is a query that could not have matched the
    missing mail, not a filter that dropped it.
    """
    if not settings.recruiter_reply_enabled:
        return {"status": "disabled"}

    window = window_days if window_days is not None else backlog_window_days()
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return {"user_id": user_id, "status": "no_user"}
        account = next((a for a in user.gmail_accounts if a.id == account_id), None)
        if account is None:
            return {"user_id": user_id, "status": "no_account"}
        if not force and recently_caught_up(db, account):
            return {"user_id": user_id, "status": "too_soon"}

        from app.services.inbound_scanner import build_query

        if dry_run:
            return {
                "user_id": user_id,
                "account_id": account_id,
                "status": "dry_run",
                "window_days": window,
                "query": build_query(window),
            }

        try:
            result, created = recruiter_reply_service.record_scan(
                db,
                user,
                account,
                # A far higher cap than the live scan's, because this one bounds
                # *detection* and detection spends no model call — it is the
                # dispatch below that bounds the expensive half. Leaving the live
                # cap here would mean a month of mail took days merely to become
                # visible in the inbox. Whatever is still over the cap comes back
                # as `deferred` and the next run takes it.
                limit=(
                    settings.recruiter_backlog_scan_max_per_run
                    if scan_limit is None
                    else scan_limit
                ),
                window_days=window,
                trigger=TRIGGER_BACKLOG,
            )
        except gmail_service.GmailAuthRevoked as exc:
            # The state the whole feature is waiting out. Recorded once so the
            # sweep stops asking, exactly as the live scan does.
            db.rollback()
            account = db.get(GmailAccount, account_id)
            if account is not None:
                gmail_accounts.mark_revoked(db, account, str(exc))
                db.commit()
            return {"user_id": user_id, "status": "gmail_revoked", "reason": str(exc)}
        db.commit()

        dispatched = _dispatch_batch([row.id for row in created], process_limit)
        return {
            "user_id": user_id,
            "account_id": account_id,
            "status": "ok",
            "window_days": window,
            "dispatched": dispatched,
            "held_for_next_run": max(0, len(created) - dispatched),
            **result.as_dict(),
        }
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The processing half                                                          #
# --------------------------------------------------------------------------- #


@celery_app.task
def drain_detected(
    user_id: int | None = None, limit: int | None = None, dry_run: bool = False
) -> dict:
    """Hand the oldest still-unprocessed detections to the classifier, slowly.

    **Oldest first.** The alternative starves: a backlog larger than one run's
    batch would leave its tail permanently behind newer arrivals, which is the
    same shape as the unguarded fan-outs this codebase has had to fix twice.
    Oldest-first guarantees the queue terminates. It also means the rows most
    likely to be held for review go first, which is the correct order for a
    backlog — the older the message, the more it is the candidate's call whether
    answering at all still makes sense.

    Rows younger than :data:`DRAIN_MIN_AGE_SECONDS` are left to the live path,
    which is already dispatching them.
    """
    if not settings.recruiter_reply_enabled:
        return {"status": "disabled"}

    batch = settings.recruiter_backlog_batch_size if limit is None else limit
    cutoff = datetime.now(UTC) - timedelta(seconds=DRAIN_MIN_AGE_SECONDS)

    db = SessionLocal()
    try:
        stmt = (
            select(RecruiterEmail)
            .where(
                RecruiterEmail.status == RecruiterEmailStatus.DETECTED,
                RecruiterEmail.created_at <= cutoff,
            )
            .order_by(RecruiterEmail.received_at.asc(), RecruiterEmail.id.asc())
        )
        if user_id is not None:
            stmt = stmt.where(RecruiterEmail.user_id == user_id)

        pending = db.scalars(stmt).all()
        rows = pending[: max(0, batch)]

        if dry_run:
            return {
                "status": "dry_run",
                "pending": len(pending),
                "considered": len(rows),
                "plan": [
                    {
                        "recruiter_email_id": row.id,
                        "user_id": row.user_id,
                        "from_address": row.from_address,
                        "subject": row.subject,
                        "age_days": recruiter_reply_service._received_age_days(row),
                    }
                    for row in rows
                ],
            }

        dispatched = _dispatch_batch([row.id for row in rows], batch)
        return {
            "status": "ok",
            "pending": len(pending),
            "dispatched": dispatched,
            "held_for_next_run": max(0, len(pending) - dispatched),
        }
    finally:
        db.close()


def _dispatch_batch(row_ids: list[int], limit: int | None = None) -> int:
    """Send at most one batch of detections for processing, spaced out.

    The countdown is what keeps a batch from firing its model calls at once. It
    is bounded on purpose — batch size times spacing, ten minutes at the defaults
    — because a Celery countdown is not a broker-side timer: the worker takes
    delivery immediately and holds the message unacked in memory until the ETA.
    Scheduling a whole backlog that way is how this deployment once turned 54
    queued sends into 47,183 deliveries (see ``celery_app.BROKER_VISIBILITY_TIMEOUT``).
    So the queue lives in Postgres, as ``DETECTED`` rows, and only a batch of it
    is ever on the broker.

    Every dispatch carries the staleness ceiling. That is the difference between
    this and the live path, and it is the whole answer to "will the catch-up mail
    two hundred recruiters?" — a message past
    :data:`~app.services.recruiter_reply_service.RETRY_AUTO_MAX_AGE_DAYS` is
    drafted for the candidate to read, never sent on its own.
    """
    from app.tasks.recruiter_reply_tasks import process_recruiter_email

    ceiling_rows = (
        settings.recruiter_backlog_batch_size if limit is None else limit
    )
    batch = row_ids[: max(0, ceiling_rows)]
    spacing = max(0, settings.recruiter_backlog_spacing_seconds)
    ceiling = recruiter_reply_service.RETRY_AUTO_MAX_AGE_DAYS

    if not settings.celery_enabled:
        # Single-box installs and the test suite. Inline and in order, with no
        # countdown to honour — the pacing that matters there is the batch size.
        for row_id in batch:
            try:
                process_recruiter_email.run(row_id, max_auto_age_days=ceiling)
            except Exception as exc:  # noqa: BLE001 - one bad message must not end the run
                logger.warning(
                    "backlog processing failed for %s: %s", row_id, exc, exc_info=True
                )
        return len(batch)

    dispatched = 0
    for i, row_id in enumerate(batch):
        try:
            process_recruiter_email.apply_async(
                args=[row_id],
                kwargs={"max_auto_age_days": ceiling},
                countdown=i * spacing,
            )
        except Exception as exc:  # noqa: BLE001 - broker down; the row stays DETECTED
            logger.warning(
                "recruiter email %s could not be queued for backlog processing: %s",
                row_id,
                exc,
            )
            continue
        dispatched += 1
    return dispatched
