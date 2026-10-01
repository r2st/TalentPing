"""Review-queue schemas — drafts the user approves before anything is sent."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.application import ApplicationStatus
from app.models.email import ReplyIntent


class ReviewItem(BaseModel):
    """One draft awaiting the user's approval, with the context to judge it."""

    email_id: int
    application_id: int
    thread_id: int
    # "outreach" (first contact) or "reply" (response to a recruiter message).
    kind: str
    to_address: str | None = None
    subject: str | None = None
    body_text: str | None = None

    company: str | None = None
    recruiter_name: str | None = None
    role: str | None = None
    application_status: ApplicationStatus
    # For replies: what the recruiter's message was classified as.
    reply_intent: ReplyIntent | None = None
    incoming_snippet: str | None = None
    # What will travel with this draft when it is approved. Resolved fresh on
    # every read because attachments are resolved at send time, not compose time
    # — a reviewer approving a send needs to see the files it will carry, and
    # until now the answer only existed after the message had gone.
    attachments: list[str] = Field(default_factory=list)
    attachment_note: str | None = None
    # How a spam filter will read this text, scored fresh on every read for the
    # same reason attachments are: the user can edit a draft before approving
    # it, so a score stored at compose time would describe a message that no
    # longer exists. Null when the content is clean, which is the common case.
    content_risk: int = 0
    content_note: str | None = None
    created_at: datetime


class ReviewCount(BaseModel):
    """Just the size of the queue — what a badge needs and nothing else.

    A separate model rather than a bare int so the endpoint can grow a field
    without every caller having to change shape.
    """

    count: int = 0


class ReviewQueue(BaseModel):
    #: The whole queue, not this page — it is what the badges display. A count
    #: that described the page would sit at the page size forever.
    count: int = 0
    #: How many rows are actually in ``items``. Without it a client cannot tell
    #: a full page from a queue that happens to be exactly that long.
    returned: int = 0
    #: True when drafts exist past this page. Mirrors the ``X-Has-More`` header
    #: for clients that read the body rather than the headers.
    has_more: bool = False
    items: list[ReviewItem] = Field(default_factory=list)
    # Where auto-send stands, so the queue can explain why these drafts exist —
    # "you paused auto-send" and "you're on email 2 of 3 of your trial" are very
    # different situations and the pile of drafts looks identical in both.
    auto_send: dict | None = None


# The most drafts one call may approve. Every approval schedules a send with a
# cumulative countdown, so an unbounded batch is an unbounded schedule: past a
# few hundred the tail is a day out, and a broker restart in between strands
# that mail QUEUED with nothing left to re-dispatch it. Both ways of asking —
# an explicit list and a scope — are held to this, which is the point: the cap
# was on the list only, so a scope was the way around it.
BATCH_LIMIT = 200


class BatchApproveRequest(BaseModel):
    """Approve several drafts in one call.

    Either name the ids explicitly, or set ``scope`` to take the whole queue.
    ``scope="outreach"`` deliberately excludes drafted *replies*: a reply is
    addressed to a human who wrote to this user personally, and "approve
    everything" should not be the gesture that sends one unread. Sweeping those
    in takes the explicit ``scope="all"``.

    A scope larger than :data:`BATCH_LIMIT` approves the oldest drafts and
    reports the rest in ``remaining``; call again to continue.
    """

    email_ids: list[int] = Field(default_factory=list, max_length=BATCH_LIMIT)
    scope: str | None = Field(default=None, pattern="^(outreach|all)$")


class BatchApproveResult(BaseModel):
    approved: int = 0
    # email_id -> why it was left alone (not a draft, not yours, already gone).
    skipped: dict[int, str] = Field(default_factory=dict)
    # Drafts a scoped batch left for the next call because it hit BATCH_LIMIT.
    # Zero for an explicit id list, which the caller already bounded itself.
    # Without this a capped batch is indistinguishable from an emptied queue.
    remaining: int = 0
    # True when this batch finished an auto-send trial, so the UI can say so.
    trial_completed: bool = False
    auto_send: dict | None = None
