"""What the user thought of a drafted reply, and what that taught us.

Every drafted reply already ends one of three ways, and all three are API calls
the product handles: approve it, discard the draft, dismiss the message. None of
them was recorded as a *judgement*. The row's ``kind`` and
``classification_confidence`` were frozen at classify time and nothing ever went
back to them, so a user who rejected eleven drafts from the same sending platform
kept getting a twelfth.

Two tables here, split for the reason the codebase already splits
:attr:`Email.open_count` from ``email_events``: one is the audit trail, the other
is the number read on the hot path.

:class:`ReplyFeedback`
    One immutable row per signal — what happened, to which message, with what
    the classifier had thought at the time. This is what answers "why did the
    product get more confident about this sender?" months later, and it is
    never updated or aggregated in place.

:class:`ClassifierPrior`
    The rolled-up counters actually read while classifying. Keyed on a *scope*
    rather than a sender, because two questions are being asked: a specific
    recruiter you have approved three drafts from is evidence about them, and a
    domain you have rejected eleven messages from is evidence about a whole
    sending platform.

The effect of a prior is bounded and asymmetric — see
:mod:`app.services.classifier_feedback` for the arithmetic and, more importantly,
for the invariant that keeps it safe: feedback may always make the product
quieter, and may never on its own make it send something unread.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin
from app.models.recruiter_email import RecruiterEmailKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class FeedbackSignal(str, enum.Enum):
    """Which way the user went on a drafted reply."""

    APPROVED = "APPROVED"  # they sent it — the read was right
    REJECTED = "REJECTED"  # they discarded it — the read was wrong, or the reply was


class PriorScope(str, enum.Enum):
    """How specific a piece of learned evidence is.

    ``ADDRESS`` beats ``DOMAIN`` when both exist: one recruiter's track record
    says more about their next message than their employer's mail server does.
    """

    ADDRESS = "ADDRESS"
    DOMAIN = "DOMAIN"


class ReplyFeedback(Base, TimestampMixin):
    """One user judgement on one drafted reply. Written once, never updated."""

    __tablename__ = "reply_feedback"
    __table_args__ = (
        Index("ix_reply_feedback_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Null once the message it judged is deleted. The judgement itself survives:
    # what was learned should not be unlearned by tidying up an inbox.
    recruiter_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("recruiter_emails.id", ondelete="SET NULL"), index=True
    )

    signal: Mapped[FeedbackSignal] = mapped_column(
        SAEnum(FeedbackSignal, native_enum=False, length=16), nullable=False
    )
    # Which endpoint the signal came from — "review_approve", "review_dismiss",
    # "inbox_dismiss". Worth storing: dismissing a *message* and discarding a
    # *draft* mean subtly different things, and only the raw row can tell them
    # apart after the fact.
    source: Mapped[str | None] = mapped_column(String(32))

    # What the classifier had thought, as of the moment of the judgement. Copied
    # rather than joined, because the point of the row is to record a
    # disagreement with a specific reading — and re-running the classifier later
    # would produce a different one.
    kind: Mapped[RecruiterEmailKind | None] = mapped_column(
        SAEnum(RecruiterEmailKind, native_enum=False, length=24)
    )
    classification_confidence: Mapped[float | None] = mapped_column(Float)

    sender_address: Mapped[str | None] = mapped_column(String(320), index=True)
    sender_domain: Mapped[str | None] = mapped_column(String(255), index=True)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<ReplyFeedback user={self.user_id} {self.signal.value} "
            f"from={self.sender_address!r}>"
        )


class ClassifierPrior(Base, TimestampMixin):
    """Rolled-up feedback for one sender or one domain, for one user.

    Denormalised from :class:`ReplyFeedback` on purpose: this is read during
    classification of every inbound message, and a ``COUNT(*)`` over the audit
    table per message is a query that gets slower exactly as the feature gets
    more useful.

    Scoped per user and never shared. One candidate's opinion of an agency is
    not evidence about anyone else's mail, and a global prior would let one
    user's rejections silence another user's opportunities.
    """

    __tablename__ = "classifier_priors"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "scope", "value", name="uq_classifier_prior_user_scope_value"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scope: Mapped[PriorScope] = mapped_column(
        SAEnum(PriorScope, native_enum=False, length=8), nullable=False
    )
    # The address or the domain, lowercased by the writer.
    value: Mapped[str] = mapped_column(String(320), nullable=False)

    approvals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejections: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<ClassifierPrior {self.scope.value}={self.value!r} "
            f"+{self.approvals}/-{self.rejections}>"
        )


__all__ = ["ClassifierPrior", "FeedbackSignal", "PriorScope", "ReplyFeedback"]
