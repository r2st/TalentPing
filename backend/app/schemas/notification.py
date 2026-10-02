"""Wire shapes for the notification centre."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.notification import NOTIFICATION_KINDS


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    severity: str
    title: str
    body: str | None = None
    link: str | None = None
    meta: dict = Field(default_factory=dict)
    created_at: datetime
    read_at: datetime | None = None


class NotificationList(BaseModel):
    """A page of notifications plus the two numbers the nav badge needs.

    The count travels with the list rather than being a second request, because
    the client reads both on the same poll and two round trips would let them
    disagree — a badge saying 3 above a list showing 4 is the bug a notification
    centre is least able to survive.
    """

    items: list[NotificationOut]
    unread: int
    total: int


class UnreadCount(BaseModel):
    """The cheap poll. One integer, no rows loaded."""

    unread: int


class NotificationPreferenceOut(BaseModel):
    enabled: bool
    muted_kinds: list[str]
    #: Every kind the server knows about, so the settings screen can render a
    #: row per kind without keeping its own copy of the list — a client-side
    #: copy is how a kind added on the backend becomes a switch nobody can find.
    available_kinds: list[str]


class NotificationPreferenceUpdate(BaseModel):
    enabled: bool | None = None
    muted_kinds: list[str] | None = Field(default=None, max_length=50)

    @field_validator("muted_kinds")
    @classmethod
    def _known_kinds(cls, value: list[str] | None) -> list[str] | None:
        """Refuse kinds the server does not have.

        A typo here is silent and permanent: it stores fine, mutes nothing, and
        the switch the user flipped goes on delivering. Better a 422 naming the
        value than a setting that lies.
        """
        if value is None:
            return None
        unknown = sorted(set(value) - set(NOTIFICATION_KINDS))
        if unknown:
            raise ValueError(f"unknown notification kinds: {', '.join(unknown)}")
        # De-duplicated and ordered so the stored list is comparable and the
        # settings screen renders in a stable order.
        return sorted(set(value))


class MarkAllReadResult(BaseModel):
    marked: int


class DismissAllResult(BaseModel):
    dismissed: int
