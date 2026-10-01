"""Individual email message model."""
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
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.core.enums import FilterEnum
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.email_attachment import EmailAttachment
    from app.models.email_event import EmailEvent
    from app.models.email_thread import EmailThread


class EmailDirection(str, enum.Enum):
    SENT = "SENT"
    RECEIVED = "RECEIVED"


class EmailStatus(str, enum.Enum):
    DRAFT = "DRAFT"       # generated, awaiting user approval
    QUEUED = "QUEUED"     # approved, waiting for the throttled sender
    SENT = "SENT"
    FAILED = "FAILED"
    RECEIVED = "RECEIVED"  # inbound message


class ReplyIntent(FilterEnum):
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    SCHEDULING = "SCHEDULING"
    QUESTION = "QUESTION"
    OFFER = "OFFER"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    OTHER = "OTHER"


class ReplyTemplate(str, enum.Enum):
    """The angle a drafted reply takes.

    Derived from the classified intent but not identical to it: an OFFER reply
    where the numbers are below the candidate's market band becomes
    ``SALARY_NEGOTIATION`` rather than a plain acknowledgement, and a thread we
    are chasing rather than answering becomes ``FOLLOW_UP``. Stored on the draft
    so the user can see which brief the agent wrote to, and change it.
    """

    INTERESTED = "INTERESTED"
    SCHEDULING = "SCHEDULING"
    SALARY_NEGOTIATION = "SALARY_NEGOTIATION"
    DECLINING = "DECLINING"
    FOLLOW_UP = "FOLLOW_UP"
    QUESTION = "QUESTION"


class Email(Base, TimestampMixin):
    __tablename__ = "emails"

    # Backstop, not the primary defense — see recruiter_reply_service._write_reply
    # for where duplicate rows actually got made. A message id is only unique
    # within the mailbox that issued it, but this app has one row of state per
    # physical email regardless of who or what path stored it, so a second row
    # for an id already on file is always a bug, never a legitimate second
    # message. Partial because most rows here are outbound mail we composed,
    # which never had a Gmail id to begin with.
    __table_args__ = (
        Index(
            "uq_emails_gmail_message_id",
            "gmail_message_id",
            unique=True,
            postgresql_where=text("gmail_message_id IS NOT NULL"),
            sqlite_where=text("gmail_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("email_threads.id", ondelete="CASCADE"), index=True, nullable=False
    )

    direction: Mapped[EmailDirection] = mapped_column(
        SAEnum(EmailDirection, native_enum=False, length=12), nullable=False
    )
    status: Mapped[EmailStatus] = mapped_column(
        SAEnum(EmailStatus, native_enum=False, length=12),
        default=EmailStatus.DRAFT,
        nullable=False,
    )

    from_address: Mapped[str | None] = mapped_column(String(320))
    to_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(998))
    body_text: Mapped[str | None] = mapped_column(Text)

    # For inbound messages: LLM-classified intent + sentiment.
    intent: Mapped[ReplyIntent | None] = mapped_column(
        SAEnum(ReplyIntent, native_enum=False, length=16)
    )
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    # How sure the classifier was of ``intent``, 0..1 — see
    # :mod:`app.services.reply_classifier`. Carried on the inbound row that was
    # classified *and* copied onto the reply drafted from it, because the
    # question the reply is routed on ("may this go out unread?") is asked about
    # the draft and answered by the number that belongs to the message it
    # answers. Null on every row written before routing existed, and a null is
    # read as "unknown", never as "sure".
    intent_confidence: Mapped[float | None] = mapped_column(Float)

    # ---- Held for a human ----
    # Set on a drafted reply the routing policy declined to send unread. The
    # inbox badges it and the "Needs review" filter selects it.
    #
    # Not derivable from ``status`` alone: every draft is DRAFT, and this
    # distinguishes "the agent stopped short of sending this" from "nothing has
    # tried to send it yet" — a distinction the review queue has no other way to
    # make now that most replies leave on their own.
    needs_attention: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    # One line, in the user's words, for why it was held — "the classifier and
    # the rules disagreed", "an offer is always yours to answer". Shown on the
    # badge. Null when nothing held it.
    attention_reason: Mapped[str | None] = mapped_column(String(160))

    # ---- Agent-drafted outbound mail ----
    # Which brief the reply agent wrote to. Null for outreach and for anything a
    # human composed, so "this was drafted for you" and "you wrote this" stay
    # distinguishable in the inbox.
    draft_template: Mapped[ReplyTemplate | None] = mapped_column(
        SAEnum(ReplyTemplate, native_enum=False, length=24)
    )
    # One line explaining the angle, shown under the "Scout suggests" label —
    # e.g. what the counter-offer is anchored on. Never sent; UI only.
    draft_note: Mapped[str | None] = mapped_column(Text)
    # How the body was written: ``"llm"`` for a generation, ``"heuristic"`` for
    # the deterministic template ``reply_agent`` falls back to. It is
    # ``ReplyDraft.generated_with``, kept on the row it produced.
    #
    # This is a safety property, not a statistic. ``thread_reply_policy`` refuses
    # to auto-send a template — a reply that is contextual only in its shape is
    # a fine thing to show someone under "Scout suggests" and not a thing to
    # send in their name — and that rule can only be enforced by a caller that
    # knows which one it is holding. The re-evaluation task reads drafts written
    # minutes or weeks earlier, so before this column it had to *assume*, and
    # the drafts it was assuming about were written during the outage that made
    # them templates in the first place.
    #
    # Null on every row written before the column existed. That holds, rather
    # than guesses — see ``thread_reply_policy.REASON_UNKNOWN_SOURCE``.
    drafted_with: Mapped[str | None] = mapped_column(String(16))
    # The letter to deliver with this outreach, when the user's preference is to
    # attach rather than inline it. Resolved by the sender at send time.
    cover_letter_id: Mapped[int | None] = mapped_column(
        ForeignKey("cover_letters.id", ondelete="SET NULL"), index=True
    )

    # ---- The user's own say over what travels ----
    # The resume the *user* picked for this message, overriding every resolver.
    # Resolution is good — tailored for this posting, else the matched profile's,
    # else the campaign's — but it is inference, and the person whose name is on
    # the document is the authority on which one a recruiter should read. Null
    # means "whatever the pipeline resolves", which is the default and the case
    # for every message nobody has touched.
    attachment_resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )
    # Resolved attachments the user removed, by kind: ``resume``,
    # ``cover_letter``. A list of what is *off* rather than a pair of booleans
    # so nothing has to be migrated when a third kind of resolved file appears —
    # and so an empty list unambiguously means "send everything", which is what
    # every existing row means.
    suppressed_attachments: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False, server_default="[]"
    )

    # The resume that actually went out with this message, filled in by the
    # sender. Null on a sent row means the send genuinely carried no attachment
    # — the distinction the pipeline previously had no way to express.
    attachment_filename: Mapped[str | None] = mapped_column(String(255))

    # What *inbound* mail arrived carrying, as
    # ``[{filename, mime_type, attachment_id, size}]``. The counterpart to
    # ``attachment_filename`` above, which only ever describes what we sent: a
    # recruiter's job spec or contract was invisible in the inbox because
    # nothing on the row could hold it. Only the description is stored — the
    # bytes stay in Gmail and are redeemed by ``attachment_id`` when the user
    # opens one, so a mailbox of large PDFs costs nothing here.
    inbound_attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False, server_default="[]"
    )

    # ---- Open / click tracking (see services/email_tracking.py) ----
    # URL-safe random token identifying this message to the pixel and click
    # endpoints. Deliberately not the row id: an enumerable pixel URL would let
    # anyone inflate a stranger's stats. Null means the message carried no
    # tracking, which is what makes the rate denominators honest.
    tracking_token: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )
    # Denormalized from email_events, which stays the source of truth. The
    # tracker list endpoint is polled and sorts on these; a COUNT(*) per row is
    # a join too far there.
    open_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    click_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Which arm of the campaign's subject-line experiment this message used.
    # Null for messages composed outside a campaign or before the experiment —
    # those are excluded from every rate rather than counted as failures.
    subject_variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("subject_variants.id", ondelete="SET NULL"), index=True
    )

    # Indexed by uq_emails_gmail_message_id above, not a second plain index —
    # the partial unique one covers every lookup a non-unique index would.
    gmail_message_id: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # When a send task for this row was last published to the broker — not when
    # it was sent, and not when the row changed. `sweep_stranded_sends` reads it
    # to tell a message nothing is going to send from one a task is already
    # aiming at; see that task and the c5f2a9d84e17 migration for why the
    # created_at bound it replaced could never re-close.
    send_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Did this go out without a human reading it? Stamped by the sender, from the
    # status the message held when it was queued: an email the user approved in
    # the review queue is False, one auto-send queued straight from the pipeline
    # is True.
    #
    # Not derivable after the fact — approved and auto-sent mail are both
    # ``SENT`` with the same shape — and it has to be a stored fact because
    # ``send_policy``'s daily ceiling counts exactly this. Also the honest
    # denominator for "how much of my outreach have I actually read?".
    auto_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )

    # ---- RFC 5322 threading ----
    # Gmail's ``threadId`` threads the conversation for people reading in Gmail
    # and nowhere else. Outlook, Apple Mail, Thunderbird and every ATS that
    # ingests mail thread on ``In-Reply-To``/``References``, so a reply carrying
    # only the thread id reads as a new message in half of all clients. Set on a
    # reply; null on anything that starts a conversation, which is most outreach.
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    # Deliberately *not* named ``references``: that is a reserved word in both
    # SQLite and Postgres and would need quoting at every call site forever.
    # Holds the full chain, oldest first, space-separated per the RFC.
    email_references: Mapped[str | None] = mapped_column(Text)
    # This message's *own* ``Message-ID``, as the world will see it.
    #
    # Not the same thing as ``gmail_message_id``, which is Gmail's internal API
    # handle ("18f2c9..."), means nothing outside this mailbox, and is not what
    # any recipient's client threads on. The RFC id is what the next message on
    # the thread has to name in ``In-Reply-To``.
    #
    # Only outbound rows use this column. An inbound message's own id is stored
    # in ``in_reply_to`` — a deliberate overload that predates this field, kept
    # because :func:`app.services.thread_headers.headers_for_next_message` is
    # the one place that has to know, and rewriting the convention would mean a
    # backfill of every received row for no behavioural gain.
    #
    # Null on anything sent before this column existed, and on a send whose
    # read-back failed. Both mean "we do not know", and a message with no id to
    # chain from goes out unthreaded rather than pointing at a guess.
    rfc_message_id: Mapped[str | None] = mapped_column(String(998))

    # When the user opened this message in the inbox. Only meaningful for inbound
    # mail — outbound rows are read by definition, so they are left null.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # When a person took this draft in hand: they asked for it to be written
    # (``POST /recruiter-inbox/{id}/generate-reply``), or they edited it
    # (``PATCH /tracker/emails/{id}``). Null on every draft the pipeline wrote
    # and nobody has touched, which is most of them.
    #
    # A guard, not a statistic. Two sweeps walk old drafts and re-decide them —
    # ``recruiter_reply_service.retry_degraded`` rewrites the body and can route
    # the result ``AUTO``, and ``inbox_tasks.reevaluate_pending_replies`` can
    # queue one — and neither could tell a draft the pipeline is still deciding
    # about from one a human has already made a decision about. So the reply the
    # candidate pressed "write me one" for, and the paragraph they rewrote in
    # their own words, were both liable to be silently replaced by a freshly
    # generated one and sent unread in their name.
    #
    # The same shape as ``RecruiterEmail.draft_discarded_at``, and for the same
    # reason: a decision the user made about *this message* has to outlive
    # whatever the classifier later thinks of it. Discarding a draft is the
    # refusal; this is the other half — keeping one.
    user_owned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    thread: Mapped[EmailThread] = relationship(back_populates="emails")
    events: Mapped[list[EmailEvent]] = relationship(
        back_populates="email", cascade="all, delete-orphan", order_by="EmailEvent.id"
    )
    # Files the user attached by hand, in the order they added them — which is
    # the order they travel in, after the resolved documents.
    user_attachments: Mapped[list[EmailAttachment]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        order_by="EmailAttachment.id",
    )
