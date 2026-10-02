"""Board schemas — the columns, the move, and the history behind a card.

The cards themselves are ``ApplicationRow`` from the dashboard read: the board
and the table are two drawings of one filtered set, and giving them separate
payloads is how they start disagreeing about how many applications there are.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.application import ApplicationStatus
from app.models.status_event import StatusEventSource


class BoardColumn(BaseModel):
    """One column, in the order the server wants it drawn."""

    key: str  # applied | screening | interview | offer | rejected
    label: str
    # Over the whole filtered set, not just the page of cards returned — the
    # same rule the funnel counts follow.
    count: int = 0


class BoardMoveRequest(BaseModel):
    """Where the card was dropped."""

    stage: str = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=255)


class StatusEventOut(BaseModel):
    """One transition, for the card's history panel."""

    at: datetime
    from_status: ApplicationStatus | None = None
    to_status: ApplicationStatus
    source: StatusEventSource
    reason: str | None = None


class BoardMoveOut(BaseModel):
    """What the card looks like after the move.

    ``moved`` is false when the drop resolved to the status the card already
    had — dropping it back where it started, or into a column it was already
    filed under. Nothing was written, and the UI should not claim otherwise.
    """

    application_id: int
    status: ApplicationStatus
    board_stage: str
    locked: bool = False
    moved: bool = False
    event: StatusEventOut | None = None


class StatusHistoryOut(BaseModel):
    application_id: int
    events: list[StatusEventOut] = Field(default_factory=list)
