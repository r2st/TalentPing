"""Form apply — the orchestrator around the ATS adapters.

One function is the whole feature from the outside: :func:`run_application`
takes a queued :class:`~app.models.form_apply.FormApplication` and drives it to a
terminal status, recording every step on the way. Everything platform-specific
lives in :mod:`app.services.ats_adapters` and
:mod:`app.services.linkedin_service`; what lives here is the part that is the
same whichever ATS is on the other end:

* **Deciding whether to run at all.** A posting already submitted is not
  submitted twice, a daily budget is a daily budget, and a server with no
  browser installed says so rather than failing.
* **Retries that cannot double-submit.** Transient failures — a page that
  wouldn't load, a login that didn't complete — are retried. Anything after the
  submit click is not: adapters swallow their own click failures and *return* a
  result, so the retry window closes before the irreversible action. That covers
  one run retrying itself; two runs of the same row at once is a separate
  problem with the same consequence, and :func:`run_application` closes it by
  claiming the row rather than assuming it owns it.
* **The audit trail.** Screenshots, answered questions, filled fields and the
  error, written to the row whatever the outcome. When an automated submission
  goes wrong the candidate needs to see what the browser saw.

Browser- and network-bound: only ever call it from a Celery worker (the API's
"run now" accepts the wait in single-process deploys, exactly like job scans).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import events
from app.core.config import settings
from app.models.form_apply import (
    ATSPlatform,
    FormApplication,
    FormApplyProfile,
    FormApplyStatus,
)
from app.models.job import JobPosting, JobStatus
from app.models.resume import Resume
from app.models.user import User
from app.services import ats_platform, browser_runner, linkedin_service
from app.services.ats_adapters import AdapterResult, ApplyContext, adapter_for
from app.services.browser_runner import (
    ApplyBlocked,
    BrowserUnavailable,
    Deadline,
    PostingGone,
    RateLimited,
    StepLog,
    TransientBrowserError,
    artifact_dir,
    with_retries,
)
from app.services.career_apply_service import ApplicantProfile
from app.services.form_answers import AnswerBank
from app.services.linkedin_service import (
    LinkedInChallenge,
    LinkedInCredentialsRejected,
)
from app.services.smart_apply_service import resolve_resume

logger = logging.getLogger(__name__)

# How an adapter's status maps onto the row's.
_STATUS_MAP: dict[str, FormApplyStatus] = {
    "submitted": FormApplyStatus.SUBMITTED,
    "filled": FormApplyStatus.FILLED,
    "needs_input": FormApplyStatus.NEEDS_INPUT,
    "no_form": FormApplyStatus.NO_FORM,
    "failed": FormApplyStatus.FAILED,
}

# What the (older, simpler) `job_postings.form_apply_status` column shows for
# each outcome, so the job feed's badge keeps working unchanged.
_POSTING_STATUS: dict[FormApplyStatus, str] = {
    FormApplyStatus.SUBMITTED: "submitted",
    FormApplyStatus.FILLED: "filled",
    FormApplyStatus.NEEDS_INPUT: "needs_input",
    FormApplyStatus.NO_FORM: "no_form",
    FormApplyStatus.FAILED: "failed",
    FormApplyStatus.UNSUPPORTED: "unsupported",
    FormApplyStatus.RATE_LIMITED: "rate_limited",
    FormApplyStatus.THROTTLED: "rate_limited",
}


class FormApplyRefused(RuntimeError):
    """The run was rejected before a browser opened (duplicate, no URL, budget)."""


# --------------------------------------------------------------------------- #
# Profile + resume plumbing                                                    #
# --------------------------------------------------------------------------- #


def get_or_create_profile(db: Session, user: User) -> FormApplyProfile:
    """The user's answer bank; created empty on first read.

    ``user_id`` is unique, and read-then-insert does not expect to lose. This is
    reached from :func:`build_context`, which runs on the worker — and two
    workers on one user is the ordinary case here, not the exotic one: the sweep
    fans a task out per application, so a candidate whose first two form
    applications are dispatched together has both of them arrive at an empty
    answer bank and both insert it. The loser took the unique constraint and the
    application failed, for no reason the candidate could see and none to do
    with the form.

    The insert therefore gets its own savepoint, and losing is a re-read rather
    than a failed application. The error is only swallowed once the row it
    complained about has been found: a conflict this cannot then read is not a
    race that resolved itself.
    """
    profile = db.scalar(
        select(FormApplyProfile).where(FormApplyProfile.user_id == user.id)
    )
    if profile is not None:
        return profile

    savepoint = db.begin_nested()
    profile = FormApplyProfile(user_id=user.id)
    db.add(profile)
    try:
        db.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = db.scalar(
            select(FormApplyProfile).where(FormApplyProfile.user_id == user.id)
        )
        if existing is None:
            raise
        logger.info(
            "form-apply profile for user %s was created concurrently; adopting it",
            user.id,
        )
        return existing
    savepoint.commit()
    db.commit()
    db.refresh(profile)
    return profile


def materialize_resume(resume: Resume | None) -> str | None:
    """A file on disk to hand to a form's upload control.

    Resumes are stored parsed, not as the original PDF, so unless a deploy keeps
    the upload around there is nothing to attach. Rather than skip the upload
    entirely — which most ATS treat as an incomplete application — we write the
    extracted text out as a ``.txt``, which Greenhouse, Lever and Workday all
    accept. A stored original is always preferred when one exists.
    """
    if resume is None:
        return None
    original = getattr(resume, "storage_path", None)
    if original and Path(original).exists():
        return str(original)
    if not (resume.raw_text or "").strip():
        return None

    target = artifact_dir() / "resumes" / f"resume-{resume.id}.txt"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(resume.raw_text or "", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - an upload is not worth failing a run
        logger.warning("could not write resume file: %s", exc)
        return None
    return str(target)


def build_context(
    db: Session,
    user: User,
    application: FormApplication,
    *,
    resume: Resume | None,
) -> ApplyContext:
    """Everything the adapter needs about this candidate and this application."""
    profile = get_or_create_profile(db, user)
    applicant = ApplicantProfile.from_resume(
        resume, resume_path=materialize_resume(resume)
    ) if resume is not None else ApplicantProfile(email=user.email)
    # The profile's own contact details win: they were typed by the candidate,
    # the resume's were parsed out of a PDF.
    applicant.phone = profile.phone or applicant.phone
    applicant.linkedin_url = profile.linkedin_url or applicant.linkedin_url
    applicant.website = profile.website_url or applicant.website

    return ApplyContext(
        url=application.url or "",
        applicant=applicant,
        bank=AnswerBank.from_models(profile, resume),
        resume=resume,
        resume_path=applicant.resume_path,
        submit=application.submit_requested,
        job_title=application.job_title,
        company=application.company,
        log=StepLog(browser_runner.safe_key("app", application.id, application.platform.value)),
    )


# --------------------------------------------------------------------------- #
# Budgets                                                                      #
# --------------------------------------------------------------------------- #


def submissions_today(db: Session, user_id: int, *, now: datetime | None = None) -> int:
    """Form applications actually submitted for this user in the last 24 hours."""
    since = (now or datetime.now(UTC)) - timedelta(hours=24)
    return int(
        db.scalar(
            select(func.count(FormApplication.id)).where(
                FormApplication.user_id == user_id,
                FormApplication.status == FormApplyStatus.SUBMITTED,
                FormApplication.finished_at >= since,
            )
        )
        or 0
    )


def budget_status(db: Session, user: User) -> dict:
    """Remaining form-apply headroom, for the UI and the pre-flight check."""
    used = submissions_today(db, user.id)
    limit = settings.form_apply_daily_limit
    account = linkedin_service.get_account(db, user)
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "linkedin": linkedin_service.apply_budget(account).as_dict(),
    }


# --------------------------------------------------------------------------- #
# Creating a run                                                               #
# --------------------------------------------------------------------------- #


def create_application(
    db: Session,
    user: User,
    *,
    posting: JobPosting | None = None,
    url: str | None = None,
    submit: bool = False,
    resume: Resume | None = None,
) -> FormApplication:
    """Queue a form application. Raises :class:`FormApplyRefused` if it shouldn't run.

    The duplicate check is the important one: a posting we have already
    submitted to must not be submitted to again by a retry, a double-click, or a
    second autopilot pass. Filling it again (``submit=False``) is harmless and
    stays allowed.
    """
    target = url or (posting.url if posting else None)
    if not target:
        raise FormApplyRefused("This posting has no application URL")

    platform = ats_platform.detect_platform(target)
    if platform is ATSPlatform.UNKNOWN:
        raise FormApplyRefused("That doesn't look like an application link")

    if submit and posting is not None:
        already = db.scalar(
            select(FormApplication).where(
                FormApplication.user_id == user.id,
                FormApplication.job_posting_id == posting.id,
                FormApplication.status == FormApplyStatus.SUBMITTED,
            )
        )
        if already is not None:
            raise FormApplyRefused(
                "You've already applied to this job through its form"
            )

    if submit:
        used = submissions_today(db, user.id)
        if used >= settings.form_apply_daily_limit:
            raise FormApplyRefused(
                f"Daily form-application limit reached ({settings.form_apply_daily_limit})"
            )

    application = FormApplication(
        user_id=user.id,
        job_posting_id=posting.id if posting else None,
        resume_id=resume.id if resume else None,
        platform=platform,
        url=target,
        job_title=posting.title if posting else None,
        company=posting.company if posting else None,
        status=FormApplyStatus.QUEUED,
        submit_requested=submit,
        max_attempts=settings.form_apply_max_attempts,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


# --------------------------------------------------------------------------- #
# Running it                                                                   #
# --------------------------------------------------------------------------- #


def run_application(db: Session, application: FormApplication) -> FormApplication:
    """Drive one queued application to a terminal status. Never raises.

    The move to ``RUNNING`` is a conditional ``UPDATE`` — a claim — and not the
    bookkeeping it looks like. The module docstring promises retries that cannot
    double-submit, and that promise held only *within* a run: the retry window
    closes before the submit click. It said nothing about two runs of the same
    row at once, which several paths here can produce:

    * ``sweep_stuck_applications`` re-dispatches every ``QUEUED`` row past the
      cutoff and leaves it ``QUEUED``, so a row waiting behind a queue backlog
      collects one more message per sweep;
    * ``task_acks_late`` is set globally, so a lost worker's message is
      redelivered while the original may still be alive;
    * the API's inline fallback runs the application in the request when a
      dispatch *appears* to fail, which does not prove the message never landed.

    Two workers reaching an unconditional ``status = RUNNING`` both write it,
    both drive a browser and both click submit — the employer receives the
    candidate's application twice. Letting the database pick the winner makes
    that impossible: the claim only matches a row that is not already running,
    so the loser returns without touching the browser.

    Deliberately ``!= RUNNING`` rather than "is QUEUED": a ``FAILED`` row is
    re-run on purpose (that is the task-level retry), and a terminal row is
    already refused by the caller.
    """
    user = db.get(User, application.user_id)
    if user is None:  # pragma: no cover - FK makes this unreachable
        return _finish(db, application, FormApplyStatus.FAILED, error="orphaned run")

    claimed = db.execute(
        update(FormApplication)
        .where(
            FormApplication.id == application.id,
            FormApplication.status != FormApplyStatus.RUNNING,
        )
        .values(
            status=FormApplyStatus.RUNNING,
            started_at=datetime.now(UTC),
            attempts=func.coalesce(FormApplication.attempts, 0) + 1,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if not claimed:
        # Another worker owns this run. Not an error, and not a failure to
        # record: the run it is doing is the one this row wanted.
        logger.info(
            "form apply %s is already running; leaving it to the run that owns it",
            application.id,
        )
        db.refresh(application)
        return application

    started = datetime.now(UTC)
    try:
        result, log = _execute(db, user, application)
    except FormApplyRefused as exc:
        return _finish(
            db, application, FormApplyStatus.RATE_LIMITED, note=str(exc), started=started
        )
    except BrowserUnavailable as exc:
        return _finish(
            db, application, FormApplyStatus.UNSUPPORTED, note=str(exc), started=started
        )
    # The three HTTP outcomes `goto` now separates. Each is caught here rather
    # than folded into the generic handler below because each wants a
    # *different* answer to "should this run again", and FAILED — where all
    # three used to end up, when they were not landing as NO_FORM — says yes to
    # all of them.
    except RateLimited as exc:
        # THROTTLED, not RATE_LIMITED. The two read the same in English and
        # behave differently on purpose: RATE_LIMITED is *our* refusal (the
        # daily budget, a disabled flag) and the task must not spend the row's
        # attempts waiting for it to clear, whereas this is the employer's site
        # asking us to come back later — which is a thing a retry can do.
        return _finish(
            db, application, FormApplyStatus.THROTTLED, note=str(exc), started=started
        )
    except PostingGone as exc:
        # NO_FORM is the honest status for a job that is not there any more —
        # there is genuinely nothing to apply to — and it is not retryable,
        # which is correct: a removed posting does not come back.
        return _finish(
            db, application, FormApplyStatus.NO_FORM, note=str(exc), started=started
        )
    except ApplyBlocked as exc:
        # A 403, a captcha wall, a redirect loop, a link that serves JSON. The
        # candidate can still open it; this process cannot, and no retry
        # changes that.
        return _finish(
            db, application, FormApplyStatus.NEEDS_INPUT, note=str(exc), started=started
        )
    except LinkedInChallenge as exc:
        _flag_linkedin(db, user, "challenge_required", str(exc))
        return _finish(
            db, application, FormApplyStatus.NEEDS_INPUT, note=str(exc), started=started
        )
    except LinkedInCredentialsRejected as exc:
        _flag_linkedin(db, user, "invalid_credentials", str(exc))
        return _finish(
            db, application, FormApplyStatus.NEEDS_INPUT, note=str(exc), started=started
        )
    except Exception as exc:  # noqa: BLE001 - a failed run is data, not a crash
        logger.warning("form apply %s failed: %s", application.id, exc, exc_info=True)
        return _finish(
            db,
            application,
            FormApplyStatus.FAILED,
            error=str(exc)[:1000],
            started=started,
        )

    status = _STATUS_MAP.get(result.status, FormApplyStatus.FAILED)
    application.filled_fields = list(result.filled_fields)
    application.answers = list(result.answers)
    application.unanswered = list(result.unanswered)
    application.resume_uploaded = result.resume_uploaded
    # The employer's own acknowledgement, kept verbatim. Null on a run that
    # submitted into a page that said nothing back, which the receipt reports as
    # unconfirmed rather than as failure.
    application.confirmation = result.confirmation
    application.steps = list(log.entries)

    if status is FormApplyStatus.SUBMITTED:
        _record_submission(db, user, application)

    return _finish(db, application, status, note=result.note, started=started)


def _execute(
    db: Session, user: User, application: FormApplication
) -> tuple[AdapterResult, StepLog]:
    """Open a browser and run the right adapter, with retries."""
    if not settings.form_apply_enabled:
        raise FormApplyRefused("Form apply is switched off on this server")
    if not browser_runner.is_available():
        raise BrowserUnavailable(browser_runner.INSTALL_HINT)

    resume = _resume_for(db, user, application)
    ctx = build_context(db, user, application, resume=resume)
    log = ctx.log or StepLog(f"app-{application.id}")
    # One clock for the whole run: the retry loop will not start an attempt
    # past it, and a Workday wizard will not start another step past it.
    deadline = Deadline.from_settings()
    ctx.deadline = deadline

    if application.platform is ATSPlatform.LINKEDIN:
        adapter, storage_state = _linkedin_adapter(db, user, application)
    else:
        adapter, storage_state = adapter_for(application.platform), None
        if adapter is None:
            raise FormApplyRefused(
                f"No adapter for {ats_platform.label(application.platform)}"
            )

    def attempt(_n: int) -> AdapterResult:
        with browser_runner.browser_page(storage_state=storage_state) as page:
            result = adapter.run(page, ctx)
            if application.platform is ATSPlatform.LINKEDIN:
                _persist_session(db, user, page)
            return result

    result, report = with_retries(
        attempt,
        attempts=application.max_attempts,
        retry_on=(TransientBrowserError,),
        deadline=deadline,
    )
    if report.retried:
        result.note = " ".join(
            filter(None, [result.note, f"(succeeded on attempt {report.attempts})"])
        )
    return result, log


def _resume_for(
    db: Session, user: User, application: FormApplication
) -> Resume | None:
    if application.resume_id is not None:
        resume = db.get(Resume, application.resume_id)
        if resume is not None and resume.user_id == user.id:
            return resume
    try:
        return resolve_resume(db, user, None)
    except Exception:  # noqa: BLE001 - a run with no resume still fills contact details
        return None


def _linkedin_adapter(db: Session, user: User, application: FormApplication):
    """The Easy Apply adapter plus a stored session, or a refusal."""
    if not settings.linkedin_easy_apply_enabled:
        raise FormApplyRefused(
            "LinkedIn Easy Apply is switched off on this server "
            "(LINKEDIN_EASY_APPLY_ENABLED)"
        )
    account = linkedin_service.get_account(db, user)
    if account is None:
        raise FormApplyRefused("Connect your LinkedIn account first")

    decision = linkedin_service.apply_budget(account)
    if application.submit_requested and not decision.allowed:
        raise FormApplyRefused(decision.reason or "LinkedIn daily limit reached")

    password = linkedin_service.password_for(account)
    return (
        linkedin_service.EasyApplyAdapter(account, password),
        linkedin_service.session_state(account),
    )


def _persist_session(db: Session, user: User, page) -> None:
    """Keep the signed-in cookies so the password isn't retyped next run."""
    account = linkedin_service.get_account(db, user)
    if account is None:
        return
    try:
        state = page.context.storage_state()
    except Exception as exc:  # noqa: BLE001 - session reuse is an optimisation
        logger.debug("could not read LinkedIn session state: %s", exc)
        return
    linkedin_service.save_session(db, account, state)


def _flag_linkedin(db: Session, user: User, status: str, error: str) -> None:
    account = linkedin_service.get_account(db, user)
    if account is not None:
        linkedin_service.set_status(db, account, status, error=error)


def _record_submission(db: Session, user: User, application: FormApplication) -> None:
    """Count a submitted application against the budgets it belongs to."""
    if application.platform is ATSPlatform.LINKEDIN:
        account = linkedin_service.get_account(db, user)
        if account is not None:
            linkedin_service.record_apply(db, account)


def mirror_to_posting(
    db: Session,
    application: FormApplication,
    status: FormApplyStatus,
    *,
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    """Show a run's outcome on the posting the job feed draws.

    ``job_postings.form_apply_status`` is the older, simpler column the feed's
    badge reads, and it is a mirror rather than a source of truth — so it is
    only ever as current as the last thing that remembered to write it.

    Extracted from :func:`_finish` because :func:`_finish` was not the only
    place a run reaches a terminal status. ``form_apply_tasks.sweep_stuck_
    applications`` writes ``FAILED`` directly onto a row whose worker died with
    its attempts spent, and it wrote nothing here — so the posting kept
    whatever badge it had when the run started. The feed said "running", with
    a spinner, against a run that had been abandoned half an hour earlier and
    was never coming back; nothing in the UI could contradict it, and the
    retry button the badge hides was the thing the user needed.

    Safe to call for a run with no posting attached — manual URLs have none —
    and safe to call twice: every write is an assignment, and ``applied_at``
    keeps the first submission's timestamp rather than moving to the latest.
    """
    if not application.job_posting_id:
        return
    posting = db.get(JobPosting, application.job_posting_id)
    if posting is None:
        return
    posting.form_apply_status = _POSTING_STATUS.get(status, status.value.lower())
    posting.form_apply_note = note
    if status is FormApplyStatus.SUBMITTED:
        posting.status = JobStatus.APPLIED
        posting.applied_at = posting.applied_at or (now or datetime.now(UTC))


def _finish(
    db: Session,
    application: FormApplication,
    status: FormApplyStatus,
    *,
    note: str | None = None,
    error: str | None = None,
    started: datetime | None = None,
) -> FormApplication:
    """Write the terminal state, and mirror it onto the posting."""
    now = datetime.now(UTC)
    application.status = status
    application.note = note
    application.error = error
    application.finished_at = now
    if started is not None:
        application.duration_ms = int((now - started).total_seconds() * 1000)

    mirror_to_posting(db, application, status, note=note or error, now=now)

    db.commit()
    db.refresh(application)

    # Emitted from ``_finish`` rather than from the SUBMITTED branch in
    # ``run_application``, because this is the one place where the outcome is
    # both decided *and* committed. Every other candidate site is a step in a
    # run that can still fail, and an application logged as submitted from one
    # of those is a claim the database might go on to contradict.
    if status is FormApplyStatus.SUBMITTED:
        events.emit(
            events.APPLICATION_SUBMITTED,
            application_id=application.id,
            user_id=application.user_id,
            job_posting_id=application.job_posting_id,
            platform=application.platform.value,
            channel="form",
            duration_ms=application.duration_ms,
            attempts=application.attempts,
            # Whether the employer acknowledged, not what it said. "Submitted
            # but unconfirmed" is a distinct and much less reassuring outcome
            # than "submitted", and it is the one worth being able to count.
            confirmed=bool(application.confirmation),
            unanswered=len(application.unanswered or []),
        )
    return application


# --------------------------------------------------------------------------- #
# Status for the UI                                                            #
# --------------------------------------------------------------------------- #


def platform_support() -> list[dict]:
    """Which platforms this build can drive, for the settings screen."""
    return [
        {
            "platform": platform.value,
            "label": ats_platform.label(platform),
            "adapter": ats_platform.has_adapter(platform),
            "requires_account": platform is ATSPlatform.LINKEDIN,
        }
        for platform in (
            ATSPlatform.GREENHOUSE,
            ATSPlatform.LEVER,
            ATSPlatform.ASHBY,
            ATSPlatform.ICIMS,
            ATSPlatform.WORKDAY,
            ATSPlatform.LINKEDIN,
            ATSPlatform.GENERIC,
        )
    ]


def service_status(db: Session, user: User) -> dict:
    """Everything the Jobs page needs to offer (or explain) Form apply."""
    return {
        "enabled": settings.form_apply_enabled,
        "browser_installed": browser_runner.is_available(),
        "screenshots": settings.form_apply_screenshots,
        "platforms": platform_support(),
        "budget": budget_status(db, user),
        "linkedin": linkedin_service.status_summary(
            linkedin_service.get_account(db, user)
        ),
    }


__all__ = [
    "FormApplyRefused",
    "budget_status",
    "build_context",
    "create_application",
    "get_or_create_profile",
    "materialize_resume",
    "platform_support",
    "mirror_to_posting",
    "run_application",
    "service_status",
    "submissions_today",
]
