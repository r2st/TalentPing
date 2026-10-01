"""Tracker + onboarding schemas — the two read models the UI runs on."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.application import ApplicationStatus
from app.models.email import ReplyIntent


class TrackerRow(BaseModel):
    """One outreach thread, flattened for the tracker table."""

    application_id: int
    campaign_id: int
    campaign_name: str | None = None

    recruiter_email: str
    recruiter_name: str | None = None
    company: str | None = None

    status: ApplicationStatus
    subject: str | None = None
    # The outreach we sent (or are about to).
    sent_at: datetime | None = None
    # The most recent inbound reply, if any.
    replied_at: datetime | None = None
    reply_intent: ReplyIntent | None = None
    reply_snippet: str | None = None
    message_count: int = 0
    updated_at: datetime


class TrackerStats(BaseModel):
    contacted: int = 0
    queued: int = 0
    replied: int = 0
    interested: int = 0
    interviews: int = 0
    reply_rate: float = 0.0


class TrackerOut(BaseModel):
    stats: TrackerStats
    rows: list[TrackerRow] = Field(default_factory=list)


class OnboardingStatus(BaseModel):
    """Drives the 4-step wizard: which step is the user on, and can they finish?"""

    gmail_configured: bool  # server has OAuth credentials at all
    gmail_connected: bool
    # The primary's address. Kept singular and kept first because the wizard has
    # always printed it as "Autopilot is running from …", and one address is
    # still the right thing to say when there is one.
    gmail_address: str | None = None
    # Every connected address, primary first. The wizard's copy is the last place
    # in the product that still implied a user has exactly one mailbox: someone
    # who had just added a second was told outreach runs from the first, which
    # is true of new threads and false of every reply.
    gmail_addresses: list[str] = Field(default_factory=list)
    gmail_account_count: int = 0
    resume_count: int = 0
    campaign_count: int = 0
    sent_count: int = 0
    autopilot_configured: bool = False  # user has saved preferences at least once
    autopilot_active: bool = False  # the master switch is on
    # When the agent last finished a cycle. Null right after switching on means
    # the first run is still going, which is the difference between "working on
    # it" and "found you nothing" — the wizard shows those very differently.
    last_run_at: datetime | None = None
    # Outreach drafted and waiting for approval. With the warm-up ramp holding
    # auto-send back, this is what a new user's first cycle usually produces, and
    # a first email nobody is told about may as well not have been written.
    drafts_waiting: int = 0
    # connect_email | upload_resume | set_preferences | start_autopilot | done
    next_step: str
    complete: bool
