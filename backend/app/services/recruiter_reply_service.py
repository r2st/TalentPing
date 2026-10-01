"""Orchestration for inbound recruiter mail: scan, classify, match, route, write.

The Celery tasks and the API's "scan now" button both need the same moves, so
they live here rather than in either caller — the same reason
:mod:`app.services.smart_apply_service` exists. Sessions come from the caller;
nothing here opens or closes one.

**The handoff.** When a message routes to a reply, this module materialises the
chain the rest of the product already understands::

    Recruiter -> Campaign -> Application -> EmailThread -> Email

It has to. ``Email.thread_id``, ``EmailThread.application_id`` and
``Application.campaign_id`` are all non-nullable, and ``routers/review`` proves
ownership by joining ``Email -> EmailThread -> Application.user_id``. Building
the chain means the generated reply is reviewable, editable and sendable through
the endpoints that already exist, with the throttling and reputation gates that
already work. The alternative — a second send path for inbound replies — is how
you end up with one email that can be sent twice.

The campaign is a lazily-created per-user row named "Inbound recruiter replies",
chosen over making ``campaign_id`` nullable: analytics, the tracker and the
pipeline all assume a campaign exists, and a synthetic row is a far smaller blast
radius than a nullable foreign key. Its follow-ups are off — chasing a recruiter
who contacted *us* inverts the dynamic.

A flagged message materialises **none** of it. A flag is one row, and dismissing
it is one delete.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import events
from app.core.config import settings
from app.models.application import Application, ApplicationStatus
from app.models.campaign import (
    INBOUND_CAMPAIGN_NAME,
    Campaign,
    CampaignStatus,
)
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.gmail_account import GmailAccount
from app.models.recruiter import Recruiter
from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailKind,
    RecruiterEmailStatus,
    RecruiterReplyPreference,
    ReplyRoute,
)
from app.models.recruiter_scan_run import TRIGGER_BEAT, RecruiterScanRun
from app.models.recruiter_scan_skip import RecruiterScanSkip
from app.models.user import User
from app.services import (
    bounce_service,
    classifier_feedback,
    conversation_stage,
    email_attachments,
    fit_scorer,
    inbound_matcher,
    inbound_reply,
    inbound_scanner,
    opt_out,
    profile_service,
    recruiter_classifier,
    recruiter_follow_up,
    reply_agent,
    reply_classifier,
    reply_routing,
    reputation_service,
    resume_selector,
)
from app.services.ai_composer import CandidateContext
from app.services.inbound_scanner import (
    CandidateMessage,
    DismissedMessage,
    ScanResult,
)
from app.services.jd_parser import parse_job
from app.services.profile_service import ScoringTarget

logger = logging.getLogger(__name__)

# Re-exported: this module's `__all__` has published it since before the model
# knew about containers, and it is the same string. The definition moved to
# `app.models.campaign` because `routers/campaigns` needs to recognise a
# container without importing the reply pipeline to do it.


# --------------------------------------------------------------------------- #
# Preferences                                                                  #
# --------------------------------------------------------------------------- #


def preference_for(db: Session, user: User) -> RecruiterReplyPreference:
    """This user's inbound settings, created off (the default) if absent."""
    pref = db.scalar(
        select(RecruiterReplyPreference).where(
            RecruiterReplyPreference.user_id == user.id
        )
    )
    if pref is None:
        pref = RecruiterReplyPreference(user_id=user.id)
        db.add(pref)
        db.flush()
    return pref


def auto_replies_last_24h(db: Session, user_id: int) -> int:
    """How many unreviewed replies have gone out for this user today.

    Dated by **when the reply went**, not by when the message it answers
    arrived. Those are the same moment on the live path and nowhere near it on
    the retry sweep, which is the whole reason this is not simply
    ``created_at >= since``: ``retry_degraded_classifications`` re-reads backlog
    up to ``RECRUITER_RETRY_MAX_AGE_DAYS`` old and routes a corrected reading
    AUTO, queueing the reply now against a ``RecruiterEmail`` row created days
    ago. Every one of those fell outside the window at the moment it was
    counted, so the sweep read the cap as zero-spent on every run and the daily
    ceiling never bound it at all — a backlog of hundreds could go out
    unreviewed from one mailbox in an afternoon, which is precisely the burst
    this cap exists to keep off the user's sending reputation.

    The reply row is the clock: it is QUEUED the instant a route of AUTO is
    chosen and carries ``sent_at`` once it leaves. ``route == AUTO`` stays the
    discriminator for *unreviewed* — a draft the user read and approved is
    routed DRAFT or FLAG and never counts against the agent's allowance, the
    same rule ``send_policy`` applies to outreach. A reply whose send FAILED is
    excluded by construction: it did not go.

    Rows with no reply email fall back to their own ``created_at``. Production
    rows always have one, but a route decision recorded without one is still a
    decision to send, and dropping it would make this undercount.
    """
    since = datetime.now(UTC) - timedelta(hours=24)
    return (
        db.scalar(
            select(func.count(RecruiterEmail.id))
            .outerjoin(Email, Email.id == RecruiterEmail.reply_email_id)
            .where(
                RecruiterEmail.user_id == user_id,
                RecruiterEmail.route == ReplyRoute.AUTO,
                or_(
                    # Committed: chosen for auto-send and not yet away.
                    Email.status == EmailStatus.QUEUED,
                    and_(
                        Email.status == EmailStatus.SENT,
                        Email.sent_at >= since,
                    ),
                    and_(
                        Email.id.is_(None),
                        RecruiterEmail.created_at >= since,
                    ),
                ),
            )
        )
        or 0
    )


def auto_reply_allowed(db: Session, user: User, pref: RecruiterReplyPreference) -> bool:
    """Whether an unreviewed reply may be sent for this user *right now*.

    Three independent switches, all of which must be on, plus the rolling cap.
    Resolved here rather than in :func:`app.services.reply_routing.decide` so
    that function stays pure and this one stays the only place the policy lives.
    """
    if not settings.recruiter_reply_auto_enabled:
        return False
    if not pref.auto_reply_enabled:
        return False
    if auto_replies_last_24h(db, user.id) >= settings.recruiter_auto_reply_daily_limit:
        logger.info(
            "auto-reply cap reached for user %s (%s/24h)",
            user.id,
            settings.recruiter_auto_reply_daily_limit,
        )
        return False
    return True


# --------------------------------------------------------------------------- #
# Scanning                                                                     #
# --------------------------------------------------------------------------- #


def record_scan(
    db: Session,
    user: User,
    account: GmailAccount,
    *,
    limit: int | None = None,
    window_days: int | None = None,
    trigger: str = TRIGGER_BEAT,
) -> tuple[ScanResult, list[RecruiterEmail]]:
    """Scan one mailbox and persist a ``DETECTED`` row per new message.

    Returns the raw scan result (for the caller's report) and the rows created,
    which the caller then processes one at a time.

    *window_days* widens the lookback for this scan alone, which is how the
    backlog catch-up reaches mail that arrived while detection was down. It
    changes nothing else: the same filters run, the same dedup applies, and a
    message already on file is skipped before it is fetched.

    *trigger* names which caller ran this — beat, push, the button, or the
    catch-up. It is written to the scan-run row, and it is the only way to answer
    "is push actually doing the work?" without reading logs, which is the
    operational cost of making push the primary route.
    """
    result = inbound_scanner.scan(db, user, account, limit=limit, window_days=window_days)
    created: list[RecruiterEmail] = []

    for message in result.messages:
        row = _persist_detected(db, user, account, message)
        # ``None`` means a concurrent scan recorded this message first. Not
        # created, so not counted and not queued for processing — the scan that
        # won owns it.
        if row is not None:
            created.append(row)

    # Booked here rather than in the scanner because this is the function that
    # owns the transaction, and a bounce has to be *remembered* in the same
    # breath it is counted — see ``_persist_bounce``.
    for message in result.hard_bounces:
        _persist_bounce(db, user, account, message)

    # And the opt-outs, for the same reason and in the same place: the scanner
    # recognised them, and until this loop existed that recognition ended in a
    # log line. The `mailto:` half of the `List-Unsubscribe` header on every
    # piece of cold outreach asks the recipient's client to send precisely this
    # message, so the recipient pressed unsubscribe, watched it succeed, and
    # went on receiving follow-ups. See `app.services.opt_out`.
    for message in result.opt_outs:
        opt_out.apply(
            db, message.from_address, reason="recipient asked us to stop by email"
        )

    # Same rule, for the mail that was fetched and thrown away on its sender:
    # remembered here because this is where the transaction lives, and
    # remembered *at all* because otherwise the next pass pays for the fetch
    # again — see ``_persist_dismissal``.
    for dismissal in result.dismissed:
        _persist_dismissal(db, user, account, dismissal)

    _persist_scan_run(db, user, account, result, trigger)

    scanned_at = datetime.now(UTC)
    # Per mailbox: this is what the scan debounce reads, and keying it per user
    # let the first mailbox scanned in a cycle suppress all the others.
    account.last_scan_at = scanned_at
    account.detected_count += len(created)

    # The per-user row stays as "when did anything last get read, and how much
    # has this user's inbox produced in total" — which is the question the Setup
    # page asks, and it should not start meaning "the least recently scanned
    # mailbox" now that there can be several.
    pref = preference_for(db, user)
    pref.last_scan_at = scanned_at
    pref.detected_count += len(created)
    db.flush()
    return result, created


def _persist_scan_run(
    db: Session,
    user: User,
    account: GmailAccount,
    result: ScanResult,
    trigger: str,
) -> RecruiterScanRun:
    """Write down what this pass saw, so the stats view has a denominator.

    Every number here was already computed and then logged and dropped, which is
    why "how many emails did you scan?" was unanswerable from the database.
    """
    run = RecruiterScanRun(
        user_id=user.id,
        gmail_account_id=account.id,
        trigger=trigger,
        listed=result.listed,
        examined=result.examined,
        detected=len(result.messages),
        skipped_known=result.skipped_known,
        skipped_own_thread=result.skipped_own_thread,
        skipped_from_self=result.skipped_from_self,
        skipped_bounce=result.skipped_bounce,
        skipped_opt_out=result.skipped_opt_out,
        # Dropped on the floor until now, which made this table's promise to be
        # "that same dict, persisted" false for the largest bucket it has: on
        # the mailbox that prompted the dismissal work, blocked senders were 145
        # of the 152 messages fetched and the stats view could not see one.
        skipped_blocked_sender=result.skipped_blocked_sender,
        skipped_auto_reply=result.skipped_auto_reply,
        skipped_unfetchable=result.skipped_unfetchable,
        deferred=result.deferred,
        query=result.query or None,
        error=result.error,
    )
    db.add(run)
    db.flush()
    return run


def _persist_detected(
    db: Session, user: User, account: GmailAccount, message: CandidateMessage
) -> RecruiterEmail | None:
    """Store one detected message, or ``None`` if another scan got there first.

    The scanner's ``known_ids`` filter is a ``SELECT``, and this is the
    ``INSERT`` — so two scans of one mailbox that overlap both decide a message
    is new and both try to store it. ``uq_recruiter_email_user_message`` is what
    stops that becoming two rows and, as the model says, two replies to the same
    recruiter. But the constraint only *refuses* the second insert: unhandled,
    it aborted the whole transaction, so one scan raised ``IntegrityError``,
    dead-lettered, and took every other message it had found down with it. The
    mailbox then had to wait for a later scan to re-find them.

    Overlapping scans are not hypothetical here. The "scan now" button passes
    ``force=True`` specifically to bypass the debounce that keeps beat and push
    off each other — so an impatient candidate pressing it during a push-driven
    scan is the ordinary way to produce two.

    The insert therefore gets its own savepoint, and losing the race is a
    skipped message rather than a failed scan.
    """
    savepoint = db.begin_nested()
    row = RecruiterEmail(
        user_id=user.id,
        gmail_account_id=account.id,
        gmail_message_id=message.gmail_message_id,
        gmail_thread_id=message.gmail_thread_id,
        from_address=message.from_address,
        reply_to_address=message.reply_to_address,
        from_name=message.from_name,
        subject=message.subject,
        body_text=message.body_text,
        snippet=message.snippet,
        received_at=message.received_at,
        # Kept so the reply can carry In-Reply-To/References. Gmail's threadId
        # threads only for people reading in Gmail; these two are what every
        # other client threads on.
        rfc_message_id=message.rfc_message_id,
        rfc_references=message.rfc_references,
        status=RecruiterEmailStatus.DETECTED,
        extracted={},
        attachments=list(message.attachments or []),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        savepoint.rollback()
        logger.info(
            "recruiter message %s was recorded by a concurrent scan; skipping",
            message.gmail_message_id,
        )
        return None
    savepoint.commit()
    # The inbound event, for the pipeline that carries most of this
    # deployment's inbound mail and emitted nothing at all.
    #
    # `reply.received` existed and was raised only by the thread poller in
    # `tasks.inbox_tasks` — the path that handles replies to mail *we* sent.
    # Mail a recruiter starts is owned by this pipeline instead, and in
    # production it is the large majority of what arrives. So the one event
    # built to answer "is inbound working?" was counting the minority and
    # reporting a collapse in volume as normal, which is precisely the outage
    # shape this event exists to make visible.
    #
    # Emitted at detection rather than after classification, which is where the
    # intent fields would come from: this event means "mail arrived and was
    # stored", and deferring it to the classifier would make an LLM outage look
    # like an empty mailbox — the two states the inbound history says are
    # hardest to tell apart. Placed after the savepoint commits so the message
    # that lost the dedupe race is not counted twice.
    events.emit(
        events.REPLY_RECEIVED,
        user_id=user.id,
        account_id=account.id,
        sender_domain=events.recipient_domain(message.from_address),
        # Which of the two inbound pipelines saw it. They differ in what
        # happens next — this one drafts, the other one may auto-send — so a
        # single undifferentiated count answers neither pipeline's question.
        pipeline="recruiter_inbox",
    )
    return row


def _persist_bounce(
    db: Session, user: User, account: GmailAccount, message: CandidateMessage
) -> RecruiterEmail | None:
    """Book a hard bounce against the mailbox and write down that we did.

    The row is the point. ``bounce_count`` is a lifetime counter with no ledger
    behind it, so "have I already counted this notice?" is only answerable by
    remembering the message — and the scanner's own first filter, ``known_ids``,
    is built from exactly these rows. Without one, every scan over the rolling
    seven-day window re-read the same DSN and booked it again; at a scan a
    minute that reached 4074 bounces against 20 real sends, a 20370% rate, and
    a mailbox that re-paused itself every minute forever.

    Stored as ``NOT_RECRUITER``/``CLASSIFIED`` — terminal, so :func:`process`
    never picks it up — rather than a new kind, because that is what a delivery
    daemon is and it keeps the counts on the stats page honest.

    Returns ``None`` when the notice was already on file, which is also the race
    between two concurrent scans: the unique constraint decides, and the loser
    books nothing.
    """
    existing = db.scalar(
        select(RecruiterEmail).where(
            RecruiterEmail.user_id == user.id,
            RecruiterEmail.gmail_message_id == message.gmail_message_id,
        )
    )
    if existing is not None:
        return None

    row = RecruiterEmail(
        user_id=user.id,
        gmail_account_id=account.id,
        gmail_message_id=message.gmail_message_id,
        gmail_thread_id=message.gmail_thread_id,
        from_address=message.from_address,
        reply_to_address=message.reply_to_address,
        from_name=message.from_name,
        subject=message.subject,
        body_text=message.body_text,
        snippet=message.snippet,
        received_at=message.received_at,
        rfc_message_id=message.rfc_message_id,
        rfc_references=message.rfc_references,
        kind=RecruiterEmailKind.NOT_RECRUITER,
        status=RecruiterEmailStatus.CLASSIFIED,
        flag_reason="Delivery-failure notice — booked against mailbox reputation.",
        extracted={},
        attachments=list(message.attachments or []),
    )
    db.add(row)
    db.flush()
    reputation_service.record_bounce(account)
    return row


def _persist_dismissal(
    db: Session,
    user: User,
    account: GmailAccount,
    dismissal: DismissedMessage,
) -> RecruiterScanSkip | None:
    """Write down that this message was fetched once and dismissed on its sender.

    The row is the whole point, exactly as it is for :func:`_persist_bounce`.
    Every scanner filter past the first one runs on a message that has already
    been fetched — a Gmail list returns ids and nothing else — so a dismissal
    that leaves no trace is a dismissal the next pass cannot act on, and the
    same body is fetched again five minutes later for as long as the message
    stays inside the scan window. Production was doing that to 145 blocked
    senders per pass, which was the entire 25-second scan and about 43,000
    pointless ``messages.get`` calls a day.

    Nothing is classified, replied to, counted or shown as a result of this row.
    It is scanner bookkeeping and it lives in its own table for that reason —
    see :mod:`app.models.recruiter_scan_skip`, which also has the argument for
    why only the sender-identity filters may write one.

    Its own savepoint, for the reason :func:`_persist_detected` gives: the
    scanner's read is a ``SELECT`` and this is the ``INSERT``, so two overlapping
    scans both fetch the same message and both try to record it. Losing that
    race must cost one row, not the scan — an ``IntegrityError`` raised here
    would abort the transaction and take every message the pass had found with
    it, which is a bug this codebase has already paid for once.
    """
    savepoint = db.begin_nested()
    row = RecruiterScanSkip(
        user_id=user.id,
        gmail_account_id=account.id,
        gmail_message_id=dismissal.gmail_message_id,
        reason=dismissal.reason,
        from_address=dismissal.from_address,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        savepoint.rollback()
        return None
    savepoint.commit()
    return row


# --------------------------------------------------------------------------- #
# Processing one message                                                       #
# --------------------------------------------------------------------------- #


def process(
    db: Session,
    row: RecruiterEmail,
    *,
    classification: recruiter_classifier.Classification | None = None,
    max_auto_age_days: int | None = None,
) -> dict:
    """Classify, match, route and (when warranted) write a reply for one row.

    Idempotent by design: anything not sitting at ``DETECTED`` is left alone, so
    a replayed task after a killed worker re-reads rather than re-replies.

    That is a *claim*, not a read, and the difference is the whole point. The
    obvious spelling — ``if row.status is not DETECTED`` — asks the copy of the
    row this session loaded, and a worker that loaded it before the winner
    committed holds a stale ``DETECTED`` for as long as it keeps the object:
    nothing refreshes it. Two workers therefore both passed the guard, and
    because :func:`_write_reply` builds a fresh ``EmailThread`` and a fresh
    ``Email`` every time, both wrote one and both handed it to the sender. The
    recruiter receives the candidate's reply twice, in two threads.

    The transport cannot catch this. ``email_tasks._claim_for_send`` locks the
    row it is about to send, which stops one email going out twice — but these
    are two *different* emails, each perfectly claimable.

    Nor is it hypothetical. ``task_acks_late`` is on globally, so a lost
    worker's message is redelivered while the original may still be running,
    and :mod:`app.routers.recruiter_inbox` also processes inline in the request
    when a dispatch appears to fail, which is not proof the message never
    landed.

    So the guard is a conditional ``UPDATE`` evaluated by the database, whose
    ``rowcount`` no stale attribute can fool. It writes nothing meaningful:
    matching the row *is* the claim, and taking the row's write lock is what
    serialises the two runs. A second worker blocks until the first commits,
    then re-reads a status that is no longer ``DETECTED`` and returns skipped.
    Blocking rather than skipping-locked costs a worker slot for the length of
    one classification, which is the right trade for a duplicate that rare.

    This leans on every successful path below leaving ``DETECTED`` before the
    caller commits — they all do. The two that don't (no user; an exception)
    correctly leave the row claimable, because neither did any work.

    *classification* lets a caller that has **already read this message** hand the
    verdict in rather than paying for it twice. Only :func:`retry_degraded` does,
    and it matters there: that sweep exists because the provider chain is rate
    limited, so spending two calls where one would do is the one mistake it
    cannot afford to make.

    *max_auto_age_days* holds an otherwise-auto reply for review when the
    recruiter's message is older than that — see :func:`_downgrade_if_stale`.
    ``None``, the default, is the live path: a message detected minutes after it
    arrived has no staleness to judge. Every caller that is *catching up* passes
    a number, because the whole difference between the live path and a catch-up
    is that a catch-up answers mail the candidate has not seen in a while.
    """
    # Explicitly, because the session is built with ``autoflush=False`` — in
    # production as well as in the tests. ``retry_degraded`` puts the row back
    # to ``DETECTED`` and calls straight in here, and that write is still
    # pending in the unit of work; without this the statement below reads the
    # *old* status, matches nothing, and the sweep silently stops re-drafting
    # anything.
    db.flush()
    claimed = db.execute(
        update(RecruiterEmail)
        .where(
            RecruiterEmail.id == row.id,
            RecruiterEmail.status == RecruiterEmailStatus.DETECTED,
        )
        .values(updated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    ).rowcount
    if not claimed:
        return {"recruiter_email_id": row.id, "status": "skipped"}

    user = db.get(User, row.user_id)
    if user is None:  # pragma: no cover - FK cascade makes this unreachable
        return {"recruiter_email_id": row.id, "status": "no_user"}

    if classification is None:
        classification = recruiter_classifier.classify(
            from_address=row.from_address,
            subject=row.subject,
            body=row.body_text,
            reply_to=row.reply_to_address,
        )
    row.kind = classification.kind
    row.classification_confidence = classification.confidence
    row.classified_by = classification.classified_by
    row.extracted = classification.extracted()

    # What this user's own verdicts on earlier mail from this sender say. Stored
    # beside the classifier's reading rather than folded into it: the audit trail
    # should still show what the model thought, and "we got more confident
    # because you approved two of these" is a different fact.
    adjustment = classifier_feedback.adjustment_for(db, row.user_id, row.reply_address)
    row.confidence_adjustment = adjustment.delta

    # Not an opportunity: recorded and done. No route, nothing to reply to.
    if not classification.is_actionable and classification.kind.value != "UNKNOWN":
        row.status = RecruiterEmailStatus.CLASSIFIED
        row.route = None
        row.route_confidence = 0.0
        row.match_reason = classification.reason or None
        db.flush()
        return {
            "recruiter_email_id": row.id,
            "status": "classified",
            "kind": row.kind.value,
        }

    matched = (
        inbound_matcher.match(
            db,
            user,
            classification,
            body=row.body_text or "",
            subject=row.subject,
        )
        if classification.is_actionable
        or classification.kind is RecruiterEmailKind.UNKNOWN
        else None
    )

    if matched is not None:
        row.matched_profile_id = matched.profile_id
        row.match_score = matched.score
        row.match_reason = matched.reason

    pref = preference_for(db, user)
    decision = _route_with_feedback(
        classification.kind,
        classification.confidence,
        adjustment,
        matched.score if matched else None,
        has_profile=matched is not None,
        location_ok=matched.location_ok if matched else True,
        profile_label=matched.label if matched else None,
        auto_enabled=auto_reply_allowed(db, user, pref),
    )
    row.route = decision.route
    row.route_confidence = decision.confidence

    # A recruiter we already answered, writing again. Never answered twice: the
    # second message is a response to something we said, and answering it needs
    # the transcript that first contact by definition doesn't have.
    prior = recruiter_follow_up.find_prior_engagement(db, row)
    if prior is not None and decision.replies:
        reason = recruiter_follow_up.escalate(db, row, prior)
        row.route = ReplyRoute.FLAG
        row.status = RecruiterEmailStatus.FLAGGED
        row.flag_reason = reason
        db.flush()
        return {
            "recruiter_email_id": row.id,
            "status": "flagged_follow_up",
            "previous_recruiter_email_id": prior.id,
        }

    if decision.route is None:
        row.status = RecruiterEmailStatus.CLASSIFIED
        db.flush()
        return {"recruiter_email_id": row.id, "status": "classified"}

    if decision.route is ReplyRoute.FLAG:
        row.status = RecruiterEmailStatus.FLAGGED
        row.flag_reason = decision.reason
        db.flush()
        return {"recruiter_email_id": row.id, "status": "flagged"}

    suppressed = _suppression(db, user, row.reply_address)
    if suppressed is not None:
        outcome, reason = suppressed
        row.route = ReplyRoute.FLAG
        row.status = RecruiterEmailStatus.FLAGGED
        row.flag_reason = reason
        db.flush()
        return {"recruiter_email_id": row.id, "status": outcome}

    assert matched is not None  # decide() only routes to a reply with a match
    return _write_reply(
        db,
        user,
        row,
        classification,
        matched,
        decision,
        max_auto_age_days=max_auto_age_days,
    )


# --------------------------------------------------------------------------- #
# Reading a message again, once a model can be reached                          #
# --------------------------------------------------------------------------- #

# How old a recruiter's message may be and still be answered without the
# candidate reading the answer first.
#
# The retry sweep exists to catch up on a backlog, and a backlog is old by
# definition. Every other gate in this pipeline asks "is this reply right?"; this
# one asks "is it still *prompt*?", which is a different question and one the
# confidence bands cannot answer. A same-day reply to a recruiter reads as an
# attentive candidate. The identical sentence sent eleven days later reads as a
# system that has just woken up, and it is worse than the draft it replaced
# because the candidate never got to decide whether answering at all still made
# sense. So a stale message is re-read, re-routed and re-drafted like any other —
# it just lands in the inbox instead of the recruiter's.
#
# ``inbox_tasks.reevaluate_pending_replies`` draws the same line for the thread
# poller's backlog, for the same reason.
RETRY_AUTO_MAX_AGE_DAYS = 7


def _downgrade_if_stale(
    row: RecruiterEmail, route: ReplyRoute, max_auto_age_days: int | None
) -> ReplyRoute:
    """``DRAFT`` instead of ``AUTO`` when the message is too old to answer unread.

    One implementation for the two paths that catch up on a backlog — the
    re-read sweep, which rewrites a draft in place, and
    :func:`process`, which the sweep also uses for a row that never got as far
    as a draft, and which :mod:`app.tasks.backlog_tasks` uses for mail that was
    never even detected. They were separate, and only one of them had the rule:
    a message that had been sitting undetected for three weeks could be read,
    routed and sent without the candidate ever seeing it, which is the exact
    thing :data:`RETRY_AUTO_MAX_AGE_DAYS` exists to prevent.

    ``None`` means "no staleness rule", which is what the live path passes: a
    message that just arrived cannot be stale, and asking the question there
    would only invite the answer to drift.

    Writes the reason onto the row, because :func:`_hold_reason` reads
    ``flag_reason`` first and this knows something the confidence bands do not.
    """
    if max_auto_age_days is None or route is not ReplyRoute.AUTO:
        return route
    age_days = _received_age_days(row)
    if age_days is None or age_days <= max_auto_age_days:
        return route
    row.flag_reason = (
        f"Their message is {age_days} days old — sending now is your call."
    )
    return ReplyRoute.DRAFT


def retry_degraded(
    db: Session,
    row: RecruiterEmail,
    *,
    max_auto_age_days: int = RETRY_AUTO_MAX_AGE_DAYS,
) -> dict:
    """Re-read one message whose verdict was a no-model guess, and re-route it.

    The pipeline classifies each message exactly once, on arrival. When every
    provider is rate limited — which on the free tiers is most of the time — that
    one reading is :func:`recruiter_classifier._rule_based`, whose confidence
    tops out at 0.8 and is usually 0.4. Routing multiplies that by the match
    score, so a real recruiter and a good match still land far below the auto
    bar, and **nothing ever asked again**. A thirty-second outage became a
    permanent draft.

    This is the asking-again. It is the whole fix: no threshold moves, no gate is
    loosened, and a message the model *can* read is routed on the model's reading
    rather than on a keyword count.

    Two shapes, because the row can be in two states:

    * **Nothing was written yet** — a flagged UNKNOWN, the case that produced
      silence rather than a draft. There is no draft to supersede and no thread
      to disturb, so the row is simply put back to ``DETECTED`` and handed to
      :func:`process`, which is the ordinary path and needs no special casing.
    * **A draft already exists.** It cannot be re-run through :func:`process` —
      ``_write_reply`` builds a fresh ``EmailThread`` every time, so that would
      leave the inbox holding two copies of one conversation. The existing reply
      is rewritten in place instead, by :func:`_redraft_in_place`.

    Returns a dict describing what happened; ``outcome`` is the field worth
    branching on. Never raises for an ordinary refusal — a row it will not touch
    comes back with an outcome saying so.
    """
    if row.escalated:
        # A conversation a human already stepped into. The escalation was a
        # decision about *this* message, and a better classification does not
        # reopen it.
        return _retry_result(row, "escalated")
    if row.draft_discarded_at is not None:
        # The user read a reply we wrote for this message and threw it away.
        # That is a verdict on answering *this recruiter at all*, and it is the
        # most explicit one the product ever gets — more explicit than any
        # confidence band. A better reading of the message does not overturn it;
        # a re-read here would compose a second paragraph they never asked for
        # and, on a confident read, send it unread.
        #
        # Permanent, and not softened by the user asking for a fresh draft
        # afterwards. That request is theirs to make and ``generate-reply``
        # serves it; what it is not is permission for a sweep to keep writing.
        return _retry_result(row, "draft_discarded")
    if row.status is RecruiterEmailStatus.IGNORED:
        # The user dismissed the message itself. That is a verdict on the
        # *sender* — a stronger one than discarding a draft, which is only a
        # verdict on the paragraph — and ``recruiter_inbox.dismiss`` records it
        # as ``FeedbackSignal.REJECTED`` to teach the classifier with.
        #
        # It was excluded only in the sweep's query. That kept it safe from the
        # one caller there is, and left this function willing to do the worst
        # thing it can do: a dismissed row reaches the ``reply is None`` branch,
        # which puts it back to ``DETECTED`` — erasing the dismissal outright —
        # and hands it to ``process``, which on a confident re-read routes
        # ``AUTO`` and mails the recruiter the candidate had just waved away.
        # Every other user decision on this row is defended in both places, for
        # exactly this reason; see ``draft_discarded_at`` above.
        return _retry_result(row, "dismissed")
    if row.route is ReplyRoute.AUTO or row.status in (
        RecruiterEmailStatus.REPLY_QUEUED,
        RecruiterEmailStatus.REPLIED,
    ):
        # Already answered. Re-reading could only produce a second reply.
        return _retry_result(row, "already_replied")

    reply = db.get(Email, row.reply_email_id) if row.reply_email_id else None
    if reply is not None and reply.status is not EmailStatus.DRAFT:
        # Sent, queued or failed — not ours to rewrite.
        return _retry_result(row, "reply_not_draft")
    if reply is not None and reply.user_owned_at is not None:
        # A draft a person has taken hold of: they pressed "write me one", or
        # they rewrote it in their own words. Either way the text is theirs and
        # the decision to answer has already been made by the only party
        # entitled to make it.
        #
        # ``_redraft_in_place`` does not amend a draft, it *replaces* the body —
        # and on a confident re-read routes the replacement ``AUTO``. So this
        # sweep could delete the paragraph the candidate typed and mail its own
        # in their name, and the only trace would be that the words in their
        # sent folder were not the ones they wrote. The same family as
        # ``draft_discarded_at`` above: a discard is the user refusing a draft,
        # this is the user keeping one, and neither is a confidence band's to
        # overturn.
        return _retry_result(row, "draft_user_owned")

    user = db.get(User, row.user_id)
    if user is None:  # pragma: no cover - FK cascade makes this unreachable
        return _retry_result(row, "no_user")

    before = row.classified_by
    classification = recruiter_classifier.classify(
        from_address=row.from_address,
        subject=row.subject,
        body=row.body_text,
        reply_to=row.reply_to_address,
    )

    if recruiter_classifier.is_degraded(
        classification.classified_by, classification.confidence
    ):
        if classification.model_answered:
            # A provider replied and we could not use what it said. Asking again
            # in ten minutes will get the same answer, so this counts as a turn
            # taken: ``updated_at`` moves, the row goes to the back of the
            # least-recently-tried order, and — crucially — it does not count
            # toward the caller's chain-is-down tally.
            #
            # Without this split the sweep wedges. A ``still_degraded`` row
            # deliberately keeps its ``updated_at``, so it is picked first again
            # next time; two unparseable messages at the head of the queue would
            # therefore abort every sweep forever and no other row would ever be
            # reached. Roughly one call in six comes back unparseable, so two
            # such rows is not a hypothetical.
            row.updated_at = datetime.now(UTC)
            db.flush()
            return _retry_result(row, "unreadable", classified_by=before)
        # The chain is down. Nothing is written — in particular the row keeps
        # its original ``updated_at``, because no turn was taken.
        return _retry_result(row, "still_degraded", classified_by=before)

    row.kind = classification.kind
    row.classification_confidence = classification.confidence
    row.classified_by = classification.classified_by
    row.extracted = classification.extracted()

    if reply is None:
        # Back onto the ordinary path. ``process`` re-does the matching, the
        # routing, the prior-engagement check and the suppression check, which
        # is exactly what should happen and is not worth a second
        # implementation. The other branch shares only the last of those, via
        # :func:`_suppression` — the rest of what it does is rewrite a draft
        # that already exists rather than compose one.
        row.status = RecruiterEmailStatus.DETECTED
        row.route = None
        row.flag_reason = None
        outcome = process(
            db,
            row,
            classification=classification,
            # The same ceiling ``_redraft_in_place`` applies on the other branch.
            # Without it this one — a row the pipeline never got as far as
            # drafting for, which is the *older* half of the backlog — could
            # answer a month-old message unread.
            max_auto_age_days=max_auto_age_days,
        )
        return _retry_result(
            row,
            "reprocessed",
            was=before,
            inner=outcome.get("status"),
            route=row.route.value if row.route else None,
        )

    return _redraft_in_place(
        db, user, row, reply, classification, max_auto_age_days=max_auto_age_days
    )


def _redraft_in_place(
    db: Session,
    user: User,
    row: RecruiterEmail,
    reply: Email,
    classification: recruiter_classifier.Classification,
    *,
    max_auto_age_days: int,
) -> dict:
    """Rewrite an existing draft against a corrected reading of the message.

    The body is regenerated rather than kept. That is the point rather than an
    extra: the draft sitting there was composed from the guess, and on the guess
    the composer was often told the role title was ``None``. Keeping that text
    and merely flipping its route to send would mail the generic paragraph out
    under the candidate's name — the exact failure ``_write_reply``'s throttle
    downgrade exists to prevent.

    Regenerating also restores the signal that downgrade needs. ``generated_with``
    lives on the draft object and is not persisted on ``Email``, so for a draft
    written days ago there is no way to know whether it was the model's work or
    the template's. Writing it again answers the question by construction.
    """
    if not classification.is_actionable:
        # The guess said recruiter; the model says newsletter. The draft should
        # never have existed. It is left in place rather than deleted — deleting
        # what the user may be part-way through reading is not this sweep's
        # call — but it stops asking for attention and the row stops claiming a
        # reply is pending.
        reply.needs_attention = False
        reply.attention_reason = _fit_reason(
            f"Re-read as {classification.kind.value.replace('_', ' ').lower()} "
            "once a model was reachable — no reply needed."
        )
        row.route = None
        row.route_confidence = 0.0
        row.status = RecruiterEmailStatus.CLASSIFIED
        row.match_reason = classification.reason or None
        db.flush()
        return _retry_result(row, "stood_down", kind=row.kind.value)

    # Asked before the matching, because none of the work below is worth doing
    # for a contact nothing may be written to — and because this branch is the
    # one that turns a ``DRAFT`` into a ``QUEUED``. The draft it is rewriting
    # was composed days or weeks ago against a contact row that passed these
    # same three checks then; a withdrawn consent, a contact the user has since
    # set aside, or an address that has hard-bounced in the meantime all leave
    # the row here, and only here. See :func:`_suppression`.
    suppressed = _suppression(db, user, row.reply_address)
    if suppressed is not None:
        outcome, reason = suppressed
        reply.needs_attention = True
        reply.attention_reason = _fit_reason(reason)
        row.route = ReplyRoute.FLAG
        row.status = RecruiterEmailStatus.FLAGGED
        row.flag_reason = reason
        db.flush()
        return _retry_result(row, outcome)

    matched = inbound_matcher.match(
        db, user, classification, body=row.body_text or "", subject=row.subject
    )
    if matched is None:
        reply.needs_attention = True
        reply.attention_reason = _fit_reason(
            "No active profile fits this role, so it's yours to answer."
        )
        row.route = ReplyRoute.FLAG
        row.status = RecruiterEmailStatus.FLAGGED
        row.flag_reason = reply.attention_reason
        db.flush()
        return _retry_result(row, "flagged_no_profile")

    row.matched_profile_id = matched.profile_id
    row.match_score = matched.score
    row.match_reason = matched.reason

    adjustment = classifier_feedback.adjustment_for(db, row.user_id, row.reply_address)
    row.confidence_adjustment = adjustment.delta
    pref = preference_for(db, user)
    decision = _route_with_feedback(
        classification.kind,
        classification.confidence,
        adjustment,
        matched.score,
        has_profile=True,
        location_ok=matched.location_ok,
        profile_label=matched.label,
        auto_enabled=auto_reply_allowed(db, user, pref),
    )
    row.route = decision.route
    row.route_confidence = decision.confidence

    # ``None`` means "not an opportunity", which the actionable check above has
    # already excluded; it is folded in here rather than asserted away so that a
    # future band returning it cannot reach ``route.value`` below.
    routed = decision.route
    if routed is ReplyRoute.FLAG or routed is None:
        reply.needs_attention = True
        reply.attention_reason = _fit_reason(decision.reason)
        row.status = RecruiterEmailStatus.FLAGGED
        row.flag_reason = decision.reason
        db.flush()
        return _retry_result(row, "flagged", route_confidence=decision.confidence)

    candidate_name = (
        user.full_name
        or (matched.target.resume.full_name if matched.target.resume else None)
        or (user.email or "").split("@")[0]
    )
    stage = conversation_stage.detect(
        subject=row.subject,
        body=row.body_text,
        references=row.rfc_references,
        own_addresses=inbound_scanner.own_addresses(user),
    )

    route = routed
    row.flag_reason = None
    if stage.is_first_contact:
        drafted = inbound_reply.draft(
            candidate_name=candidate_name,
            classification=classification,
            target=matched.target,
            message_body=row.body_text or "",
            subject=row.subject,
        )
        if drafted.generated_with != "llm" and route is ReplyRoute.AUTO:
            # The classifier got through and the composer did not. Same rule as
            # the live path: no model's confidence justifies sending a paragraph
            # no model wrote.
            route = ReplyRoute.DRAFT
            row.flag_reason = (
                "Re-read successfully, but no model could write the reply — "
                "worth a read before it goes."
            )
    else:
        drafted = _draft_mid_thread(candidate_name, row, matched, stage)
        route = ReplyRoute.DRAFT

    age_days = _received_age_days(row)
    route = _downgrade_if_stale(row, route, max_auto_age_days)

    row.route = route
    reply.body_text = drafted.body
    reply.draft_template = drafted.template
    reply.draft_note = drafted.note
    reply.subject = inbound_reply.reply_subject(classification, row.subject)
    reply.status = EmailStatus.QUEUED if route is ReplyRoute.AUTO else EmailStatus.DRAFT
    hold = None if route is ReplyRoute.AUTO else _hold_reason(row, decision, stage)
    reply.needs_attention = hold is not None
    reply.attention_reason = hold
    row.match_reason = (
        decision.reason
        if stage.is_first_contact
        else f"{decision.reason} {stage.reason}".strip()
    )
    row.status = (
        RecruiterEmailStatus.REPLY_QUEUED
        if route is ReplyRoute.AUTO
        else RecruiterEmailStatus.DRAFTED
    )
    _choose_resume(db, user, row, matched)
    db.flush()

    return _retry_result(
        row,
        "redrafted",
        route=route.value,
        route_confidence=decision.confidence,
        generated_with=drafted.generated_with,
        email_id=reply.id,
        age_days=age_days,
    )


def _received_age_days(row: RecruiterEmail) -> int | None:
    """Whole days since the recruiter wrote, or ``None`` when we never recorded it."""
    if row.received_at is None:
        return None
    received = row.received_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - received).days)


def _retry_result(row: RecruiterEmail, outcome: str, **extra) -> dict:
    return {
        "recruiter_email_id": row.id,
        "outcome": outcome,
        "status": row.status.value if row.status else None,
        **extra,
    }


def _route_with_feedback(
    kind: recruiter_classifier.RecruiterEmailKind,
    confidence: float,
    adjustment: classifier_feedback.Adjustment,
    score: float | None,
    *,
    has_profile: bool,
    location_ok: bool,
    profile_label: str | None,
    auto_enabled: bool,
) -> reply_routing.RouteDecision:
    """Route the message, letting the user's history move the confidence — within limits.

    The route is computed **twice**, and that is the whole safety mechanism:

    * once on the classifier's own reading, which decides whether an automatic
      send was ever on the table;
    * once on the adjusted reading, which is what actually routes.

    If the raw reading would not have cleared the auto bar, an adjusted ``AUTO``
    is clamped back to ``DRAFT``. Without that, "approve four drafts from this
    recruiter" becomes a way to teach the product to send unread mail to them —
    and the user doing the approving has no idea that is what they are agreeing
    to. Downward adjustments are unrestricted: feedback may always make the
    product quieter, never louder past the bar.

    The same shape as the existing rule that a *templated* reply is never sent
    unreviewed however confident the routing was.
    """
    common = {
        "has_profile": has_profile,
        "location_ok": location_ok,
        "profile_label": profile_label,
        "auto_enabled": auto_enabled,
    }

    raw = reply_routing.decide(kind, confidence, score, **common)
    if adjustment.is_zero:
        return raw

    decision = reply_routing.decide(
        kind,
        classifier_feedback.adjusted_confidence(confidence, adjustment),
        score,
        **common,
    )

    if decision.route is ReplyRoute.AUTO and raw.route is not ReplyRoute.AUTO:
        logger.info(
            "feedback boost held back from auto: %s", adjustment.reason or "history"
        )
        return reply_routing.RouteDecision(
            ReplyRoute.DRAFT,
            decision.confidence,
            f"{raw.reason} {adjustment.reason or ''}".strip(),
        )

    if adjustment.reason and decision.route != raw.route:
        return reply_routing.RouteDecision(
            decision.route,
            decision.confidence,
            f"{decision.reason} {adjustment.reason}".strip(),
        )
    return decision


def _hold_reason(
    row: RecruiterEmail,
    decision: reply_routing.RouteDecision,
    stage: conversation_stage.Stage,
) -> str:
    """The sentence the inbox shows beside a reply that did not send itself.

    Most specific first. ``flag_reason`` is only set by the throttle downgrade,
    which knows something the routing bands do not — that this would have gone
    out as the generic template — so it wins where it exists. A mid-thread hold
    adds the stage's evidence to the band's reason, matching what is already
    written to ``match_reason``.

    Truncated to the column width here rather than at the call site, for the same
    reason ``inbox_tasks._hold`` does it: every caller would otherwise have to
    remember, and the one that forgets fails at flush time in a Celery task.
    """
    return _fit_reason(
        row.flag_reason
        or (
            decision.reason
            if stage.is_first_contact
            else f"{decision.reason} {stage.reason}".strip()
        )
    )


# The sentence shown when a row records no reason at all. Only reachable for a
# draft written before the pipeline kept one.
HOLD_FALLBACK = "Drafted for your review."


def _fit_reason(reason: str | None) -> str:
    """Default and truncate, in the one place both callers can share."""
    return (reason or HOLD_FALLBACK)[:160]


def hold_reason_for(row: RecruiterEmail) -> str:
    """The sentence beside a held reply, reconstructed from the row alone.

    :func:`_hold_reason` answers this at the moment of the decision, when
    ``match_reason`` has not been written yet. This answers it afterwards, for a
    draft already on disk — the backfill in
    ``tasks.recruiter_reply_tasks.flag_pending_recruiter_replies``, which has to
    explain 159 replies that were held before anything recorded that they were.

    The two agree by construction: ``_write_reply`` stores exactly the string
    ``_hold_reason`` built into ``match_reason``, and ``flag_reason`` outranks it
    in both. So a re-run of the live path over the same row produces the same
    sentence this does, which is the property that makes the backfill safe to
    run beside a pipeline that is still writing.
    """
    return _fit_reason(row.flag_reason or row.match_reason)


def _thread_by_gmail_id(user_id: int, gmail_thread_id: str):
    """An existing thread for this Gmail conversation, scoped to the user.

    Scoped by user rather than by the caller's already-resolved application:
    the two rows racing to create this thread are guaranteed to agree on the
    Gmail conversation and not necessarily on anything this module inferred
    from it, so matching on the inferred value would miss the very case this
    exists to catch. A Gmail thread id is only meaningful inside the mailbox
    that holds it (see ``EmailThread.gmail_account_id``), so this must not
    cross users.
    """
    return (
        select(EmailThread)
        .join(Application, Application.id == EmailThread.application_id)
        .where(
            Application.user_id == user_id,
            EmailThread.gmail_thread_id == gmail_thread_id,
        )
    )


def _write_reply(
    db: Session,
    user: User,
    row: RecruiterEmail,
    classification: recruiter_classifier.Classification,
    matched: inbound_matcher.InboundMatch,
    decision: reply_routing.RouteDecision,
    *,
    max_auto_age_days: int | None = None,
) -> dict:
    """Generate the reply and build the pipeline chain that can send it."""
    candidate_name = (
        user.full_name or (matched.target.resume.full_name if matched.target.resume else None)
        or (user.email or "").split("@")[0]
    )

    # First contact and mid-conversation are different letters, and the message
    # itself says which one this is — see :mod:`app.services.conversation_stage`.
    # Asking it here rather than inside the composer keeps the routing and the
    # attachment consequences in the one place that can act on them.
    stage = conversation_stage.detect(
        subject=row.subject,
        body=row.body_text,
        references=row.rfc_references,
        own_addresses=inbound_scanner.own_addresses(user),
    )

    # Template-generated replies are allowed to auto-send. The keyword
    # classifier and the match score already gate whether auto is warranted,
    # and the template produces a safe, professional response. Holding every
    # template reply for manual review defeats auto-reply during LLM outages,
    # which is exactly when the fallback needs to work.
    route = decision.route

    if stage.is_first_contact:
        drafted = inbound_reply.draft(
            candidate_name=candidate_name,
            classification=classification,
            target=matched.target,
            message_body=row.body_text or "",
            subject=row.subject,
        )
        # ...with one exception, and it is the reason this exists. A *throttled*
        # fallback is not an outage: every provider was rate limited, which
        # happens precisely when a scan finds a lot of mail at once, and clears
        # in seconds. Auto-sending then is what produced the reported bug —
        # dozens of recruiters receiving the same paragraph, byte for byte,
        # signed by the candidate, on the one day the inbox was busiest.
        #
        # The distinction the comment above draws is still right for a real
        # outage: a provider that is down stays down, waiting buys nothing, and
        # a safe template beats silence. Being asked to slow down is the other
        # case, and the answer to it is to let the user press send.
        if drafted.fallback_reason == "throttled" and route is ReplyRoute.AUTO:
            route = ReplyRoute.DRAFT
            row.route = route
            row.flag_reason = (
                "Held for review: every AI provider was rate limited, so this "
                "would have gone out as the generic template. Re-drafting it "
                "will pick up the personalised version."
            )
            logger.warning(
                "recruiter email %s downgraded AUTO->DRAFT: providers throttled",
                row.id,
            )
    else:
        drafted = _draft_mid_thread(candidate_name, row, matched, stage)
        # Never unreviewed. The transcript this was written from was recovered
        # from a quoted body, which is a good enough basis for a draft the
        # candidate reads and a poor one for mail that leaves without them.
        #
        # Written back onto the row as well as used locally: ``process`` set the
        # route from the confidence bands, and a row reading AUTO beside an
        # email sitting in DRAFT is a lie the inbox would have to explain.
        route = ReplyRoute.DRAFT
        row.route = route
        logger.info(
            "recruiter email %s answered mid-thread (%s)",
            row.id,
            "; ".join(stage.evidence) or "no evidence recorded",
        )

    # Last, because it can only ever downgrade and both branches above may have
    # settled the route already. A backlog catch-up is the caller that passes a
    # ceiling; the live path passes none.
    stale = _downgrade_if_stale(row, route, max_auto_age_days)
    if stale is not route:
        route = stale
        row.route = route
        logger.info(
            "recruiter email %s downgraded AUTO->DRAFT: %s days old",
            row.id,
            _received_age_days(row),
        )

    recruiter = _get_or_create_recruiter(db, user, row, classification)
    campaign = _inbound_campaign(db, user)

    # A recruiter who sends several messages produces several detected rows,
    # each of which routes to a reply independently. The unique constraint
    # ``uq_application_campaign_recruiter`` prevents two Application rows for
    # the same (campaign, recruiter) pair, so the second message through this
    # path would crash with a UniqueViolation. Reuse the existing row: the
    # application is a *relationship* record, not a per-message one, and a
    # second thread under the same application is the honest representation.
    application = db.scalar(
        select(Application).where(
            Application.campaign_id == campaign.id,
            Application.recruiter_id == recruiter.id,
        )
    )
    if application is None:
        application = Application(
            user_id=user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            profile_id=matched.profile_id,
            status=ApplicationStatus.REPLIED,
        )
        db.add(application)
        db.flush()

    # A recruiter's second message on the *same* Gmail conversation must land on
    # the *same* thread. Two RecruiterEmail rows for one physical conversation
    # can both reach here: inbound_scanner's "ours" filter — the thing that is
    # supposed to route a reply on an existing thread to poll_thread instead —
    # is a snapshot taken once per scan, so two messages on a brand-new
    # conversation that arrive in the same scan (or in overlapping scans before
    # the first one's thread row commits) both find no EmailThread yet and both
    # pass. Each used to build its own fresh EmailThread carrying the *same*
    # gmail_thread_id, and nothing here or in the schema noticed. Worse,
    # poll_thread backfills a Gmail thread's *entire* history into whichever
    # thread row it is handed, keyed only on that row's own ``Email.thread_id``
    # — so on the next poll each duplicate thread filled in the message the
    # other one was missing, and the recruiter's message ended up stored twice,
    # under two different threads. That produced the ~40 duplicate rows cleaned
    # up in the c9a4f1e6b382 migration.
    #
    # The lookup below closes the common case. ``uq_email_threads_gmail_thread``
    # (same migration) is the backstop for the residual race — two workers
    # reaching this function for the two messages at the same instant, neither
    # having committed yet when the other's lookup runs.
    thread = (
        db.scalar(_thread_by_gmail_id(user.id, row.gmail_thread_id))
        if row.gmail_thread_id
        else None
    )
    if thread is not None:
        # Reused, not created — the message this thread was made for already
        # counted it. Bump rather than trust ``row.received_at``: a reused
        # thread's row may be answering an *older* message than the one the
        # thread last heard from, and last_message_at must never go backwards.
        if thread.last_message_at is None or (
            row.received_at is not None and row.received_at > thread.last_message_at
        ):
            thread.last_message_at = row.received_at
    else:
        savepoint = db.begin_nested()
        thread = EmailThread(
            application_id=application.id,
            gmail_thread_id=row.gmail_thread_id,
            subject=row.subject,
            message_count=0,
            last_message_at=row.received_at,
            # The mailbox the recruiter actually wrote to, already recorded on
            # the detected row. This is not configurable and must not be: a
            # recruiter who emails one address and is answered from another
            # sees a broken thread and a sender they have no record of
            # contacting.
            gmail_account_id=row.gmail_account_id,
        )
        db.add(thread)
        try:
            db.flush()
        except IntegrityError:
            savepoint.rollback()
            thread = db.scalar(_thread_by_gmail_id(user.id, row.gmail_thread_id))
        else:
            savepoint.commit()
    db.flush()

    # The recruiter's own message, so the conversation reads from its real start.
    #
    # Looked up before it is written, because this function can run twice for
    # one detected message and the second run must not try to store the
    # recruiter's mail again. ``emails.gmail_message_id`` is unique, so it
    # didn't: it raised, and the endpoint that got there answered 500 with the
    # session left in an aborted transaction. The route is short and entirely
    # ordinary — draft a reply, discard it in ``/review``, then ask for another
    # one. Discarding deletes the ``Email`` and the ``SET NULL`` on
    # ``reply_email_id`` clears the pointer ``generate-reply`` refuses on, so
    # nothing stood between the user and the second run.
    #
    # Reused rather than rewritten: it is the recruiter's own message and it has
    # not changed. Only the reply is written again.
    #
    # Scoped to the application rather than to ``thread`` — the constraint is
    # global but the search must not be, or it could reuse another account's
    # row. The application is the narrowest scope that still finds it: a row
    # with no ``gmail_thread_id`` gets a *fresh* thread on every run (there is
    # nothing to look one up by), so a thread-scoped search would miss the very
    # inbound it is about to collide with.
    inbound = (
        db.scalar(
            select(Email)
            .join(EmailThread, Email.thread_id == EmailThread.id)
            .where(
                EmailThread.application_id == application.id,
                Email.gmail_message_id == row.gmail_message_id,
                Email.direction == EmailDirection.RECEIVED,
            )
        )
        if row.gmail_message_id
        else None
    )
    inbound_is_new = inbound is None
    if inbound_is_new:
        inbound = Email(
            thread_id=thread.id,
            direction=EmailDirection.RECEIVED,
            status=EmailStatus.RECEIVED,
            from_address=row.from_address,
            to_address=user.email,
            subject=row.subject,
            body_text=row.body_text,
            gmail_message_id=row.gmail_message_id,
            # Carried on the inbound row too, so a later message in this thread
            # can chain off it without going back to Gmail for the header.
            in_reply_to=row.rfc_message_id,
            email_references=row.rfc_references,
            sent_at=row.received_at,
            # Carried across so the conversation can open what the recruiter
            # sent. The bytes were never downloaded — this is the description,
            # and the ``gmail_message_id`` above is what redeems it.
            inbound_attachments=list(row.attachments or []),
        )
        db.add(inbound)

    # RFC 5322 §3.6.4. Gmail's threadId (already on the thread row) threads this
    # for people reading in Gmail; these two headers are what Outlook, Apple
    # Mail and every ATS that ingests mail thread on.
    in_reply_to, references = inbound_scanner.reply_headers_for(
        row.rfc_message_id, row.rfc_references
    )

    # Why this one is waiting, in the user's words — or ``None`` when it isn't.
    #
    # The thread pipeline has always written this pair (``inbox_tasks``, via
    # ``thread_reply_policy.ReplyDecision``); this one never did, and the two
    # feed the same inbox. The visible cost was that a held reply from *here*
    # arrived at ``DRAFT`` with ``needs_attention`` false — indistinguishable, to
    # the "Needs review" filter and to the badge, from mail nothing had looked at
    # yet. A degraded LLM chain drafts rather than sends, by design, and that is
    # only a safe default if the user can *see* the hold and its reason.
    hold_reason = None if route is ReplyRoute.AUTO else _hold_reason(row, decision, stage)

    reply = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.QUEUED if route is ReplyRoute.AUTO else EmailStatus.DRAFT,
        to_address=recruiter.email,
        subject=inbound_reply.reply_subject(classification, row.subject),
        body_text=drafted.body,
        draft_template=drafted.template,
        draft_note=drafted.note,
        # See ``Email.drafted_with``. Both pipelines feed the same inbox and the
        # same re-evaluation task, so both have to record this or that task is
        # back to guessing for half the rows it walks.
        drafted_with=drafted.generated_with,
        needs_attention=hold_reason is not None,
        attention_reason=hold_reason,
        in_reply_to=in_reply_to,
        email_references=references,
        # A recruiter three messages into scheduling an interview already has
        # the CV; attaching it again to "Thursday works for me" reads as a
        # candidate who has lost the thread. Suppressed rather than simply not
        # resolved, so the review screen shows it as removed and the user can
        # put it back in one click — the same control they already have.
        suppressed_attachments=(
            [] if stage.is_first_contact else [email_attachments.RESUME]
        ),
    )
    db.add(reply)
    # Not a bare ``= 2``: a reused thread already counted the message it was
    # created for, and hardcoding the total here silently overwrote that on
    # every follow-up exchange once threads started being reused. And not a
    # bare ``+ 2`` either — a second run over the same detected message reuses
    # the inbound row above and adds only the reply.
    thread.message_count = (thread.message_count or 0) + (2 if inbound_is_new else 1)
    db.flush()

    # Which resume argues best for *this* role, rather than whichever one the
    # matched profile names. Recorded on the row rather than resolved now: the
    # draft can sit in review for days and the document should be rendered as it
    # stands on the day it goes, which is the property email_attachments exists
    # to protect. What is decided here is only *which* document.
    #
    # Still chosen mid-thread, even though the reply won't carry it: the user
    # may un-suppress it, and when they do the document should be the one this
    # role argues for rather than whatever the default resolution lands on.
    _choose_resume(db, user, row, matched)

    row.application_id = application.id
    row.reply_email_id = reply.id
    row.status = (
        RecruiterEmailStatus.REPLY_QUEUED
        if route is ReplyRoute.AUTO
        else RecruiterEmailStatus.DRAFTED
    )
    row.match_reason = (
        decision.reason
        if stage.is_first_contact
        else f"{decision.reason} {stage.reason}".strip()
    )
    db.flush()

    return {
        "recruiter_email_id": row.id,
        "status": row.status.value,
        "route": route.value,
        "email_id": reply.id,
        "generated_with": drafted.generated_with,
    }


def _draft_mid_thread(
    candidate_name: str,
    row: RecruiterEmail,
    matched: inbound_matcher.InboundMatch,
    stage: conversation_stage.Stage,
) -> reply_agent.ReplyDraft:
    """Answer a conversation already in progress, from its own transcript.

    Two things change versus :func:`app.services.inbound_reply.draft`, and both
    of them are the bug this exists to fix:

    * **The brief.** :mod:`app.services.reply_agent` picks a template from the
      classified intent of the latest message, so a recruiter asking to book an
      L2 gets a reply that confirms availability. The first-contact composer has
      no such choice to make — its brief is fixed at "introduce yourself and ask
      what the process looks like", which is precisely the letter that went out
      to a thread five messages deep.
    * **The context.** The whole recovered conversation goes to the model under
      an instruction never to repeat what the candidate already said, so a
      thread where they have given their notice period twice doesn't get a third
      offer to provide it.

    The intent is classified from the newest message alone —
    :func:`app.services.reply_classifier.classify_reply` strips the quoted
    thread first, which matters more here than anywhere else: the quote contains
    our own words, and reading them as the recruiter's is how a scheduling
    request gets filed as a fresh expression of interest.
    """
    targeting = matched.target.targeting
    resume = matched.target.resume
    cand = CandidateContext(
        name=candidate_name,
        headline=getattr(resume, "headline", None),
        skills=list(targeting.skills[:12]) if targeting.skills else None,
        target_roles=list(targeting.roles[:6]) if targeting.roles else None,
    )

    intent = reply_classifier.classify_reply(
        row.body_text or "", subject=row.subject
    )
    draft = reply_agent.draft_reply(cand, stage.messages, intent=intent)
    draft.note = f"{draft.note} {stage.reason}".strip()
    return draft


def _choose_resume(
    db: Session,
    user: User,
    row: RecruiterEmail,
    matched: inbound_matcher.InboundMatch,
) -> None:
    """Pick the resume this reply should carry, and say why.

    The incumbent is the matched profile's own document — what
    :func:`app.services.email_attachments._base_resume_for` would have resolved
    — so the selector can only improve on that answer or leave it alone. Never
    raises: a failed selection means the ordinary resolution stands, which is
    exactly what happened before this existed.
    """
    try:
        incumbent = matched.target.resume
        choice = resume_selector.select(
            db,
            user,
            matched.parsed,
            incumbent_id=incumbent.id if incumbent is not None else None,
            targeting=matched.target.targeting,
        )
    except Exception:  # noqa: BLE001 - the reply matters more than the attachment
        logger.warning("resume selection failed for recruiter email %s", row.id, exc_info=True)
        return

    if choice is None:
        return
    row.selected_resume_id = choice.resume_id
    row.resume_choice_reason = choice.reason
    if choice.switched:
        logger.info(
            "recruiter email %s will attach %s: %s",
            row.id,
            choice.resume.display_label,
            choice.reason,
        )


def _existing_recruiter(db: Session, user: User, email: str) -> Recruiter | None:
    return db.scalar(
        select(Recruiter).where(
            Recruiter.user_id == user.id, Recruiter.email == email.lower()
        )
    )


def _suppression(db: Session, user: User, address: str) -> tuple[str, str] | None:
    """``(outcome, reason)`` when nothing may be written to *address*, else None.

    The three axes every other composing path already asks about, in one place
    because this pipeline has two of them — :func:`process` and
    :func:`_redraft_in_place` — and they did not agree.

    * **Consent.** ``opted_out`` is the recipient's decision, and it holds even
      when they are the one who just wrote. CAN-SPAM aside, it is simply the
      promise the product made.
    * **The user's own decision.** ``excluded_at`` is "stop writing to this
      contact". Flagged rather than dropped: they wrote in, so the mail is
      still worth reading — but a reply the agent might auto-send is precisely
      what that flag ruled out.
    * **Deliverability.** A hard bounce is permanent by design — nothing
      revives it but the user (see :func:`bounce_service.clear_soft_bounces`) —
      so a reply to that address is mail that cannot arrive, and each attempt
      is another bounce against the mailbox the user's whole pipeline depends
      on.

    That third one was missing here while `outreach_service`,
    `follow_up_service` and `follow_up_suggestions` all had it, and the
    send-time backstop in ``email_tasks`` — whose own comment calls itself the
    guard for an address that bounced *after* composing — was carrying the
    whole case. It stops the mail, but only after the row has been drafted,
    routed AUTO and moved to ``REPLY_QUEUED``: the ``Email`` goes ``FAILED``
    and nothing walks that back onto the ``RecruiterEmail``, so the recruiter
    inbox reads "Reply queued" forever for a reply that was refused at the
    transport and never explained.

    :func:`_redraft_in_place` asked none of the three. Its docstring's promise
    that the re-read path "re-does ... the opt-out check" was true only of the
    branch that hands the row back to :func:`process`; the branch that rewrites
    an existing draft is the one that flips ``DRAFT`` to ``QUEUED``, and it is
    reached days or weeks after the draft was written — which is exactly long
    enough for consent to have been withdrawn in between.
    """
    existing = _existing_recruiter(db, user, address)
    if existing is None:
        return None
    if existing.opted_out:
        return (
            "flagged_opted_out",
            "This address previously asked not to be contacted, so nothing was "
            "drafted. Reply from your own inbox if you want to.",
        )
    if existing.is_excluded:
        return (
            "flagged_excluded",
            "You excluded this contact, so nothing was drafted. Reply from your "
            "own inbox, or un-exclude them to let the agent answer.",
        )
    if bounce_service.is_suppressed(existing):
        return (
            "flagged_undeliverable",
            "Mail to this address has hard-bounced before, so nothing was "
            "drafted — a reply would bounce again and count against your "
            "sending reputation. Reply from your own inbox if you have a "
            "working address for them.",
        )
    return None


def _get_or_create_recruiter(
    db: Session,
    user: User,
    row: RecruiterEmail,
    classification: recruiter_classifier.Classification,
) -> Recruiter:
    """The contact row for whoever wrote in.

    Keyed on :attr:`RecruiterEmail.reply_address` rather than the ``From``: the
    contact is the person a reply reaches, and for platform-sent outreach those
    are different addresses. Filing it under the ``noreply@`` would make one
    contact row out of every recruiter who happens to use the same platform.

    ``confidence`` is 1.0 unconditionally: they emailed us, so the address is
    verified by the strongest evidence there is — unlike a scraped or guessed one.
    An existing row keeps whatever the user has curated on it and only gains
    facts it was missing.
    """
    recruiter = _existing_recruiter(db, user, row.reply_address)
    if recruiter is not None:
        recruiter.name = recruiter.name or row.from_name
        recruiter.company = recruiter.company or classification.company
        return recruiter

    recruiter = Recruiter(
        user_id=user.id,
        email=row.reply_address,
        name=row.from_name,
        company=classification.company,
        source="inbound",
        confidence=1.0,
    )
    db.add(recruiter)
    db.flush()
    return recruiter


def is_inbound_reply(db: Session, email: Email) -> bool:
    """True when *email* is our answer to a message a recruiter sent us.

    Asked by the sender, which is shared with cold outreach and so cannot tell
    the two apart from the row alone. The link is
    :attr:`RecruiterEmail.reply_email_id`, written when the reply is
    materialised, rather than the inbound campaign's name: the name is a label a
    future migration could reasonably change, and this is load-bearing enough
    that it should key off a foreign key.
    """
    return db.scalar(
        select(func.count(RecruiterEmail.id)).where(
            RecruiterEmail.reply_email_id == email.id
        )
    ) > 0


#: How much of a transport's reason fits on the row that has to explain it.
_LAST_ERROR_LIMIT = 500


def _reply_row(db: Session, email: Email) -> RecruiterEmail | None:
    """The recruiter-inbox row *email* is the answer to, if it is one."""
    return db.scalar(
        select(RecruiterEmail).where(RecruiterEmail.reply_email_id == email.id)
    )


def record_send_failure(db: Session, email: Email, reason: str) -> bool:
    """Tell the recruiter inbox that the reply it queued never went out.

    ``REPLY_QUEUED`` is where this pipeline leaves a row once the reply is on
    the broker, and nothing ever moved it again — ``REPLIED`` is read in four
    places and assigned in none. So the terminal state of an auto-reply was
    "queued", whatever the transport went on to do with it.

    That is not a cosmetic gap, because ``recruiter_inbox._REPLIED`` counts
    ``REPLY_QUEUED`` as replied: it feeds the "Replied" chip, the "Replied"
    filter, the per-conversation stats and the auto-reply tally. A reply
    refused at the transport — the recipient opted out while the batch
    trickled, the user excluded them, the address had hard-bounced, the mailbox
    lost its grant, Gmail rejected the message — left the row reading *replied*
    for a reply that was never sent, with nothing anywhere saying otherwise.
    The user's own count of what the agent had answered was wrong, in the
    direction that stops them answering it themselves.

    ``FAILED`` is the state that already exists for this and is already inside
    :func:`recruiter_inbox._needs_you_clause`, so the row moves from a silent
    "replied" to the "Needs you" tab.

    The reason is written twice on purpose. ``last_error`` is what the enum's
    own comment points at and is the machine-facing record; ``flag_reason`` is
    "why a human is needed, in the words the UI shows verbatim", and it is the
    only one of the two the recruiter-inbox row carries to the client. Writing
    only ``last_error`` would put the row in "Needs you" with nothing on screen
    but the routing sentence that explained why it was auto-sent — which is now
    the least useful thing it could say.

    Returns whether a row was updated, so a caller sending ordinary cold
    outreach — which has no recruiter-inbox row — can tell that this did
    nothing. The caller owns the commit.
    """
    row = _reply_row(db, email)
    if row is None:
        return False
    sentence = (reason or "The reply could not be sent.")[:_LAST_ERROR_LIMIT]
    row.status = RecruiterEmailStatus.FAILED
    row.last_error = sentence
    row.flag_reason = sentence
    db.flush()
    return True


def record_send_held(db: Session, email: Email, reason: str | None) -> bool:
    """The same write-back for a reply parked in the review queue, not written off.

    ``_park_for_review`` turns the queued mail back into a ``DRAFT`` so the work
    survives a reputation hold that outlived its retry budget. The recruiter row
    kept saying ``REPLY_QUEUED``, which is now the opposite of true: the message
    is waiting on the user, and the inbox was counting it as answered.
    """
    row = _reply_row(db, email)
    if row is None:
        return False
    row.status = RecruiterEmailStatus.DRAFTED
    row.route = ReplyRoute.DRAFT
    row.flag_reason = (reason or "Held back to protect your sending reputation.")[
        :_LAST_ERROR_LIMIT
    ]
    db.flush()
    return True


#: Row states a successful send is allowed to advance. Both mean "a reply for
#: this message is on its way": ``REPLY_QUEUED`` is the auto-send path handing it
#: to the broker, ``DRAFTED`` is one waiting in ``/review`` that a human then
#: approved. Every other state is either about a different decision (``IGNORED``,
#: ``FLAGGED``, ``CLASSIFIED``) or already terminal, and a send landing on one of
#: those is not this function's business to reinterpret.
_SENDABLE_STATES = (
    RecruiterEmailStatus.REPLY_QUEUED,
    RecruiterEmailStatus.DRAFTED,
)


def record_send_success(db: Session, email: Email) -> bool:
    """Tell the recruiter inbox that the reply it was holding actually went.

    The counterpart to :func:`record_send_failure`, and the half that was
    missing. ``RecruiterEmailStatus.REPLIED`` was read in four places and
    assigned in none, so a row never moved once a reply was written — and the
    two states it got stuck in are wrong in opposite directions.

    ``REPLY_QUEUED`` is the milder one: ``recruiter_inbox._REPLIED`` counts it as
    replied, so the chips and filters read correctly even though the row can no
    longer tell "on the broker" from "confirmed sent" — the distinction
    ``integrity.stranded:reply_queued_never_sent`` exists to look for.

    ``DRAFTED`` is the damaging one, and it is the ordinary path: most inbound
    replies are routed ``DRAFT``, the user approves them in ``/review``, and
    :func:`app.routers.review.approve` queues the ``Email`` without touching the
    recruiter row. So a reply the user read, approved and sent left the row
    saying a draft was still waiting. That is a stale "Drafts" chip pointing at a
    review queue with nothing in it, an undercounted "Replied", and — the part
    that sends mail — a sender ``recruiter_follow_up.find_prior_engagement``
    cannot see: ``ENGAGED_STATUSES`` deliberately excludes ``DRAFTED``, because
    a draft nobody approved is not a conversation. A draft somebody *did*
    approve is, and looking exactly like one that was never sent is how the
    recruiter's second message — arriving on a fresh thread, as platform mail
    does — was read as first contact and became eligible to be auto-replied to a
    second time.

    ``flag_reason`` and ``last_error`` are cleared because both are sentences
    about a send that has now happened: a row parked by
    :func:`record_send_held` carries "Held back to protect your sending
    reputation", and ``flag_reason`` is the one the client shows.

    Returns whether a row was updated, so cold outreach — which has no
    recruiter-inbox row — can tell that this did nothing. The caller owns the
    commit.
    """
    row = _reply_row(db, email)
    if row is None or row.status not in _SENDABLE_STATES:
        return False
    row.status = RecruiterEmailStatus.REPLIED
    row.flag_reason = None
    row.last_error = None
    db.flush()
    return True


def record_draft_discarded(db: Session, email: Email) -> bool:
    """The user read the reply we wrote for them and threw it away.

    ``review.dismiss`` deletes the ``Email``. ``reply_email_id`` is ``SET NULL``
    so the pointer goes, and the recruiter row was left at ``DRAFTED`` —
    claiming a reply is waiting in a review queue that no longer holds one. The
    "Drafts" chip counted it forever and the row appeared in no other view, so
    the one thing that was actually true — a recruiter wrote, nobody has
    answered, and the product is no longer going to — was the one thing the
    inbox could not say.

    ``FLAGGED`` says it. It is inside :func:`recruiter_inbox._needs_you_clause`,
    so the row moves to "Needs you", which is where an unanswered recruiter with
    no draft behind it belongs. It is also the state the ``FLAG`` band leaves a
    row in, and the recovery from there already exists: ``generate-reply``
    refuses only while ``reply_email_id`` is set, and this clears it.

    Not ``IGNORED``. Discarding a draft is a verdict on what *we wrote*, and
    dismissing the message is a verdict on the *sender* — the inbox has a
    separate gesture for that, and collapsing the two would hide a live
    recruiter behind a rejected paragraph.

    ``draft_discarded_at`` is stamped as well, and it is the half that matters
    for safety. ``FLAGGED`` with no draft attached is precisely the shape
    :func:`retry_degraded` exists to sweep up and answer, so without a mark of
    its own the refusal read as "never got a reply" ten minutes later: the row
    was re-classified, re-drafted, and on a confident re-read routed to ``AUTO``
    and sent in the candidate's name — the one paragraph they had already read
    and said no to. The mark is on the row rather than inferred from
    ``reply_feedback``, because that table is the *learning* signal and lives
    behind ``recruiter_feedback_enabled``; switching the learning off must not
    turn a refusal back into an invitation.

    ``reply_email_id`` is cleared here rather than left to the foreign key,
    because the ORM does not know about the ``SET NULL`` and would keep serving
    the stale id from its identity map for the rest of the request.

    Returns whether a row was updated — ``False`` for cold outreach and for the
    thread pipeline's drafts, neither of which has a recruiter-inbox row. Must
    be called *before* the delete: the lookup is by ``reply_email_id``, and
    afterwards there is nothing left to find it by. The caller owns the commit.
    """
    row = _reply_row(db, email)
    if row is None:
        return False
    row.status = RecruiterEmailStatus.FLAGGED
    row.reply_email_id = None
    # Kept from the first discard: a second one is the same verdict again, not a
    # newer one, and the age of the refusal is not what any reader of this is
    # asking about.
    if row.draft_discarded_at is None:
        row.draft_discarded_at = datetime.now(UTC)
    row.flag_reason = (
        "You discarded the reply we drafted, so this one is still unanswered."
    )
    db.flush()
    return True


def _inbound_campaign(db: Session, user: User) -> Campaign:
    """The one campaign every inbound reply is filed under, created on demand."""
    campaign = db.scalar(
        select(Campaign).where(
            Campaign.user_id == user.id, Campaign.name == INBOUND_CAMPAIGN_NAME
        )
    )
    if campaign is not None:
        # Re-armed the way the autopilot container is, and against the same
        # dead end: `_maybe_complete_campaign` marks this row COMPLETED as soon
        # as its last reply drains, and the next reply to arrive was then filed
        # under a campaign the tracker renders as finished and the completion
        # check — which only ever looks at an ACTIVE campaign — could never
        # correct. PAUSED is left alone: that one is the user talking.
        if campaign.status is not CampaignStatus.PAUSED:
            campaign.status = CampaignStatus.ACTIVE
            campaign.completed_at = None
        return campaign

    campaign = Campaign(
        user_id=user.id,
        name=INBOUND_CAMPAIGN_NAME,
        status=CampaignStatus.ACTIVE,
        # Replies are governed by the confidence bands, not by a campaign-wide
        # send switch, and nothing chases a recruiter who contacted us first.
        auto_send=False,
        follow_up_enabled=False,
    )
    db.add(campaign)
    db.flush()
    return campaign


# --------------------------------------------------------------------------- #
# Regenerating on demand (the API's rematch / generate-reply)                   #
# --------------------------------------------------------------------------- #


def classification_from_row(row: RecruiterEmail) -> recruiter_classifier.Classification:
    """Rebuild the stored classification without calling a model again.

    Used by ``rematch`` and ``generate-reply``: the user is asking us to redo the
    *matching* or the *writing*, not the reading, and the reading is the only part
    that costs a model call.
    """
    extracted = row.extracted or {}
    asks = extracted.get("asks")
    return recruiter_classifier.Classification(
        kind=row.kind,
        confidence=row.classification_confidence,
        classified_by=row.classified_by or "stored",
        role_title=extracted.get("role_title"),
        company=extracted.get("company"),
        location=extracted.get("location"),
        remote=extracted.get("remote"),
        salary_text=extracted.get("salary_text"),
        seniority=extracted.get("seniority"),
        asks=list(asks) if isinstance(asks, list) else [],
        reason=row.match_reason or "",
    )


def target_for_profile(
    db: Session, user: User, profile_id: int | None
) -> ScoringTarget | None:
    """The scoring target for a specific profile, or the best available one."""
    targets = profile_service.active_targets(db, user)
    if not targets:
        return None
    if profile_id is not None:
        for target in targets:
            if target.profile_id == profile_id:
                return target
        return None
    return targets[0]


def rematch(
    db: Session, row: RecruiterEmail, *, profile_id: int | None = None
) -> RecruiterEmail:
    """Re-run matching for a row, optionally forcing a profile the user picked.

    Rewrites the score, the route and the reason. Does **not** touch a reply that
    already exists — the user's next move is "generate a reply", which is a
    separate, explicit ask.
    """
    user = db.get(User, row.user_id)
    classification = classification_from_row(row)

    if profile_id is None:
        matched = inbound_matcher.match(
            db, user, classification, body=row.body_text or "", subject=row.subject
        )
        row.matched_profile_id = matched.profile_id if matched else None
        row.match_score = matched.score if matched else None
        row.match_reason = matched.reason if matched else None
        location_ok = matched.location_ok if matched else True
        label = matched.label if matched else None
        has_profile = matched is not None
    else:
        target = target_for_profile(db, user, profile_id)
        if target is None:
            raise ValueError("Profile not found")
        parsed = parse_job(
            classification.as_job_text(row.body_text or "", row.subject),
            page_title=classification.role_title,
            use_llm=False,
        )
        fit = fit_scorer.score_fit(
            target.resume, parsed, targeting=target.targeting, explain=True
        )
        row.matched_profile_id = target.profile_id
        row.match_score = round(fit.overall, 2)
        row.match_reason = fit.summary or fit.recommendation
        location_ok = (
            inbound_matcher.location_gate(target, parsed, classification) is None
        )
        label = target.label
        has_profile = True

    pref = preference_for(db, user)
    # Re-read the feedback rather than reusing the number stored at classify
    # time: the user may have approved or discarded something since, and a
    # rematch is exactly the moment they are asking for a fresh answer.
    adjustment = classifier_feedback.adjustment_for(db, row.user_id, row.reply_address)
    row.confidence_adjustment = adjustment.delta
    decision = _route_with_feedback(
        row.kind,
        row.classification_confidence,
        adjustment,
        row.match_score,
        has_profile=has_profile,
        location_ok=location_ok,
        profile_label=label,
        auto_enabled=auto_reply_allowed(db, user, pref),
    )
    row.route = decision.route
    row.route_confidence = decision.confidence
    if decision.route is ReplyRoute.FLAG and row.reply_email_id is None:
        row.status = RecruiterEmailStatus.FLAGGED
        row.flag_reason = decision.reason
    db.flush()
    return row


def generate_reply(db: Session, row: RecruiterEmail) -> RecruiterEmail:
    """Write a reply for a row the user has asked us to answer.

    The escape hatch for the ``FLAG`` band: the product wasn't confident enough
    to write unprompted, the user disagrees, and this honours that. Always
    produces a ``DRAFT`` — an explicit "write me one" is a request for something
    to read, never for something already gone.

    Raises ``ValueError`` when nothing may be written to the address at all; the
    router turns it into a 409 carrying the sentence. This was the third caller
    of :func:`_suppression` and the one that did not ask — the same gap that
    function was written to close between :func:`process` and
    :func:`_redraft_in_place`, in the one path a *person* triggers. An opted-out
    recruiter is the case that matters: consent is the recipient's and holds
    however the drafting was started, and the send-time backstop refusing it
    afterwards means the user reads a draft, approves it, and is told it failed.
    A hard-bounced address is the same story with a bounce on the end of it.

    The draft this writes is stamped ``Email.user_owned_at``. It exists because
    the user asked for it, which makes it theirs to send and not something a
    later sweep may rewrite and route ``AUTO`` — see :func:`retry_degraded`.
    """
    user = db.get(User, row.user_id)

    suppressed = _suppression(db, user, row.reply_address)
    if suppressed is not None:
        raise ValueError(suppressed[1])

    classification = classification_from_row(row)

    target = target_for_profile(db, user, row.matched_profile_id)
    if target is None:
        raise ValueError("No profile is available to write from")

    parsed = parse_job(
        classification.as_job_text(row.body_text or "", row.subject),
        page_title=classification.role_title,
        use_llm=False,
    )
    fit = fit_scorer.score_fit(
        target.resume, parsed, targeting=target.targeting, explain=True
    )
    matched = inbound_matcher.InboundMatch(
        target=target,
        parsed=parsed,
        score=round(fit.overall, 2),
        reason=fit.summary or fit.recommendation,
        location_ok=True,
    )
    row.matched_profile_id = target.profile_id
    row.match_score = matched.score

    decision = reply_routing.RouteDecision(
        ReplyRoute.DRAFT,
        row.route_confidence or 0.0,
        f"You asked for a reply — drafted against your {target.label} profile.",
    )
    _write_reply(db, user, row, classification, matched, decision)
    # ``_write_reply`` sets the route only where it *downgrades* one, because
    # ``process`` has already written the band it chose. Nothing writes it on
    # this path, so a row the user asked for a draft on read ``DRAFTED`` beside
    # a null route — "never routed" — which is exactly the shape the retry
    # sweep goes looking for.
    row.route = ReplyRoute.DRAFT
    if row.reply_email_id is not None:
        reply = db.get(Email, row.reply_email_id)
        if reply is not None:
            reply.user_owned_at = datetime.now(UTC)
    return row


__all__ = [
    "INBOUND_CAMPAIGN_NAME",
    "auto_reply_allowed",
    "auto_replies_last_24h",
    "classification_from_row",
    "generate_reply",
    "preference_for",
    "process",
    "record_scan",
    "rematch",
    "retry_degraded",
    "target_for_profile",
]
