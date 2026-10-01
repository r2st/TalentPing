"""Review queue — the human-in-the-loop safety gate.

Two kinds of message land here as ``DRAFT`` and wait for the user:

* **Reply drafts** the reply agent wrote after a recruiter wrote back. Replies on
  threads we started always land here. A first reply to *unsolicited* inbound can
  skip review when the top confidence band and every gate in
  ``recruiter_reply_service.auto_reply_allowed`` agree; everything else drafts.
* **Outreach drafts**, when the user runs a campaign or autopilot in review mode,
  or when :mod:`app.services.send_policy` held auto-send back (a pause, a spent
  daily ceiling, an unfinished trial).

    GET  /review                     -> a page of what is awaiting approval
    GET  /review/count               -> just the number, for a badge
    POST /review/emails/{id}/approve -> queue + dispatch the send
    POST /review/emails/{id}/dismiss -> discard the draft
    POST /review/approve-batch       -> approve many at once

Editing a draft before approving is the existing ``PATCH /tracker/emails/{id}``.

Approving an *outreach* draft here also counts toward the auto-send trial
(``send_policy``): reading a cold email by hand is exactly the evidence that
trial is waiting for, so the ramp from "show me each one" to "just send them"
needs no separate action from the user — they finish reading N emails and it
graduates itself. Reply approvals deliberately earn no credit; see
:func:`_is_reply`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import Page, page_params
from app.models.application import Application
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_thread import EmailThread
from app.models.reply_feedback import FeedbackSignal
from app.models.user import User
from app.schemas.email import EmailOut
from app.schemas.review import (
    BATCH_LIMIT,
    BatchApproveRequest,
    BatchApproveResult,
    ReviewCount,
    ReviewItem,
    ReviewQueue,
)
from app.services import (
    classifier_feedback,
    email_attachments,
    outreach_service,
    recruiter_reply_service,
    send_policy,
    spam_risk,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/review", tags=["review"])


def _is_reply(db: Session, email: Email) -> bool:
    """True when this draft answers inbound mail rather than opening a thread.

    The same test the queue uses to label an item ``reply`` vs ``outreach``: a
    thread that already holds a received message is a conversation, and anything
    we add to it is a response. Kept as one helper so the label the user reads and
    the trial credit they earn can never disagree.
    """
    return (
        db.scalar(
            select(Email.id)
            .where(
                Email.thread_id == email.thread_id,
                Email.direction == EmailDirection.RECEIVED,
            )
            .limit(1)
        )
        is not None
    )


def _owned_draft(db: Session, user: User, email_id: int) -> Email:
    email = db.scalar(
        select(Email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(Email.id == email_id, Application.user_id == user.id)
    )
    if email is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found"
        )
    if email.status != EmailStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only DRAFT emails can be approved or dismissed",
        )
    return email


def _drafts_of(user: User) -> Select:
    """The join and filters that define "in this user's review queue".

    One definition, used by the page, by the count behind the badge, and by
    :func:`_queue_ids`. Three copies of it is how the number on the tab starts
    disagreeing with the list behind the tab.
    """
    return (
        select(Email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == user.id,
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.DRAFT,
        )
    )


def _draft_count(db: Session, user: User) -> int:
    """How many drafts are waiting, without loading any of them.

    ``with_only_columns`` rather than counting over a subquery of
    :func:`_drafts_of`: that select is an ORM entity select, so wrapping it
    makes the inner query name all thirty-odd columns of ``emails`` —
    ``body_text`` among them — for a result that discards every one. The point
    of this function is that the badge stops reading message bodies, so it had
    better not read them itself.
    """
    return db.scalar(_drafts_of(user).with_only_columns(func.count(Email.id))) or 0


def _latest_inbound_by_thread(
    db: Session, thread_ids: list[int]
) -> dict[int, Email]:
    """The most recent received message on each of *thread_ids*, in one query.

    Ordering by id ascending and letting each thread's later rows overwrite the
    earlier ones leaves the highest id per thread — the same answer as a
    ``order_by(Email.id.desc()).first()`` per thread, for one query instead of
    one per draft.
    """
    if not thread_ids:
        return {}
    rows = db.scalars(
        select(Email)
        .where(
            Email.thread_id.in_(set(thread_ids)),
            Email.direction == EmailDirection.RECEIVED,
        )
        .order_by(Email.thread_id, Email.id)
    ).all()
    return {email.thread_id: email for email in rows}


@router.get("/count", response_model=ReviewCount)
def review_count(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ReviewCount:
    """Just the number of drafts waiting — what a badge actually needs.

    The nav badge, and the two tab badges on the Inbox, all wanted one integer
    and the only endpoint that offered it was the full queue. So the shell polled
    ``GET /review`` every thirty seconds, on every route, and got back every
    draft the account had: bodies in full, a spam-risk pass over each one, and
    the resume-and-cover-letter resolution for each one. On an account mid-
    campaign that is a few hundred emails serialized and thrown away, four times
    a minute, to render a number.

    This is that number, as one ``COUNT(*)``. It loads no bodies, scores no
    content and resolves no attachments — and it is deliberately the same query
    the queue itself counts with, so the badge and the list can never disagree.
    """
    return ReviewCount(count=_draft_count(db, user))


@router.get("", response_model=ReviewQueue)
def review_queue(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    page: Page = Depends(page_params),
) -> ReviewQueue:
    """A page of drafts awaiting the user's approval, newest first.

    Bounded like every other list here — it was the last one that was not, and
    it was the worst one to leave unbounded. A queue row is not a summary: it
    carries the draft's whole body, the snippet of the message it answers, a
    freshly scored spam assessment and the resolved attachment plan. A campaign
    run in review mode writes one per contact it found, so this is another list
    whose length is a function of the pipeline rather than of anything a person
    did.

    ``count`` stays the size of the *whole* queue, not of this page. It is what
    the nav badge and both Inbox tab badges display, and a badge that counted the
    page would report "200 waiting" forever. Same split the tracker makes between
    its stats and its rows, and the inbox between its counts and its threads.
    """
    total = _draft_count(db, user)
    drafts = db.scalars(
        _drafts_of(user)
        .options(
            # The application, its recruiter and its campaign are what a queue
            # row is *made of* — company, name, role, status. Loading them per
            # draft cost three queries a row on the screen a user stares at
            # while a fifty-draft campaign waits for approval.
            #
            # These also pay for themselves twice over: `plan_for_email` looks
            # the same rows up again by primary key, and once they are in the
            # session those `db.get` calls are identity-map hits that emit no
            # SQL at all.
            selectinload(Email.thread)
            .selectinload(EmailThread.application)
            .selectinload(Application.recruiter),
            selectinload(Email.thread)
            .selectinload(EmailThread.application)
            .selectinload(Application.campaign),
        )
        .order_by(Email.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    ).all()

    has_more = page.offset + len(drafts) < total
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Has-More"] = "true" if has_more else "false"

    latest_inbound_by_thread = _latest_inbound_by_thread(
        db, [email.thread_id for email in drafts]
    )
    # The resume-resolution chain, read for the whole queue at once. Strictly a
    # cache — it changes how many queries the loop below runs, not which
    # document any draft resolves to.
    prefetch = email_attachments.prefetch_plans(db, user, drafts)

    items: list[ReviewItem] = []
    for email in drafts:
        thread = email.thread
        application = thread.application
        recruiter = application.recruiter
        campaign = application.campaign

        # A reply draft is a response on a thread that already has inbound mail.
        latest_inbound = latest_inbound_by_thread.get(thread.id)
        kind = "reply" if latest_inbound is not None else "outreach"
        plan = email_attachments.plan_for_email(db, email, prefetch=prefetch)
        # Scored here rather than read off the row: `PATCH /tracker/emails/{id}`
        # lets the user rewrite a draft before approving it, so the only text
        # worth judging is the text sitting in front of them right now.
        content = spam_risk.assess(email.subject, email.body_text)

        items.append(
            ReviewItem(
                email_id=email.id,
                application_id=application.id,
                thread_id=thread.id,
                kind=kind,
                to_address=email.to_address,
                subject=email.subject,
                body_text=email.body_text,
                company=recruiter.company if recruiter else None,
                recruiter_name=(recruiter.name or recruiter.email) if recruiter else None,
                role=(campaign.target_roles or [None])[0] if campaign else None,
                application_status=application.status,
                reply_intent=latest_inbound.intent if latest_inbound else None,
                incoming_snippet=(
                    (latest_inbound.body_text or "")[:200] if latest_inbound else None
                ),
                attachments=plan.filenames,
                attachment_note=plan.reason,
                content_risk=content.risk,
                content_note=content.summary,
                created_at=email.created_at,
            )
        )

    return ReviewQueue(
        count=total,
        returned=len(items),
        has_more=has_more,
        items=items,
        auto_send=send_policy.status(db, user, user.autopilot),
    )


def _queue_for_send(email: Email) -> None:
    """Mark an approved draft for the sender. Caller commits.

    ``auto_sent`` is cleared, not left alone: the draft may have been created by
    the auto-send path and parked by the policy, and now that a human has read it
    it must not count against the unreviewed-send ceiling.
    """
    email.status = EmailStatus.QUEUED
    email.auto_sent = False


def _dispatch(db: Session, email_id: int, *, countdown: int = 30) -> None:
    """Hand one approved email to the throttled sender, best-effort.

    A broker that is down leaves the email ``QUEUED``, which is recoverable — so
    this never raises into the caller's response.

    Stamps ``send_dispatched_at`` on a publish the broker accepted, in its own
    commit because both callers have already committed the approval by the time
    they get here. Without it ``sweep_stranded_sends`` has nothing to tell this
    row apart from an abandoned one: an approved draft is dispatched whenever
    the human gets to it, which can be long after it was written, and the
    sweep's other bound is the *write* time. A draft approved on day ten with a
    countdown still to run therefore looked stranded on sight — see that task
    for what re-dispatching it every hour did to the worker.

    The stamp failing is not the caller's problem either: the message is
    published, and the worst case is one redundant sweep re-dispatch that
    ``_claim_for_send`` collapses to a no-op.
    """
    if not settings.celery_enabled:
        return
    try:
        from app.tasks.email_tasks import send_outreach_email

        send_outreach_email.apply_async(args=[email_id], countdown=countdown)
    except Exception as exc:  # noqa: BLE001 - broker down; email stays QUEUED
        logger.warning("approve dispatch failed for email %s: %s", email_id, exc)
        return

    try:
        email = db.get(Email, email_id)
        if email is not None:
            email.send_dispatched_at = datetime.now(UTC)
            db.commit()
    except Exception:  # noqa: BLE001 - the send is already on its way
        logger.warning("could not stamp dispatch time for email %s", email_id)
        db.rollback()


@router.post("/emails/{email_id}/approve", response_model=EmailOut)
def approve(
    email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Email:
    """Approve a draft: queue it and hand it to the throttled sender.

    Approving an *inbound* reply is also the user agreeing the classification was
    right, which is the only clean signal this product gets about whether a
    stranger was really a recruiter. A no-op for ordinary outreach: approving a
    cold email says nothing about anyone's inbox.
    """
    email = _owned_draft(db, user, email_id)
    is_reply = _is_reply(db, email)
    _queue_for_send(email)
    _record_verdict(db, email_id, FeedbackSignal.APPROVED, "review_approve")
    if not is_reply:
        send_policy.record_approval(user.autopilot)
    db.commit()
    db.refresh(email)

    _dispatch(db, email.id)
    return email


@router.post("/approve-batch", response_model=BatchApproveResult)
def approve_batch(
    payload: BatchApproveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BatchApproveResult:
    """Approve many drafts at once — the "these all look right" gesture.

    Reading twenty near-identical cold emails one dialog at a time is the reason
    review mode gets abandoned rather than used, so the batch exists to make
    reviewing survivable at volume. It is still one deliberate human action per
    batch, and every draft in it was listed on screen first.

    Ids that aren't approvable (already sent, dismissed, someone else's) are
    reported in ``skipped`` rather than failing the batch: a queue the user is
    looking at can go stale between the render and the click, and losing the other
    nineteen approvals to that is worse than skipping one.

    Sends are dispatched with a spread countdown so a batch of twenty does not
    become a burst of twenty — the throttle that protects the mailbox has to
    survive the convenience feature.
    """
    # Explicit ids win over a scope: the caller that names them is looking at a
    # specific list on screen, which is the more precise intent of the two.
    ids = list(dict.fromkeys(payload.email_ids))  # de-dup, keep the user's order
    remaining = 0
    if not ids:
        if payload.scope is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Pass email_ids or a scope to approve",
            )
        ids, remaining = _queue_ids(db, user, payload.scope)

    before = send_policy.evaluate(db, user, user.autopilot)

    approved: list[int] = []
    skipped: dict[int, str] = {}
    outreach_approved = 0
    for email_id in ids:
        try:
            email = _owned_draft(db, user, email_id)
        except HTTPException as exc:
            skipped[email_id] = str(exc.detail)
            continue
        if not _is_reply(db, email):
            outreach_approved += 1
        _queue_for_send(email)
        _record_verdict(db, email_id, FeedbackSignal.APPROVED, "review_approve_batch")
        approved.append(email_id)

    # Only the cold emails count toward the trial — see record_approval. A batch
    # of twenty recruiter replies is not the user vetting what we write to
    # strangers, however many clicks it took.
    send_policy.record_approval(user.autopilot, count=outreach_approved)
    db.commit()

    for offset, email_id in enumerate(approved):
        # 30s, then ~2min apart. The sender re-checks the reputation gate per
        # message anyway, so this only shapes the burst; it does not have to be
        # the real send schedule.
        _dispatch(db, email_id, countdown=30 + offset * 120)

    after = send_policy.evaluate(db, user, user.autopilot)
    return BatchApproveResult(
        approved=len(approved),
        skipped=skipped,
        remaining=remaining,
        trial_completed=(
            before.code == send_policy.REASON_TRIAL and after.enabled
        ),
        auto_send=send_policy.status(db, user, user.autopilot),
    )


def _queue_ids(db: Session, user: User, scope: str) -> tuple[list[int], int]:
    """Draft ids in the user's queue, oldest first, filtered by *scope*.

    Returns ``(ids, remaining)``, capped at :data:`BATCH_LIMIT` — the same
    ceiling the schema puts on an explicit ``email_ids`` list. A scope used to
    bypass it entirely, which made ``scope="all"`` the one way to approve an
    unbounded number of sends in a single request: the dispatch countdown is
    cumulative, so a queue of a thousand drafts scheduled its tail more than a
    day out, and a broker that restarted in between left that mail QUEUED with
    nothing to re-dispatch it. ``remaining`` is how many were left for a
    follow-up call, because a batch that silently stops at 200 reads exactly
    like a queue that is now empty.

    Oldest first so a batch that trips a limit partway sends the mail that has
    been waiting longest, rather than the most recent.
    """
    stmt = _drafts_of(user).with_only_columns(Email.id, Email.thread_id).order_by(Email.id)
    rows = db.execute(stmt).all()

    if scope != "all":
        # Outreach only: a thread carrying inbound mail is a conversation, and
        # the draft on it is a reply to a person. Asked as one query over the
        # threads in hand rather than one per draft — the queue this runs on is
        # a campaign's worth of mail by design.
        thread_ids = {thread_id for _, thread_id in rows}
        conversations = set(
            db.scalars(
                select(Email.thread_id).where(
                    Email.thread_id.in_(thread_ids),
                    Email.direction == EmailDirection.RECEIVED,
                )
            )
        )
        rows = [row for row in rows if row.thread_id not in conversations]

    ids = [row.id for row in rows[:BATCH_LIMIT]]
    return ids, max(0, len(rows) - len(ids))


@router.post("/emails/{email_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss(
    email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Discard a draft the user doesn't want sent."""
    email = _owned_draft(db, user, email_id)
    thread = db.get(EmailThread, email.thread_id)
    if thread is not None and thread.message_count:
        thread.message_count -= 1
    # Booked *before* the delete: the feedback row points at the recruiter
    # message, which is looked up through this email's id, and after the delete
    # there is nothing left to find it by.
    _record_verdict(db, email_id, FeedbackSignal.REJECTED, "review_dismiss")
    # And the recruiter-inbox row, for the same reason and with the same
    # deadline. It said DRAFTED, which after this line is a claim about a draft
    # that no longer exists — see `recruiter_reply_service.record_draft_discarded`.
    recruiter_reply_service.record_draft_discarded(db, email)
    # Read before the delete, and acted on after it: the campaign is reachable
    # only through this row's thread, and the count that decides completion has
    # to run with the delete already flushed.
    campaign = outreach_service.campaign_for_email(db, email)
    db.delete(email)
    # A dismissal drains the campaign's pending set exactly as a send does, and
    # this was the one drain with nothing watching it: the last draft of a batch
    # dismissed by hand left the campaign ACTIVE with nothing outstanding, which
    # the tracker polls forever and the strip offers no way out of. See
    # `outreach_service.maybe_complete_campaign`.
    outreach_service.maybe_complete_campaign(db, campaign)
    db.commit()


def _record_verdict(
    db: Session, email_id: int, signal: FeedbackSignal, source: str
) -> None:
    """Book what the user decided, without letting it break what they asked for.

    A verdict that fails to record is a missed lesson; an approval that 500s
    because of one is a reply that does not go out. The first is much cheaper.
    """
    try:
        classifier_feedback.record_for_email(db, email_id, signal, source=source)
    except Exception:  # noqa: BLE001 - the user's action matters more than the signal
        logger.warning("feedback not recorded for email %s", email_id, exc_info=True)


# Re-exported so callers can reason about which intents produce a review draft.
REVIEWABLE_INTENTS = {
    ReplyIntent.INTERESTED,
    ReplyIntent.SCHEDULING,
    ReplyIntent.QUESTION,
    ReplyIntent.OFFER,
    ReplyIntent.NOT_INTERESTED,
}
