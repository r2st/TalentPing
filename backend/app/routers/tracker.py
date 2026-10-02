"""Tracker + onboarding — the two reads the whole UI is built on.

    GET   /onboarding        -> which wizard step the user is on
    GET   /tracker           -> a page of outreach, with account-wide stats
    GET   /tracker/{id}      -> the full message history for one outreach
    PATCH /tracker/emails/{id} -> edit a draft before it is sent
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import MAX_DB_INT, Page, page_params
from app.core.rate_limit import rate_limit
from app.models.application import (
    ENGAGED_STATUSES,
    INTERVIEWING_STATUSES,
    Application,
    ApplicationStatus,
)
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.recruiter import Recruiter
from app.models.resume import Resume
from app.models.user import User
from app.schemas.email import ApplicationDetail, EmailOut, EmailUpdate
from app.schemas.tracker import OnboardingStatus, TrackerOut, TrackerRow, TrackerStats
from app.services import gmail_accounts, google_oauth

router = APIRouter(tags=["tracker"])

_write_limit = rate_limit(
    lambda: settings.write_rate_limit,
    lambda: settings.write_rate_window_seconds,
    scope="tracker-write",
)

# "Did the recruiter write back?" and "is a real conversation happening?" come
# from the model rather than being restated here.
#
# They were a local copy, written before ``ApplicationStatus.OFFER`` existed and
# never extended when it did — so an application that reached an offer counted
# as neither a reply nor an interview on this page, while the dashboard and
# analytics counted it as both. The dashboard had the identical bug and fixed it
# the same way; see the comment beside ``_ENGAGED`` in routers/dashboard.py, and
# models/application.py for why the sets live there.
_REPLIED_STATUSES = ENGAGED_STATUSES
_INTERVIEW_STATUSES = INTERVIEWING_STATUSES


@router.get("/onboarding", response_model=OnboardingStatus)
def onboarding(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> OnboardingStatus:
    """Where the user is in setup — drives the three-step wizard.

    upload_resume -> set_preferences -> connect_email -> start_autopilot -> done,
    where the last two are one step in the UI ("connect your email and start").

    Resumes come first because that is the step that does work for the user:
    everything the search needs is extracted from the files, so uploading turns
    the rest of setup into a review. Gmail is asked for last, when there is
    something to send.

    ``configured_at`` (not row existence) marks the preferences step done, since
    reading ``GET /autopilot`` creates the row as a side effect.

    Past ``done`` it keeps answering, and the answer changes: ``last_run_at``,
    ``drafts_waiting`` and ``sent_count`` are what the wizard watches while the
    first cycle runs, so the minutes between switching on and the first email
    show progress instead of an empty page.
    """
    resume_count = (
        db.scalar(select(func.count(Resume.id)).where(Resume.user_id == user.id)) or 0
    )
    campaign_count = (
        db.scalar(select(func.count(Campaign.id)).where(Campaign.user_id == user.id)) or 0
    )
    sent_count = (
        db.scalar(
            select(func.count(Email.id))
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .where(Application.user_id == user.id, Email.status == EmailStatus.SENT)
        )
        or 0
    )

    drafts_waiting = (
        db.scalar(
            select(func.count(Email.id))
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .where(
                Application.user_id == user.id,
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.DRAFT,
            )
        )
        or 0
    )

    accounts = gmail_accounts.live_accounts(user)
    account = user.primary_gmail
    pref = user.autopilot
    autopilot_configured = pref is not None and pref.configured_at is not None
    autopilot_active = bool(pref is not None and pref.is_active)

    if resume_count == 0:
        next_step = "upload_resume"
    elif not autopilot_configured:
        next_step = "set_preferences"
    elif not user.gmail_connected:
        next_step = "connect_email"
    elif not autopilot_active:
        next_step = "start_autopilot"
    else:
        next_step = "done"

    return OnboardingStatus(
        gmail_configured=google_oauth.is_configured(),
        gmail_connected=user.gmail_connected,
        gmail_address=account.email if account else None,
        gmail_addresses=[a.email for a in accounts],
        gmail_account_count=len(accounts),
        resume_count=resume_count,
        campaign_count=campaign_count,
        sent_count=sent_count,
        autopilot_configured=autopilot_configured,
        autopilot_active=autopilot_active,
        last_run_at=pref.last_run_at if pref is not None else None,
        drafts_waiting=drafts_waiting,
        next_step=next_step,
        complete=next_step == "done",
    )


def _has_unsent_outbound():
    """``EXISTS`` a queued-or-draft outbound email on this application.

    The ``queued`` stat, as a correlated subquery rather than as a scan of every
    email the user owns.
    """
    return (
        select(Email.id)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .where(
            EmailThread.application_id == Application.id,
            Email.direction == EmailDirection.SENT,
            Email.status.in_((EmailStatus.QUEUED, EmailStatus.DRAFT)),
        )
        .exists()
    )


@router.get("/tracker", response_model=TrackerOut)
def tracker(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    campaign_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    status_filter: ApplicationStatus | None = Query(default=None, alias="status"),
    page: Page = Depends(page_params),
) -> TrackerOut:
    """This user's outreach, newest first, with its latest reply.

    Bounded like every other list here — it was the one that was not, which is
    backwards: applications are written by the pipeline rather than by a person,
    one per contact it found, so this is precisely the endpoint whose row count
    grows on its own. Unbounded it loaded every application, then every thread on
    them, then every message in those threads *including the bodies*, on the
    endpoint the UI polls.

    ``stats`` stays whole-account whatever page is asked for. Counting the rows
    on the page instead would be a worse bug than the one this fixes: the header
    numbers would silently start describing the page, and "12 replies" would mean
    twelve on this screen rather than twelve at all. So the stats are aggregates
    over the whole filtered set, and only ``rows`` is paged.
    """
    filters = [Application.user_id == user.id]
    if campaign_id is not None:
        filters.append(Application.campaign_id == campaign_id)
    if status_filter is not None:
        filters.append(Application.status == status_filter)

    def _tally(condition):
        """Conditional count. ``func.count`` cannot take a predicate, and a
        second query per counter is four more round trips per poll."""
        return func.sum(case((condition, 1), else_=0))

    totals = db.execute(
        select(
            func.count(Application.id),
            _tally(Application.status.in_(_REPLIED_STATUSES)),
            _tally(Application.status == ApplicationStatus.INTERESTED),
            _tally(Application.status.in_(_INTERVIEW_STATUSES)),
            _tally(_has_unsent_outbound()),
        ).where(*filters)
    ).one()
    contacted, replied, interested, interviews, queued = (int(v or 0) for v in totals)

    stmt = (
        select(Application, Recruiter, Campaign)
        .join(Recruiter, Application.recruiter_id == Recruiter.id)
        .join(Campaign, Application.campaign_id == Campaign.id)
        .where(*filters)
        # `id` breaks ties on `updated_at`, which the pipeline stamps in bulk —
        # a whole campaign's applications share a timestamp to the microsecond,
        # and an unstable order across pages repeats rows and drops others.
        .order_by(Application.updated_at.desc(), Application.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )

    records = db.execute(stmt).all()
    application_ids = [a.id for a, _, _ in records]

    response.headers["X-Total-Count"] = str(contacted)
    response.headers["X-Has-More"] = (
        "true" if page.offset + len(records) < contacted else "false"
    )

    # One pass over the emails for every listed application, rather than a query
    # per row — the tracker is the most-hit endpoint in the product.
    threads_by_app: dict[int, list[EmailThread]] = {}
    if application_ids:
        thread_rows = db.scalars(
            select(EmailThread)
            .where(EmailThread.application_id.in_(application_ids))
            .options(selectinload(EmailThread.emails))
        ).all()
        for thread in thread_rows:
            threads_by_app.setdefault(thread.application_id, []).append(thread)

    rows: list[TrackerRow] = []

    for application, recruiter, campaign in records:
        emails = [e for t in threads_by_app.get(application.id, []) for e in t.emails]
        outbound = [e for e in emails if e.direction == EmailDirection.SENT]
        inbound = [e for e in emails if e.direction == EmailDirection.RECEIVED]
        sent = [e for e in outbound if e.sent_at is not None]
        latest_reply = inbound[-1] if inbound else None
        subject = next((e.subject for e in outbound if e.subject), None)

        rows.append(
            TrackerRow(
                application_id=application.id,
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                recruiter_email=recruiter.email,
                recruiter_name=recruiter.name,
                company=recruiter.company,
                status=application.status,
                subject=subject,
                sent_at=max((e.sent_at for e in sent), default=None),
                replied_at=latest_reply.sent_at if latest_reply else None,
                reply_intent=latest_reply.intent if latest_reply else None,
                reply_snippet=(
                    (latest_reply.body_text or "")[:200] if latest_reply else None
                ),
                message_count=len(emails),
                updated_at=application.updated_at,
            )
        )

    return TrackerOut(
        stats=TrackerStats(
            contacted=contacted,
            queued=queued,
            replied=replied,
            interested=interested,
            interviews=interviews,
            reply_rate=round(replied / contacted, 3) if contacted else 0.0,
        ),
        rows=rows,
    )


@router.get("/tracker/{application_id}", response_model=ApplicationDetail)
def get_outreach(
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Application:
    """Full message history for one outreach thread."""
    application = db.scalar(
        select(Application)
        .where(Application.id == application_id, Application.user_id == user.id)
        .options(selectinload(Application.threads).selectinload(EmailThread.emails))
    )
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Outreach not found"
        )
    return application


@router.patch(
    "/tracker/emails/{email_id}",
    response_model=EmailOut,
    dependencies=[Depends(_write_limit)],
)
def edit_email_draft(
    email_id: int,
    payload: EmailUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Email:
    """Edit a DRAFT email's subject/body before it is sent."""
    email = db.scalar(
        select(Email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(Email.id == email_id, Application.user_id == user.id)
    )
    if email is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Email not found"
        )
    if email.status != EmailStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only DRAFT emails can be edited",
        )
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(email, field, value)
    # This draft is the user's now. Two sweeps rewrite agent-written drafts from
    # a fresh classification and can queue the result unread — see
    # ``Email.user_owned_at`` — and neither had any way to know that the words
    # they were about to replace were the ones a person typed. An empty PATCH
    # stamps nothing: it changed no text, so it is not a claim on any.
    if changes:
        email.user_owned_at = datetime.now(UTC)
    db.commit()
    db.refresh(email)
    return email
