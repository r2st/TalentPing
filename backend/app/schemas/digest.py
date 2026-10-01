"""Weekly digest schemas — preferences, and a preview of Monday's email."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class DigestDraft(BaseModel):
    """One draft named in the digest's "waiting for you" list."""

    email_id: int
    company: str | None = None
    subject: str | None = None
    kind: str  # reply | outreach
    waiting_days: int = 0


class DigestOpportunity(BaseModel):
    """One new role the digest names, with the number that ranked it."""

    job_id: int
    title: str | None = None
    company: str | None = None
    # "Remote", the posting's own city, or "Location not stated" — resolved
    # server-side so the email and the preview cannot disagree about it.
    where: str = ""
    fit_score: float | None = None
    remote: bool = False


class DigestPipeline(BaseModel):
    """The shape of the pipeline, not the week's activity.

    ``active`` and ``stall_rate`` are derived server-side rather than left to
    the client: they are also what the rendered email says, and two places
    computing "active" from five counts is two places that can define it
    differently.
    """

    awaiting_reply: int = 0
    in_conversation: int = 0
    interviewing: int = 0
    offers: int = 0
    closed: int = 0
    # Sent, silent for STALL_DAYS, and with no follow-up on the books. The
    # number this block exists for — nothing else in the product names it.
    stalled: int = 0
    active: int = 0
    stall_rate: float = 0.0


class DigestContent(BaseModel):
    """Everything the email says, before it is rendered.

    Returned by the preview endpoint so the user can see exactly what will land
    in their inbox — including the rendered subject and body — before agreeing
    to receive it weekly.
    """

    period_start: datetime
    period_end: datetime

    sent: int = 0
    follow_ups_sent: int = 0
    jobs_found: int = 0
    replies: int = 0
    interviews: int = 0

    drafts_waiting: int = 0
    drafts: list[DigestDraft] = Field(default_factory=list)
    # Threads whose newest message is inbound — a recruiter is waiting.
    unanswered_replies: int = 0

    queued_emails: int = 0
    follow_ups_due: int = 0

    opportunities: list[DigestOpportunity] = Field(default_factory=list)
    pipeline: DigestPipeline = Field(default_factory=DigestPipeline)

    response_rate: float = 0.0
    previous_response_rate: float = 0.0
    # The one line at the top, chosen by what the user can act on this week.
    headline: str = ""
    # True when nothing happened and nothing is waiting. The beat task skips
    # these: a digest of seven zeroes teaches the user to delete it unread.
    is_quiet: bool = False

    subject: str
    body_text: str
    # The HTML part, verbatim. Returned so the preview can show the mail as it
    # will actually arrive rather than a text approximation of it — the point of
    # a preview is that the user reads the real email before agreeing to receive
    # it weekly, and for most of them the real email is the HTML one.
    #
    # This is a fragment (no <html>/<head>), which is what the multipart message
    # carries. It must never be injected into the app's own DOM; the settings
    # page renders it inside a sandboxed iframe.
    body_html: str = ""


class DigestPreferenceOut(BaseModel):
    enabled: bool = True
    weekday: int = 0  # 0 = Monday, matching date.weekday()
    hour: int = 8     # UTC — see models/digest.py for why not local time
    last_sent_at: datetime | None = None
    last_error: str | None = None
    sent_count: int = 0
    # Whether a digest could actually be delivered right now. A user with no
    # connected mailbox has nothing to send from, and the settings page should
    # say so rather than showing a switch that silently does nothing.
    can_send: bool = False

    model_config = {"from_attributes": True}


class DigestPreferenceUpdate(BaseModel):
    enabled: bool | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    hour: int | None = Field(default=None, ge=0, le=23)


class DigestSendResult(BaseModel):
    # sent | unsubscribed | not_due | claimed_elsewhere | no_mailbox |
    # mailbox_paused | skipped_quiet | failed
    status: str
    to: str | None = None
    error: str | None = None
