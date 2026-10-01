"""Follow-up suggestion schemas — applications nobody is scheduled to chase."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FollowUpSuggestion(BaseModel):
    """One application worth chasing, with the reason attached.

    ``reason`` is composed server-side rather than left to the client, for the
    same reason every skip reason in the autopilot is: the sentence is the
    product's judgement, and two clients phrasing it differently would be two
    products. It is also what makes the row auditable — a suggestion the user
    cannot second-guess is one they either obey blindly or ignore entirely.
    """

    application_id: int
    company: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str
    subject: str | None = None
    last_sent_at: datetime
    silent_days: int = 0
    # Delivered messages on this thread, so the row can say "after 3 emails".
    touches: int = 1
    # due | cold | last_call — see `follow_up_suggestions.URGENCIES`.
    urgency: str = "due"
    reason: str = ""
    # False when the campaign has follow-ups switched off. The row is still
    # shown — the user can send by hand or change the campaign — but the
    # one-click action would do nothing, and a button that silently does
    # nothing is worse than one that is not there.
    can_schedule: bool = True


class FollowUpSuggestions(BaseModel):
    suggestions: list[FollowUpSuggestion] = Field(default_factory=list)
    # Uncapped, so a badge does not stop counting at the page limit — which is
    # exactly where the number starts mattering.
    total: int = 0
    # Echoed so the client can say "showing 25 of 40" without knowing the cap.
    limit: int = 0


class FollowUpScheduled(BaseModel):
    """What acting on a suggestion produced, or why it produced nothing."""

    scheduled: int = 0
    # The first step's send time, so the UI can say when it will actually go.
    next_at: datetime | None = None
    # A sentence when nothing was scheduled. Every reason this can decline is a
    # race the user lost — a reply that landed, a tab that was already open —
    # rather than an error, so it is a 200 with an explanation, not a 4xx.
    refused: str | None = None
