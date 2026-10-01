"""Celery tasks for inbox monitoring: poll Gmail threads, classify replies,
and draft AI responses for the user to review.

Polling records *both* sides of a thread. Inbound mail is classified and can move
the application along; outbound mail we find but have no row for — sent from the
user's own client, or before we started keeping Gmail ids — is stored as history
so the inbox shows the conversation as it actually happened. Every message keeps
Gmail's own timestamp, so old mail sorts as old.
"""
from __future__ import annotations

import logging
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core import events
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.pii import mask_email
from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_thread import EmailThread
from app.models.gmail_account import GmailAccount
from app.models.job import JobPosting
from app.models.recruiter import Recruiter
from app.models.status_event import StatusEventSource
from app.models.user import User
from app.services import (
    bounce_service,
    conversation_stage,
    email_attachments,
    gmail_accounts,
    gmail_push,
    gmail_service,
    inbound_scanner,
    opt_out,
    pipeline_board,
    recruiter_follow_up,
    reply_agent,
    reputation_service,
    salary_service,
    subject_ab_service,
    thread_reply_policy,
)
from app.services.ai_composer import CandidateContext
from app.services.follow_up_service import cancel_for_application
from app.services.reply_classifier import classify_reply_detailed, looks_like_bounce
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Map a classified reply intent onto the application's pipeline status. This is
# the application status auto-detection (roadmap improvement 4): the pipeline
# moves itself as recruiters reply, no manual dragging of cards.
_INTENT_TO_STATUS = {
    ReplyIntent.INTERESTED: ApplicationStatus.INTERESTED,
    ReplyIntent.SCHEDULING: ApplicationStatus.SCHEDULING,
    ReplyIntent.OFFER: ApplicationStatus.OFFER,
    ReplyIntent.NOT_INTERESTED: ApplicationStatus.NOT_INTERESTED,
    ReplyIntent.UNSUBSCRIBE: ApplicationStatus.UNSUBSCRIBED,
}
# Intents for which the reply agent auto-drafts a response (for the user to
# review — a draft is never sent unseen). A rejection gets a graceful thank-you;
# an offer/interest/scheduling reply gets a warm, forward-moving draft.
_ACTIONABLE = {
    ReplyIntent.INTERESTED,
    ReplyIntent.SCHEDULING,
    ReplyIntent.QUESTION,
    ReplyIntent.OFFER,
    ReplyIntent.NOT_INTERESTED,
}


def _stale_before(now: datetime) -> datetime | None:
    """The cut-off past which a silent thread stops being polled, or ``None``.

    ``None`` when ``INBOX_POLL_MAX_AGE_DAYS`` is 0, which is the opt-out.
    """
    days = settings.inbox_poll_max_age_days
    if days <= 0:
        return None
    return now - timedelta(days=days)


@celery_app.task
def poll_all_inboxes() -> dict:
    """Beat entrypoint: enqueue a poll task per thread that push isn't covering.

    Push and polling are complementary rather than exclusive. A mailbox with a
    healthy watch is skipped — its replies arrive by webhook in seconds, and
    polling it again is pure duplicated cost. Everything else is polled, which is
    what makes push safe to turn on: the failure mode is "back to the old speed",
    never "replies stop arriving".

    **Bounded by age.** This used to select every thread that had ever been given
    a Gmail id, so the work one tick did was the size of the account's whole
    history and grew with it forever — twelve times an hour, a `threads.get`
    apiece, over conversations that ended months ago. Threads silent for longer
    than ``INBOX_POLL_MAX_AGE_DAYS`` are left alone; ``last_message_at`` is the
    clock, falling back to when the row was created for a thread that has never
    carried a message.

    The bound is a real trade rather than free: `inbound_scanner` deliberately
    skips mail on threads we started, so the poller is the only thing that reads
    them, and a reply arriving past the window is not seen by anything. That is
    why the window is ninety days and configurable rather than tight — the cost
    being removed is unbounded growth, not the last few weeks of coverage.
    """
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        stmt = select(EmailThread).where(EmailThread.gmail_thread_id.is_not(None))
        cutoff = _stale_before(now)
        if cutoff is not None:
            # `created_at` carries the fallback rather than a Python-side
            # default, so the filter stays one query — COALESCE is evaluated by
            # the database and a thread with no message yet is judged on its age.
            stmt = stmt.where(
                func.coalesce(EmailThread.last_message_at, EmailThread.created_at)
                >= cutoff
            )

        threads = db.scalars(stmt).all()

        enqueued = skipped = failed = 0
        dispatch_error: str | None = None
        for thread in threads:
            # Guarded per thread, because the loop is a fan-out and the whole
            # point of a fan-out is that one item's trouble is one item's
            # trouble. It was unguarded, so a single raise ended the sweep where
            # it stood and left every thread after it unpolled — and not only for
            # that tick. The query has no ORDER BY and the rows come back in the
            # same order every time, so the same tail was starved on every tick,
            # which looked exactly like replies on those threads never arriving.
            try:
                if _push_covers(db, thread):
                    skipped += 1
                    continue
            except Exception as exc:  # noqa: BLE001 - one row must not stall the sweep
                logger.warning(
                    "thread %s could not be examined: %s", thread.id, exc, exc_info=True
                )
                failed += 1
                continue

            if dispatch_error is not None:
                # The broker already refused a publish this sweep. Asking it
                # again per thread costs a connect timeout apiece for an answer
                # we have. Kept separate from the guard above on purpose: a row
                # the gate choked on says nothing about whether the broker is up.
                continue
            try:
                poll_thread.delay(thread.id)
            except Exception as exc:  # noqa: BLE001 - broker down; the next tick retries
                logger.warning("thread %s could not be enqueued: %s", thread.id, exc)
                dispatch_error = str(exc)[:200]
                continue
            enqueued += 1

        return {
            "threads_enqueued": enqueued,
            "threads_on_push": skipped,
            "failed": failed,
            # Named rather than logged, so "why did nothing happen?" is
            # answerable from the task result — and so a sweep that covered only
            # part of its list never reads as one that covered all of it.
            "threads_considered": len(threads),
            "dispatch_error": dispatch_error,
        }
    finally:
        db.close()


def _push_covers(db, thread) -> bool:
    """True when this thread's own mailbox has a healthy push subscription.

    "This thread's own", not "the user's primary". Asking the primary meant a
    thread living in a second mailbox — which may have no watch at all — was
    skipped by the poller on the strength of a subscription covering a different
    inbox. It was neither pushed nor polled, and nothing said so.
    """
    if not thread.application_id:
        return False
    application = db.get(Application, thread.application_id)
    if application is None:
        return False
    user = db.get(User, application.user_id)
    account = gmail_accounts.resolve_for_thread(db, thread, user=user)
    return gmail_push.push_is_healthy(account.watch if account is not None else None)


@celery_app.task
def ingest_push_notification(account_id: int, history_id: str) -> dict:
    """Fetch what changed in a mailbox after Gmail said something did.

    The notification carried no message content, so this walks
    ``users.history.list`` from the cursor we last *processed* and polls each
    thread the new messages belong to. Reusing :func:`poll_thread` rather than
    reimplementing ingestion is deliberate: classification, drafting, status
    transitions and bounce handling all live there, and push must not become a
    second code path where those can drift.

    The cursor advances only after that work is done — a crash replays the
    range instead of skipping it — and only as far as the fetch actually read.
    A mailbox with more changes than one fetch's page budget is drained over
    several rounds rather than having its unread pages jumped over; see
    :func:`app.services.gmail_service.list_history`.
    """
    db = SessionLocal()
    try:
        account = db.get(GmailAccount, account_id)
        if account is None or account.watch is None:
            return {"status": "no_watch"}

        watch = account.watch
        cursor = watch.history_id
        if not cursor:
            # Nothing to walk from — take the notification's id as the new
            # baseline and let the next change be the first one we act on.
            gmail_push.advance_cursor(db, watch, history_id, 0)
            db.commit()
            return {"status": "cursor_initialized"}

        try:
            history = gmail_service.list_history(account, cursor)
        except Exception as exc:  # noqa: BLE001 - any failure falls back to polling
            # A cursor Gmail has aged out (404) lands here too. Re-watching
            # resets it; the threads themselves are still covered by the poll.
            logger.warning(
                "gmail history fetch failed for %s: %s", mask_email(account.email), exc
            )
            watch.status = "failed"
            watch.last_error = str(exc)[:500]
            db.commit()
            return {"status": "history_failed"}

        thread_ids = {
            message.get("threadId")
            for record in history.get("history", []) or []
            for added in record.get("messagesAdded", []) or []
            for message in [added.get("message") or {}]
            if message.get("threadId")
        }

        polled = 0
        untracked = 0
        for gmail_thread_id in thread_ids:
            thread = db.scalar(
                select(EmailThread).where(
                    EmailThread.gmail_thread_id == gmail_thread_id
                )
            )
            # A thread we have no row for is mail this product didn't send. That
            # used to be the end of it — and it is precisely the shape of a
            # recruiter writing in for the first time, which is the one message
            # this product most wants to see. Counted here and handed to the
            # inbound scanner below, which is the half of ingestion that reads
            # mail we didn't start.
            if thread is None:
                untracked += 1
                continue
            poll_thread.delay(thread.id)
            polled += 1

        if untracked:
            _scan_for_inbound(account)

        # More changes than one call's page budget. The cursor below advances
        # only as far as the fetch actually read, so the remainder is still
        # reachable — but nothing would come back for it on its own: the next
        # notification only arrives when the *next* message does, and the poller
        # skips this mailbox precisely because its watch is healthy. Ask for the
        # rest now.
        if history.get("truncated"):
            _continue_ingest(account_id, history_id)

        # Only now, with the fetches enqueued, is the range accounted for.
        gmail_push.advance_cursor(
            db, watch, history.get("historyId") or history_id, polled
        )
        db.commit()
        return {
            "status": "ok",
            "threads_polled": polled,
            "untracked_threads": untracked,
            # Named in the result so a mailbox that needed several rounds to
            # drain is visible, rather than looking like one quiet ingest.
            "truncated": bool(history.get("truncated")),
        }
    finally:
        db.close()


def _continue_ingest(account_id: int, history_id: str) -> None:
    """Come back for the rest of a history range that didn't fit one fetch.

    Re-enqueued rather than looped in-process: the range can be arbitrarily
    long, and a task that walks all of it holds a worker and a database session
    for as long as the mailbox is busy. Each round re-reads the cursor, so the
    work is resumable and a crash costs one round rather than the lot.

    Best-effort dispatch. If the broker refuses, the cursor still sits at the
    last record actually processed, so the remainder is picked up by the next
    notification instead of being lost — later than it should be, never skipped.
    """
    if not settings.celery_enabled:
        return
    try:
        ingest_push_notification.apply_async(args=[account_id, history_id])
    except Exception as exc:  # noqa: BLE001 - the cursor already protects the range
        logger.warning(
            "continuation of push ingest for account %s not dispatched: %s",
            account_id,
            exc,
        )


def _scan_for_inbound(account: GmailAccount) -> None:
    """Ask the inbound scanner to look, because mail arrived on no thread of ours.

    Best-effort in every direction. The feature may be off, the user may not have
    asked for their mailbox to be watched, and the broker may be down — none of
    which is this function's problem, because the five-minute beat sweep covers
    the same ground whenever push stops being trustworthy (see
    ``gmail_push.push_covers``). This is what makes detection immediate while
    push is working.

    The scan task debounces itself, so a mailbox getting a burst of deliveries
    produces one scan rather than one per message.
    """
    if not settings.recruiter_reply_enabled or not settings.celery_enabled:
        return
    try:
        from app.models.recruiter_scan_run import TRIGGER_PUSH
        from app.tasks.recruiter_reply_tasks import scan_mailbox

        # Tagged so the stats view can say how much of the detection push is
        # actually responsible for, which is the question that decides whether
        # the 5-minute fallback cadence is safe to keep.
        scan_mailbox.apply_async(
            args=[account.user_id, account.id], kwargs={"trigger": TRIGGER_PUSH}
        )
    except Exception as exc:  # noqa: BLE001 - beat covers the same ground
        logger.info("push-triggered recruiter scan not dispatched: %s", exc)


@celery_app.task
def renew_gmail_watches() -> dict:
    """Beat entrypoint: re-register watches nearing their 7-day expiry.

    A lapsed watch fails silently — Google simply stops publishing — so this
    runs on a timer rather than reacting to an error that never arrives.
    """
    if not gmail_push.is_configured():
        return {"status": "push_not_configured"}

    db = SessionLocal()
    try:
        renewed = failed = 0
        for watch in gmail_push.due_for_renewal(db):
            account = db.get(GmailAccount, watch.gmail_account_id)
            if account is None:
                continue
            refreshed = gmail_push.start_watch(db, account)
            if refreshed.status == "active":
                renewed += 1
            else:
                failed += 1
        db.commit()
        return {"renewed": renewed, "failed": failed}
    finally:
        db.close()


def _message_time(msg: dict) -> datetime:
    """Gmail's own timestamp for a message, falling back to now.

    ``internalDate`` is epoch milliseconds. Using it (rather than the moment we
    happened to poll) is what lets a mailbox's whole history land in the right
    order instead of arriving all at once, dated today.
    """
    raw = msg.get("internalDate")
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _already_booked(recruiter: Recruiter | None, when: datetime) -> bool:
    """True when this contact's bounce watermark already covers *when*.

    A delivery-failure notice stays on the thread forever and nothing marks it
    read, so the only way to count it once is to remember how far we have
    counted. ``last_bounce_at`` holds the timestamp of the newest notice already
    booked; anything at or before it is a re-read, not a new failure.

    A contact with no recorded bounce has counted nothing, so nothing is
    suppressed — the first notice always lands.
    """
    if recruiter is None or recruiter.last_bounce_at is None:
        return False
    # SQLite round-trips naive datetimes; the comparison needs both aware.
    seen = recruiter.last_bounce_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return seen >= when


def _claim_thread(db, thread_id: int) -> EmailThread | None:
    """Take exclusive hold of one thread for the duration of a poll, or ``None``.

    ``SKIP LOCKED`` rather than a plain read, for the same reason
    ``email_tasks._claim_for_send`` uses it on an outbound row: "which messages
    on this thread do we already have?" is answered by reading ``known_ids``
    below, and a read is not a claim. Two polls of one thread both read the same
    set, both find the recruiter's new message missing from it, and both store
    it — so the reply is classified twice, ``_apply_intent`` writes two drafts,
    and with auto-reply armed the recruiter is answered twice by the candidate.
    There is no unique constraint on ``emails.gmail_message_id`` to catch it.

    Two dispatchers aim at the same thread by design. ``poll_all_inboxes`` runs
    every five minutes with no ``expires`` on the beat entry, so a worker that
    falls behind works through a queue of ticks that each enqueue the same
    thread; ``ingest_push_notification`` enqueues it again the moment Gmail says
    something changed; ``POST /inbox/sync`` is a button; and ``task_acks_late``
    replays a poll whose worker died mid-flight.

    The lock is held to the commit that stores the messages, which is exactly
    the window the duplicate opens in. A second worker gets ``None`` at once
    rather than queueing behind it: whoever holds the row is already reading the
    same mailbox, so there is nothing left for this task to find. Postgres
    enforces it; SQLite ignores row locking entirely, which is why the statement
    — not only the outcome — is asserted on in the tests.
    """
    return db.scalar(
        select(EmailThread)
        .where(EmailThread.id == thread_id)
        .with_for_update(skip_locked=True)
    )


@celery_app.task
def poll_thread(thread_id: int) -> dict:
    """Fetch new messages on a thread: store inbound replies (classified, with a
    draft answer) and any outbound mail we have no row for yet."""
    db = SessionLocal()
    try:
        thread = _claim_thread(db, thread_id)
        if thread is None:
            # Either the row is gone or another worker is polling it right now.
            # Told apart only for the log line; both mean "not this task's work".
            if db.scalar(select(EmailThread.id).where(EmailThread.id == thread_id)) is None:
                return {"thread_id": thread_id, "status": "skipped"}
            logger.info("thread %s is already being polled by another worker", thread_id)
            return {"thread_id": thread_id, "status": "in_flight"}
        if not thread.gmail_thread_id:
            return {"thread_id": thread_id, "status": "skipped"}

        application = db.get(Application, thread.application_id)
        user = db.get(User, application.user_id)

        # This thread's own mailbox, not the user's primary. Polling a thread
        # that lives in the second mailbox with the first one's credentials is a
        # Gmail 404, and the replies on it were never ingested.
        account = gmail_accounts.resolve_for_thread(db, thread, user=user)

        try:
            messages = gmail_service.list_thread_messages(
                account, thread.gmail_thread_id
            )
        except gmail_service.GmailNotConfigured as exc:
            return {"thread_id": thread_id, "status": "gmail_unconfigured", "reason": str(exc)}
        except gmail_service.GmailAuthRevoked as exc:
            # Terminal, and retrying re-raises it on every tick. Mark the mailbox
            # so the resolvers stop handing it out and the setup page can ask for
            # a reconnect, then report rather than raise.
            gmail_accounts.mark_revoked(db, account, str(exc))
            db.commit()
            return {"thread_id": thread_id, "status": "gmail_revoked", "reason": str(exc)}
        except gmail_service.GmailThreadNotFound as exc:
            # Degrade rather than raise. A 404 means the mailbox we resolved does
            # not hold this thread — a disconnected account, or a row whose
            # mailbox we could only guess at — and there is nothing a retry can
            # do about it. Raising here failed the whole task and, with
            # ``task_acks_late``, replayed it forever.
            logger.info("poll_thread %s: %s", thread_id, exc)
            return {"thread_id": thread_id, "status": "thread_not_in_mailbox"}

        known_ids = set(
            db.scalars(
                select(Email.gmail_message_id).where(Email.thread_id == thread.id)
            )
        )
        # Messages we sent come back on the thread; match on the *sending*
        # address, which is the connected Gmail — not necessarily the login email.
        #
        # Compared exactly, against the address parsed out of the header rather
        # than against the header text. A substring test over the raw ``From:``
        # claimed two kinds of message that were never ours: a sender at a
        # domain our own address is a prefix of ("candidate@example.com" inside
        # "candidate@example.com.mx"), and any mailer that puts the recipient in
        # its display name ('"reply to candidate@example.com" <no-reply@…>').
        # Either one was filed as outbound history — so the recruiter's reply
        # was never classified, never drafted against, and invisible to
        # ``thread_has_reply``, which is the signal that stops the drip. The
        # scanner already learned this; see ``inbound_scanner.is_from_self``.
        mine = inbound_scanner.own_addresses(user)

        new_replies = 0
        new_sent = 0
        # Replies the routing policy approved for sending. Held until after the
        # commit — see the dispatch below.
        queued: list[Email] = []
        # Whether this thread had already heard back before this pass. Captured
        # up front so the subject-line experiment credits the *first* reply once,
        # rather than once per poll that happens to find one.
        had_reply_before = (
            db.scalar(
                select(Email.id).where(
                    Email.thread_id == thread.id,
                    Email.direction == EmailDirection.RECEIVED,
                )
            )
            is not None
        )
        # The newest message stored this pass, so backfilling a year-old thread
        # doesn't shove it to the top of the list as if it just spoke.
        newest: datetime | None = None
        for msg in messages:
            gmail_id = msg.get("id")
            if not gmail_id or gmail_id in known_ids:
                continue
            # Shared with the recruiter scanner rather than rebuilt here. Both
            # copies read Gmail's headers exactly as they travelled, so an
            # RFC 2047 subject — every non-ASCII one — was stored, classified,
            # bounce-checked and echoed into the reply's ``Re:`` line as its own
            # base64. See ``inbound_scanner.headers_of``.
            headers = inbound_scanner.headers_of(msg)
            from_addr = headers.get("from", "")
            when = _message_time(msg)

            # Mail from the user themselves is not a reply, but it *is* part of
            # the conversation — record it so the inbox shows what went out,
            # including anything sent from their own client.
            _, sender = inbound_scanner.parse_from(from_addr)
            if inbound_scanner.is_from_self(sender, mine):
                db.add(
                    Email(
                        thread_id=thread.id,
                        direction=EmailDirection.SENT,
                        status=EmailStatus.SENT,
                        from_address=from_addr,
                        to_address=headers.get("to"),
                        subject=headers.get("subject"),
                        body_text=gmail_service.extract_plain_text(msg),
                        gmail_message_id=gmail_id,
                        sent_at=when,
                        # Mail the user sent from their own client carries files
                        # our sender never resolved, so ``attachment_filename``
                        # is empty for it and this is the only record of them.
                        inbound_attachments=[
                            item.as_dict()
                            for item in gmail_service.list_attachments(msg)
                        ],
                    )
                )
                thread.message_count += 1
                new_sent += 1
                newest = when if newest is None else max(newest, when)
                continue

            body = gmail_service.extract_plain_text(msg)
            subject = headers.get("subject", "")

            # A delivery-failure notice is not a recruiter reply — it's a bounce.
            # Hard and soft are handled very differently: a permanent failure
            # suppresses the address and counts against the mailbox's reputation,
            # while a full inbox does neither (see bounce_service). Neither
            # creates a "reply", and neither sets opted_out — that flag records
            # consent, not deliverability.
            if looks_like_bounce(from_addr, subject, body):
                verdict = bounce_service.classify_bounce(subject, body)
                recruiter = db.get(Recruiter, application.recruiter_id)
                # One notice, counted once — however many times we read it.
                #
                # This branch stores no ``Email`` row, so the DSN never enters
                # ``known_ids`` above: every poll re-read the same notice and
                # booked it again. ``bounce_count`` is a lifetime counter with no
                # ledger behind it, so nothing noticed. In production two dead
                # addresses reached 4081 bounces against 20 real sends — a
                # 20400% rate that paused the mailbox, re-paused it every five
                # minutes forever, and stopped all outbound mail. The audit
                # ledger took the same beating (2754 rows for those two
                # addresses), and one merely *full* mailbox was re-counted past
                # ``SOFT_BOUNCE_LIMIT`` to 1958 and written off as permanently
                # undeliverable — a live contact lost to arithmetic.
                #
                # Deduped on the notice's own Gmail timestamp rather than on a
                # stored seen-marker, because a DSN stored as ``RECEIVED`` would
                # read as the recruiter replying to the twenty-one places that
                # count a reply — see ``test_a_bounce_is_never_stored_as_a_reply``.
                # ``internalDate`` is fixed for the life of the message, so
                # ``last_bounce_at`` becomes a watermark: a notice at or before
                # it has already been booked. Distinct notices carry distinct
                # timestamps and still each count.
                if bounce_service.is_suppressed(recruiter):
                    continue
                if _already_booked(recruiter, when):
                    continue
                bounce_service.record_bounce(
                    db,
                    user_id=application.user_id,
                    recruiter=recruiter,
                    verdict=verdict,
                    address=recruiter.email if recruiter else None,
                    # The notice's time, not the moment we read it — that is what
                    # makes the watermark above comparable to the next DSN.
                    now=when,
                )
                # Against the mailbox that actually sent the outreach. Booked on
                # the primary, a hard bounce on the second mailbox's mail could
                # pause the first one — punishing a mailbox for a delivery it had
                # nothing to do with, while leaving the real offender's rate
                # looking clean.
                if verdict.is_hard and account is not None:
                    reputation_service.record_bounce(account)
                cancel_for_application(
                    db,
                    application.id,
                    "outreach hard-bounced" if verdict.is_hard else "outreach soft-bounced",
                )
                continue

            # Both halves of the classification are kept. The intent moves the
            # pipeline; the confidence decides whether the reply we draft from it
            # may go back out unread — see ``services/thread_reply_policy``.
            #
            # The subject and the headers go with the body. They are the half of
            # the message a *machine* wrote, and an automatic responder announces
            # itself there and frequently nowhere else — see
            # ``reply_classifier.looks_like_auto_reply``. Reading them was free:
            # both were already parsed above and then dropped.
            classified = classify_reply_detailed(
                body, subject=subject, headers=headers
            )
            intent = classified.intent

            inbound = Email(
                thread_id=thread.id,
                direction=EmailDirection.RECEIVED,
                status=EmailStatus.RECEIVED,
                from_address=from_addr,
                to_address=user.email,
                subject=headers.get("subject"),
                body_text=body,
                intent=intent,
                intent_confidence=classified.confidence,
                gmail_message_id=gmail_id,
                # Kept so our next message on this thread can chain off it. Same
                # reason as everywhere else: Gmail's threadId threads for Gmail
                # readers and nobody else.
                in_reply_to=headers.get("message-id"),
                email_references=headers.get("references"),
                sent_at=when,
                # What the recruiter attached. Described, not downloaded — the
                # bytes are redeemed from Gmail when the user opens one.
                inbound_attachments=[
                    item.as_dict() for item in gmail_service.list_attachments(msg)
                ],
            )
            db.add(inbound)
            thread.message_count += 1
            new_replies += 1
            newest = when if newest is None else max(newest, when)

            # The other half of the product, and the half that was silent. A
            # send is at least visible as an outbound row someone went looking
            # for; a reply that never arrived looks exactly like a reply that
            # arrived and was classified as noise, and telling those apart is
            # the whole of diagnosing a stalled inbound agent. The intent and
            # its confidence ride along because they are what decides whether
            # this reply is answered automatically or waits for a human.
            events.emit(
                events.REPLY_RECEIVED,
                thread_id=thread.id,
                application_id=thread.application_id,
                user_id=user.id,
                sender_domain=events.recipient_domain(from_addr),
                intent=intent.value if intent is not None else None,
                intent_confidence=classified.confidence,
                # See the matching emit in `recruiter_reply_service`: the two
                # inbound pipelines share this event and are worth telling
                # apart, and until both emitted it the field would have been
                # a distinction with only one side.
                pipeline="thread_poll",
            )

            # If this thread began as inbound recruiter mail, the recruiter has
            # now answered the answer we sent. Nothing here re-routes or
            # re-replies — poll_thread already drafts rather than sends — but the
            # Recruiter Inbox row still read REPLIED and told the candidate
            # nothing. Escalating makes it visible in "Needs you".
            recruiter_follow_up.note_thread_follow_up(db, thread.application_id)

            # A reply proves the address works — forget any transient failures
            # against it. Hard bounces are deliberately not revived here.
            bounce_service.clear_soft_bounces(
                db.get(Recruiter, application.recruiter_id)
            )
            # Credit the subject line that started this thread, once. Reply rate
            # is the metric that matters; open rate is the one that converges
            # fast enough to act on. The experiment reports both.
            if not had_reply_before and new_replies == 1:
                outreach = db.scalar(
                    select(Email)
                    .where(
                        Email.thread_id == thread.id,
                        Email.direction == EmailDirection.SENT,
                        Email.subject_variant_id.is_not(None),
                    )
                    .order_by(Email.id)
                )
                if outreach is not None:
                    subject_ab_service.record_reply(db, outreach.subject_variant_id)

            draft = _apply_intent(
                db, application, thread, user, intent, body, inbound=inbound
            )
            if draft is not None and draft.status == EmailStatus.QUEUED:
                queued.append(draft)

        if newest is not None:
            current = thread.last_message_at
            if current is not None and current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            thread.last_message_at = max(newest, current) if current else newest
        db.commit()
        # After the commit, never before: a task picked up by a worker before its
        # row exists finds nothing to send, and anything that rolled the session
        # back after the dispatch would have mailed a recruiter a message this
        # database has no record of.
        sent = _dispatch_auto_replies(queued)
        return {
            "thread_id": thread_id,
            "new_replies": new_replies,
            "new_sent": new_sent,
            # What the routing did with this poll's replies, so the journal
            # answers "why did nothing send?" without a database session.
            "auto_replied": sent,
            "drafted": new_replies - sent,
        }
    finally:
        db.close()


def _dispatch_auto_replies(emails: list[Email]) -> int:
    """Hand every auto-approved reply to the throttled sender. Best-effort.

    A broker that is down leaves the rows ``QUEUED``, which is the recoverable
    state: ``enqueue_campaign_sends`` re-dispatches exactly those whenever the
    campaign is resumed, and the user can send them by hand meanwhile. So a
    failure here delays a reply rather than losing it, and must never take the
    poll down with it — the replies are already recorded by the time we get here.

    The countdown is deliberate rather than immediate. A reply that lands the
    same second the recruiter's message arrived reads as a machine, and the
    reputation gate at the far end is happier with mail that arrives spread out.
    """
    if not emails or not settings.celery_enabled:
        return 0
    from app.tasks.email_tasks import send_outreach_email

    sent = 0
    for email in emails:
        countdown = random.randint(
            settings.min_send_interval_seconds, settings.max_send_interval_seconds
        )
        try:
            send_outreach_email.apply_async(args=[email.id], countdown=countdown)
        except Exception as exc:  # noqa: BLE001 - broker down; the row stays QUEUED
            logger.warning("auto-reply dispatch failed for email %s: %s", email.id, exc)
            continue
        sent += 1
    return sent


def _may_not_write_to(db, application) -> str | None:
    """Why the agent must not compose for this application's contact, or None.

    The three axes, in the order the rest of the product asks them, and they
    are three because they are three different facts:

    * ``opted_out`` is the **recipient's** consent, and it holds even when they
      are the one who just wrote — which on this path is always.
    * ``excluded_at`` is the **user's own** decision to stop writing to this
      contact. A draft the review queue offers a send button for is exactly
      what that flag ruled out.
    * a hard bounce is **deliverability**. A reply to that address is mail that
      cannot arrive, and each attempt is another bounce against the mailbox the
      candidate's whole pipeline depends on.

    Returned as a sentence for the log rather than raised, because a suppressed
    contact is an ordinary outcome of polling a thread and not an error.
    """
    recruiter = db.get(Recruiter, application.recruiter_id)
    if recruiter is None:
        return None
    if recruiter.opted_out:
        return "the recipient opted out"
    if recruiter.is_excluded:
        return "you excluded this contact"
    if bounce_service.is_suppressed(recruiter):
        return "the address has hard-bounced"
    return None


def _apply_intent(
    db,
    application,
    thread,
    user,
    intent: ReplyIntent,
    body: str,
    *,
    inbound: Email | None = None,
) -> Email | None:
    """Update status, handle unsubscribe, and answer the actionable intents.

    Returns the reply row when one was written, so the caller can dispatch it
    after the commit — ``QUEUED`` on it means the routing policy approved sending
    it unread, ``DRAFT`` means it is waiting for the user. Returns ``None`` when
    nothing was written.

    *inbound* is the message being answered. It carries the classifier's
    confidence, which is what the policy routes on, and the RFC message-id the
    reply has to chain off to thread anywhere other than Gmail.
    """
    # Both branches go through the board's recorder so the history reads the
    # same whether the classifier moved the card or a person did — and so an
    # interview that shows up in the funnel can always be traced back to the
    # reply that was read that way.
    new_status = _INTENT_TO_STATUS.get(intent)
    if (
        new_status is None
        # An out-of-office is the mail server talking, and moving the card to
        # REPLIED on one is wrong twice over. It tells the user a recruiter
        # engaged when nobody has read their email yet, and — because REPLIED is
        # in ``follow_up_service.REPLIED_STATUSES`` — it cancelled the rest of
        # the sequence. A candidate who wrote to somebody on holiday got one
        # touch, an autoresponder, and silence: the drip that existed to catch
        # exactly this was stopped by it.
        and intent is not ReplyIntent.OUT_OF_OFFICE
        # Any rung before REPLIED, not just the first one. See
        # `pipeline_board.not_yet_replied` for what asking `== OUTREACH_SENT`
        # cost: a recruiter answering after a nudge is answering an application
        # already at FOLLOW_UP, and their reply was recorded nowhere.
        and pipeline_board.not_yet_replied(application.status)
    ):
        new_status = ApplicationStatus.REPLIED
    # Forward, or to an ending, and nowhere else. The classifier reads the newest
    # message on its own, so a recruiter who is mid-offer and writes "still
    # interested?" reads as INTERESTED and used to drag the card back down the
    # board with it — losing the offer from the funnel and putting a booked
    # interview back in reach of the follow-up drip. See pipeline_board.advances.
    if (
        new_status is not None
        and new_status != application.status
        and pipeline_board.advances(application.status, new_status)
    ):
        pipeline_board.record_change(
            db,
            application,
            new_status,
            source=StatusEventSource.AUTOMATIC,
            reason=f"recruiter replied ({intent.value.lower()})",
        )

    # A human wrote back: stop the drip. An auto-responder did not, so an
    # out-of-office leaves the sequence running.
    if intent != ReplyIntent.OUT_OF_OFFICE:
        campaign = db.get(Campaign, application.campaign_id)
        if campaign is None or campaign.follow_up_stop_on_reply:
            cancel_for_application(
                db, application.id, f"recruiter replied ({intent.value.lower()})"
            )

    if intent == ReplyIntent.UNSUBSCRIBE:
        recruiter = db.get(Recruiter, application.recruiter_id)
        if recruiter:
            # Through the same door the ``List-Unsubscribe`` header goes
            # through. This branch used to do its own half: set the flag, write
            # off the queued batch — someone who replies "take me off this
            # list" is usually answering the first message of a batch still
            # trickling behind it — and cancel the follow-ups for *this
            # application*. Which is one application. A recruiter reached from
            # two campaigns kept the second campaign's sequence scheduled and
            # showing on the tracker as mail still coming, and the second
            # application stayed on whatever rung it had reached, unlocked on
            # the board, counted as live in the funnel.
            #
            # ``opt_out.suppress`` is that work done once, for every
            # application the contact appears in. See its docstring for the two
            # halves the two doors each used to be missing.
            opt_out.suppress(db, recruiter, reason="recruiter unsubscribed")
        return None

    if intent not in _ACTIONABLE:
        return None

    blocked = _may_not_write_to(db, application)
    if blocked is not None:
        # Nothing is composed. This is the fourth path in the product that
        # writes a message, and it was the one that asked none of the three
        # questions the other three ask before composing:
        #
        #   outreach_service._send_batch          opted_out / is_excluded / hard bounce
        #   follow_up_service (the drain)         "                        "
        #   recruiter_reply_service._suppression  "                        "
        #   email_tasks._send_outreach_email      the same three, at the transport
        #
        # That last one is a *backstop*, and its own comment says so: it exists
        # for consent withdrawn in the hours between composing and sending. It
        # was carrying this whole case instead. The mail did not go — the
        # backstop held — but an auto-approved reply still reached ``QUEUED``
        # and was dispatched, and the tracker showed a reply on its way to a
        # recruiter who had opted out until the send task wrote it off
        # ``FAILED`` hours later. And on the ordinary path — a draft — the
        # review queue offered the candidate a send button for a contact they
        # had just excluded.
        #
        # ``_apply_intent``'s other side effects have already run and stay:
        # the status moved because the recruiter really did write back, and the
        # drip stopped because they really did. What does not happen is us
        # writing to them.
        logger.info("thread %s: no reply drafted (%s)", thread.id, blocked)
        return None

    cand = CandidateContext(name=user.full_name or user.email.split("@")[0])
    # Scout drafts against the whole conversation, not just the message that
    # triggered this: a thread where the candidate already gave availability
    # should not produce a draft offering it again. The market band is passed
    # so a below-band number in the thread turns the reply into a
    # negotiation rather than a grateful acknowledgement.
    draft = reply_agent.draft_for_thread(
        cand,
        thread,
        intent=intent,
        band=_market_band(db, application),
        latest_inbound=body,
    )

    # Does this go back out on its own, or wait for the candidate? The policy is
    # pure and lives in one module; everything database-shaped it needs is
    # resolved here. A below-market offer is held whatever the policy says — the
    # draft is a negotiating position, and that is nobody's to take but theirs.
    confidence = getattr(inbound, "intent_confidence", None)
    decision = thread_reply_policy.decide(
        intent,
        confidence,
        user.autopilot,
        drafted_with=draft.generated_with,
    )
    if decision.auto_send and draft.negotiation_detected:
        decision = thread_reply_policy.ReplyDecision(
            False,
            thread_reply_policy.REASON_INTENT,
            "The thread quotes a number below your market band — yours to answer.",
            decision.threshold,
        )

    # RFC 5322 §3.6.4. Gmail's threadId threads this for people reading in
    # Gmail; these two headers are what everything else threads on. It mattered
    # less when every reply waited for a human to press send from a screen that
    # said which conversation it was on — a reply that sends itself has to
    # arrive *in* the conversation, or it reads as a stranger's cold email that
    # happens to share a subject line.
    in_reply_to, references = inbound_scanner.reply_headers_for(
        getattr(inbound, "in_reply_to", None),
        getattr(inbound, "email_references", None),
    )

    # A recruiter three messages into scheduling an interview already has the
    # CV — it went with the outreach that started this thread. Attaching it
    # again to "Thursday works for me" reads as a candidate who has lost the
    # thread, and nobody is going to catch it now that these send themselves.
    # Suppressed rather than merely not resolved, so the review screen shows it
    # as removed and one click puts it back. Mirrors what
    # ``recruiter_reply_service`` decided for the mail it answers.
    already_wrote = any(
        e.direction == EmailDirection.SENT and e.status == EmailStatus.SENT
        for e in thread.emails
    )

    reply = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.QUEUED if decision.auto_send else EmailStatus.DRAFT,
        suppressed_attachments=[email_attachments.RESUME] if already_wrote else [],
        to_address=application and db.get(Recruiter, application.recruiter_id).email,
        # Not ``f"Re: {thread.subject}"``. That prepended a second marker to a
        # subject which already carried one behind a gateway tag, and turned a
        # thread with no subject at all into the literal string ``"Re:"``.
        subject=conversation_stage.reply_subject(thread.subject),
        body_text=draft.body,
        draft_template=draft.template,
        draft_note=draft.note,
        # How it was written, kept beside what it says. The routing decision
        # above already used this; recording it is what lets a later pass —
        # ``route_existing_reply_drafts`` — apply the same rule instead of
        # assuming an answer.
        drafted_with=draft.generated_with,
        # The confidence that routed it, kept on the row it routed. Without it
        # the inbox could say a reply was held but not what it was held on, and
        # the re-evaluation task would have to classify the thread again.
        intent_confidence=confidence,
        needs_attention=decision.needs_attention,
        attention_reason=decision.reason if decision.needs_attention else None,
        # Stamped at the moment the status is chosen, not at delivery — the same
        # contract every other producer of unreviewed mail follows, and what lets
        # ``send_policy`` tell a pending unreviewed send from a pending approved
        # one.
        auto_sent=decision.auto_send,
        in_reply_to=in_reply_to,
        email_references=references,
    )
    db.add(reply)
    thread.message_count += 1
    logger.info(
        "thread %s: %s reply to %s (%s)",
        thread.id,
        "queued" if decision.auto_send else "drafted",
        intent.value,
        decision.code,
    )
    return reply


def _market_band(db, application):
    """The salary band for the role this thread is about, when there is one.

    Best-effort: an application with no posting attached (manual outreach, a
    recruiter who found the candidate) simply has no band, and the reply agent
    treats a missing band as "no negotiation signal" rather than an error.
    """
    posting_id = getattr(application, "job_posting_id", None)
    if not posting_id:
        return None
    posting = db.get(JobPosting, posting_id)
    if posting is None:
        return None
    try:
        return salary_service.insight_for_posting(db, posting).band
    except Exception:  # pragma: no cover - benchmarks must never break the inbox
        logger.warning("salary band lookup failed for posting %s", posting_id, exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Routing the drafts that were written before routing existed                  #
# --------------------------------------------------------------------------- #


@celery_app.task
def reevaluate_pending_replies(
    user_id: int | None = None,
    limit: int = 500,
    dry_run: bool = True,
    max_age_days: int = 14,
) -> dict:
    """Put existing reply drafts through the routing policy, once.

    Every draft written before :func:`_apply_intent` learned to route is sitting
    at ``DRAFT`` with ``needs_attention`` false — which, under the new inbox,
    reads as "nothing has looked at this" rather than as what it is. This walks
    them: the ones that would have sent themselves are queued, and the rest are
    flagged with the reason they were held, so the "Needs review" filter is
    complete on the day it ships rather than only for mail that arrives after it.

    **Dry run by default.** The other setting sends real email to real recruiters
    in a batch, which is not a thing to do as a side effect of typing a command
    with a plausible name. Pass ``dry_run=False`` deliberately, having read what
    the dry run printed.

    Only agent-written drafts are considered — ``draft_template`` is the marker.
    A message the *user* composed and left unsent is theirs, and this task has no
    business queueing it.

    **Old conversations are held rather than answered.** The live path replies
    within minutes of a recruiter writing; this one is catching up on a backlog,
    and a reply to a question somebody asked three weeks ago does not read as
    prompt — it reads as a system that has just woken up. So a draft whose
    inbound message is older than *max_age_days* is flagged for the user instead
    of sent, whatever its confidence. It is still a good draft; it just wants a
    human to decide whether answering now is right at all.

    Idempotent. A second run sees the first run's drafts already flagged and
    leaves them alone, so a partial run can simply be repeated.
    """
    db = SessionLocal()
    try:
        stmt = (
            select(Email)
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .join(Application, EmailThread.application_id == Application.id)
            .where(
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.DRAFT,
                # Written by the agent, not by the user.
                Email.draft_template.is_not(None),
                # And not since taken over by one. ``draft_template`` says who
                # composed the first version; it does not say whether the user
                # has rewritten it, and this task *sends* what it walks. A
                # candidate who edited a reply and left it for later did not
                # leave it for a batch job to send unread — see
                # ``Email.user_owned_at``.
                Email.user_owned_at.is_(None),
                # Not already routed by a previous run of this task.
                Email.needs_attention.is_(False),
            )
            .order_by(Email.id)
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(Application.user_id == user_id)

        queued: list[Email] = []
        held = 0
        skipped = 0
        plan: list[dict] = []
        now = datetime.now(UTC)

        for draft in db.scalars(stmt).all():
            thread = db.get(EmailThread, draft.thread_id)
            application = db.get(Application, thread.application_id) if thread else None
            user = db.get(User, application.user_id) if application else None
            if thread is None or user is None:
                skipped += 1
                continue

            inbound = _latest_inbound(db, thread.id, before_id=draft.id)
            if inbound is None:
                # Nothing on the thread to have replied to — a follow-up the
                # agent wrote, or a draft whose inbound row is gone. There is no
                # intent to route on, so it is left exactly as it is rather than
                # guessed at or flagged.
                skipped += 1
                plan.append(_plan_row(draft, None, None, "no_inbound"))
                continue

            if inbound.intent is None:
                # **Not this pipeline's draft.** Only ``poll_thread`` classifies
                # an inbound row into ``Email.intent``; the recruiter-inbox
                # scanner stores its verdict on ``recruiter_emails`` and leaves
                # this column null. So a null here means the draft was written
                # and *already routed* by ``recruiter_reply_service``, under its
                # own bands, switches and daily cap — and this task queueing it
                # would be one policy silently overruling another's decision to
                # hold. It is left alone, and it is not flagged either: it is not
                # waiting on the user, it is waiting on that pipeline.
                #
                # On production this was 144 of 162 drafts, which is the entire
                # reason this branch is not the same as the one above.
                skipped += 1
                plan.append(_plan_row(draft, None, None, "other_pipeline"))
                continue

            age = _age_days(inbound, now)
            classified = _confidence_for(db, inbound, dry_run)
            decision = thread_reply_policy.decide(
                inbound.intent,
                classified.confidence if classified.intent is inbound.intent else None,
                user.autopilot,
                # Read from the row rather than assumed. This used to pass a flat
                # ``"llm"`` because nothing recorded the answer, which quietly
                # inverted the policy's most important rule: the drafts this task
                # walks are the backlog, the backlog is what accumulates while
                # the model chain is down, and a template fallback is exactly
                # what that produces. So the population most likely to be a
                # template was the one being told it was a generation. ``None``
                # — a row older than the column — holds.
                drafted_with=draft.drafted_with,
            )
            draft.intent_confidence = classified.confidence

            if decision.auto_send and age is not None and age > max_age_days:
                decision = thread_reply_policy.ReplyDecision(
                    False,
                    "stale",
                    f"Their message is {age} days old — answering now is your call.",
                    decision.threshold,
                )

            if decision.auto_send:
                if not dry_run:
                    draft.status = EmailStatus.QUEUED
                    draft.auto_sent = True
                    draft.needs_attention = False
                    draft.attention_reason = None
                queued.append(draft)
            else:
                _hold(draft, decision.reason, dry_run)
                held += 1
            plan.append(
                _plan_row(
                    draft,
                    inbound.intent,
                    classified.confidence,
                    decision.code,
                    age=age,
                )
            )

        if not dry_run:
            db.commit()
            sent = _dispatch_auto_replies(queued)
        else:
            db.rollback()
            sent = 0

        return {
            "dry_run": dry_run,
            "considered": len(plan),
            # On a dry run this is what *would* be queued. The key is the same
            # either way so the two runs can be diffed against each other.
            "queued": len(queued),
            "dispatched": sent,
            "held": held,
            "skipped": skipped,
            "plan": plan,
        }
    finally:
        db.close()


def _plan_row(draft, intent, confidence, code: str, age: int | None = None) -> dict:
    """One line of the report, for the console and for the task result."""
    return {
        "email_id": draft.id,
        "thread_id": draft.thread_id,
        "intent": intent.value if intent else None,
        "confidence": None if confidence is None else round(confidence, 2),
        "age_days": age,
        "outcome": code,
    }


def _age_days(inbound: Email, now: datetime) -> int | None:
    """How long ago the message being answered arrived, in whole days."""
    when = inbound.sent_at or inbound.created_at
    if when is None:  # pragma: no cover - both columns are always written
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, (now - when).days)


def _hold(draft: Email, reason: str, dry_run: bool) -> None:
    if dry_run:
        return
    draft.needs_attention = True
    draft.attention_reason = reason[:160]


def _latest_inbound(db, thread_id: int, *, before_id: int) -> Email | None:
    """The recruiter message this draft answers: the newest one before it.

    By id rather than by timestamp, because that is the order the draft was
    written in — a backfilled message carrying an older Gmail timestamp is not
    what the agent was replying to.
    """
    return db.scalar(
        select(Email)
        .where(
            Email.thread_id == thread_id,
            Email.direction == EmailDirection.RECEIVED,
            Email.id < before_id,
        )
        .order_by(Email.id.desc())
        .limit(1)
    )


def _confidence_for(db, inbound: Email, dry_run: bool):
    """The classifier's confidence in *inbound*, computed if it was never stored.

    Re-classifying is a model call per draft, which is the price of routing mail
    that was classified before confidence existed. The answer is written back
    (outside a dry run) so this is paid once.

    The returned classification carries its *own* intent, which the caller
    compares against the stored one: a classifier that has since changed its mind
    about a message is exactly the disagreement a human should settle, and the
    caller holds the draft when the two differ.
    """
    from app.services.reply_classifier import Classification

    if inbound.intent_confidence is not None:
        return Classification(inbound.intent, inbound.intent_confidence, "stored")
    classified = classify_reply_detailed(inbound.body_text or "")
    if not dry_run:
        inbound.intent_confidence = classified.confidence
    return classified
