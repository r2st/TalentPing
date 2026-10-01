"""LinkedIn connection endpoints.

    GET    /linkedin/status        -> connection state + today's Easy Apply budget
    PUT    /linkedin/credentials   -> store the sign-in, encrypted at rest
    DELETE /linkedin/credentials   -> forget it
    POST   /linkedin/easy-apply/{job_id}
                                   -> Easy Apply to a saved posting

The password is accepted here and nowhere else, encrypted before it reaches the
database, and never returned by any response on this router. Easy Apply itself is
just a form application whose platform happens to be LinkedIn, so it is created
through :mod:`app.services.form_apply_service` and shows up in the same run
history as Workday or Greenhouse.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.rate_limit import rate_limit
from app.models.form_apply import ATSPlatform
from app.models.job import JobPosting
from app.models.user import User
from app.schemas.form_apply import FormApplyDispatch, FormApplyRequest
from app.schemas.linkedin import LinkedInCredentialsIn, LinkedInStatusOut
from app.services import ats_platform, crypto, linkedin_service
from app.services.form_apply_service import FormApplyRefused, create_application

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/linkedin", tags=["linkedin"])


@router.get("/status", response_model=LinkedInStatusOut)
def get_status(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    return linkedin_service.status_summary(linkedin_service.get_account(db, user))


@router.put("/credentials", response_model=LinkedInStatusOut)
def put_credentials(
    payload: LinkedInCredentialsIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Store the candidate's LinkedIn sign-in for Easy Apply.

    Refused outright when no encryption key is configured: a password in a
    plaintext column is worse than a feature that doesn't work.
    """
    if not crypto.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "TOKEN_ENCRYPTION_KEY is not set on this server, so credentials "
                "can't be stored securely."
            ),
        )
    account = linkedin_service.store_credentials(
        db, user, email=str(payload.email), password=payload.password
    )
    logger.info("stored LinkedIn credentials for user %s", user.id)
    return linkedin_service.status_summary(account)


@router.delete("/credentials", status_code=status.HTTP_204_NO_CONTENT)
def delete_credentials(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    """Forget the stored credentials and any session cookies with them."""
    if not linkedin_service.clear_credentials(db, user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No LinkedIn account stored"
        )


@router.post(
    "/easy-apply/{job_id}",
    response_model=FormApplyDispatch,
    # Everything past `_dispatch` is the form-apply machinery, so this draws on
    # the form-apply budget — the docstring below has always claimed it obeys
    # the same ones, and until this dependency existed that was true only of the
    # daily submit budget, which a dry run never touches. Without it this route
    # is simply the unmetered way to ask for the run the other doors ration,
    # and the costliest: every attempt signs into LinkedIn with the stored
    # password.
    dependencies=[Depends(rate_limit(10, 300, scope="form-apply-run"))],
)
def easy_apply(
    job_id: int,
    payload: FormApplyRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FormApplyDispatch:
    """Easy Apply to a LinkedIn posting in the feed.

    Everything past creating the run is shared with the other ATS platforms, so
    this delegates to the form-apply router's dispatch — the run appears in the
    same history and obeys the same budgets.
    """
    from app.routers.form_apply import _dispatch  # local: avoids an import cycle

    payload = payload or FormApplyRequest()
    if not settings.linkedin_easy_apply_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "LinkedIn Easy Apply is switched off on this server "
                "(set LINKEDIN_EASY_APPLY_ENABLED=true to enable it)."
            ),
        )

    posting = db.get(JobPosting, job_id)
    if posting is None or posting.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found"
        )

    account = linkedin_service.get_account(db, user)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect your LinkedIn account before using Easy Apply",
        )

    # Checked *before* the run is created rather than after. ``create_application``
    # commits, so rejecting a non-LinkedIn posting downstream of it left a QUEUED
    # row nothing would ever pick up — a permanent "queued" entry in the run
    # history for an application that was refused.
    # A posting with no URL at all falls through to ``create_application``,
    # whose refusal names the actual problem rather than the platform.
    if posting.url and ats_platform.detect_platform(posting.url) is not ATSPlatform.LINKEDIN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That posting doesn't link to LinkedIn — use Form apply instead",
        )

    decision = linkedin_service.apply_budget(account)
    if payload.submit and not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=decision.reason or "LinkedIn daily limit reached",
        )

    try:
        application = create_application(
            db, user, posting=posting, submit=payload.submit
        )
    except FormApplyRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return _dispatch(db, application)
