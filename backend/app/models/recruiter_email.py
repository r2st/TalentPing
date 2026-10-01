"""Inbound recruiter mail — the messages we did not start.

Everything else in this product is outbound-first: a campaign creates an
:class:`~app.models.application.Application`, the application creates an
:class:`~app.models.email_thread.EmailThread`, and the inbox poller walks those
threads. A recruiter who writes *first* has no thread, so
:mod:`app.tasks.inbox_tasks` never sees them — its push handler says as much
before skipping the message ("mail this product didn't send and isn't
tracking"). That comment was true; this table is what makes it no longer the
whole story.

One row per detected inbound message, scoped to the user whose mailbox it landed
in. The row is the audit trail as much as the work item: it records what the
classifier thought, which profile the deterministic scorer matched, which
confidence band that produced, and what was done about it. A user who asks "why
did you answer this one and flag that one?" gets the answer off the row rather
than out of a log file.

Three fields look redundant and are not:

``kind``    what the message *is* (recruiter, job alert, ATS noise).
``route``   which confidence band fired — the decision.
``status``  where the work item is now — the lifecycle.

They move independently. A ``RECRUITER_OUTREACH`` routed to ``DRAFT`` sits at
``DRAFTED`` until the user approves it and then at ``REPLIED``; the kind and the
route never change again, which is exactly what makes the row auditable.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.core.enums import FilterEnum
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class RecruiterEmailKind(FilterEnum):
    """What the classifier decided the message is.

    The two actionable kinds are separated because they are not the same
    conversation: an in-house hiring manager is the decision maker, an agency
    recruiter is an intermediary, and a reply that treats one as the other reads
    wrong. Everything else exists so the counts are honest — a message we chose
    not to act on is recorded as such rather than silently dropped.
    """

    RECRUITER_OUTREACH = "RECRUITER_OUTREACH"
    HIRING_MANAGER = "HIRING_MANAGER"
    JOB_ALERT = "JOB_ALERT"
    ATS_AUTOMATED = "ATS_AUTOMATED"
    NOT_RECRUITER = "NOT_RECRUITER"
    # The classifier could not decide. Never actionable: a degraded classifier
    # may cost us speed, it must not be able to send mail.
    UNKNOWN = "UNKNOWN"


#: Kinds a reply may ever be written for.
ACTIONABLE_KINDS = frozenset(
    {RecruiterEmailKind.RECRUITER_OUTREACH, RecruiterEmailKind.HIRING_MANAGER}
)


class ReplyRoute(str, enum.Enum):
    """Which confidence band the message fell into."""

    AUTO = "AUTO"    # reply generated and sent without review (opt-in; see config)
    DRAFT = "DRAFT"  # reply generated, waiting in the review queue
    FLAG = "FLAG"    # recorded for a human; nothing written, nothing sent


class RecruiterEmailStatus(str, enum.Enum):
    DETECTED = "DETECTED"          # stored, not yet processed
    CLASSIFIED = "CLASSIFIED"      # processed; no reply warranted
    FLAGGED = "FLAGGED"            # needs a human look
    DRAFTED = "DRAFTED"            # a reply is waiting in /review
    REPLY_QUEUED = "REPLY_QUEUED"  # auto-reply handed to the throttled sender
    REPLIED = "REPLIED"            # reply confirmed sent
    IGNORED = "IGNORED"            # user dismissed it
    FAILED = "FAILED"              # pipeline error; see last_error


class RecruiterEmail(Base, TimestampMixin):
    __tablename__ = "recruiter_emails"
    __table_args__ = (
        # What makes the scanner safely re-runnable: a message seen twice is a
        # no-op rather than a duplicate row (and, worse, a duplicate reply).
        UniqueConstraint(
            "user_id", "gmail_message_id", name="uq_recruiter_email_user_message"
        ),
        Index("ix_recruiter_emails_user_status", "user_id", "status"),
        # The Recruiter Inbox's own sort. `routers/recruiter_inbox` orders every
        # page by `coalesce(received_at, created_at) DESC, id DESC` — when the
        # recruiter sent it, falling back to when we noticed, with the id
        # breaking ties so two pages of a same-second batch cannot repeat a row.
        #
        # The index above cannot serve that: it locates the user's rows and then
        # the database sorts all of them, every time, to hand back twenty. The
        # number of rows it sorts is the number of messages the mailbox has ever
        # detected, which is a function of how long the account has been watched
        # rather than of anything the reader did — so the first page got slower
        # every week whether or not the user ever scrolled past it.
        #
        # Expression index because the sort key is an expression: a plain
        # `(user_id, received_at)` would not match it, and `received_at` is null
        # on exactly the rows the coalesce exists for.
        Index(
            "ix_recruiter_emails_user_received",
            "user_id",
            text("coalesce(received_at, created_at) DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Which connected mailbox it arrived in. A user may have several, and the
    # reply must go back out of the one the recruiter wrote to.
    #
    # SET NULL, matching ``recruiter_scan_runs``. It was CASCADE, which meant
    # disconnecting a mailbox deleted every inbound message ever detected in it —
    # the detection history and the classifier's audit trail with it. Survivable
    # when disconnecting was rare and terminal; not when removing a mailbox you
    # no longer use is a routine act.
    gmail_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="SET NULL"), index=True
    )

    gmail_message_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # The RFC 5322 ``Message-ID`` and ``References`` headers, kept because
    # ``gmail_thread_id`` only threads for people reading in Gmail. Outlook,
    # Apple Mail and every ATS that ingests mail thread on these two headers, so
    # a reply that carries only the Gmail thread id shows up in half the world's
    # clients as a brand-new message that happens to share a subject line.
    # 998 is the RFC's own line-length ceiling.
    rfc_message_id: Mapped[str | None] = mapped_column(String(998))
    rfc_references: Mapped[str | None] = mapped_column(Text)

    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(200))
    # The address a reply actually has to go to, when the sender said so and it
    # isn't the From. Recruiting platforms (Gem, Loxo, Bullhorn, most in-house
    # ATS mail merges) send from an unmonitored address and set Reply-To to the
    # human. Storing it separately keeps the From honest in the UI — the user
    # sees who the message came from — while the reply still reaches a person.
    reply_to_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(998))
    body_text: Mapped[str | None] = mapped_column(Text)
    # Denormalised first slice of the body so the list view never loads bodies.
    snippet: Mapped[str | None] = mapped_column(String(500))
    # Gmail's own timestamp, not poll time — the same rule inbox_tasks follows,
    # so a week-old message backfilled today still sorts as a week old.
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- Classification ----
    kind: Mapped[RecruiterEmailKind] = mapped_column(
        SAEnum(RecruiterEmailKind, native_enum=False, length=24),
        default=RecruiterEmailKind.UNKNOWN,
        nullable=False,
    )
    # 0..1 — the classifier's confidence in `kind`, not in the match.
    classification_confidence: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    # What the user's own history with this sender did to that number, in the
    # same 0..1 units and signed. Stored *beside* the classifier's reading rather
    # than folded into it: the audit trail should still show what the model
    # thought, and "we got more confident because you approved two of these"
    # is a different fact from "the model was sure". See services.classifier_feedback.
    confidence_adjustment: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    # "openrouter:openai/gpt-oss-20b:free" when a model read it, otherwise one of
    # ``recruiter_classifier``'s ``BY_*`` markers. Worth storing: it is the first
    # thing to look at when a classification looks wrong, and it is what
    # ``recruiter_classifier.is_degraded`` reads to decide whether this verdict
    # was a guess made during an outage and should be asked again. Rows written
    # before those markers existed all say "rules"; see that function for how the
    # pre-filter and the fallback are still told apart.
    classified_by: Mapped[str | None] = mapped_column(String(120))
    # {role_title, company, location, remote, salary_text, seniority, asks}
    # Every field best-effort and nullable — the prompt forbids inference.
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # What the recruiter attached, described but not downloaded, as
    # ``[{filename, mime_type, attachment_id, size}]``. Carried here because the
    # scan is the only place the raw Gmail message is in hand, and the
    # conversation's ``Email`` row — which is what the inbox reads — is built
    # from this row long afterwards. See :class:`app.models.email.Email`.
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False, server_default="[]"
    )

    # ---- Matching ----
    matched_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL"), index=True
    )
    # 0..100, from the deterministic scorer — directly comparable with every
    # other fit score in the product.
    match_score: Mapped[float | None] = mapped_column(Float)
    match_reason: Mapped[str | None] = mapped_column(Text)

    # ---- Routing ----
    # classification_confidence * match_score, on a 0-100 scale. Multiplicative
    # rather than averaged: a confident read of a bad fit must not clear the bar
    # by being averaged with a good number. See services.reply_routing.
    route_confidence: Mapped[float | None] = mapped_column(Float)
    route: Mapped[ReplyRoute | None] = mapped_column(
        SAEnum(ReplyRoute, native_enum=False, length=8)
    )
    status: Mapped[RecruiterEmailStatus] = mapped_column(
        SAEnum(RecruiterEmailStatus, native_enum=False, length=16),
        default=RecruiterEmailStatus.DETECTED,
        nullable=False,
    )
    # Why a human is needed, in the words the UI shows verbatim.
    flag_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    # When the user read a reply we had written for this message and threw it
    # away. A separate axis from ``status`` for the same reason ``escalated`` is
    # one: the discard is a decision about *this message* that outlives whatever
    # state the row later reaches, and the state it lands in immediately
    # afterwards (``FLAGGED``) is one the retry sweep is specifically built to
    # pick up and re-answer. Recorded here rather than read back out of
    # ``reply_feedback``, because that table is a *learning* signal behind
    # ``recruiter_feedback_enabled`` and turning the learning off must not turn
    # a user's refusal into an invitation to try again. See
    # :func:`app.services.recruiter_reply_service.record_draft_discarded`.
    draft_discarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # ---- Follow-ups ----
    # A recruiter we already answered has written again. Deliberately a separate
    # axis from ``status`` rather than a new member of it: ``kind``/``route``/
    # ``status`` record a decision that was made at a moment in time and must not
    # change retroactively, and a message that was correctly REPLIED last week is
    # still correctly REPLIED after the recruiter follows up. What changed is
    # that it now wants a human.
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    # How many times they have come back on the conversation this row started.
    follow_up_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The earlier message from this same sender that we already answered. Set on
    # the *follow-up* row, pointing back — so "why was this one flagged?" is one
    # hop rather than a search.
    previous_recruiter_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("recruiter_emails.id", ondelete="SET NULL"), index=True
    )

    # ---- Attachment choice ----
    # Which resume the reply should carry, chosen against this specific role
    # rather than taken from the matched profile by default. Null means "resolve
    # it the ordinary way" — a user with one resume, or the selector switched
    # off, changes nothing. See services.resume_selector.
    selected_resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )
    # Why that document won, in the words the review screen shows.
    resume_choice_reason: Mapped[str | None] = mapped_column(Text)

    # ---- Links into the existing pipeline ----
    # The generated reply (draft or sent). Approving it goes through the review
    # endpoints that already exist — this is only the pointer.
    reply_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL"), index=True
    )
    # Set once we engage: from here on the conversation is an ordinary thread and
    # inbox_tasks.poll_thread owns it.
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), index=True
    )

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="recruiter_emails")

    @property
    def is_actionable(self) -> bool:
        """True when this kind of message may ever be replied to."""
        return self.kind in ACTIONABLE_KINDS

    @property
    def needs_attention(self) -> bool:
        """True when this row belongs in the "Needs you" view.

        Either the product declined to answer, or it answered and the recruiter
        came back. The second case is why this is a property rather than a
        status check: an escalated row still reads ``REPLIED``, which is the
        truth about what happened and not the truth about what to do next.
        """
        return self.escalated or self.status in (
            RecruiterEmailStatus.FLAGGED,
            RecruiterEmailStatus.FAILED,
        )

    @property
    def reply_address(self) -> str:
        """Where an answer to this message goes.

        ``Reply-To`` when the sender set one, otherwise the ``From``. This is the
        only address the reply pipeline should ever use: writing back to a
        ``noreply@`` From is a message nobody reads.
        """
        return (self.reply_to_address or self.from_address or "").lower()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<RecruiterEmail id={self.id} from={self.from_address!r} "
            f"kind={self.kind.value} route={self.route.value if self.route else None}>"
        )


class RecruiterReplyPreference(Base, TimestampMixin):
    """Per-user switches for inbound scanning, and the scan bookkeeping.

    Kept out of :class:`~app.models.autopilot.AutopilotPreference` on purpose.
    That row governs *outbound* behaviour — who to chase, how selectively, how
    often — and a user may reasonably want their inbox watched while the
    autopilot is off, or the reverse. Sharing a row would make one switch imply
    the other.

    ``enabled`` defaults off. The feature ships inert and inbox watching is
    turned on deliberately, the same way ``form_apply_enabled`` and the LinkedIn
    switches are.

    ``auto_reply_enabled`` defaults *on*, to match outreach: someone who has
    asked for their inbox to be watched gets the same hands-off default for
    replies that ``AutopilotPreference.auto_send`` already gives first contact.
    It is inert on its own — it does nothing until ``enabled`` is also on, and it
    additionally requires the server-side ``recruiter_reply_auto_enabled`` before
    anything is ever sent unreviewed. The consequence worth knowing: turning
    watching on is now the single act that arms both, so that switch means "watch
    and reply" unless the user separately says otherwise.
    """

    __tablename__ = "recruiter_reply_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    # Watch this user's inbox at all.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Allow the top confidence band to send without review. Requires `enabled`
    # *and* the server-wide switch; either one off means every reply is drafted.
    # Defaults on — see the class docstring. Note this is the ORM default, so it
    # only applies to rows created from here; the column's server_default is
    # still false, which is what existing rows keep.
    auto_reply_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lifetime counters, for the Setup page's "3 detected this week" line and for
    # answering "is this thing actually doing anything?" without a query.
    detected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replied_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="recruiter_reply_preference")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<RecruiterReplyPreference user_id={self.user_id} "
            f"enabled={self.enabled} auto={self.auto_reply_enabled}>"
        )
