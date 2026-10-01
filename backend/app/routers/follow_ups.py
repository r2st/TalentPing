"""Follow-up suggestions — the applications nobody is scheduled to chase.

    GET    /follow-ups/suggestions              -> the list, longest silence first
    POST   /follow-ups/suggestions/{id}/accept  -> schedule this one's sequence
    DELETE /follow-ups/suggestions/{id}         -> stop suggesting it

Everything a suggestion knows is derived at read time from applications, emails
and follow-up rows: there is no ``suggestions`` table and deliberately so. A
stored suggestion is a fact that goes stale the moment a recruiter replies, and
the whole failure this feature exists to prevent is chasing somebody who already
wrote back.

That has one consequence worth stating: **both write routes re-check
everything**. The list is a page that stays open, and the gap between showing a
row and clicking it is exactly where the reply lands — or where the user moves
the card to OFFER themselves. See
:func:`~app.services.follow_up_suggestions.accept` and
:func:`~app.services.follow_up_suggestions.dismiss`.

The two write routes answer 200 with a sentence rather than 4xx when they
decline. Every reason they can decline — a reply arrived, a second tab got there
first, the campaign's follow-ups are off — is a race the user lost, not a
malformed request, and a red error for "they replied, so we didn't chase them"
would be the product reporting its own correct behaviour as a failure.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.follow_up import (
    FollowUpScheduled,
    FollowUpSuggestion,
    FollowUpSuggestions,
)
from app.services import follow_up_suggestions

router = APIRouter(prefix="/follow-ups", tags=["follow-ups"])


@router.get("/suggestions", response_model=FollowUpSuggestions)
def suggestions(
    limit: int = Query(default=follow_up_suggestions.LIMIT, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FollowUpSuggestions:
    """Applications that went quiet, longest silence first.

    ``total`` is computed uncapped so the client can say "25 of 40" and so a
    badge keeps counting past the page — a number that silently stops at the
    limit is a number that stops being true exactly when it matters.
    """
    rows = follow_up_suggestions.build(db, user, limit=limit)
    total = (
        len(rows)
        if len(rows) < limit
        else follow_up_suggestions.count(db, user)
    )
    return FollowUpSuggestions(
        suggestions=[FollowUpSuggestion(**vars(row)) for row in rows],
        total=total,
        limit=limit,
    )


@router.post("/suggestions/{application_id}/accept", response_model=FollowUpScheduled)
def accept(
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FollowUpScheduled:
    """Schedule the follow-up sequence for one suggested application."""
    created, refused = follow_up_suggestions.accept(db, user, application_id)
    if refused is not None:
        return FollowUpScheduled(refused=refused)
    return FollowUpScheduled(
        scheduled=len(created),
        next_at=min(f.scheduled_at for f in created) if created else None,
    )


@router.delete("/suggestions/{application_id}", response_model=FollowUpScheduled)
def dismiss(
    application_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FollowUpScheduled:
    """Stop suggesting this one — the conversation is over.

    Not a 204. Dismissing closes the application, which is a state change the
    client needs to know succeeded and which can decline (the row may already be
    gone), so it answers with the same envelope the accept route does.
    """
    return FollowUpScheduled(
        refused=follow_up_suggestions.dismiss(db, user, application_id)
    )
