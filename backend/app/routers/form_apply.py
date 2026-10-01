"""Form-based applications — the browser agent's endpoints.

    GET  /form-apply/status                 -> what this server can drive
    GET  /form-apply/profile                -> the answer bank
    PUT  /form-apply/profile
    GET  /form-apply                        -> runs, newest first
    POST /form-apply/jobs/{job_id}          -> apply to a saved posting
    POST /form-apply/url                    -> apply to a pasted URL
    GET  /form-apply/{id}                   -> one run, with its audit trail
    POST /form-apply/{id}/retry
    GET  /form-apply/{id}/screenshots/{name}

Static paths are declared before ``/{application_id}`` so "status", "profile"
and "url" are never captured as ids.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import MAX_DB_INT, Page, page_params, paginate
from app.core.patching import reject_nulls
from app.core.rate_limit import rate_limit
from app.models.form_apply import (
    ATSPlatform,
    FormApplication,
    FormApplyProfile,
    FormApplyStatus,
)
from app.models.job import JobPosting
from app.models.resume import Resume
from app.models.user import User
from app.schemas.form_apply import (
    FormApplicationDetail,
    FormApplicationOut,
    FormApplyDispatch,
    FormApplyProfileOut,
    FormApplyProfileUpdate,
    FormApplyRequest,
    FormApplyServiceStatus,
    FormApplyUrlRequest,
)
from app.services import ats_platform, browser_runner, form_apply_service
from app.services.form_apply_service import FormApplyRefused

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/form-apply", tags=["form-apply"])

# Every route that starts a browser run draws on this one budget.
#
# `create_application` already refuses a duplicate submission and enforces
# `form_apply_daily_limit` — but every one of those guards sits behind
# `if submit:`, and `submit` defaults to False. A dry run is therefore
# unbounded by construction, and a dry run is not cheap: `_execute` opens a
# real browser either way, `form_answers` sends the questions it cannot answer
# from the bank to the LLM either way, and on LinkedIn `_linkedin_adapter`
# signs in with the stored password either way. The budget counts submissions
# because submissions are what an employer sees; nothing was counting what a
# run costs *us*, which is the same whether or not it ends in a click.
#
# One scope across all three routes, for the reason the two job-search routes
# share theirs: separate budgets are sidestepped rather than enforced. Retry is
# a fresh run of the same machinery, and "apply to a pasted URL" is the same
# work as "apply to a saved posting" with the posting lookup skipped — metering
# them apart would just mean alternating between them for three times the
# budget.
#
# 10 per 5 minutes is above what the single worker can actually drain at a
# minute or two per run, so it does not stand between a user and a burst of
# real applications; it bounds the loop, not the work. The limiter sits on the
# routes and not in the service on purpose — autopilot and the Celery task call
# `create_application` directly, and the beat is not the thing being rationed.
_run_limit = rate_limit(10, 300, scope="form-apply-run")


def _get_owned(db: Session, user: User, application_id: int) -> FormApplication:
    application = db.get(FormApplication, application_id)
    if application is None or application.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form application not found"
        )
    return application


def _out(application: FormApplication) -> FormApplicationDetail:
    """Serialize a run, adding the platform's display name."""
    detail = FormApplicationDetail.model_validate(application)
    detail.platform_label = ats_platform.label(application.platform)
    detail.screenshot_count = application.screenshot_count
    return detail


def _dispatch(
    db: Session, application: FormApplication
) -> FormApplyDispatch:
    """Hand the run to a worker, or drive it inline when there is no broker.

    Same shape as autopilot's "run now": a queued task is the right answer in
    production, but a single-process deploy (and the test suite) has to get a
    result back or the feature simply never happens.
    """
    if settings.celery_enabled:
        try:
            from app.tasks.form_apply_tasks import run_form_application

            run_form_application.delay(application.id)
            return FormApplyDispatch(
                application=_out(application),
                queued=True,
                detail="Queued — this takes a minute or two in the browser.",
            )
        except Exception as exc:  # noqa: BLE001 - broker down; run it here instead
            logger.warning("form apply dispatch failed, running inline: %s", exc)

    result = form_apply_service.run_application(db, application)
    return FormApplyDispatch(application=_out(result), queued=False)


# --------------------------------------------------------------------------- #
# Status + profile                                                             #
# --------------------------------------------------------------------------- #


@router.get("/status", response_model=FormApplyServiceStatus)
def service_status(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """What this deployment can drive, and how much budget is left today."""
    return form_apply_service.service_status(db, user)


@router.get("/profile", response_model=FormApplyProfileOut)
def get_profile(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> FormApplyProfile:
    """The answers only the candidate can give — created empty on first read."""
    return form_apply_service.get_or_create_profile(db, user)


@router.put("/profile", response_model=FormApplyProfileOut)
def update_profile(
    payload: FormApplyProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FormApplyProfile:
    profile = form_apply_service.get_or_create_profile(db, user)
    fields = payload.model_dump(exclude_unset=True)
    reject_nulls(FormApplyProfile, fields)
    for name, value in fields.items():
        setattr(profile, name, value)
    db.commit()
    db.refresh(profile)
    return profile


# --------------------------------------------------------------------------- #
# Runs                                                                         #
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[FormApplicationOut])
def list_applications(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    status_filter: FormApplyStatus | None = Query(default=None, alias="status"),
    platform: ATSPlatform | None = Query(default=None),
    job_posting_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    page: Page = Depends(page_params),
) -> list[FormApplicationOut]:
    """Newest first. Bounded like every list here; see `app.core.pagination`.

    These rows are written by the agent, not by the user — an autopilot account
    accumulates one per posting it tries — so this is exactly the list the
    module's ceiling exists for. It used to take a private ``limit`` that
    stopped at 50 with no ``offset`` beside it and no count on the way out,
    which is the silent truncation that module was written to stop: the 51st
    run was not on the page and there was no page to move to.
    """
    stmt = select(FormApplication).where(FormApplication.user_id == user.id)
    if status_filter is not None:
        stmt = stmt.where(FormApplication.status == status_filter)
    if platform is not None:
        stmt = stmt.where(FormApplication.platform == platform)
    if job_posting_id is not None:
        stmt = stmt.where(FormApplication.job_posting_id == job_posting_id)
    stmt = stmt.order_by(FormApplication.id.desc())
    return [_out(row) for row in paginate(db, stmt, page, response)]


@router.post(
    "/jobs/{job_id}",
    response_model=FormApplyDispatch,
    dependencies=[Depends(_run_limit)],
)
def apply_to_job(
    job_id: int,
    payload: FormApplyRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FormApplyDispatch:
    """Fill (and optionally submit) a saved posting's application form."""
    payload = payload or FormApplyRequest()
    posting = db.get(JobPosting, job_id)
    if posting is None or posting.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found"
        )
    return _dispatch(db, _create(db, user, payload, posting=posting))


@router.post(
    "/url", response_model=FormApplyDispatch, dependencies=[Depends(_run_limit)]
)
def apply_to_url(
    payload: FormApplyUrlRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FormApplyDispatch:
    """Same, for an application link that never came through the feed."""
    return _dispatch(db, _create(db, user, payload, url=payload.url))


def _create(
    db: Session,
    user: User,
    payload: FormApplyRequest,
    *,
    posting: JobPosting | None = None,
    url: str | None = None,
) -> FormApplication:
    resume = None
    if payload.resume_id is not None:
        resume = db.get(Resume, payload.resume_id)
        if resume is None or resume.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found"
            )
    try:
        return form_apply_service.create_application(
            db, user, posting=posting, url=url, submit=payload.submit, resume=resume
        )
    except FormApplyRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


@router.get("/{application_id}", response_model=FormApplicationDetail)
def get_application(
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FormApplicationDetail:
    return _out(_get_owned(db, user, application_id))


@router.post(
    "/{application_id}/retry",
    response_model=FormApplyDispatch,
    dependencies=[Depends(_run_limit)],
)
def retry_application(
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FormApplyDispatch:
    """Run a failed application again.

    Only failures and rate-limited runs may be retried. A run that ended in
    ``NEEDS_INPUT`` is waiting on the candidate, and a submitted one is done —
    re-running either is at best wasted and at worst a second application.
    """
    application = _get_owned(db, user, application_id)
    if not application.status.is_retryable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {application.status.value.lower()} application can't be retried",
        )
    application.status = FormApplyStatus.QUEUED
    application.max_attempts = max(
        application.max_attempts, application.attempts + 1
    )
    application.error = None
    db.commit()
    return _dispatch(db, application)


@router.get("/{application_id}/screenshots/{name}")
def get_screenshot(
    application_id: int,
    name: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    """One step's screenshot.

    The filename is matched against the ones this run actually recorded rather
    than joined onto the artifact directory — a name from the URL must never be
    able to address a file the run didn't produce.
    """
    application = _get_owned(db, user, application_id)
    known = {
        step.get("screenshot")
        for step in (application.steps or [])
        if step.get("screenshot")
    }
    if name not in known:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such screenshot"
        )
    path = browser_runner.artifact_dir() / name
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="That screenshot has been cleaned up",
        )
    return FileResponse(path, media_type="image/png", filename=name)
