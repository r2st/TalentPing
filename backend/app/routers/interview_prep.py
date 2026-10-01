"""Interview prep — the briefing for an application that reached a real person.

    POST /interview-prep/{application_id}  -> research, questions, talking points

A POST rather than a GET because it generates: each call runs the model afresh,
so a candidate who wants a second angle can just ask again. Nothing is cached —
the briefing is cheap to rebuild and stale prep is worse than none.

The endpoint deliberately does *not* require the application to be at an
interview status. The UI surfaces it when a recruiter replies positively, but a
candidate who wants to prepare the moment they send the application should not
be told no.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.rate_limit import rate_limit
from app.models.application import Application
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection
from app.models.email_thread import EmailThread
from app.models.job import JobPosting
from app.models.recruiter import Recruiter
from app.models.resume import Resume
from app.models.user import User
from app.schemas.interview_prep import (
    InterviewPrepOut,
    JobPrepRequest,
    PrepQuestion,
    TalkingPoint,
)
from app.services import interview_prep_service as svc

router = APIRouter(
    tags=["interview-prep"],
    dependencies=[Depends(rate_limit(10, 60, scope="interview-prep"))],
)


def _resume_for(db: Session, user: User, campaign: Campaign | None) -> Resume | None:
    """The resume this application ran off: the campaign's, else the default."""
    if campaign is not None and campaign.resume_id is not None:
        resume = db.get(Resume, campaign.resume_id)
        if resume is not None and resume.user_id == user.id:
            return resume
    return db.scalar(
        select(Resume)
        .where(Resume.user_id == user.id)
        .order_by(Resume.is_default.desc(), Resume.id.desc())
    )


def _render(context: svc.PrepContext, prep: svc.Prep) -> InterviewPrepOut:
    """The briefing as the API shape. Shared by both entry points."""
    return InterviewPrepOut(
        application_id=context.application_id,
        company=context.company,
        role=context.role,
        company_research=prep.company_research,
        company_facts=prep.company_facts,
        questions=[PrepQuestion(question=q, why=why) for q, why in prep.questions],
        talking_points=[
            TalkingPoint(point=p, evidence=evidence) for p, evidence in prep.talking_points
        ],
        gaps=prep.gaps,
        questions_to_ask=prep.questions_to_ask,
        generated_with=prep.generated_with,
    )


@router.post("/interview-prep/job", response_model=InterviewPrepOut)
def interview_prep_for_job(
    payload: JobPrepRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InterviewPrepOut:
    """Prep for a posting with no application behind it.

    Two callers: the feed, which passes a ``job_posting_id`` for something the
    user is still deciding about, and a candidate who pastes a description for a
    role this product never found. Both are people preparing for an interview
    they have not applied to through us, and refusing them is the wrong answer —
    the briefing is built from the posting either way, and only the conversation
    (which needs a thread) is missing.
    """
    posting: JobPosting | None = None
    if payload.job_posting_id is not None:
        posting = db.scalar(
            select(JobPosting).where(
                JobPosting.id == payload.job_posting_id,
                JobPosting.user_id == user.id,
            )
        )
        if posting is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found"
            )

    resume: Resume | None
    if payload.resume_id is not None:
        resume = db.scalar(
            select(Resume).where(
                Resume.id == payload.resume_id, Resume.user_id == user.id
            )
        )
        if resume is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found"
            )
    else:
        resume = _resume_for(db, user, None)

    # Pasted text beats the stored row: the user typed it more recently than the
    # crawler found it, and a feed row's description is often a truncated blurb.
    description = (payload.description or "").strip() or (
        posting.description if posting else None
    )

    context = svc.PrepContext(
        application_id=None,
        company=payload.company or (posting.company if posting else None),
        role=payload.role or (posting.title if posting else None),
        location=posting.location if posting else None,
        salary_text=posting.salary_text if posting else None,
        remote=posting.remote if posting else None,
        description=description,
        conversation=[],
    )
    return _render(context, svc.build_prep(context, resume))


@router.post("/interview-prep/{application_id}", response_model=InterviewPrepOut)
def interview_prep(
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InterviewPrepOut:
    """Generate prep material for one application."""
    application = db.scalar(
        select(Application).where(
            Application.id == application_id, Application.user_id == user.id
        )
    )
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )

    recruiter = db.get(Recruiter, application.recruiter_id)
    campaign = db.get(Campaign, application.campaign_id)
    posting = (
        db.get(JobPosting, application.job_posting_id)
        if application.job_posting_id is not None
        else None
    )

    # What the recruiter has actually written — the only first-hand account of
    # what they want, and worth more than anything inferred from the posting.
    inbound = db.scalars(
        select(Email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .where(
            EmailThread.application_id == application.id,
            Email.direction == EmailDirection.RECEIVED,
        )
        .order_by(Email.id)
    ).all()

    context = svc.PrepContext(
        application_id=application.id,
        company=(posting.company if posting else None)
        or (recruiter.company if recruiter else None),
        role=(posting.title if posting else None)
        or ((campaign.target_roles or [None])[0] if campaign else None),
        location=posting.location if posting else None,
        salary_text=posting.salary_text if posting else None,
        remote=posting.remote if posting else None,
        industry=(recruiter.industry if recruiter else None)
        or ((campaign.target_industries or [None])[0] if campaign else None),
        description=posting.description if posting else None,
        conversation=[e.body_text for e in inbound if e.body_text],
    )

    prep = svc.build_prep(context, _resume_for(db, user, campaign))
    return _render(context, prep)
