"""Recruiter routes — read-only, plus an on-demand discovery escape hatch.

The autopilot finds contacts on its own, so there is no CRUD here. What remains:

    GET    /recruiters                -> who the system found for you
    POST   /recruiters/discover       -> crawl named companies now (preview a campaign)
    POST   /recruiters/{id}/exclude   -> stop writing to a contact, keep the file
    DELETE /recruiters/{id}/exclude   -> let the agent write to them again
    DELETE /recruiters/{id}           -> erase a contact and everything about them

**Excluding is what "stop emailing this person" means; deleting is not.**
``recruiters.applications`` cascades, and an application cascades to its thread
and the thread to its messages — so a DELETE that looks like removing a row from
a list actually erases every conversation ever had with that contact, along with
the interview status events and follow-ups hanging off it. It also does not
last: discovery matches contacts on ``(user_id, email)``, so the next crawl of
that company recreates the row and re-arms it for outreach.

So DELETE refuses when there is history to lose, and names what it would take.
``?force=true`` is the deliberate second ask.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import Page, page_params, paginate
from app.core.rate_limit import rate_limit
from app.core.sql_text import SEARCH_MAX_LENGTH, search_clause
from app.models.application import Application
from app.models.email import Email
from app.models.email_thread import EmailThread
from app.models.recruiter import Recruiter
from app.models.user import User
from app.schemas.recruiter import (
    DeleteBlocked,
    DiscoverRequest,
    DiscoverResult,
    ExcludeResult,
    RecruiterOut,
)
from app.services import outreach_service
from app.services.follow_up_service import cancel_for_application
from app.services.recruiter_discovery import discover_for_companies

router = APIRouter(prefix="/recruiters", tags=["recruiters"])


@router.get("", response_model=list[RecruiterOut])
def list_recruiters(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: str | None = Query(
        default=None,
        max_length=SEARCH_MAX_LENGTH,
        description="Case-insensitive match on name, company, or email",
    ),
    page: Page = Depends(page_params),
) -> list[Recruiter]:
    """Contacts this user owns, best-confidence first.

    The list that most needs a ceiling: rows here are written by discovery runs
    rather than by a person, so the count is a function of how long the account
    has been running rather than of anything the user did. ``X-Total-Count``
    says how many matched.
    """
    stmt = (
        select(Recruiter)
        .where(Recruiter.user_id == user.id)
        # Confidence alone is not a total order — plenty of rows tie on it — and
        # the id breaks the tie, which is what keeps a row from appearing on two
        # pages and another on none.
        .order_by(Recruiter.confidence.desc(), Recruiter.id.desc())
    )
    # Escaped, because an address is exactly the kind of string that carries
    # LIKE metacharacters by accident: `ml_infra@acme.com` typed into the box
    # must not also return `mlXinfra@acme.com`. `None` back means the box was
    # empty, which is not the same request as "match the empty string": the
    # three columns below are two-thirds nullable, so an `ilike '%%'` over them
    # is not the no-op it looks like — it drops every contact with no name and
    # no company, which is exactly the row discovery writes first.
    if (clause := search_clause(
        q, Recruiter.name, Recruiter.company, Recruiter.email
    )) is not None:
        stmt = stmt.where(clause)
    return paginate(db, stmt, page, response)


@router.post(
    "/discover",
    response_model=DiscoverResult,
    # The schema caps the list at ten companies per call; nothing capped the
    # calls. Each company is a synchronous scrape off the request thread, so ten
    # a minute is already fifty outbound crawls a minute against one worker.
    dependencies=[Depends(rate_limit(10, 60, scope="recruiter-discover"))],
)
def discover(
    payload: DiscoverRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DiscoverResult:
    """Crawl the named companies for recruiting contacts, right now.

    Synchronous and network-bound — a handful of companies at a time. The
    autopilot does this in the background for a full campaign; this endpoint
    exists so the UI can preview what a target list will yield.
    """
    created, notes = discover_for_companies(db, user, payload.companies)
    found = (
        list(db.scalars(select(Recruiter).where(Recruiter.id.in_(created))))
        if created
        else []
    )
    return DiscoverResult(
        found=len(found),
        recruiters=[RecruiterOut.model_validate(r) for r in found],
        notes=notes,
    )


def _get_owned(db: Session, user: User, recruiter_id: int) -> Recruiter:
    """The caller's own contact, or a 404.

    A 404 rather than a 403 for somebody else's row: whether an id exists is not
    ours to confirm, and the id space is guessable.
    """
    recruiter = db.get(Recruiter, recruiter_id)
    if recruiter is None or recruiter.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Recruiter not found"
        )
    return recruiter


def _history_counts(db: Session, recruiter: Recruiter) -> tuple[int, int]:
    """How many applications and messages would go with this contact.

    Counted rather than fetched — the point is the size of the loss, and the
    biggest number here is the one that matters most.
    """
    applications = (
        db.scalar(
            select(func.count(Application.id)).where(
                Application.recruiter_id == recruiter.id
            )
        )
        or 0
    )
    emails = (
        db.scalar(
            select(func.count(Email.id))
            .select_from(Email)
            .join(EmailThread, EmailThread.id == Email.thread_id)
            .join(Application, Application.id == EmailThread.application_id)
            .where(Application.recruiter_id == recruiter.id)
        )
        or 0
    )
    return applications, emails


@router.post("/{recruiter_id}/exclude", response_model=ExcludeResult)
def exclude_recruiter(
    recruiter_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExcludeResult:
    """Stop the agent writing to this contact, and keep everything about them.

    The non-destructive half of "don't email this person": outreach, follow-ups,
    auto-apply and the send-time backstop all refuse an excluded contact, and
    discovery stops offering them — but the thread, the applications and the
    history stay exactly where they are.

    **The flag alone is not enough**, for the same reason it is not enough when a
    recruiter unsubscribes: a campaign puts its messages on the broker hours
    ahead and a sequence is scheduled days ahead. The send path refuses every one
    of them at due time, so nothing reaches the contact either way — but left
    alone, the tracker goes on showing mail as still coming, and each draft goes
    on offering the user a send button for someone they just set aside. So the
    queued and drafted mail is written off and the pending sequences cancelled,
    here, in the same transaction as the flag.

    Idempotent. Re-excluding an already-excluded contact keeps the original
    timestamp, because the answer to "since when" should not move every time the
    button is pressed twice — but the sweep runs again regardless, since mail can
    have been queued between the two calls.
    """
    recruiter = _get_owned(db, user, recruiter_id)
    if recruiter.excluded_at is None:
        recruiter.excluded_at = datetime.now(UTC)

    cancelled_emails = outreach_service.cancel_unsent_for_recruiter(db, recruiter.id)
    cancelled_follow_ups = 0
    for application_id in db.scalars(
        select(Application.id).where(Application.recruiter_id == recruiter.id)
    ):
        cancelled_follow_ups += cancel_for_application(
            db, application_id, "you excluded this contact"
        )

    db.commit()
    db.refresh(recruiter)
    return ExcludeResult(
        recruiter=RecruiterOut.model_validate(recruiter),
        cancelled_emails=cancelled_emails,
        cancelled_follow_ups=cancelled_follow_ups,
    )


@router.delete("/{recruiter_id}/exclude", response_model=RecruiterOut)
def unexclude_recruiter(
    recruiter_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Recruiter:
    """Let the agent write to this contact again.

    Clears only the user's own decision. A contact who opted out, or whose
    address hard-bounced, stays unsendable — those are not this flag's to undo,
    and a route that appeared to undo them would be lying.
    """
    recruiter = _get_owned(db, user, recruiter_id)
    if recruiter.excluded_at is not None:
        recruiter.excluded_at = None
        db.commit()
        db.refresh(recruiter)
    return recruiter


@router.delete("/{recruiter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recruiter(
    recruiter_id: int,
    force: bool = Query(
        default=False,
        description="Erase the contact even though it has applications or mail.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Erase a contact. Refuses, once, when there is history behind it.

    A contact with no applications is a mis-scraped address and deleting it is
    free. A contact with applications is a conversation, and the delete takes
    the thread, the messages, the attachments, the status events and the
    follow-ups with it — so the first attempt comes back 409 with the counts,
    and the caller has to mean it.
    """
    recruiter = _get_owned(db, user, recruiter_id)

    if not force:
        applications, emails = _history_counts(db, recruiter)
        if applications or emails:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=DeleteBlocked(
                    detail=(
                        f"{recruiter.email} has {applications} application(s) and "
                        f"{emails} message(s). Deleting erases all of it. Exclude "
                        f"the contact to stop emailing them and keep the history, "
                        f"or repeat with ?force=true."
                    ),
                    applications=applications,
                    emails=emails,
                    excluded=recruiter.is_excluded,
                ).model_dump(),
            )

    db.delete(recruiter)
    db.commit()
