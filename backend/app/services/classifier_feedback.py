"""Learning from the user's own verdict on a draft.

Every drafted reply already ends with the user telling us whether the
classification was right — they approve it, they discard the draft, or they
dismiss the message. Until now none of that was recorded, so a user who rejected
eleven drafts from the same sending platform got a twelfth.

**What "learning" means here.** Not fine-tuning and not an embedding index: a
per-user counter of approvals and rejections, kept against the sender's address
and against their domain, converted into a small bounded delta on the
classifier's confidence. That is deliberately the least clever thing that works,
and it is chosen for four properties the clever versions do not have — it is
auditable (every adjustment traces to rows a user can be shown), it works from
the very first signal, it needs no new infrastructure, and it cannot fail in an
interesting way.

"Similar patterns" is read as **the same sender, or the same sending domain**,
which is the similarity a user would actually predict. Address beats domain when
both exist: one recruiter's track record says more about their next message than
their employer's mail server does.

**The arithmetic is asymmetric, by a factor of three.**

    +0.05 per approval, capped at +0.10
    -0.10 per rejection, capped at -0.30

A reply sent in the candidate's name that should not have been is far worse than
a draft they never see, so rejections move the number further and get there
faster.

**And the invariant that keeps this from being a security hole:**

    An upward adjustment can never move a message from DRAFT into AUTO.

Enforced in :func:`adjusted_confidence`'s caller (see
:func:`app.services.recruiter_reply_service.process`), by computing the route
twice and clamping. Without it, "approve four drafts from this recruiter" becomes
a way to teach the product to send unread mail to that recruiter — and the user
doing the approving has no idea that is what they are agreeing to. Downward
adjustments are unrestricted: feedback may always make the product quieter.

Nothing here sends, drafts or classifies. Counters in, a number out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.recruiter_email import RecruiterEmail, RecruiterEmailKind
from app.models.reply_feedback import (
    ClassifierPrior,
    FeedbackSignal,
    PriorScope,
    ReplyFeedback,
)

logger = logging.getLogger(__name__)

#: What one signal is worth. See the module docstring for the asymmetry.
APPROVAL_STEP = 0.05
REJECTION_STEP = 0.10


def domain_of(address: str | None) -> str | None:
    """The sending domain of an address, lowercased, or ``None``."""
    _, _, domain = (address or "").strip().lower().partition("@")
    return domain or None


@dataclass(frozen=True)
class Adjustment:
    """What history says about a sender, and why — in the words the UI shows."""

    delta: float = 0.0
    reason: str | None = None
    approvals: int = 0
    rejections: int = 0

    @property
    def is_zero(self) -> bool:
        return abs(self.delta) < 1e-9


# --------------------------------------------------------------------------- #
# Reading                                                                      #
# --------------------------------------------------------------------------- #


def _prior(
    db: Session, user_id: int, scope: PriorScope, value: str | None
) -> ClassifierPrior | None:
    if not value:
        return None
    return db.scalar(
        select(ClassifierPrior).where(
            ClassifierPrior.user_id == user_id,
            ClassifierPrior.scope == scope,
            ClassifierPrior.value == value,
        )
    )


def _delta_for(prior: ClassifierPrior | None) -> float:
    if prior is None:
        return 0.0
    return (prior.approvals * APPROVAL_STEP) - (prior.rejections * REJECTION_STEP)


def adjustment_for(db: Session, user_id: int, address: str | None) -> Adjustment:
    """What this user's history with *address* does to a classification.

    The address prior **replaces** the domain prior rather than adding to it when
    one exists. Summing would double-count the same evidence: approving a draft
    from ``alex@acme.com`` writes both an address prior and a domain prior, so a
    sum would credit one judgement twice and reach the cap in half the signals.
    """
    if not settings.recruiter_feedback_enabled:
        return Adjustment()

    address = (address or "").strip().lower()
    by_address = _prior(db, user_id, PriorScope.ADDRESS, address)
    by_domain = _prior(db, user_id, PriorScope.DOMAIN, domain_of(address))

    prior = by_address if by_address is not None else by_domain
    if prior is None:
        return Adjustment()

    raw = _delta_for(prior)
    delta = max(
        -abs(settings.recruiter_feedback_max_penalty),
        min(abs(settings.recruiter_feedback_max_boost), raw),
    )
    if abs(delta) < 1e-9:
        return Adjustment(approvals=prior.approvals, rejections=prior.rejections)

    scope_label = "this sender" if prior.scope is PriorScope.ADDRESS else prior.value
    if delta > 0:
        reason = (
            f"You've approved {prior.approvals} "
            f"{'reply' if prior.approvals == 1 else 'replies'} to {scope_label} before."
        )
    else:
        reason = (
            f"You've discarded {prior.rejections} "
            f"{'draft' if prior.rejections == 1 else 'drafts'} from {scope_label} before."
        )

    return Adjustment(
        delta=round(delta, 4),
        reason=reason,
        approvals=prior.approvals,
        rejections=prior.rejections,
    )


def adjusted_confidence(confidence: float, adjustment: Adjustment) -> float:
    """*confidence* plus *adjustment*, clamped to the classifier's 0..1 range."""
    return round(max(0.0, min(1.0, float(confidence or 0.0) + adjustment.delta)), 4)


# --------------------------------------------------------------------------- #
# Writing                                                                      #
# --------------------------------------------------------------------------- #


def _bump(
    db: Session,
    user_id: int,
    scope: PriorScope,
    value: str | None,
    signal: FeedbackSignal,
    now: datetime,
) -> None:
    if not value:
        return
    prior = _prior(db, user_id, scope, value)
    if prior is None:
        prior = ClassifierPrior(user_id=user_id, scope=scope, value=value)
        db.add(prior)
    if signal is FeedbackSignal.APPROVED:
        prior.approvals = (prior.approvals or 0) + 1
    else:
        prior.rejections = (prior.rejections or 0) + 1
    prior.last_signal_at = now
    db.flush()


def record(
    db: Session,
    row: RecruiterEmail,
    signal: FeedbackSignal,
    *,
    source: str,
) -> ReplyFeedback | None:
    """Book one user verdict on one detected message.

    Writes the audit row and rolls the counters forward in one transaction —
    they must not be able to disagree, because the counters are what the
    classifier reads and the audit row is what a user would be shown to explain
    it.

    Returns ``None`` when feedback is switched off, so callers can treat this as
    fire-and-forget. Never raises for a reason the caller could not act on: a
    verdict that fails to record must not fail the approval the user actually
    asked for.
    """
    if not settings.recruiter_feedback_enabled:
        return None

    address = (row.reply_address or "").strip().lower() or None
    now = datetime.now(UTC)

    feedback = ReplyFeedback(
        user_id=row.user_id,
        recruiter_email_id=row.id,
        signal=signal,
        source=source[:32],
        # Copied, not joined: the point of the row is a disagreement with one
        # specific reading, and re-running the classifier later gives a different
        # one.
        kind=row.kind if isinstance(row.kind, RecruiterEmailKind) else None,
        classification_confidence=row.classification_confidence,
        sender_address=address,
        sender_domain=domain_of(address),
    )
    db.add(feedback)

    _bump(db, row.user_id, PriorScope.ADDRESS, address, signal, now)
    _bump(db, row.user_id, PriorScope.DOMAIN, domain_of(address), signal, now)
    db.flush()

    logger.info(
        "recruiter feedback: user=%s %s from=%s (%s)",
        row.user_id,
        signal.value,
        address,
        source,
    )
    return feedback


def record_for_email(
    db: Session, email_id: int, signal: FeedbackSignal, *, source: str
) -> ReplyFeedback | None:
    """Book a verdict given the *reply* email's id rather than the message's.

    The review endpoints act on an :class:`~app.models.email.Email`, and only
    some of those are inbound recruiter replies. A draft with no
    ``RecruiterEmail`` pointing at it is ordinary outreach and produces no
    signal — which is correct, not a miss: approving a cold outreach email says
    nothing about whether some stranger was a recruiter.
    """
    row = db.scalar(
        select(RecruiterEmail).where(RecruiterEmail.reply_email_id == email_id)
    )
    if row is None:
        return None
    return record(db, row, signal, source=source)


__all__ = [
    "APPROVAL_STEP",
    "REJECTION_STEP",
    "Adjustment",
    "adjusted_confidence",
    "adjustment_for",
    "domain_of",
    "record",
    "record_for_email",
]
