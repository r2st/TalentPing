"""Celery tasks for form-based applications.

Deliberately separate from :mod:`app.tasks.email_tasks`: an email send is a
sub-second API call, whereas driving a Workday wizard is a browser, a minute of
wall-clock and a few hundred megabytes of Chromium. Mixing them on one queue
means a form application starving a mailbox poll, so these tasks are their own
task module and can be given their own worker:

    celery -A app.tasks.celery_app.celery_app worker -Q form_apply -c 2

Two layers of retry, doing different jobs:

* :func:`app.services.browser_runner.with_retries`, *inside* one run, retries the
  transient browser failures — a page that wouldn't load, a login that didn't
  complete. It closes before any submit click, so it cannot double-submit.
* The task-level retry here re-queues a run that came back ``FAILED`` or
  ``THROTTLED``, with backoff, up to the row's own ``max_attempts``. That
  covers the failures a fresh process or a few quiet minutes fix: a worker that
  died, a browser that wedged, an ATS that was down, an ATS that told us to
  slow down. ``RATE_LIMITED`` — *our* refusal rather than the site's — is not
  on that list; see :func:`_should_retry`.

A run that ends ``NEEDS_INPUT`` is never retried. The page asked the candidate
something — a sign-in, a captcha, a question with no honest answer — and asking
it again gets the same answer. Nor is ``NO_FORM``: there is nothing there to
apply to, whether the posting was removed or never had a form.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.form_apply import FormApplication, FormApplyStatus
from app.models.job import JobPosting
from app.models.user import User
from app.services import form_apply_service
from app.services.form_apply_service import FormApplyRefused
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Grows per attempt: 1 min, then 5, then 15. A form that failed because the ATS
# was having a moment is worth coming back to, but not immediately.
_RETRY_DELAYS = (60, 300, 900)


# A run that has not finished by here is not going to. `form_apply_deadline_seconds`
# is the ceiling the *run* keeps to; this is the backstop for the run that
# cannot keep to it — a wedged driver, a browser that never returns from a
# click — and it is generous on purpose, because tripping it kills the browser
# and loses the audit trail with it. The soft limit fires first and raises
# `SoftTimeLimitExceeded` inside the task, where `run_application`'s handler
# writes the row as FAILED before the hard limit takes the process.
#
# Without either, a wedged run held a worker slot indefinitely: the row sat at
# RUNNING until `sweep_stuck_applications` came past half an hour later, and the
# concurrency of the form_apply queue is 2.
_SOFT_TIME_LIMIT = 600
_HARD_TIME_LIMIT = 660


@celery_app.task(
    bind=True,
    name="app.tasks.form_apply_tasks.run_form_application",
    max_retries=3,
    soft_time_limit=_SOFT_TIME_LIMIT,
    time_limit=_HARD_TIME_LIMIT,
)
def run_form_application(self, application_id: int) -> dict:
    """Drive one queued :class:`FormApplication` to a terminal status."""
    db = SessionLocal()
    try:
        application = db.get(FormApplication, application_id)
        if application is None:
            return {"application_id": application_id, "status": "missing"}
        if application.status.is_terminal and application.status is not FormApplyStatus.FAILED:
            # Already settled — a duplicate delivery, which Celery does allow.
            return {
                "application_id": application_id,
                "status": application.status.value,
                "note": "already finished",
            }

        result = form_apply_service.run_application(db, application)
        payload = {
            "application_id": result.id,
            "status": result.status.value,
            "platform": result.platform.value,
            "attempts": result.attempts,
            "note": result.note,
        }

        if _should_retry(result):
            delay = _delay_for(result)
            logger.info(
                "form apply %s failed (attempt %s/%s), retrying in %ss",
                result.id,
                result.attempts,
                result.max_attempts,
                delay,
            )
            # No point putting the delay on ``payload``: ``self.retry`` raises,
            # so nothing returns it. The log line above is where it is readable.
            raise self.retry(countdown=delay, max_retries=result.max_attempts)

        return payload
    finally:
        db.close()


def _should_retry(application: FormApplication) -> bool:
    """Only outcomes a fresh attempt could clear, and only while attempts remain.

    ``RATE_LIMITED`` is deliberately *not* one of them, and ``THROTTLED`` is —
    the reasoning lives on
    :attr:`~app.models.form_apply.FormApplyStatus.retries_automatically`, which
    is where it belongs now that the manual retry endpoint and this disagree on
    purpose. The endpoint reads ``is_retryable`` and offers the button; this
    reads the narrower property and presses it.
    """
    return (
        application.status.retries_automatically
        and application.attempts < application.max_attempts
    )


def _delay_for(application: FormApplication) -> int:
    """How long to wait before running this one again.

    A rate limit goes straight to the longest delay rather than walking up from
    one minute. The site has just told us we are asking too often; coming back
    in sixty seconds is the same mistake with a shorter gap, and each attempt
    that trips the limit again is one the run cannot afford to spend.
    """
    if application.status is FormApplyStatus.THROTTLED:
        return _RETRY_DELAYS[-1]
    return _RETRY_DELAYS[min(application.attempts - 1, len(_RETRY_DELAYS) - 1)]


@celery_app.task(name="app.tasks.form_apply_tasks.apply_to_posting")
def apply_to_posting(user_id: int, posting_id: int, submit: bool = False) -> dict:
    """Queue a form application for one posting, by id.

    Creating the row here rather than in the caller means the refusal rules
    (already applied, over budget, no application URL) are enforced in exactly
    one place, whoever asked for the application.

    **Nothing in this codebase calls it.** It read as "the autopilot's entry
    point", which it is not: the autopilot's form-apply path does not exist,
    and ``auto_apply_service.apply_to_posting`` — a different function with the
    same name, reached from ``run_all_autopilots`` — sends outreach email
    instead. Left registered because the name is a usable operator handle
    (``celery call app.tasks.form_apply_tasks.apply_to_posting``) for applying
    to one posting by hand, and because it is the shape a future autopilot path
    wants. Anything wiring it up should know that a dispatch failure here
    raises: the row is left ``QUEUED`` and only
    :func:`sweep_stuck_applications` will come back for it, half an hour later.
    The API's own dispatcher (``routers/form_apply._dispatch``) instead falls
    back to running inline, which is what a user watching a spinner needs.

    The two ids arrive off a queue as bare integers, so the pairing of user and
    posting is checked here — nothing upstream is guaranteed to have done it.
    """
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        posting = db.get(JobPosting, posting_id)
        if user is None or posting is None or posting.user_id != user_id:
            return {"status": "missing", "posting_id": posting_id}
        try:
            application = form_apply_service.create_application(
                db, user, posting=posting, submit=submit
            )
        except FormApplyRefused as exc:
            return {"status": "refused", "reason": str(exc), "posting_id": posting_id}

        run_form_application.delay(application.id)
        return {"status": "queued", "application_id": application.id}
    finally:
        db.close()


@celery_app.task(name="app.tasks.form_apply_tasks.sweep_stuck_applications")
def sweep_stuck_applications(older_than_minutes: int = 30) -> dict:
    """Re-queue runs a worker or a broker left behind.

    Without this a killed worker leaves a row that never reaches a terminal
    status, and the UI shows "running" forever.

    **QUEUED is swept as well as RUNNING**, and that is not belt-and-braces: it
    is the only thing that can recover a row this task itself stranded. The
    sweep flips RUNNING to QUEUED, commits, and *then* dispatches — and Celery
    here is configured to fail fast on an unreachable broker (one connection
    retry, no publish retry, a two-second socket timeout), precisely so callers
    are not blocked. A blip in that window therefore raised out of the dispatch
    loop, leaving every remaining row sitting at QUEUED with nothing on the
    queue for it — and the old query looked only at RUNNING, so no later sweep
    could ever see them again. One dropped Redis connection retired a form
    application permanently. The same applies to a row the API queued and then
    failed to dispatch (``routers/form_apply`` has the same shape).

    Re-dispatching a QUEUED row that *was* delivered is harmless, but not for
    the reason it first appears. :func:`run_form_application` returning early
    for anything already terminal only covers a row whose run has *finished*;
    a row being worked on right now is RUNNING, which is not terminal. Since
    this sweep leaves a re-dispatched row QUEUED, a row sitting behind a queue
    backlog collects one more message per sweep, and two workers taking two of
    those messages would once have driven two browsers and clicked submit
    twice. What actually makes it harmless is the claim in
    ``form_apply_service.run_application``: the move to RUNNING is a conditional
    UPDATE, so exactly one of them starts and the rest return untouched.

    The cutoff keeps a freshly created row from being swept before its own task
    has had a chance to start.

    Dispatch failures are counted rather than raised, so one unreachable broker
    cannot stop the rows behind it from being tried — the next sweep picks up
    whatever did not go out.
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=older_than_minutes)
    db = SessionLocal()
    try:
        stuck = list(
            db.scalars(
                select(FormApplication).where(
                    FormApplication.status.in_(
                        (FormApplyStatus.RUNNING, FormApplyStatus.QUEUED)
                    ),
                    # A RUNNING row is aged from when it started; a QUEUED one
                    # has no start yet (or carries a stale one from an earlier
                    # attempt), so it is aged from when the row was created.
                    func.coalesce(
                        FormApplication.started_at, FormApplication.created_at
                    )
                    < cutoff,
                )
            )
        )
        requeued = 0
        abandoned = 0
        for application in stuck:
            if application.status is FormApplyStatus.QUEUED:
                # Already where it needs to be — it just never reached a worker.
                requeued += 1
                continue
            if application.attempts >= application.max_attempts:
                application.status = FormApplyStatus.FAILED
                application.error = "Worker stopped before the run finished"
                application.finished_at = now
                # This is a terminal status written outside ``_finish``, and it
                # is the only one. Without the mirror the posting kept the badge
                # it had when the run started — the job feed showed "running",
                # with a spinner, against a run abandoned half an hour earlier
                # and never coming back, and hid the retry button behind it.
                form_apply_service.mirror_to_posting(
                    db, application, FormApplyStatus.FAILED,
                    note=application.error, now=now,
                )
                abandoned += 1
                continue
            application.status = FormApplyStatus.QUEUED
            requeued += 1
        db.commit()

        dispatched = 0
        dispatch_error: str | None = None
        for application in stuck:
            if application.status is not FormApplyStatus.QUEUED:
                continue
            try:
                run_form_application.delay(application.id)
            except Exception as exc:  # noqa: BLE001 - one dead broker, not one dead sweep
                dispatch_error = str(exc)[:200]
                logger.warning(
                    "form apply %s could not be dispatched: %s", application.id, exc
                )
                continue
            dispatched += 1
        return {
            "stuck": len(stuck),
            "requeued": requeued,
            "abandoned": abandoned,
            "dispatched": dispatched,
            "dispatch_error": dispatch_error,
        }
    finally:
        db.close()
