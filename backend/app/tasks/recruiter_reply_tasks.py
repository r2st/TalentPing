"""Celery tasks for inbound recruiter mail.

Four tasks, in a deliberate shape:

``scan_all_recruiter_inboxes``
    The beat entrypoint. Fans out one task per eligible mailbox and does no work
    itself, so a slow mailbox delays only its own scan.

``scan_mailbox``
    Reads one mailbox and writes a ``DETECTED`` row per new message, then
    enqueues one processing task per row.

``process_recruiter_email``
    Classifies, matches, routes and writes a reply for exactly one message.

``retry_degraded_classifications``
    Reads again the messages that were classified while no model could be
    reached. The three above give each message exactly one attempt at a provider
    chain that, on the free tiers, answers about a third of the time; this is
    what stops the other two thirds being stuck with a keyword guess forever.

**Why one task per message rather than a loop.** Processing makes one or two
model calls, and a provider timing out must not hold a scan's database session
open behind it. It also means a single unparseable message fails a single task
instead of a mailbox's whole run.

**Every task is replay-safe.** ``task_acks_late`` is on globally, so a worker
killed mid-flight replays rather than drops. The scanner dedups on
``(user_id, gmail_message_id)`` and :func:`recruiter_reply_service.process`
no-ops on anything that has moved past ``DETECTED``, so a replay re-reads instead
of re-replying.

**And every scan is debounced.** Beat runs every minute, Gmail push fires on
delivery and the user can press "scan now" — three sources aiming at one mailbox.
:func:`recently_scanned` is the single gate in front of all of them: a mailbox
read inside ``RECRUITER_SCAN_MIN_GAP_SECONDS`` is left alone. Without it, a scan
that takes longer than the beat interval has its successor queued behind it, and
a slow mailbox turns into a queue that never drains.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.application import Application
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.gmail_account import GmailAccount
from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailStatus,
    RecruiterReplyPreference,
    ReplyRoute,
)
from app.models.recruiter_scan_run import TRIGGER_BEAT, TRIGGER_MANUAL
from app.models.user import User
from app.services import (
    gmail_accounts,
    gmail_push,
    gmail_service,
    recruiter_classifier,
    recruiter_reply_service,
    reputation_service,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def recently_scanned(
    source: RecruiterReplyPreference | GmailAccount, *, now: datetime | None = None
) -> bool:
    """True when this mailbox was read too recently to read again.

    A user who presses "scan now" is asking for a scan and gets one — the button
    passes ``force``. This gate is for the automatic callers, which have no
    opinion about *when*, only about *often*.

    *source* is a :class:`GmailAccount` — the debounce is **per mailbox**. It
    used to be keyed on ``RecruiterReplyPreference``, which is one row per user,
    so with several mailboxes the first one scanned in a cycle suppressed every
    other one until the gap elapsed. Multi-mailbox scanning would have looked
    like it worked while only ever reading one inbox per cycle. A preference row
    is still accepted, because a user with no connected mailbox has nothing else
    to be gated on.
    """
    gap = settings.recruiter_scan_min_gap_seconds
    if gap <= 0 or source.last_scan_at is None:
        return False
    last = source.last_scan_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - last < timedelta(seconds=gap)


@celery_app.task
def scan_all_recruiter_inboxes() -> dict:
    """Beat entrypoint: enqueue a scan per mailbox that has asked to be watched.

    Every skip is counted and named in the result rather than logged and
    forgotten — "why did nothing happen?" should be answerable from the task
    result alone.

    **And every failure is counted too.** The module docstring promises that a
    fan-out means "a slow mailbox delays only its own scan", which was true of a
    slow mailbox and not of a failing one: nothing here was guarded, so a single
    raise — a Redis blip on ``apply_async``, a row that upsets one of the gates —
    ended the sweep where it stood and left every mailbox after it unscanned. The
    preference rows come back in the same order every tick, so it was always the
    same tail, and to the users behind it the feature had simply stopped
    detecting mail.
    """
    if not settings.recruiter_reply_enabled:
        return {"status": "disabled"}

    db = SessionLocal()
    try:
        prefs = db.scalars(
            select(RecruiterReplyPreference).where(
                RecruiterReplyPreference.enabled.is_(True)
            )
        ).all()

        tally = dict.fromkeys(
            ("enqueued", "no_mailbox", "paused", "too_soon", "push_covered", "failed"),
            0,
        )
        dispatch_error: str | None = None
        for pref in prefs:
            try:
                dispatch_error = _fan_out_for(db, pref, tally, dispatch_error)
            except Exception as exc:  # noqa: BLE001 - one mailbox must not stall the sweep
                tally["failed"] += 1
                logger.exception("recruiter scan fan-out failed for pref %s", pref.id)
                dispatch_error = dispatch_error or str(exc)[:200]

        return {
            "mailboxes_enqueued": tally["enqueued"],
            "skipped_no_mailbox": tally["no_mailbox"],
            "skipped_paused": tally["paused"],
            "skipped_too_soon": tally["too_soon"],
            "skipped_push_covered": tally["push_covered"],
            "failed": tally["failed"],
            "dispatch_error": dispatch_error,
            "watched": len(prefs),
        }
    finally:
        db.close()


def _fan_out_for(
    db, pref: RecruiterReplyPreference, tally: dict[str, int], dispatch_error: str | None
) -> str | None:
    """Enqueue a scan for each of one user's eligible mailboxes.

    Split out so the caller's ``except`` has something to wrap that is exactly
    one preference row's worth of work. Counts land in *tally*; the return value
    carries forward the first broker refusal of the sweep, because once the
    broker has refused a publish, asking it again per mailbox costs a connect
    timeout apiece and will get the same answer.
    """
    user = db.get(User, pref.user_id)
    if user is None:
        return dispatch_error
    # Every connected mailbox, not just the primary. A recruiter writing to a
    # secondary address was previously never detected at all — the sweep only
    # ever looked in one inbox.
    accounts = gmail_accounts.live_accounts(user)
    if not accounts:
        tally["no_mailbox"] += 1
        return dispatch_error

    for account in accounts:
        # Push already covers this mailbox and is demonstrably delivering, so
        # there is nothing for a sweep to find that the webhook has not already
        # enqueued. This is what makes beat a *fallback* rather than a duplicate
        # — and `push_covers` deliberately requires recent traffic, not just an
        # "active" row, so a subscription that dies quietly is back on polling
        # within the trust window instead of going silent. See
        # services/gmail_push.push_covers.
        if gmail_push.push_covers(account.watch):
            tally["push_covered"] += 1
            continue
        # A mailbox in a reputation pause is in trouble already; reading it is
        # harmless but replying from it is not, and the honest thing is to leave
        # it alone until the pause lifts. Per mailbox: one account's pause is no
        # reason to stop reading the others.
        decision = reputation_service.evaluate(account, 0)
        if not decision.allowed and account.paused_until is not None:
            tally["paused"] += 1
            continue
        # Checked before enqueueing as well as inside the task: a scan that will
        # be refused is better not queued at all. Keyed on the mailbox, so the
        # first account scanned does not suppress the rest of them.
        if recently_scanned(account):
            tally["too_soon"] += 1
            continue
        if dispatch_error is not None:
            continue
        try:
            # Expire alongside the next tick. A scan still waiting when its
            # replacement is due has nothing left to contribute — the newer one
            # sees everything it would have.
            scan_mailbox.apply_async(
                args=[user.id, account.id],
                expires=settings.recruiter_scan_interval_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - broker down; the next tick retries
            logger.warning("recruiter scan for %s not dispatched: %s", account.id, exc)
            dispatch_error = str(exc)[:200]
            continue
        tally["enqueued"] += 1

    return dispatch_error


@celery_app.task
def scan_mailbox(
    user_id: int,
    account_id: int,
    *,
    force: bool = False,
    trigger: str = TRIGGER_BEAT,
) -> dict:
    """Read one mailbox, record what's new, and queue each message for processing.

    *force* is the user pressing "scan now": they asked, so the debounce that
    protects against beat and push overlapping doesn't apply to them.

    *trigger* names the caller so the scan-run row can record it. It is what
    answers "is push actually doing the work, or is beat still finding
    everything?" — the operational question that making push primary creates.
    """
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return {"user_id": user_id, "status": "no_user"}
        account = next(
            (a for a in user.gmail_accounts if a.id == account_id), None
        )
        if account is None:
            return {"user_id": user_id, "status": "no_account"}

        if not force and recently_scanned(account):
            db.commit()
            return {"user_id": user_id, "status": "too_soon"}

        try:
            result, created = recruiter_reply_service.record_scan(
                db, user, account, trigger=TRIGGER_MANUAL if force else trigger
            )
        except gmail_service.GmailAuthRevoked as exc:
            # Nothing can be read from this mailbox until the candidate grants
            # access again. Record that once, so beat skips it from here on
            # instead of re-raising the same traceback every minute.
            db.rollback()
            account = db.get(GmailAccount, account_id)
            if account is not None:
                gmail_accounts.mark_revoked(db, account, str(exc))
                db.commit()
            return {"user_id": user_id, "status": "gmail_revoked", "reason": str(exc)}
        db.commit()

        dispatched = 0
        for i, row in enumerate(created):
            try:
                # Stagger processing so LLM calls don't fire simultaneously
                # and exhaust free-tier rate limits on every provider at once.
                # Ten seconds between messages keeps each classify call well
                # inside the per-minute windows of OpenRouter, Gemini and Groq.
                process_recruiter_email.apply_async(
                    args=[row.id], countdown=i * 10
                )
                dispatched += 1
            except Exception as exc:  # noqa: BLE001 - broker down; beat retries
                logger.warning(
                    "recruiter email %s could not be queued for processing: %s",
                    row.id,
                    exc,
                )
        return {"user_id": user_id, "dispatched": dispatched, **result.as_dict()}
    finally:
        db.close()


@celery_app.task
def process_recruiter_email(
    recruiter_email_id: int, max_auto_age_days: int | None = None
) -> dict:
    """Classify, match, route and (when warranted) reply to one message.

    *max_auto_age_days* holds an otherwise-auto reply for review once the
    recruiter's message is older than that. The live path leaves it ``None`` —
    a message detected minutes after it arrived has no staleness to judge — and
    :mod:`app.tasks.backlog_tasks` passes a ceiling, because a catch-up answers
    mail nobody has looked at in a while and a prompt-sounding reply to a
    three-week-old note is worse than the draft it replaced.
    """
    db = SessionLocal()
    try:
        row = db.get(RecruiterEmail, recruiter_email_id)
        if row is None:
            return {"recruiter_email_id": recruiter_email_id, "status": "missing"}

        try:
            outcome = recruiter_reply_service.process(
                db, row, max_auto_age_days=max_auto_age_days
            )
        except Exception as exc:  # noqa: BLE001 - one bad message must not poison the queue
            logger.exception("processing recruiter email %s failed", recruiter_email_id)
            db.rollback()
            row = db.get(RecruiterEmail, recruiter_email_id)
            if row is not None:
                row.status = RecruiterEmailStatus.FAILED
                row.last_error = str(exc)[:500]
                db.commit()
            return {
                "recruiter_email_id": recruiter_email_id,
                "status": "failed",
                "reason": str(exc)[:200],
            }

        db.commit()

        # An auto-reply goes out through the ordinary throttled sender, which is
        # what keeps the reputation gate, the warm-up ramp and the daily cap in
        # front of it. There is no second send path.
        if row.route is ReplyRoute.AUTO and row.reply_email_id is not None:
            _dispatch_send(row.reply_email_id)

        return outcome
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The replies this pipeline held before it recorded that it had                 #
# --------------------------------------------------------------------------- #


@celery_app.task
def flag_pending_recruiter_replies(
    user_id: int | None = None,
    limit: int = 1000,
    dry_run: bool = True,
) -> dict:
    """Give every already-held reply the reason it was held.

    ``_write_reply`` learned to set ``needs_attention``/``attention_reason`` in
    the fix that shipped before this; drafts written *earlier* still sit at
    ``DRAFT`` with the flag false, which the "Needs review" filter and the badge
    read as "nothing has looked at this yet". On production that is 159 replies.

    **This is the recruiter pipeline's half of the backlog, and it is a separate
    task on purpose.** ``inbox_tasks.reevaluate_pending_replies`` walks the
    thread poller's drafts and re-runs ``thread_reply_policy`` over them, which
    it may do because that policy is the one that routed them. It explicitly
    skips everything written here — reported as ``other_pipeline`` — because
    routing a recruiter-inbox draft under the thread policy would be one policy
    silently overruling another's decision to hold. Running it on production
    confirmed the split exactly: 161 considered, 161 skipped, 0 routed.

    So this task does not re-decide anything. The route, the confidence band and
    the reason were all settled by :func:`recruiter_reply_service.process` when
    the mail arrived, and are on the row; the only thing missing was that the
    inbox could not see them. It copies that decision onto the draft and changes
    nothing else.

    **Nothing is sent.** Not a caution — an accurate description of the data. A
    row that routed ``AUTO`` had its reply queued at the time and is not in this
    set; every row that is here routed ``DRAFT`` or ``FLAG``. There is no
    qualifying-but-unsent draft for this task to find, so there is no send path
    to write, and adding one would mean re-deciding under today's settings mail
    that was decided under the settings in force when it arrived.

    Idempotent: a flagged draft no longer matches, so a partial run repeats
    safely. Dry run by default, so the count can be read before it is written.
    """
    db = SessionLocal()
    try:
        stmt = (
            select(Email, RecruiterEmail)
            .join(RecruiterEmail, RecruiterEmail.reply_email_id == Email.id)
            .where(
                Email.direction == EmailDirection.SENT,
                Email.status == EmailStatus.DRAFT,
                # Agent-written. A message the *user* left unsent is theirs.
                Email.draft_template.is_not(None),
                # Not already carrying a reason, from the live path or a re-run.
                Email.needs_attention.is_(False),
            )
            .order_by(Email.id)
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(RecruiterEmail.user_id == user_id)

        flagged = 0
        by_route: dict[str, int] = {}
        plan: list[dict] = []

        for draft, row in db.execute(stmt).all():
            reason = recruiter_reply_service.hold_reason_for(row)
            route = row.route.value if row.route else None
            by_route[route or "none"] = by_route.get(route or "none", 0) + 1
            if not dry_run:
                draft.needs_attention = True
                draft.attention_reason = reason
            flagged += 1
            plan.append(
                {
                    "email_id": draft.id,
                    "recruiter_email_id": row.id,
                    "route": route,
                    "confidence": row.route_confidence,
                    "reason": reason,
                }
            )

        # The drafts no pipeline claims. Written here — the template and the
        # application say so — but with nothing pointing back from a
        # ``recruiter_emails`` row, so the pass above cannot see them and
        # ``reevaluate_pending_replies`` reports them as ``other_pipeline`` and
        # leaves them alone. On production that was two, both from 2026-07-30,
        # and between the two tasks they were the only held replies left with no
        # reason on them at all.
        #
        # Narrow on purpose. A draft whose inbound message carries a classified
        # ``intent`` belongs to the thread poller, which re-routes and may *send*
        # it; taking those would be this task reaching into the other pipeline,
        # which is the whole thing it exists not to do. Only a draft that nobody
        # classified and nobody linked is picked up, and all it gets is the
        # generic sentence, because there is no recorded decision to quote.
        unclaimed = 0
        for draft in db.scalars(_unclaimed_stmt(user_id, limit)).all():
            inbound = db.scalars(
                select(Email)
                .where(
                    Email.thread_id == draft.thread_id,
                    Email.direction == EmailDirection.RECEIVED,
                    Email.id < draft.id,
                )
                .order_by(Email.id.desc())
                .limit(1)
            ).first()
            if inbound is not None and inbound.intent is not None:
                continue  # the thread poller's, not ours
            if not dry_run:
                draft.needs_attention = True
                draft.attention_reason = recruiter_reply_service.HOLD_FALLBACK
            unclaimed += 1
            flagged += 1
            plan.append(
                {
                    "email_id": draft.id,
                    "recruiter_email_id": None,
                    "route": None,
                    "confidence": None,
                    "reason": recruiter_reply_service.HOLD_FALLBACK,
                }
            )
        if unclaimed:
            by_route["unclaimed"] = unclaimed

        if dry_run:
            db.rollback()
        else:
            db.commit()

        logger.info(
            "recruiter reply backfill: %s %s draft(s) %s",
            "would flag" if dry_run else "flagged",
            flagged,
            by_route,
        )
        return {
            "dry_run": dry_run,
            "flagged": flagged,
            "unclaimed": unclaimed,
            "sent": 0,
            "by_route": by_route,
            "plan": plan,
        }
    finally:
        db.close()


def _unclaimed_stmt(user_id: int | None, limit: int):
    """Held agent drafts with no ``recruiter_emails`` row pointing at them."""
    owned = select(RecruiterEmail.reply_email_id).where(
        RecruiterEmail.reply_email_id.is_not(None)
    )
    stmt = (
        select(Email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.DRAFT,
            Email.draft_template.is_not(None),
            Email.needs_attention.is_(False),
            Email.id.not_in(owned),
        )
        .order_by(Email.id)
        .limit(limit)
    )
    if user_id is not None:
        stmt = stmt.where(Application.user_id == user_id)
    return stmt


@celery_app.task
def retry_degraded_classifications(
    user_id: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    max_age_days: int | None = None,
) -> dict:
    """Re-read the messages whose verdict was a no-model guess.

    The pipeline reads each message once, when it arrives. On the free provider
    tiers that one attempt succeeds about a third of the time; the rest fall to
    the keyword fallback, whose confidence cannot clear the auto bar once it is
    multiplied by the match score. Nothing re-read them, so every rate limit that
    lasted seconds left a draft that lasted forever. On production that was 455
    of 822 messages, including 223 real recruiter emails that got no reply at
    all.

    This is the sweep that asks again. The work per row is
    :func:`recruiter_reply_service.retry_degraded`; everything here is about
    *pacing*, because the condition being recovered from is caused by too many
    model calls at once and a sweep that ignored that would sustain it.

    Three things do the pacing:

    * **A small batch.** ``recruiter_retry_batch_size`` rows per run, so new mail
      — which is worth more than backlog — keeps its share of the rate limit.
    * **Least-recently-touched first.** A row that gets a real answer — usable or
      not — gets a new ``updated_at`` and goes to the back of the queue. A row
      that failed because the *chain* was down keeps its timestamp, because no
      turn was taken and it should be first again next time.
    * **An early exit.** ``recruiter_retry_abort_after`` consecutive rows that
      could not reach a provider ends the run. A chain that is down costs two
      attempts, not a batch of provider timeouts.

    **Those two interact, and getting it wrong wedges the sweep.** The abort
    counts only ``still_degraded`` — the chain refused — and never ``unreadable``,
    where a provider answered with something unusable. If both held their
    timestamp and both counted toward the abort, two unparseable messages at the
    head of the queue would end every sweep before it reached anything else, for
    good. About one call in six comes back unparseable, so that is a real queue,
    not a hypothetical one.

    ``dry_run`` reports what it would touch without a single model call — the
    selection is the interesting half, and confirming it should not cost the
    rate limit it is trying to protect.
    """
    if not settings.recruiter_retry_degraded_enabled:
        return {"status": "disabled", "considered": 0}

    limit = settings.recruiter_retry_batch_size if limit is None else limit
    max_age = (
        settings.recruiter_retry_max_age_days if max_age_days is None else max_age_days
    )
    cutoff = datetime.now(UTC) - timedelta(days=max_age)

    db = SessionLocal()
    try:
        stmt = (
            select(RecruiterEmail)
            .where(
                RecruiterEmail.escalated.is_(False),
                # A reply the user read and threw away. Excluded here rather
                # than only refused in ``retry_degraded`` for the same reason
                # the already-sent rows below are: a refusal that writes nothing
                # leaves ``updated_at`` alone, so the row leads the
                # least-recently-tried order again on every sweep and starves
                # the ones a re-read would help. A discard is permanent, so this
                # belongs in the query.
                RecruiterEmail.draft_discarded_at.is_(None),
                RecruiterEmail.received_at >= cutoff,
                # Never a message that has already been answered.
                RecruiterEmail.status.notin_(
                    (
                        RecruiterEmailStatus.REPLY_QUEUED,
                        RecruiterEmailStatus.REPLIED,
                        RecruiterEmailStatus.IGNORED,
                    )
                ),
                # Spelled out rather than ``!= AUTO``: SQL's inequality drops
                # NULLs, and a NULL route is a CLASSIFIED row that never got as
                # far as being routed — exactly the shape this sweep is for.
                or_(
                    RecruiterEmail.route.is_(None),
                    RecruiterEmail.route != ReplyRoute.AUTO,
                ),
                # A row whose reply has already been queued or sent is finished,
                # and it must be excluded *here* rather than skipped in the loop
                # below. ``retry_degraded`` refuses it before making a model
                # call, so skipping costs nothing — but it also writes nothing,
                # which means the row keeps its ``updated_at`` and leads the
                # least-recently-tried order again on the next sweep. On
                # production 57 of 415 rows are in this state: enough to fill
                # every batch of ten, forever, and starve the 137 that a re-read
                # would actually help. A SENT email never returns to DRAFT, so
                # this is a permanent exclusion and belongs in the query.
                or_(
                    RecruiterEmail.reply_email_id.is_(None),
                    RecruiterEmail.reply_email_id.in_(
                        select(Email.id).where(
                            Email.status == EmailStatus.DRAFT,
                            # And a draft nobody has taken hold of. A user who
                            # asked for this reply, or edited it, has already
                            # decided it; ``retry_degraded`` refuses it for that
                            # reason, and a refusal writes nothing — so the row
                            # would keep its ``updated_at``, lead the
                            # least-recently-tried order again on every sweep,
                            # and starve the rows a re-read would help. Same
                            # argument as ``draft_discarded_at`` above.
                            Email.user_owned_at.is_(None),
                        )
                    ),
                ),
            )
            # Least recently touched first — see the docstring.
            .order_by(RecruiterEmail.updated_at.asc())
        )
        if user_id is not None:
            stmt = stmt.where(RecruiterEmail.user_id == user_id)

        # ``is_degraded`` is a two-column predicate with a legacy arm, and
        # keeping it in Python keeps one definition of it rather than a SQL copy
        # that can drift. The candidate set is bounded by the filters above.
        candidates = [
            row
            for row in db.scalars(stmt).all()
            if recruiter_classifier.is_degraded(
                row.classified_by, row.classification_confidence
            )
        ]
        eligible = len(candidates)
        batch = candidates[:limit]

        if dry_run:
            return {
                "status": "dry_run",
                "eligible": eligible,
                "considered": len(batch),
                "plan": [
                    {
                        "recruiter_email_id": row.id,
                        "kind": row.kind.value,
                        "classified_by": row.classified_by,
                        "confidence": row.classification_confidence,
                        "match_score": row.match_score,
                        "route": row.route.value if row.route else None,
                        "row_status": row.status.value,
                        "has_draft": row.reply_email_id is not None,
                        "age_days": recruiter_reply_service._received_age_days(row),
                    }
                    for row in batch
                ],
            }

        outcomes: dict[str, int] = {}
        results: list[dict] = []
        consecutive_degraded = 0
        sent = 0

        for row in batch:
            row_id = row.id
            try:
                result = recruiter_reply_service.retry_degraded(db, row)
            except Exception as exc:  # noqa: BLE001 - one bad row must not end the sweep
                logger.exception("retrying recruiter email %s failed", row_id)
                db.rollback()
                result = {"recruiter_email_id": row_id, "outcome": "error",
                          "reason": str(exc)[:200]}
            else:
                db.commit()

            outcome = result.get("outcome", "unknown")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            results.append(result)

            if outcome == "still_degraded":
                consecutive_degraded += 1
                if consecutive_degraded >= settings.recruiter_retry_abort_after:
                    logger.info(
                        "retry sweep stopping: %s rows in a row could not "
                        "reach a provider",
                        consecutive_degraded,
                    )
                    break
                continue
            consecutive_degraded = 0

            # Re-read the row after the commit: ``process`` may have written a
            # reply and routed it to AUTO, and that send goes out through the
            # ordinary throttled sender exactly as it does on the live path.
            refreshed = db.get(RecruiterEmail, row_id)
            if (
                refreshed is not None
                and refreshed.route is ReplyRoute.AUTO
                and refreshed.reply_email_id is not None
            ):
                _dispatch_send(refreshed.reply_email_id)
                sent += 1

        return {
            "status": "ok",
            "eligible": eligible,
            "considered": len(results),
            "outcomes": outcomes,
            "dispatched": sent,
            "results": results,
        }
    finally:
        db.close()


def _dispatch_send(email_id: int) -> None:
    """Hand an approved auto-reply to the throttled sender.

    A short countdown rather than immediate: it matches what
    ``POST /review/emails/{id}/approve`` does, and it leaves a window in which a
    user watching the Recruiter Inbox can still discard the message.
    """
    if not settings.celery_enabled:
        return
    try:
        from app.tasks.email_tasks import send_outreach_email

        send_outreach_email.apply_async(args=[email_id], countdown=60)
    except Exception as exc:  # noqa: BLE001 - broker down; the email stays QUEUED
        logger.warning("auto-reply dispatch failed for email %s: %s", email_id, exc)
