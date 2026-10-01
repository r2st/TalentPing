"""Honouring "stop emailing me", from whichever door it arrives at.

``Recruiter.opted_out`` is the CAN-SPAM consent flag, checked before every send
forever. Setting it is only half the job: a sequence left ``SCHEDULED`` and a
batch already ``QUEUED`` both read on the tracker as mail still coming, and the
send path writes each one off one at a time as it comes due. So an opt-out
cancels the work behind it as well, and this module is the one place that knows
that.

It exists because the product offers **two** opt-out routes and honoured one.
Every unsolicited message carries::

    List-Unsubscribe: <mailto:candidate@gmail.com?subject=unsubscribe>,
                      <https://.../unsubscribe?email=...&sig=...>

The HTTPS half is Gmail's and Yahoo's one-click route and lands on
``routers/misc.unsubscribe``. The ``mailto:`` half is what Apple Mail,
Thunderbird and several Outlook builds actually use, and it asks the recipient's
client to send us a message with the subject "unsubscribe" and no body at all.
That message arrived, ``inbound_scanner`` recognised it — ``looks_like_opt_out``
has read the subject since it was written — and then *skipped* it: counted as
``skipped_opt_out``, logged, and dropped. Nobody was opted out.

Which is the worst available outcome. The recipient used the control their mail
client offered, watched it succeed, and kept receiving follow-ups from a
candidate's personal Gmail. The next thing they press is "report spam", and the
header we shipped is what invited them to try the door that did not open.

Matching is on the address across **every** user's recruiter rows, not just the
one whose mailbox received the request, because consent belongs to the human and
not to whoever happened to add them — the same rule the HTTPS endpoint has
always applied.

**And the header now waits for the reader.** The loop above lives in
``recruiter_reply_service.record_scan``, which only ever runs behind the
recruiter-inbox scanner — server flag on, and a per-user preference row that
ships *off*. Nothing narrowed the ``List-Unsubscribe`` header to match, so the
``mailto:`` half went out on every unsolicited message regardless of whether
anything would read the reply, and for a default account it was still the door
that did not open. :func:`mail_route_is_read` is the question
``gmail_service.send_email`` asks before making the promise; the HTTPS half is
unconditional, because the route behind it always answers.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.sql_text import ci_equals
from app.models.application import Application, ApplicationStatus
from app.models.recruiter import Recruiter
from app.models.recruiter_email import RecruiterReplyPreference
from app.models.status_event import StatusEventSource

logger = logging.getLogger(__name__)


def mail_route_is_read(db: Session, user_id: int) -> bool:
    """Whether an opt-out arriving *as mail* would reach :func:`apply` at all.

    The ``mailto:`` half of ``List-Unsubscribe`` is a request that the recipient
    send us an email, and this module's docstring describes what used to happen
    to that email: recognised, counted, dropped. The loop in
    ``recruiter_reply_service.record_scan`` fixed it — but that loop only runs
    behind :func:`app.services.inbound_scanner.scan`, and nothing reaches the
    scanner unless the server flag is on *and* this user asked for their mailbox
    to be watched. ``RecruiterReplyPreference.enabled`` ships **off**.

    So the fix landed for the users who had opted into inbox scanning and for
    nobody else, while the header was advertised on every unsolicited message
    regardless. This is the question ``gmail_service.send_email`` asks before
    making the promise: no reader, no ``mailto:``.

    Read-only on purpose — it must not create the preference row it is looking
    for. A user with no row has never enabled anything, which is the answer.
    Deliberately does not model the push-driven scan, which can reach a mailbox
    whose preference is off: this decides whether to *promise* an opt-out route,
    and a promise wants the condition that always holds, not the one that
    sometimes does.
    """
    if not settings.recruiter_reply_enabled:
        return False
    return bool(
        db.scalar(
            select(RecruiterReplyPreference.id).where(
                RecruiterReplyPreference.user_id == user_id,
                RecruiterReplyPreference.enabled.is_(True),
            )
        )
    )


def apply(db: Session, address: str, *, reason: str) -> int:
    """Opt *address* out everywhere and cancel the work queued behind it.

    Returns how many recruiter rows matched, which is a number for logs and for
    the caller's own bookkeeping — never for a response body. An unsubscribe
    route that reports what it found is an oracle telling any caller whether a
    given address is in the database.

    Idempotent: an address already opted out simply matches again and its
    already-cancelled work is already cancelled.

    Does not commit. The caller owns the transaction — both call sites are in
    the middle of one — and an opt-out that committed here would tear a scan's
    unit of work in half.
    """
    cleaned = (address or "").strip()
    if not cleaned:
        return 0

    rows = db.scalars(select(Recruiter).where(ci_equals(Recruiter.email, cleaned))).all()
    for recruiter in rows:
        suppress(db, recruiter, reason=reason)
    return len(rows)


def suppress(db: Session, recruiter: Recruiter, *, reason: str) -> int:
    """Opt one contact out, and retire everything that was aimed at them.

    The whole of what "they asked to be left alone" means, in one function,
    because the product has **two** doors onto it and each used to do a
    different half:

    * :func:`apply` — the ``List-Unsubscribe`` header, both halves — cancelled
      the queued mail and every pending sequence, and left the applications
      themselves sitting in whatever stage they had reached. Nothing ever
      reached ``UNSUBSCRIBED``, so the board never locked the card (see
      ``pipeline_board.LOCKED_STATUSES``, whose entire job is to stop a drag
      re-arming the drip against someone who opted out), the dashboard funnel
      went on counting the conversation as live, and the history panel had no
      row saying why it stopped.
    * ``inbox_tasks._apply_intent`` — a reply classified ``UNSUBSCRIBE`` — did
      record the transition, and then cancelled the follow-ups for *that one
      application only*. A recruiter contacted from two campaigns kept the
      second campaign's sequence, scheduled and showing on the tracker as mail
      still coming.

    Neither half is optional and the door the request came through is not a
    reason to get a different one, so both now call this.

    Idempotent. A contact already opted out matches again, their already
    cancelled work is already cancelled, and an application already at
    ``UNSUBSCRIBED`` is left alone rather than given a second history row
    saying it moved from itself to itself.

    Returns how many applications were moved, for logs and for the caller's
    bookkeeping. Does not commit — see :func:`apply`.
    """
    # Imported inside the function on purpose. ``follow_up_service`` and
    # ``outreach_service`` import each other and half the services package
    # between them; a module-level import here would put this module inside that
    # cycle for the benefit of one call site. ``pipeline_board`` is model-only
    # and would import cleanly, but it is kept alongside them so the three
    # things this function reaches for read as one group.
    from app.services import outreach_service, pipeline_board
    from app.services.follow_up_service import cancel_for_application

    recruiter.opted_out = True
    # The send path refuses an opted-out contact at due time, so neither of
    # these changes what goes out. They change what the user sees.
    outreach_service.cancel_unsent_for_recruiter(db, recruiter.id)

    moved = 0
    for application in db.scalars(
        select(Application).where(Application.recruiter_id == recruiter.id)
    ):
        cancel_for_application(db, application.id, reason)
        if application.status is ApplicationStatus.UNSUBSCRIBED:
            continue
        # ``advances`` rather than an unconditional write: it is what says an
        # ending may be recorded from any rung, and it is also what refuses to
        # move a card that is *already* locked. Same call the reply-classified
        # door makes, so the two produce the same history row.
        if not pipeline_board.advances(
            application.status, ApplicationStatus.UNSUBSCRIBED
        ):
            continue
        pipeline_board.record_change(
            db,
            application,
            ApplicationStatus.UNSUBSCRIBED,
            source=StatusEventSource.AUTOMATIC,
            reason=reason,
        )
        moved += 1
    return moved


__all__ = ["apply", "mail_route_is_read", "suppress"]
