"""Recruiter schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RecruiterOut(BaseModel):
    id: int
    name: str | None = None
    email: EmailStr
    company: str | None = None
    title: str | None = None
    industry: str | None = None
    specialization: str | None = None
    linkedin_url: str | None = None
    source: str | None = None
    source_url: str | None = None
    confidence: float = 1.0
    opted_out: bool
    # When the *user* asked us to stop writing to this contact — as opposed to
    # ``opted_out``, which is when the contact did. Null means we may write.
    excluded_at: datetime | None = None

    model_config = {"from_attributes": True}


class ExcludeResult(BaseModel):
    """The contact, plus what excluding them stopped.

    The counts are the point of returning anything at all. Excluding is not only
    a flag on a row — a campaign queues its mail hours ahead and a follow-up
    sequence is scheduled days ahead, so the decision has to reach work that was
    already in flight. Saying how much it reached is how the user knows it did.
    """

    recruiter: RecruiterOut
    cancelled_emails: int = 0
    cancelled_follow_ups: int = 0


class DeleteBlocked(BaseModel):
    """What a destructive delete would have taken with it.

    Returned with a 409 so the caller can tell the user the size of what they
    are about to lose, rather than discovering it afterwards.
    """

    detail: str
    applications: int
    emails: int
    excluded: bool


class DiscoverRequest(BaseModel):
    """Crawl these companies for recruiting contacts."""

    companies: list[str] = Field(min_length=1, max_length=10)


class DiscoverResult(BaseModel):
    found: int
    recruiters: list[RecruiterOut] = Field(default_factory=list)
    # Per-company outcomes for the ones that yielded nothing.
    notes: list[str] = Field(default_factory=list)
