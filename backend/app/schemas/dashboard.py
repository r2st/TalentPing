"""Dashboard schemas — the pipeline read model.

One endpoint feeds the whole page (stages, stats, feed, filter options) because
four round trips to render one screen is four chances to show a half-updated
view.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.application import ApplicationStatus
from app.schemas.board import BoardColumn


class PipelineStage(BaseModel):
    """One column of the funnel."""

    key: str      # applied | viewed | responded | interview | offer
    label: str
    count: int = 0
    # Share of everything that entered the pipeline, 0..1 — the funnel's width.
    rate: float = 0.0
    # Share of the *previous* stage that made it this far, 0..1. The width above
    # answers "how much of everything got here"; this answers "how much of what
    # got to the last step survived this one", and they are different questions
    # with different answers. A candidate whose replies almost never turn into
    # interviews has a problem in the conversation, not in the outreach, and
    # only this number says so.
    #
    # ``None`` on the first stage, where there is no previous step: a hard-coded
    # 100% there reads as a measurement, and it is only an artefact of being
    # first.
    conversion: float | None = None
    # How many were lost at this step — the previous stage's count minus this
    # one's. Sent rather than left to the client so the funnel's arithmetic has
    # one owner; a client re-deriving it from array order gets it wrong the
    # first time a stage is inserted.
    drop_off: int = 0


class DashboardStats(BaseModel):
    total_applications: int = 0
    active: int = 0
    responded: int = 0
    interviews: int = 0
    offers: int = 0
    rejected: int = 0

    response_rate: float = 0.0
    interview_rate: float = 0.0
    offer_rate: float = 0.0

    # Follow-up automation, at a glance.
    follow_ups_scheduled: int = 0
    follow_ups_sent: int = 0

    # Smart Apply usage.
    tailored_resumes: int = 0
    jobs_tracked: int = 0
    average_fit_score: float | None = None

    # Median days from first outreach to the first reply, over replied threads.
    median_days_to_reply: float | None = None


class ActivityEvent(BaseModel):
    """One entry in the timeline."""

    at: datetime
    kind: str  # outreach_sent | reply_received | follow_up_sent | status_change | job_found
    application_id: int | None = None
    company: str | None = None
    role: str | None = None
    contact: str | None = None
    summary: str | None = None
    status: ApplicationStatus | None = None


class ApplicationRow(BaseModel):
    """One application in the pipeline table."""

    application_id: int
    campaign_id: int
    campaign_name: str | None = None
    company: str | None = None
    role: str | None = None
    contact: str | None = None
    status: ApplicationStatus
    # How far it got, for the funnel: applied | viewed | responded | interview | offer.
    stage: str
    # Which board column it belongs in, which is a different question and a
    # different five: applied | screening | interview | offer | rejected.
    board_stage: str
    # True when the board must refuse to move this card — the contact opted out
    # of email, and their stage is not the user's to change.
    board_locked: bool = False
    first_sent_at: datetime | None = None
    last_activity_at: datetime | None = None
    replied_at: datetime | None = None
    message_count: int = 0
    follow_ups_scheduled: int = 0
    next_follow_up_at: datetime | None = None
    fit_score: float | None = None


class FilterOptions(BaseModel):
    """Distinct values present in this user's data, for the filter controls."""

    companies: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    campaigns: list[dict] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)


class DashboardOut(BaseModel):
    stats: DashboardStats
    pipeline: list[PipelineStage] = Field(default_factory=list)
    # The board's columns, in the order they are drawn. Sent with the read so
    # the client renders whatever columns the server has rather than a copy of
    # them that drifts the first time one is renamed.
    board: list[BoardColumn] = Field(default_factory=list)
    applications: list[ApplicationRow] = Field(default_factory=list)
    activity: list[ActivityEvent] = Field(default_factory=list)
    filters: FilterOptions = Field(default_factory=FilterOptions)
    # True when `applications` was truncated by `limit`/`offset` — the funnel
    # and stats always describe the full filtered set regardless.
    has_more: bool = False
