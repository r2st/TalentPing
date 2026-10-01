"""What ``In-Reply-To``/``References`` a new message on an existing thread needs.

Gmail's ``threadId`` threads a conversation for people reading in Gmail and for
nobody else. Outlook, Apple Mail, Thunderbird and every ATS that ingests mail
thread on the RFC 5322 headers, so a message carrying only the thread id shows
up in roughly half of all clients as a new message that happens to share a
subject line.

Three producers put outbound mail onto a thread somebody is already reading:
:mod:`app.tasks.inbox_tasks` and :mod:`app.services.recruiter_reply_service`
(both answering mail that just arrived) and :mod:`app.services.follow_up_service`
(nudging a conversation nobody has answered). The first two had the headers
because the message they answer is *inbound*, and an inbound message arrives
with its own ``Message-ID`` — which :mod:`app.tasks.inbox_tasks` stores.

The follow-up path had nothing to chain from, because until
``emails.rfc_message_id`` existed the product could not name its own sent mail:
``gmail_message_id`` is Gmail's internal handle and no recipient has ever seen
one. So a follow-up sequence — the case where threading matters *most*, being
several near-identical messages from one sender to one recipient — was the one
path that went out unthreaded. That is the reading experience of three cold
emails and the deliverability signature of one.

This module is the single answer, so a fourth producer gets it by asking rather
than by remembering.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.email import Email, EmailDirection
from app.models.email_thread import EmailThread
from app.services import inbound_scanner


def own_message_id(email: Email) -> str | None:
    """The RFC 5322 ``Message-ID`` of *email* itself, when it is known.

    Two columns, because the two directions learned their id at different times
    and by different means:

    * a **sent** message's id is read back from Gmail after the send and lives
      in ``rfc_message_id``;
    * a **received** message's id arrived in its own headers and lives in
      ``in_reply_to`` — an overload documented on the model and on the
      :mod:`app.tasks.inbox_tasks` write that created it.

    Reading ``in_reply_to`` for a sent row would be exactly wrong: there it
    holds the id of the message being *answered*, so chaining off it would
    thread the new message to the recruiter's mail rather than to ours, and
    would repeat an id that is already at the end of ``References``.
    """
    if email.direction == EmailDirection.SENT:
        return (email.rfc_message_id or "").strip() or None
    return (email.in_reply_to or "").strip() or None


def headers_for_next_message(
    db: Session, thread: EmailThread
) -> tuple[str | None, str | None]:
    """``(In-Reply-To, References)`` for the next message we put on *thread*.

    Chained off the newest message on the thread whose own id we know — not
    simply the newest, because a row written before ``rfc_message_id`` existed
    (or one whose read-back failed) has no id to offer, and the message before
    it usually does. Walking back is strictly better than giving up: an
    ``In-Reply-To`` naming an older message in the same conversation threads
    correctly in every client that walks ``References``, which is what the
    header is for.

    ``(None, None)`` when nothing on the thread has a usable id. That is the
    safe failure and it is the old behaviour: unthreaded is worse than threaded
    and far better than threaded to a fabricated id, which
    ``inbound_scanner.reply_headers_for`` refuses to produce for the same
    reason.
    """
    # Newest first by primary key rather than by ``sent_at``. Every row on a
    # thread is written as its message happens — an inbound one when the poll
    # reads it, an outbound one when it is composed — so id order is arrival
    # order, and unlike ``sent_at`` it is never null. Sorting on a nullable
    # column would need NULLS LAST to keep an unsent draft from outranking real
    # mail, and that is a dialect difference this lookup has no reason to buy.
    rows = db.scalars(
        select(Email)
        .where(Email.thread_id == thread.id)
        .order_by(Email.id.desc())
    ).all()
    for row in rows:
        message_id = own_message_id(row)
        if message_id:
            return inbound_scanner.reply_headers_for(message_id, row.email_references)
    return None, None


__all__ = ["headers_for_next_message", "own_message_id"]
