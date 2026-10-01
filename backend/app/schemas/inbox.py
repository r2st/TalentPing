"""Inbox schemas — every email on a thread, as the user reads them.

The review queue answers "what am I being asked to approve?". The inbox answers
"what has been said?" — both halves of it: the outreach and follow-ups sent on
the user's behalf, and whatever came back. It is thread-shaped rather than
draft-shaped, and a thread counts as inbox material the moment a message exists
on it, replied to or not.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.enums import FilterEnum
from app.models.application import ApplicationStatus
from app.models.email import EmailDirection, EmailStatus, ReplyIntent, ReplyTemplate


class DirectionFilter(FilterEnum):
    """Which side of the conversation the list is narrowed to.

    A thread matches ``SENT`` when anything has gone out on it and ``RECEIVED``
    when anything has come back, so a thread with a reply matches both.
    """

    ALL = "all"
    SENT = "sent"
    RECEIVED = "received"


class AttachmentItem(BaseModel):
    """One queued file, described well enough for the UI to act on it.

    ``index`` is the position the sender will attach it at, and the position
    ``GET /inbox/emails/{id}/attachments/{index}`` resolves the bytes at.
    ``kind`` is ``resume``, ``cover_letter`` or ``upload`` — what decides which
    control the compose UI offers. ``attachment_id`` is set on uploads alone:
    the row to delete, so removing a file never depends on a position that may
    have shifted since the page rendered.
    """

    index: int
    filename: str
    kind: str
    attachment_id: int | None = None


class ResumeOption(BaseModel):
    """One of the candidate's resumes, as the compose picker lists it."""

    id: int
    label: str
    filename: str | None = None
    is_default: bool = False
    # False for a resume uploaded before the original bytes were kept — it can
    # still be sent, but as a re-render rather than as the user's own file.
    has_original_file: bool = False


class EmailAttachmentsOut(BaseModel):
    """Everything queued on one draft, and everything it could carry instead."""

    email_id: int
    # Whether this message can still be changed. False once it is out of the
    # user's hands: the list is then a record, not a plan.
    editable: bool = False
    files: list[AttachmentItem] = Field(default_factory=list)
    # Why no resume is queued, when none is.
    note: str | None = None
    # The resume the user pinned, if they pinned one. Null means the pipeline
    # is still choosing.
    resume_id: int | None = None
    # True when the user removed the resolved resume outright.
    resume_removed: bool = False
    resume_options: list[ResumeOption] = Field(default_factory=list)


class AttachmentSettings(BaseModel):
    """Which resume a draft should carry.

    ``resume_id`` is tri-state on purpose: an id pins that document, ``null``
    hands the choice back to the pipeline, and omitting the field leaves the
    current pin alone.
    """

    resume_id: int | None = None


class InboxMessage(BaseModel):
    """One message in a conversation, inbound or outbound."""

    id: int
    direction: EmailDirection
    status: EmailStatus
    from_address: str | None = None
    to_address: str | None = None
    subject: str | None = None
    body_text: str | None = None
    intent: ReplyIntent | None = None
    # How sure the classifier was, 0..1. On an inbound message it is the reading
    # of that message; on a draft it is the reading of the message it answers —
    # the number the routing policy judged. Null on anything classified before
    # confidence was recorded.
    intent_confidence: float | None = None
    # True for a reply the agent wrote that is still awaiting approval.
    is_draft: bool = False
    # True for a draft the agent declined to send on its own. The inbox badges
    # these; ``attention_reason`` is the sentence on the badge.
    needs_attention: bool = False
    attention_reason: str | None = None
    # Which brief Scout wrote to, and the one-line rationale shown beside it.
    # Both null for anything a human composed, so "Scout suggests" and "you
    # wrote this" stay distinguishable in the UI.
    draft_template: ReplyTemplate | None = None
    draft_note: str | None = None
    # The files this message carries. On a draft these are resolved fresh — what
    # *would* travel if it were approved now — because attachments are resolved
    # at send time and a draft otherwise had nothing on screen to show a resume
    # was coming. On a sent message it is the recorded filename: what actually
    # went, not what would go today.
    attachments: list[str] = Field(default_factory=list)
    # Why no resume is queued on a draft, when none is.
    attachment_note: str | None = None
    # The same files, described rather than just named — what the compose UI
    # needs to offer remove and swap controls. Only populated on a draft: a sent
    # message has nothing left to change, and a received one carries the
    # sender's files, which are not ours to edit.
    attachment_items: list[AttachmentItem] = Field(default_factory=list)
    read_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime


class InboxThread(BaseModel):
    """A conversation, summarised for the thread list."""

    thread_id: int
    application_id: int
    subject: str | None = None

    company: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None
    role: str | None = None
    application_status: ApplicationStatus

    # How the recruiter's most recent message was classified. Null until one
    # writes back.
    last_intent: ReplyIntent | None = None
    # The most recent message either way, so an outreach-only thread still shows
    # what was said.
    snippet: str | None = None
    # Which side spoke last — what the list badges "sent" from.
    last_direction: EmailDirection | None = None

    message_count: int = 0
    inbound_count: int = 0
    # Outbound mail that is real: sent, queued or failed. Drafts are excluded —
    # they are counted by ``draft_email_id`` and the Drafts view instead.
    outbound_count: int = 0
    unread_count: int = 0
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    # Newest activity in either direction; what the list is ordered by.
    last_activity_at: datetime | None = None
    last_message_at: datetime | None = None

    # The reply draft waiting on this thread, if the agent wrote one. Approving
    # or discarding it goes through the existing /review endpoints.
    draft_email_id: int | None = None
    # True when that draft was held back for the user rather than sent. What the
    # "Needs review" filter selects and the row badges.
    needs_attention: bool = False
    attention_reason: str | None = None

    # How long a *live* conversation has sat on the candidate's last word —
    # they replied, we answered, and nothing has come back since. Null whenever
    # nobody is waiting: cold outreach (the follow-up sequence owns that), a
    # thread where they spoke last, one with a draft ready to send, and anything
    # the recruiter already ended. See `routers/inbox._quiet_days`.
    #
    # Zero is a real value — a conversation that went quiet today — so the flag
    # beside it is what the UI should badge on, not `quiet_days > 0`.
    quiet_days: int | None = None
    # Whether `quiet_days` has passed the server's threshold. Decided server-side
    # so the chip's count and the row's badge cannot disagree.
    gone_quiet: bool = False


class InboxThreadDetail(InboxThread):
    """A conversation with its full message history, oldest first."""

    messages: list[InboxMessage] = Field(default_factory=list)


class InboxCounts(BaseModel):
    """Headline numbers for the inbox, computed before any filter is applied."""

    threads: int = 0
    # Threads with outbound / inbound mail. They overlap: a replied-to thread is
    # in both, which is what makes All ≤ sent + received.
    sent: int = 0
    received: int = 0
    unread: int = 0
    # Threads carrying a reply draft that still needs the user's approval.
    awaiting_reply: int = 0
    # The subset of those the agent deliberately held back — a rejection to
    # answer, an offer, a reading it wasn't sure enough of. Always <=
    # ``awaiting_reply``: every held reply is a waiting draft, and now that most
    # replies send themselves, this is the number that actually wants the user.
    needs_attention: int = 0
    # Live conversations sitting on the candidate's last word past the
    # threshold. Not a subset of anything else here: a thread with a draft
    # waiting is excluded by construction, and so is cold outreach.
    gone_quiet: int = 0
    by_intent: dict[str, int] = Field(default_factory=dict)


class InboxOut(BaseModel):
    counts: InboxCounts
    threads: list[InboxThread] = Field(default_factory=list)
    # How many conversations the current filters select, which is not
    # `counts.threads` — that one ignores the filters on purpose, so the chips
    # can say what they would reveal. This one says whether `threads` is all of
    # what was asked for. Mirrored in `X-Total-Count` / `X-Has-More`; carried in
    # the body as well because this response is already a wrapper, and a client
    # that reads the counts out of it should not have to reach for headers to
    # find out the list is short.
    matched: int = 0
    has_more: bool = False
    # The threshold behind `counts.gone_quiet`, so the UI can caption the chip
    # with the real number instead of hard-coding one that drifts from the
    # server's.
    quiet_after_days: int = 7


class InboxSyncOut(BaseModel):
    """Result of asking Gmail for new messages right now."""

    threads_polled: int = 0
    # True when the work was handed to Celery instead of run inline.
    dispatched: bool = False
    errors: int = 0
    # Threads left unpolled because an inline sync is capped — never silently:
    # the UI says so, and a worker (or another click) picks them up.
    threads_skipped: int = 0
