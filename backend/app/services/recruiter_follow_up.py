"""A recruiter we already answered has written again.

A follow-up arrives in one of two shapes, and they travel completely different
code paths — which is why detecting them takes two functions rather than one.

**(a) The same Gmail thread.** They hit reply. Once we answered, that thread has
an :class:`~app.models.email_thread.EmailThread` row, so
:func:`app.services.inbound_scanner.scan` skips it as ``skipped_own_thread`` and
:func:`app.tasks.inbox_tasks.poll_thread` owns it. That path drafts for review
and never auto-sends, so "don't answer twice unread" already held here — by
accident rather than by design. What was missing is *visibility*: the Recruiter
Inbox row still read ``REPLIED`` and nothing told the candidate their recruiter
had come back. :func:`note_thread_follow_up` is that.

**(b) A brand-new thread.** They start a fresh message instead of replying —
extremely common when the original outreach came from a platform (Gem, Loxo,
Bullhorn), where every send is its own thread. This one flows through the whole
inbound pipeline again as though it were first contact: classified, matched,
routed, and eligible to be **auto-replied to a second time**. That is the real
bug, and :func:`find_prior_engagement` is what closes it.

**Why escalate rather than answer again.** The second message is, by
construction, the one an automatic answer is worst at. It is a response to
something we said — a scheduling request, a question about comp, a follow-up to
a reply the candidate never read — and answering it needs the transcript. The
entire reason the inbound pipeline exists separately from
:mod:`app.services.reply_agent` is that first contact *has* no transcript.
Handing the second message to the user is not a degradation; it is the correct
destination.

**Keyed on the reply address, not the ``From``.** For platform-sent outreach
those differ, and keying on the ``From`` would file every recruiter using the
same platform as one person — so the first ``noreply@gem.com`` message anyone
answered would escalate everybody else's.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recruiter_email import RecruiterEmail, RecruiterEmailStatus

logger = logging.getLogger(__name__)

#: Statuses that mean we actually engaged with the sender — the product either
#: sent a reply or has one queued. ``DRAFTED`` is deliberately **not** here: a
#: draft the user never approved is not a conversation, and treating it as one
#: would escalate a recruiter's second message because we once wrote something
#: nobody sent. ``FLAGGED`` is out for the same reason, more obviously.
ENGAGED_STATUSES = frozenset(
    {RecruiterEmailStatus.REPLY_QUEUED, RecruiterEmailStatus.REPLIED}
)


def find_prior_engagement(db: Session, row: RecruiterEmail) -> RecruiterEmail | None:
    """An earlier message from this sender that we already answered.

    Scoped to the same user, matched on :attr:`RecruiterEmail.reply_address`, and
    restricted to rows older than *row* so replaying a task can never make a
    message its own predecessor.
    """
    address = (row.reply_address or "").strip().lower()
    if not address:
        return None

    candidates = db.scalars(
        select(RecruiterEmail)
        .where(
            RecruiterEmail.user_id == row.user_id,
            RecruiterEmail.id != row.id,
            RecruiterEmail.status.in_(tuple(ENGAGED_STATUSES)),
        )
        .order_by(RecruiterEmail.id.desc())
    ).all()

    for prior in candidates:
        if prior.id >= (row.id or 0):
            continue
        if (prior.reply_address or "").strip().lower() == address:
            return prior
    return None


def _when(prior: RecruiterEmail) -> str:
    """The date the earlier reply happened, as the UI phrases it.

    "earlier" when there is no timestamp — a vaguer sentence is better than one
    naming a date we are guessing at.
    """
    when = prior.received_at or prior.created_at
    if when is None:
        return "earlier"
    return f"on {when.day} {when:%B}"


def escalate(
    db: Session, row: RecruiterEmail, prior: RecruiterEmail, *, now: datetime | None = None
) -> str:
    """Mark *row* as a follow-up on *prior* and return the reason shown to the user.

    Sets the escalation on *row* (this is the message that needs a human) and
    bumps the counters on *prior* (that is the conversation being followed up).
    """
    now = now or datetime.now(UTC)
    reason = (
        f"You replied to {row.reply_address} {_when(prior)} and they've written "
        "again. This one's for you rather than an automatic answer."
    )
    row.escalated = True
    row.escalation_reason = reason
    row.previous_recruiter_email_id = prior.id

    prior.follow_up_count = (prior.follow_up_count or 0) + 1
    prior.last_follow_up_at = now
    db.flush()

    logger.info(
        "recruiter email %s escalated: follow-up to %s from %s",
        row.id,
        prior.id,
        row.reply_address,
    )
    return reason


def note_thread_follow_up(
    db: Session, application_id: int | None, *, now: datetime | None = None
) -> RecruiterEmail | None:
    """Record that a reply landed on a thread we started from inbound mail.

    Called by the thread poller, which owns conversations once we engage. The
    row's ``kind``/``route``/``status`` are left exactly as they are: they record
    a decision made at a moment in time, and a correctly-``REPLIED`` message is
    still correctly ``REPLIED`` after the recruiter answers. What changed is that
    it now wants a human, which is a separate axis — hence a flag rather than a
    new status.

    Returns ``None`` for the ordinary case: a thread from cold outreach has no
    ``RecruiterEmail`` behind it and there is nothing to escalate.
    """
    if application_id is None:
        return None
    row = db.scalar(
        select(RecruiterEmail).where(RecruiterEmail.application_id == application_id)
    )
    if row is None:
        return None

    row.follow_up_count = (row.follow_up_count or 0) + 1
    row.last_follow_up_at = now or datetime.now(UTC)
    if not row.escalated:
        row.escalated = True
        row.escalation_reason = (
            f"{row.from_name or row.from_address} replied to the answer we sent. "
            "The conversation is yours from here."
        )
    db.flush()
    logger.info("recruiter email %s has a thread follow-up", row.id)
    return row


__all__ = [
    "ENGAGED_STATUSES",
    "escalate",
    "find_prior_engagement",
    "note_thread_follow_up",
]
