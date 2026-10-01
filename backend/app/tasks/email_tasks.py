"""Celery tasks for the autopilot pipeline and throttled outreach sending.

Sending respects a per-mailbox daily limit and a randomized interval between
messages (never bursts) — the warm-up/deliverability strategy from research §3.2.
Every message goes out through the sender's own connected Gmail account.
"""
from __future__ import annotations

import logging
import random
from datetime import UTC, datetime, timedelta

from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core import events
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.gmail_account import GmailAccount
from app.models.recruiter import Recruiter
from app.models.status_event import StatusEventSource
from app.models.user import User
from app.services import (
    bounce_service,
    email_attachments,
    email_tracking,
    gmail_accounts,
    gmail_service,
    opt_out,
    pipeline_board,
    recruiter_reply_service,
    reputation_service,
    send_time,
    subject_ab_service,
)
from app.services import (
    unsubscribe as unsubscribe_service,
)
from app.services.follow_up_service import schedule_for_application
from app.services.outreach_service import (
    maybe_complete_campaign,
    maybe_complete_campaign_for_email,
    run_autopilot,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _sent_in_last_24h(
    db,
    user_id: int,
    account: GmailAccount | None = None,
    *,
    now: datetime | None = None,
) -> tuple[int, datetime | None]:
    """The rolling window this send is about to be weighed against.

    Scoped to one mailbox because that is what it is compared against:
    ``reputation_service.evaluate`` weighs this number against *one* account's
    warm-up allowance. Fed a user-wide count, two fresh mailboxes on 5/day each
    shared a single 5/day budget and each blamed the other.

    The implementation lives in :func:`app.services.gmail_accounts.sent_in_last_24h`
    — it is the same question ``auto_apply_service`` asks to size its budget, and
    having written it twice is how both copies came to miss that a reconnected
    mailbox loses its thread stamps. See that function for the whole reasoning.

    *now* is forwarded so this link can be pinned like every other one in the
    warm-up chain; see the delegate for what leaving it unpinnable cost.
    """
    return gmail_accounts.sent_in_last_24h(db, user_id, account, now=now)


def _recruiter_for_email(db: Session, email: Email) -> tuple[Recruiter | None, object]:
    """The recruiter an outbound email is addressed to, and its application."""
    thread = db.get(EmailThread, email.thread_id)
    if thread is None:
        return None, None
    application = db.get(Application, thread.application_id)
    if application is None:
        return None, None
    return db.get(Recruiter, application.recruiter_id), application


def send_countdown(
    db: Session, email: Email, spacing_seconds: int, *, now: datetime | None = None
) -> int:
    """Seconds to delay this send: business hours where the recipient is.

    The randomized 90-600s spacing the caller accumulated is preserved as the
    input to the slot search rather than replaced by it, so the throttle and the
    timing rule compose: messages still trickle, they just trickle during the
    recruiter's morning instead of through their night.

    Public because every dispatcher of outreach has to go through it, and one of
    them lives in another module (:mod:`app.tasks.follow_up_tasks`). A dispatcher
    that accumulates spacing and hands it to the broker raw is a dispatcher that
    sends through the recipient's night, whatever the row's ``scheduled_at``
    said — see that module for what it cost.

    Falls back to the accumulated spacing when the optimization is off — which is
    what keeps the existing campaign tests meaningful.

    Either way the result is clamped to ``send_time.MAX_SEND_DELAY_SECONDS``.
    That ceiling is not this function's own hygiene, it is a load-bearing
    invariant two other pieces of the send path are *derived* from:

    * :data:`app.tasks.celery_app.BROKER_VISIBILITY_TIMEOUT` sits a day past it,
      so a worker holding a countdown in memory always acks before Kombu decides
      the message was lost and restores a second copy to the queue.
    * :data:`_STRANDED_AFTER_SECONDS` equals it, so ``sweep_stranded_sends`` can
      say that a row queued longer than that has no live task aiming at it.

    The optimization-on branch gets the clamp from ``send_time.delay_seconds``.
    The branch below returned the caller's accumulation raw, and every caller
    accumulates: ``enqueue_campaign_sends``, ``sweep_stranded_sends`` and
    ``follow_up_tasks`` all add a random ``MIN``-``MAX_SEND_INTERVAL_SECONDS``
    per message. At the default 90-600s that crosses seven days at about 1,750
    messages in one campaign; at a ceiling an operator raised to an hour it
    crosses at roughly 170. Past that point both derivations above are false at
    once — the broker re-queues the message hourly *and* the sweep re-dispatches
    the row — which is precisely the pair that produced the storm the visibility
    timeout was set to end. Turning the send-time feature *off* is not a reason
    to hand the broker an ETA that nothing downstream can survive.
    """
    if not settings.send_time_optimization_enabled:
        return max(0, min(spacing_seconds, send_time.MAX_SEND_DELAY_SECONDS))

    now = now or datetime.now(UTC)
    recruiter, application = _recruiter_for_email(db, email)
    tz = send_time.resolve_timezone(db, recruiter, application=application)
    slot = send_time.next_slot(now + timedelta(seconds=spacing_seconds), tz)
    return send_time.delay_seconds(slot, now=now)


@celery_app.task(name="app.tasks.email_tasks.start_campaign")
def start_campaign(campaign_id: int) -> dict:
    """Autopilot entrypoint: discover → generate → queue, for one campaign."""
    db = SessionLocal()
    try:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return {"campaign_id": campaign_id, "status": "missing"}
        return run_autopilot(db, campaign)
    finally:
        db.close()


def enqueue_campaign_sends(db: Session, campaign_id: int) -> tuple[int, str | None]:
    """Queue a throttled send task per pending email in the campaign.

    Takes the caller's session rather than opening its own: the autopilot runs
    both from a Celery worker and inline inside a request, and the emails it
    just wrote must be visible either way.

    Returns ``(dispatched, error)``. Every pending email is marked ``QUEUED``
    whether or not the broker accepted the task, so an outage delays the send
    rather than losing it — the user can resume the campaign to re-dispatch.
    The first dispatch failure stops the loop; retrying each of fifty emails
    against a dead broker would stall the caller for minutes.

    Each task gets a cumulative randomized countdown, so a campaign trickles out
    over hours instead of bursting.
    """
    pending = db.scalars(
        select(Email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.campaign_id == campaign_id,
            Email.direction == EmailDirection.SENT,
            # QUEUED only — never DRAFT. A draft under an auto-send campaign is
            # there because ``send_policy`` put it there (a pause, a spent daily
            # ceiling, an unfinished trial), and a sweep that promoted it would
            # send unread the exact message the policy held back. Nothing is
            # lost: the producer marks everything it wants sent QUEUED already,
            # so this only ever re-dispatches sends a broker outage stranded.
            Email.status == EmailStatus.QUEUED,
        )
    ).all()

    dispatched = 0
    cumulative = 0
    error: str | None = None
    if not settings.celery_enabled:
        error = "Sending is queued — no background worker is configured"

    now = datetime.now(UTC)
    for email in pending:
        email.status = EmailStatus.QUEUED
        if error is not None:
            continue
        cumulative += random.randint(
            settings.min_send_interval_seconds, settings.max_send_interval_seconds
        )
        countdown = send_countdown(db, email, cumulative, now=now)
        try:
            send_outreach_email.apply_async(args=[email.id], countdown=countdown)
        except Exception as exc:  # noqa: BLE001 - broker down or not configured
            logger.warning("campaign %s: send dispatch failed: %s", campaign_id, exc)
            error = f"Sending is delayed — the task queue is unreachable ({exc})"
            continue
        # Same stamp the sweep keeps, for the same reason: this publish is a
        # live task aiming at the row, and a sweep that runs before it fires
        # must not treat the row as abandoned.
        email.send_dispatched_at = now
        dispatched += 1
    db.commit()
    return dispatched, error


def _answers_the_recruiter(db: Session, email: Email) -> bool:
    """True when this outbound row was written as an answer to inbound mail.

    Both reply pipelines stamp ``draft_template`` — it names the angle the reply
    takes — and neither cold outreach nor a follow-up ever does, which makes it
    the one column that separates "we are answering someone" from "we are
    starting or continuing a sequence of our own". The foreign key is checked
    too, because it is the authoritative link for the recruiter-inbox path and
    is not going to be re-derived if that column is ever repurposed.

    Deliberately not ``in_reply_to``: it is null when the message being answered
    carried no ``Message-ID`` (see ``inbound_scanner.reply_headers_for``), and a
    reply's threading headers are not where its provenance should be read from.
    """
    if email.draft_template is not None:
        return True
    return recruiter_reply_service.is_inbound_reply(db, email)


def _claim_for_send(db: Session, email_id: int) -> Email | None:
    """Take exclusive hold of one queued email, or return ``None``.

    ``SKIP LOCKED`` rather than a plain read, because "is this row still QUEUED?"
    is not a claim — two tasks can both read QUEUED and both call Gmail, and the
    recruiter gets the message twice under the candidate's name.

    That is not hypothetical: two dispatchers aim at the same row by design.
    ``enqueue_campaign_sends`` re-dispatches *every* QUEUED email whenever a
    campaign is resumed, including ones already sitting on the broker with a
    countdown; the reputation gate re-queues a held message with ``self.retry``
    while that resume happens; and ``task_acks_late`` replays a task whose
    worker died. Any of those overlapping produced a duplicate send, and the
    r43 fix does not cover it — that one stopped a *delivered* message from
    reverting to QUEUED, which is a different way of arriving here.

    The lock is held until the commit that records the send, so the window it
    covers is exactly the Gmail call. A second worker gets ``None`` immediately
    instead of blocking behind it: whoever holds the row is already sending, so
    there is nothing useful to wait for. Postgres enforces this; SQLite ignores
    row locking entirely, which is correct for a test suite with no concurrency
    and is why the statement — not just the outcome — is asserted on.
    """
    return db.scalar(
        select(Email).where(Email.id == email_id).with_for_update(skip_locked=True)
    )


def _lock_account_for_send(
    db: Session, account: GmailAccount | None
) -> GmailAccount | None:
    """Take exclusive hold of the mailbox the reputation gate is about to check.

    ``_claim_for_send`` locks the *email* row, which stops two workers sending
    the same message — but a campaign trickles many QUEUED emails out against
    one mailbox, each with its own row and its own lock, so two workers each
    sending a *different* message for the same account both ran the
    rolling-24h count below before either one recorded a send, both saw a seat
    free, and both sent: a five-a-day warm-up step paid out six.

    The account row is the shared resource the ceiling is actually about, so
    it is what has to serialize. Plain ``with_for_update`` rather than
    ``skip_locked`` — this is capacity worth a moment's wait, not a claim to
    walk away from — so a second worker blocks here until the first commits
    its send and releases the row, then recounts and finds the seat taken.

    Postgres enforces the wait; SQLite ignores row locking entirely, same as
    ``_claim_for_send``, which is why the statement itself is what gets
    asserted on in tests.
    """
    if account is None:
        return None
    return db.execute(
        select(GmailAccount).where(GmailAccount.id == account.id).with_for_update()
    ).scalar_one()


# How many times an *unexpected* failure is worth trying again before the
# message is written off. Small, and deliberately much smaller than
# ``max_retries``: that budget is for the reputation gate, which holds a message
# back for reasons that genuinely do clear on their own. Nothing here knows why
# an unhandled exception happened, so three attempts is the whole of the benefit
# of the doubt.
_UNEXPECTED_ATTEMPTS = 3

# How many times a *transient transport* failure is worth trying again, and how
# long to wait when the transport did not say. Larger than the poison budget
# above because this one knows something: Gmail answered with a throttle or a
# 5xx, or the socket never got there, and both of those are states that clear.
# A Google incident is routinely half an hour, so three five-minute attempts
# would park a whole campaign's mail over an outage that ended before anyone
# noticed it.
#
# The delay is a fallback. A ``Retry-After`` on the response wins, because the
# server saying when to come back beats any curve this module could pick — see
# `gmail_service.retry_after_seconds`.
_TRANSPORT_ATTEMPTS = 6
_TRANSPORT_RETRY_DELAY = 300


@celery_app.task(bind=True, max_retries=24, default_retry_delay=300)
def send_outreach_email(self, email_id: int) -> dict:
    """Send a single queued outreach email via Gmail, respecting daily limits.

    Wrapped so that no exception can leave the row QUEUED indefinitely. Only the
    two Gmail failures below were ever handled, and anything else — a malformed
    header, an encoding the composer produced, a connection dropped mid-flight —
    escaped the task with the claim rolled back, so the status stayed QUEUED.
    That is not an idle state: ``enqueue_campaign_sends`` re-dispatches exactly
    the QUEUED rows whenever a campaign is resumed, and the sweep found the same
    row every time. The message failed forever, its campaign never reached
    completion, and nothing written down said why.

    A poison message now costs :data:`_UNEXPECTED_ATTEMPTS` tries and is then
    FAILED, where the review queue can show it and the campaign can finish.
    """
    try:
        return _send_outreach_email(self, email_id)
    except Retry:
        # `self.retry` signals by raising. Swallowing it here would turn every
        # deliberate hold — the reputation gate's, above all — into a send.
        raise
    except Exception as exc:  # noqa: BLE001 - the backstop this wrapper exists for
        logger.exception("sending email %s failed unexpectedly", email_id)
        if self.request.retries < _UNEXPECTED_ATTEMPTS - 1:
            raise self.retry(exc=exc, countdown=300) from exc
        _mark_failed(email_id, f"send failed: {exc}"[:500])
        return {
            "email_id": email_id,
            "status": "failed",
            "reason": str(exc)[:200],
        }


#: Fits ``Email.attention_reason``.
_ATTENTION_LIMIT = 160


def _park_for_review(db: Session, email: Email, reason: str | None) -> None:
    """Put a message the reputation gate will not release into the review queue.

    A DRAFT rather than a FAILED, so the work survives and the user can send it
    by hand — the invariant :mod:`app.services.send_policy` states for every
    producer, kept here at the transport for the one hold that can outlive its
    retry budget.

    ``auto_sent`` is cleared for the same reason ``review._queue_for_send``
    clears it: the message is now waiting on a human, so it must not go on
    counting against the ceiling for unreviewed sending. The caller commits
    nothing else, so this owns the commit.
    """
    email.status = EmailStatus.DRAFT
    email.auto_sent = False
    email.needs_attention = True
    email.attention_reason = (
        reason or "Held back to protect your sending reputation."
    )[:_ATTENTION_LIMIT]
    # And the recruiter-inbox row, when this is an inbound reply rather than
    # cold outreach. It says ``REPLY_QUEUED``, which this line has just made
    # false — see `recruiter_reply_service.record_send_held`.
    recruiter_reply_service.record_send_held(db, email, reason)
    db.commit()
    events.emit(
        events.EMAIL_PARKED,
        email_id=email.id,
        reason=email.attention_reason,
    )


def _fail_send(db: Session, email: Email, reason: str) -> None:
    """Write a message off, and tell the recruiter inbox if it owns one.

    Every terminal failure in this task used to be a bare
    ``email.status = FAILED``. That is the whole truth for cold outreach, and
    half of it for a reply: ``RecruiterEmail.status`` still read
    ``REPLY_QUEUED``, which the inbox counts as *replied*. See
    :func:`app.services.recruiter_reply_service.record_send_failure`.

    Does not commit — every caller already does, and two of them have another
    write (``mark_revoked``) that has to land in the same transaction.

    And it re-checks the campaign, because writing a message off drains the
    pending set exactly as delivering it does. Every terminal refusal in this
    task — an opt-out, an exclusion, a hard bounce — arrives *while* a campaign
    is trickling, so the message being written off here is routinely the last
    one outstanding. ``_after_send`` was the only caller of the completion
    check, so those campaigns stayed ACTIVE with an empty queue forever; see
    :func:`app.services.outreach_service.maybe_complete_campaign`.
    """
    email.status = EmailStatus.FAILED
    recruiter_reply_service.record_send_failure(db, email, reason)
    maybe_complete_campaign_for_email(db, email)


def _emit_failed(email: Email, reason: str) -> None:
    """Say out loud that a message was written off.

    Called *after* the commit that wrote it off, for the reason the send event
    is: the line asserts both that the message is dead and that the database
    says so, and a rolled-back transaction would otherwise leave a claim nothing
    backs.

    Only :func:`_mark_failed` — the poison-message backstop — ever emitted this,
    so the event existed for the one failure nobody understands and for none of
    the ones we do. A campaign whose mail was written off for an opt-out, a hard
    bounce or a dead grant produced no ``email.failed`` events at all, which
    made the metric read as "nothing is failing" at precisely the moments most
    worth alerting on. ``/admin/ops`` and every dashboard downstream count these.
    """
    events.emit(
        events.EMAIL_FAILED,
        level=logging.ERROR,
        email_id=email.id,
        recipient_domain=events.recipient_domain(email.to_address),
        reason=reason,
    )


def _mark_failed(email_id: int, reason: str) -> None:
    """Write a message off, in its own session.

    A fresh session on purpose: the one the failure happened in is in whatever
    state the exception left it, and the entire point of this call is that it
    still records something. A failure to record the failure is logged and
    dropped — re-raising here would put the task back where it started.
    """
    db = SessionLocal()
    try:
        email = db.get(Email, email_id)
        if email is None or email.status != EmailStatus.QUEUED:
            return
        _fail_send(db, email, reason)
        db.commit()
        _emit_failed(email, reason)
    except Exception:  # noqa: BLE001 - nothing left to escalate to
        logger.exception("could not record the failure of email %s", email_id)
        db.rollback()
    finally:
        db.close()


def _send_outreach_email(self, email_id: int) -> dict:
    """The body of :func:`send_outreach_email`; see it for the wrapper's job."""
    db = SessionLocal()
    try:
        email = _claim_for_send(db, email_id)
        if email is None:
            # Either the row is gone, or another worker is sending it right now.
            # Told apart only for the log line; both mean "not this task's work".
            if db.scalar(select(Email.id).where(Email.id == email_id)) is None:
                return {"email_id": email_id, "status": "skipped"}
            logger.info("email %s is already being sent by another worker", email_id)
            return {"email_id": email_id, "status": "in_flight"}
        if email.status != EmailStatus.QUEUED:
            return {"email_id": email_id, "status": "skipped"}

        thread = db.get(EmailThread, email.thread_id)
        application = db.get(Application, thread.application_id)
        campaign = db.get(Campaign, application.campaign_id)
        user = db.get(User, application.user_id)

        if (
            campaign is not None
            and campaign.status == CampaignStatus.PAUSED
            and not _answers_the_recruiter(db, email)
        ):
            # Leave it QUEUED — resuming the campaign re-enqueues it.
            return {"email_id": email_id, "status": "paused"}

        recruiter = db.get(Recruiter, application.recruiter_id)

        # Consent backstop, and the one that matters most. Everything that
        # *composes* a message already refuses an opted-out contact —
        # `outreach_service`, `follow_up_service`, `recruiter_reply_service` all
        # check — but nothing asked again between composing and sending, and that
        # gap is hours wide by design: a campaign's messages go onto the broker
        # with a cumulative randomized countdown so they trickle.
        #
        # So the opt-out arrived in the gap, which is exactly when it arrives —
        # a recruiter unsubscribes *because* the first message landed, while the
        # rest of the batch is still queued behind it. Every one of those went
        # out after they pressed the button, which is a CAN-SPAM violation, a
        # near-certain spam complaint, and a reputation hit against the mailbox
        # the user's whole pipeline depends on.
        #
        # Applies to replies as well as cold mail. `recruiter_reply_service`
        # already refuses to draft for an opted-out contact "even when they are
        # the one who just wrote", and this is that promise kept at the transport.
        if recruiter is not None and recruiter.opted_out:
            reason = "The recipient opted out before this went."
            _fail_send(db, email, reason)
            db.commit()
            _emit_failed(email, reason)
            return {
                "email_id": email_id,
                "status": "failed",
                "reason": "recipient opted out",
            }

        # The same gap, for the user's own decision. Excluding a contact while a
        # batch is trickling has to stop the rest of that batch, or the flag
        # only governs mail that had not been composed yet.
        if recruiter is not None and recruiter.is_excluded:
            reason = "You excluded this contact before this went."
            _fail_send(db, email, reason)
            db.commit()
            _emit_failed(email, reason)
            return {
                "email_id": email_id,
                "status": "failed",
                "reason": "contact excluded",
            }

        # Deliverability backstop. The address may have hard-bounced *after* this
        # email was composed — the same gap — and the compose-time check can't
        # see the future either.
        if bounce_service.is_suppressed(recruiter):
            reason = "This address has hard-bounced, so it was not sent."
            _fail_send(db, email, reason)
            db.commit()
            _emit_failed(email, reason)
            return {
                "email_id": email_id,
                "status": "failed",
                "reason": "address previously hard-bounced",
            }

        # The mailbox this conversation belongs to — not simply the user's
        # primary. A reply to a recruiter who wrote to the candidate's second
        # address has to go back out of *that* address: answering from another
        # one breaks the recruiter's threading and reads as a spoof.
        account = gmail_accounts.resolve_for_thread(db, thread, user=user)
        account = _lock_account_for_send(db, account)
        # Reputation gate: warm-up ramp, rolling-24h cap, and bounce/complaint
        # cool-downs (see reputation_service). Leaves the email QUEUED and retries
        # so a temporary hold delays the send rather than dropping it. The 24h
        # count is scoped to this mailbox, because that is the ledger the ceiling
        # it is compared against belongs to.
        sent_24h, oldest_24h = _sent_in_last_24h(db, user.id, account)
        decision = reputation_service.evaluate(
            account, sent_24h, oldest_in_window=oldest_24h
        )
        if not decision.allowed:
            countdown = int(
                decision.retry_after.total_seconds() if decision.retry_after else 3600
            )
            try:
                raise self.retry(countdown=max(300, countdown))
            except MaxRetriesExceededError:
                # The hold outlasted the retry budget. `max_retries=24` against
                # the daily cap's one-hour ``retry_after`` is 24 hours of
                # waiting, and a warm-up ceiling does not clear in 24 hours: a
                # brand-new mailbox sends 5/day for three days, so a first
                # campaign of twenty contacts has fifteen messages that cannot
                # possibly go out inside the budget.
                #
                # Falling through to the poison-message backstop wrote every one
                # of them off as FAILED — a status the review queue does not
                # show — carrying Celery's own "Can't retry <task-id>" as the
                # recorded reason. So the product silently destroyed three
                # quarters of a new user's first campaign, at exactly the moment
                # they were deciding whether it worked, and left nothing behind
                # that named the warm-up ramp as the cause.
                #
                # Parked as a draft instead, which is what every *producer* in
                # this codebase does with a message policy will not let go yet
                # (see send_policy: "a block is never a drop"). The work
                # survives, the review queue shows it with the gate's own
                # sentence attached, the user can send it by hand, and the
                # campaign stays ACTIVE because `_maybe_complete_campaign`
                # counts a draft as outstanding.
                _park_for_review(db, email, decision.reason)
                return {
                    "email_id": email_id,
                    "status": "held",
                    "reason": decision.reason,
                }
        # A reply to mail the recruiter sent *us* carries no opt-out footer and
        # no List-Unsubscribe headers. CAN-SPAM governs commercial messages we
        # initiate; answering someone who wrote to us first is not that, and
        # inviting them to unsubscribe from their own thread reads as bulk mail
        # — exactly the impression an inbound reply should not give. Cold
        # outreach is unchanged: it still gets both.
        unsub: str | None = None
        # And the ``mailto:`` half of ``List-Unsubscribe`` only when this user's
        # mailbox is actually scanned for the mail it asks the recipient to
        # send. Otherwise the header is a door with nothing behind it — see
        # `opt_out.mail_route_is_read`.
        mailto_unsub = False
        if not recruiter_reply_service.is_inbound_reply(db, email):
            # Signed, so the link proves it came from a message we sent. An
            # unsigned one still works — see app.services.unsubscribe — but only
            # a signed one can be told apart from a guess at somebody's address.
            unsub = unsubscribe_service.build_url(email.to_address or "")
            mailto_unsub = opt_out.mail_route_is_read(db, user.id)
        # Resolved here rather than at compose time: a draft can wait in the
        # review queue, and what should go out is the resume — and the letter as
        # last edited — as they stand now. An empty result means we could not
        # build them: logged upstream, and recorded by leaving the columns null
        # rather than by failing the send.
        files = email_attachments.files_for_email(db, email)
        # The tracked HTML alternative, when tracking is on. The plain-text part
        # is unchanged either way, so a recipient who blocks images reads exactly
        # what they read before this existed.
        body_html: str | None = None
        if settings.email_tracking_enabled:
            token = email_tracking.ensure_token(email)
            body_html = email_tracking.build_tracked_html(
                email.body_text or "",
                token,
                footer=gmail_service.canspam_footer(unsub) if unsub else "",
                unsubscribe_url=unsub,
            )
        try:
            result = gmail_service.send_email(
                account=account,
                to=email.to_address or "",
                subject=email.subject or "",
                body_text=email.body_text or "",
                unsubscribe_url=unsub,
                mailto_unsubscribe=mailto_unsub,
                thread_id=thread.gmail_thread_id,
                # Gmail's threadId above threads this for recipients reading in
                # Gmail. These two headers are what Outlook, Apple Mail,
                # Thunderbird and every ATS that ingests mail thread on — without
                # them a reply shows up in half the world's clients as a new
                # message that happens to share a subject line. Null on anything
                # that starts a conversation, which is most outreach, and then
                # the MIME is byte-for-byte what it was before.
                in_reply_to=email.in_reply_to,
                references=email.email_references,
                attachments=files.attachments,
                body_html=body_html,
            )
        except gmail_service.GmailNotConfigured as exc:
            _fail_send(db, email, str(exc))
            db.commit()
            _emit_failed(email, str(exc))
            return {"email_id": email_id, "status": "failed", "reason": str(exc)}
        except gmail_service.GmailScopeInsufficient as exc:
            # Alive, but connected without permission to send. No amount of
            # retrying fixes it and every later message would fail identically,
            # so the mailbox is marked exactly as a dead grant is: taken out of
            # `live_accounts` so background work stops choosing it, and shown in
            # the UI as needing to be connected again — which is the remedy.
            _fail_send(db, email, str(exc))
            gmail_accounts.mark_revoked(db, account, str(exc))
            db.commit()
            _emit_failed(email, str(exc))
            return {"email_id": email_id, "status": "gmail_scope", "reason": str(exc)}
        except gmail_service.GmailAuthRevoked as exc:
            # The mailbox lost its grant between being resolved and being used.
            # Fail this message, and mark the account so the next send resolves
            # to a working mailbox — or stops — rather than repeating this.
            _fail_send(db, email, str(exc))
            gmail_accounts.mark_revoked(db, account, str(exc))
            db.commit()
            _emit_failed(email, str(exc))
            return {"email_id": email_id, "status": "gmail_revoked", "reason": str(exc)}
        except (
            gmail_service.HttpError,
            gmail_service.MessageTooLarge,
            *gmail_service._NETWORK_ERRORS,
        ) as exc:
            # Everything else the transport can do, sorted into "try again" and
            # "never going to work" — see
            # `gmail_service.classify_transport_failure`.
            #
            # All of it used to arrive at the wrapper's poison-message backstop
            # as one undifferentiated exception, which meant two wrong answers
            # at once. A 413 — the message's attachments are over Gmail's limit,
            # which is true now and will be true in five minutes — spent three
            # attempts and a quarter of an hour before being written off. And
            # when it was written off, the reason recorded on the row, which is
            # the sentence the review queue shows the candidate about their own
            # message, was ``send failed: <HttpError 413 when requesting
            # https://gmail.googleapis.com/... returned "Request Entity Too
            # Large">``.
            verdict = gmail_service.classify_transport_failure(exc)
            logger.warning(
                "Gmail refused email %s (%s): %s", email_id, verdict.status, exc
            )
            if verdict.terminal:
                _fail_send(db, email, verdict.message)
                db.commit()
                _emit_failed(email, verdict.message)
                return {
                    "email_id": email_id,
                    "status": "failed",
                    "reason": verdict.message,
                }
            # Transient. The row stays QUEUED and the task comes back — after
            # exactly as long as Gmail asked for, when it said, rather than
            # after a number this codebase made up.
            #
            # A budget of its own, larger than the poison one: a Gmail incident
            # is measured in tens of minutes and an unknown exception is not
            # evidence of anything, so the two deserve different amounts of
            # patience. `self.request.retries` is shared between them, which is
            # why this ceiling has to be the larger of the two — the wrapper's
            # backstop only ever sees an exception this clause did not handle.
            if self.request.retries < _TRANSPORT_ATTEMPTS - 1:
                countdown = int(verdict.retry_after or _TRANSPORT_RETRY_DELAY)
                raise self.retry(exc=exc, countdown=max(1, countdown)) from exc
            # The outage outlasted the budget. Parked rather than failed, for
            # the reason `_park_for_review` exists: the message is good, nothing
            # about it caused this, and a DRAFT keeps it where the user can send
            # it by hand while FAILED hides it from the review queue entirely.
            _park_for_review(db, email, verdict.message)
            return {
                "email_id": email_id,
                "status": "held",
                "reason": verdict.message,
            }

        # The message has left the building. Everything from here on is
        # bookkeeping *about* a delivery that already happened, and it is split
        # in two — recorded and committed first, reasoned about second — because
        # the two halves have very different consequences when they fail.
        #
        # Undivided, they shared one commit at the end, and every line after the
        # Gmail call was a chance to lose the only record that the call was ever
        # made. A follow-up row that violates a constraint, a subject experiment
        # that trips over a converged variant, a dropped connection on the commit
        # itself: any of them rolls the session back, `email.status` returns to
        # QUEUED, and the recruiter is now holding a message this database says
        # was never sent. That is not a lost stat. `enqueue_campaign_sends`
        # re-dispatches exactly the QUEUED rows, so the next resume — or the
        # sweep, or a retry — sends the same email to the same person again.
        email.status = EmailStatus.SENT
        email.from_address = account.email
        email.attachment_filename = files.resume_filename
        # Overwrite rather than leave the composer's intent standing: a letter
        # that failed to render did not go out, and the row should say so.
        email.cover_letter_id = files.cover_letter_id
        email.gmail_message_id = result.gmail_message_id
        # The id the *world* knows this message by, so the next message on this
        # thread can name it in ``In-Reply-To``. `gmail_message_id` above cannot:
        # it is Gmail's internal handle and no recipient's client has ever seen
        # one. Null when the read-back failed, which is "unknown" and never a
        # guess — see `thread_headers.headers_for_next_message`.
        email.rfc_message_id = result.rfc_message_id
        email.sent_at = datetime.now(UTC)
        thread.gmail_thread_id = result.gmail_thread_id
        thread.last_message_at = email.sent_at
        # Pin the mailbox now that a Gmail thread id exists, because from here on
        # the id is only redeemable in this account. Threads written before the
        # column existed get resolved once, here, rather than re-inferred on
        # every poll.
        if thread.gmail_account_id is None:
            thread.gmail_account_id = account.id
        # Books the send against the mailbox's reputation ledger: lifetime total,
        # warm-up clock, and the daily counter (rolled over first, so a send on a
        # new day reads 1 rather than yesterday's total plus one). In this half
        # rather than the next because it is a fact about a message that left,
        # and losing it lets the mailbox spend the same allowance twice.
        reputation_service.record_send(account, now=email.sent_at)
        # And the recruiter-inbox row, when this reply belongs to one. The
        # mirror of `_fail_send`: without it a row stayed at whatever it said
        # while the message was in flight, which for the ordinary reviewed reply
        # is ``DRAFTED`` — a draft still waiting, for a reply the user approved
        # and we have just delivered. See
        # `recruiter_reply_service.record_send_success`.
        recruiter_reply_service.record_send_success(db, email)
        db.commit()

        # After the commit, not before: this line asserts a message left *and*
        # that the database says so, and those became two separate facts the
        # moment the bookkeeping was split from the delivery above. Emitting it
        # earlier would log a send that a rolled-back transaction then denied.
        events.emit(
            events.EMAIL_SENT,
            email_id=email.id,
            user_id=user.id,
            campaign_id=campaign.id if campaign else None,
            application_id=application.id,
            gmail_account_id=account.id,
            recipient_domain=events.recipient_domain(email.to_address),
            gmail_message_id=result.gmail_message_id,
            sends_today=sent_24h + 1,
        )

        _after_send(db, email, application, campaign)
        return {"email_id": email_id, "status": "sent", "gmail_id": result.gmail_message_id}
    finally:
        db.close()


def _after_send(
    db: Session, email: Email, application: Application, campaign: Campaign | None
) -> None:
    """Everything a successful send implies, none of which may undo the send.

    Follow-up scheduling, the pipeline stage, the subject-line experiment and the
    campaign's own completion. All of it is worth having and none of it is worth
    a duplicate message, so a failure here is logged and dropped rather than
    allowed back out into the task — where it would strand the send in an
    aborted transaction, and where Celery would re-run a task whose Gmail call
    has already succeeded.
    """
    try:
        # An impression for the subject-line experiment. Booked here rather than
        # at compose time: a draft that is never approved is not an impression.
        subject_ab_service.record_send(db, email.subject_variant_id)

        # Narrowed from a set to one status, and the narrowing is the fix.
        #
        # It used to be ``(QUEUED, FOLLOW_UP)``, and it predates the board
        # by a long way. Every message this task sends comes through here,
        # including the follow-ups — ``follow_up_tasks`` dispatches each due
        # step with ``send_outreach_email`` — and ``follow_up_service`` moves
        # the application to ``FOLLOW_UP`` when it *composes* the step, hours
        # before the throttled sender gets to it. So the send of follow-up 1
        # arrived at an application already on ``FOLLOW_UP`` and pushed it back
        # down to ``OUTREACH_SENT``; composing step 2 read ``OUTREACH_SENT``
        # and pushed it up again. The card oscillated between two columns for
        # the length of every sequence, and each lap left a spurious
        # ``AUTOMATIC`` history row claiming an outreach had just been sent
        # when what went was a nudge.
        #
        # ``QUEUED`` alone is the whole of it: it is the only rung an outreach
        # send moves an application *off*. Deliberately not
        # ``pipeline_board.advances``, which would also be true of the four
        # endings — that function lets a reply reopen a conversation that
        # closed in March, and a send is not a reply. Everything else here is
        # either already past this rung or over, and both should stand.
        if application.status is ApplicationStatus.QUEUED:
            pipeline_board.record_change(
                db,
                application,
                ApplicationStatus.OUTREACH_SENT,
                source=StatusEventSource.AUTOMATIC,
                reason="outreach sent",
            )
        # The follow-up clock starts when the first email actually lands, not
        # when the campaign was created — scheduling here means a campaign that
        # trickles out over hours still gets correctly spaced nudges.
        if campaign is not None:
            schedule_for_application(db, application, campaign, sent_at=email.sent_at)
            # Check whether the experiment now has enough evidence to call it.
            if settings.subject_ab_enabled:
                subject_ab_service.maybe_converge(db, campaign)
        maybe_complete_campaign(db, campaign)
        db.commit()
    except Exception:  # noqa: BLE001 - the message is already delivered
        logger.exception(
            "post-send bookkeeping failed for email %s; the send itself stands",
            email.id,
        )
        db.rollback()


# Re-exported under its old private name: this module was its home while a
# successful send was the only thing that could drain a campaign, and it is
# still the caller that runs most often. It lives in ``outreach_service`` now
# because the *other* drains — a write-off here, an opt-out sweeping a batch, a
# dismissal in the review queue — are spread across three modules, and a check
# only one of them can reach is the bug it was moved to fix.
_maybe_complete_campaign = maybe_complete_campaign


# A message queued longer than this has no live task aiming at it. The bound is
# not a guess: every countdown this module hands the broker for a fresh send goes
# through `send_time.delay_seconds`, which clamps to `MAX_SEND_DELAY_SECONDS`. So
# past that horizon there is nothing left to collide with, and a re-dispatch
# cannot pull a legitimately-waiting message forward.
_STRANDED_AFTER_SECONDS = send_time.MAX_SEND_DELAY_SECONDS

# Per sweep. Large enough to drain a real backlog in a few passes, small enough
# that one sweep cannot flood the broker with ETA tasks the worker holds in
# memory.
_SWEEP_BATCH = 200


@celery_app.task(name="app.tasks.email_tasks.sweep_stranded_sends")
def sweep_stranded_sends(older_than_seconds: int | None = None) -> dict:
    """Re-dispatch queued outreach that no longer has a task behind it.

    QUEUED is not an idle state — it means "a worker is going to send this" — and
    until now the only thing that could ever say that again was
    ``enqueue_campaign_sends``, reachable *only* by resuming the campaign. That
    left two whole classes of message with no way back:

    * **An approved review-queue draft.** ``review._dispatch`` is best-effort by
      design: a broker that is down leaves the row QUEUED, which the docstring
      calls recoverable. Nothing recovered it. The email belongs to a campaign
      that has usually already COMPLETED, and ``_RESUMABLE`` excludes COMPLETED,
      so resume answers 409 and the row is stranded for good.
    * **A follow-up.** ``process_due_follow_ups`` marks its rows SENT once
      actioned, so a dispatch failure there is equally terminal — and follow-ups
      land on applications whose campaign has long since completed.

    On production this had already happened 60 times, the oldest ten days old:
    fifty-seven messages a user approved *by hand* and three the agent queued,
    none of them ever sent, none of them reported anywhere. Reading the pipeline
    they looked queued; they were abandoned.

    This is the same recovery ``form_apply_tasks.sweep_stuck_applications``
    already does for browser runs, for the same reason and with the same
    reasoning about a dropped connection retiring real work permanently.

    Three things keep it from doing harm:

    * **The age bound**, measured from the last dispatch and not only from the
      write. See :data:`_STRANDED_AFTER_SECONDS` — past it, no live task can be
      aiming at the row, so nothing is duplicated or pulled forward.

      ``Email.created_at`` alone could not carry that claim, and the way it
      failed is worth keeping: ``created_at`` never moves, so once a row aged
      past the horizon the bound stayed open forever, and every hourly tick
      published another task with an ETA up to seven days out. Any row the send
      path declines to retire becomes a permanent publisher that way, and the
      reputation gate produces exactly such rows on purpose — it leaves the
      message QUEUED so a temporary hold delays the send rather than dropping
      it. Production held 54 of them behind a mis-read warm-up ramp and the
      worker accumulated 2,502 unacked copies of 53 emails in two days.
      ``_claim_for_send`` meant none of them double-sent, so the only symptom
      was a worker filling with tasks that could not do anything — which looks
      like the broker storm ``BROKER_VISIBILITY_TIMEOUT`` was raised to end and
      is not it.

      So the row also has to have been *dispatched* longer ago than the horizon,
      which is what ``send_dispatched_at`` records. NULL keeps the old meaning
      for a row nothing has dispatched since the column shipped.
    * **The same slot logic.** Re-dispatch goes through ``send_countdown``, so a
      recovered message still lands in the recipient's business morning rather
      than firing the instant it is noticed.
    * **A paused campaign is left alone.** Its rows are QUEUED because the
      sender deliberately put them back; resuming is what should release them.

    Idempotent regardless: ``_claim_for_send`` takes the row under
    ``FOR UPDATE SKIP LOCKED`` and the status is re-checked, so a duplicate
    delivery is a no-op rather than a second copy to the recruiter.
    """
    cutoff_seconds = (
        _STRANDED_AFTER_SECONDS if older_than_seconds is None else older_than_seconds
    )
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=cutoff_seconds)

    db = SessionLocal()
    try:
        stranded = list(
            db.scalars(
                select(Email)
                .join(EmailThread, Email.thread_id == EmailThread.id)
                .join(Application, EmailThread.application_id == Application.id)
                .join(Campaign, Application.campaign_id == Campaign.id)
                .where(
                    Email.direction == EmailDirection.SENT,
                    Email.status == EmailStatus.QUEUED,
                    Email.created_at < cutoff,
                    # NULL is the pre-migration state and the never-dispatched
                    # one, and both mean "no live task known" — so it has to
                    # pass, or this sweep would stop recovering the very rows
                    # it was written for.
                    or_(
                        Email.send_dispatched_at.is_(None),
                        Email.send_dispatched_at < cutoff,
                    ),
                    Campaign.status != CampaignStatus.PAUSED,
                )
                .order_by(Email.id)
                .limit(_SWEEP_BATCH)
            )
        )
        if not stranded:
            return {"stranded": 0, "dispatched": 0, "at": now.isoformat()}

        dispatched = 0
        dispatch_error: str | None = None
        cumulative = 0
        for email in stranded:
            if dispatch_error is not None:
                # The broker already refused a publish this sweep; the next one
                # picks up whatever did not go out.
                break
            cumulative += random.randint(
                settings.min_send_interval_seconds, settings.max_send_interval_seconds
            )
            countdown = send_countdown(db, email, cumulative, now=now)
            try:
                send_outreach_email.apply_async(args=[email.id], countdown=countdown)
            except Exception as exc:  # noqa: BLE001 - one dead broker, not one dead sweep
                logger.warning("stranded send %s could not be dispatched: %s", email.id, exc)
                dispatch_error = str(exc)[:200]
                continue
            # Only after the publish is accepted. Stamping before it would claim
            # a live task for a row whose publish then failed, and the next
            # sweep — the thing that is supposed to retry it — would skip it for
            # a week.
            email.send_dispatched_at = now
            dispatched += 1

        # Two writes to land: every `send_dispatched_at` stamped above, and the
        # timezone `send_countdown` caches back onto the recruiter row as a side
        # effect of resolving it.
        db.commit()
        if dispatched:
            logger.warning(
                "re-dispatched %s stranded send(s) older than %ss",
                dispatched,
                cutoff_seconds,
            )
        return {
            "stranded": len(stranded),
            "dispatched": dispatched,
            "dispatch_error": dispatch_error,
            "at": now.isoformat(),
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.email_tasks.reconcile_warmup_ramps")
def reconcile_warmup_ramps(now: datetime | None = None) -> dict:
    """Age every connected mailbox's warm-up clock to the sending it really did.

    ``reputation_service.adopt_send_history`` fixed the reconnect that drops a
    mailbox to 5/day, but it is reachable from exactly one place: the OAuth
    callback in :mod:`app.routers.gmail`. That makes the fix forward-only, and
    the rows it was written for are the ones that already exist — a mailbox
    reconnected *before* the fix shipped keeps its wrong clock until someone
    disconnects and reconnects it by hand, which nobody has a reason to do
    because the symptom looks like an ordinary warm-up.

    Production is the case in point. Both mailboxes were re-added on 2026-08-06
    when the Google OAuth client was rotated; the primary had been sending since
    2026-06-23 and had 198 delivered messages behind it, but its clock read one
    day old, so the ramp held it at 5/day while 55 approved replies sat QUEUED.
    The hourly ``sweep_stranded_sends`` re-dispatched all 55 every hour and the
    reputation gate turned every one of them away — the queue could not drain at
    the rate the gate allowed, and nothing in the pipeline was broken.

    Safe to run on a schedule because ``adopt_send_history`` is idempotent and
    one-directional: it only ever moves the clock *earlier*, only from delivered
    sends this user made from this exact address, and it declines a start in the
    future. A mailbox that is genuinely new has no history to adopt and stays at
    step 0, so this cannot manufacture warmth — it can only stop the ramp from
    re-counting age the address already served.

    *now* is injectable for the same reason the rest of the warm-up chain's is —
    every decision below is a comparison against it, and a caller that pins the
    rows but not the clock is testing the calendar. Beat passes nothing.
    """
    now = now or datetime.now(UTC)
    db = SessionLocal()
    try:
        accounts = list(
            db.scalars(select(GmailAccount).order_by(GmailAccount.id)).all()
        )
        adjusted: list[dict] = []
        for account in accounts:
            before = reputation_service.warmup_day_limit(account, now=now)
            first_sent_at, sent_count = gmail_accounts.observed_send_history(
                db, account.user_id, account.email
            )
            if not reputation_service.adopt_send_history(
                account, first_sent_at, sent_count, now=now
            ):
                continue
            after = reputation_service.warmup_day_limit(account, now=now)
            adjusted.append(
                {
                    "account_id": account.id,
                    "adopted_from": first_sent_at.isoformat() if first_sent_at else None,
                    "prior_sends": sent_count,
                    "day_limit_before": before,
                    "day_limit_after": after,
                }
            )
            logger.warning(
                "mailbox %s adopted %s prior send(s) since %s: ramp %s/day -> %s/day",
                account.id,
                sent_count,
                first_sent_at,
                before,
                after,
            )
        db.commit()
        return {
            "accounts": len(accounts),
            "adjusted": len(adjusted),
            "details": adjusted,
            "at": now.isoformat(),
        }
    finally:
        db.close()
