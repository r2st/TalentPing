"""Recruiter Inbox — inbound mail we detected, and what we did about it.

    GET   /recruiter-inbox                    -> detected mail + headline counts
    GET   /recruiter-inbox/preferences        -> the two switches + scan state
    PATCH /recruiter-inbox/preferences        -> flip them
    POST  /recruiter-inbox/scan               -> scan the mailbox now
    GET   /recruiter-inbox/{id}               -> one message, full body + reply
    GET   /recruiter-inbox/{id}/intel         -> market band for the role it names
    POST  /recruiter-inbox/{id}/read          -> mark read
    POST  /recruiter-inbox/{id}/rematch       -> re-run matching, or force a profile
    POST  /recruiter-inbox/{id}/generate-reply-> write a reply for a flagged one
    POST  /recruiter-inbox/{id}/dismiss       -> stop showing it

Acting on the reply itself deliberately lives elsewhere: approving is
``POST /review/emails/{id}/approve``, discarding is the matching ``/dismiss``, and
editing is ``PATCH /tracker/emails/{id}``. The Inbox page learned this lesson
once — two places to act on one draft meant two chances to send the same thing
twice — and ``reply_email_id`` is the pointer that keeps it to one.

Ownership is enforced by filtering on ``user_id`` and answering **404** for
anything else, which is what :func:`app.routers.inbox._owned_thread` and
:func:`app.routers.review._owned_draft` do: a 403 would confirm that another
user's row exists.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, defer

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import MAX_DB_INT, Page, page_params, set_page_headers
from app.core.pii import mask_email, safe_reason, scrub
from app.core.rate_limit import rate_limit
from app.core.sql_text import SEARCH_MAX_LENGTH, search_clause
from app.models.application import Application
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.profile import Profile
from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailKind,
    RecruiterEmailStatus,
)
from app.models.recruiter_scan_run import TRIGGER_MANUAL, RecruiterScanRun
from app.models.reply_feedback import FeedbackSignal
from app.models.user import User
from app.schemas.recruiter_inbox import (
    OpportunityIntelOut,
    OpportunityOut,
    RecruiterEmailDetail,
    RecruiterEmailRow,
    RecruiterInboxCounts,
    RecruiterInboxFilter,
    RecruiterInboxOut,
    RecruiterPreferenceIn,
    RecruiterPreferenceOut,
    RecruiterScanOut,
    RecruiterStatsOut,
    RecruiterStatsTotals,
    RematchIn,
    SkipReason,
    StatsPushState,
    StatsTrendPoint,
)
from app.services import (
    classifier_feedback,
    email_attachments,
    gmail_accounts,
    gmail_push,
    opportunity,
    outreach_service,
    recruiter_reply_service,
    usage_events,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/recruiter-inbox", tags=["recruiter-inbox"])

_DRAFTED = {RecruiterEmailStatus.DRAFTED}
_REPLIED = {RecruiterEmailStatus.REPLY_QUEUED, RecruiterEmailStatus.REPLIED}


def _owned(db: Session, user: User, email_id: int) -> RecruiterEmail:
    row = db.scalar(
        select(RecruiterEmail).where(
            RecruiterEmail.id == email_id, RecruiterEmail.user_id == user.id
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Message not found"
        )
    return row


def _profile_names(db: Session, user: User) -> dict[int, str]:
    return {
        p.id: p.name
        for p in db.scalars(select(Profile).where(Profile.user_id == user.id))
    }


def _row(email: RecruiterEmail, profile_names: dict[int, str]) -> RecruiterEmailRow:
    return RecruiterEmailRow(
        id=email.id,
        from_address=email.from_address,
        from_name=email.from_name,
        subject=email.subject,
        snippet=email.snippet,
        received_at=email.received_at,
        kind=email.kind,
        classification_confidence=email.classification_confidence,
        classified_by=email.classified_by,
        route=email.route,
        route_confidence=email.route_confidence,
        status=email.status,
        matched_profile_id=email.matched_profile_id,
        matched_profile_name=profile_names.get(email.matched_profile_id or -1),
        match_score=email.match_score,
        match_reason=email.match_reason,
        flag_reason=email.flag_reason,
        extracted=email.extracted or {},
        opportunity=OpportunityOut(**opportunity.intel(email.extracted).as_dict()),
        reply_email_id=email.reply_email_id,
        application_id=email.application_id,
        read_at=email.read_at,
        created_at=email.created_at,
        escalated=email.escalated,
        escalation_reason=email.escalation_reason,
        follow_up_count=email.follow_up_count,
        last_follow_up_at=email.last_follow_up_at,
        confidence_adjustment=email.confidence_adjustment,
        resume_choice_reason=email.resume_choice_reason,
    )


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; every comparison here wants UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _visible(user: User):
    """The rows this inbox is about: everything detected but not dismissed."""
    return (
        RecruiterEmail.user_id == user.id,
        RecruiterEmail.status != RecruiterEmailStatus.IGNORED,
    )


def _needs_you_clause():
    """:attr:`RecruiterEmail.needs_attention` as SQL.

    The Python property stays the definition — an *escalated* row still reads
    ``REPLIED``, which is the truth about what happened and not about what to do
    next, and that is why escalation is a separate flag. This has to agree with
    it, so ``tests/test_recruiter_inbox_api.py`` checks the two against every
    status rather than trusting them to be edited together.
    """
    return or_(
        RecruiterEmail.escalated.is_(True),
        RecruiterEmail.status.in_(
            (RecruiterEmailStatus.FLAGGED, RecruiterEmailStatus.FAILED)
        ),
    )


def _inbox_counts(db: Session, user: User) -> RecruiterInboxCounts:
    """The headline numbers, over everything, in two aggregate queries.

    These are computed *before* filters by contract — the chips say what they
    would reveal, not what survived the current tab — which used to mean loading
    every row the account had ever detected in order to count them in Python.
    That is the one thing this endpoint must not do: detection is driven by how
    much mail arrives rather than by anything the user does, so the cost grew
    with the age of the account and was paid in the API process on every poll.
    """
    totals = db.execute(
        select(
            func.count(),
            func.count().filter(RecruiterEmail.read_at.is_(None)),
            func.count().filter(_needs_you_clause()),
            func.count().filter(RecruiterEmail.status.in_(tuple(_DRAFTED))),
            func.count().filter(RecruiterEmail.status.in_(tuple(_REPLIED))),
            func.count().filter(
                RecruiterEmail.status == RecruiterEmailStatus.CLASSIFIED
            ),
            func.count().filter(RecruiterEmail.escalated.is_(True)),
        ).where(*_visible(user))
    ).one()
    by_kind = db.execute(
        select(RecruiterEmail.kind, func.count())
        .where(*_visible(user))
        .group_by(RecruiterEmail.kind)
    ).all()
    return RecruiterInboxCounts(
        detected=totals[0],
        unread=totals[1],
        needs_you=totals[2],
        drafted=totals[3],
        replied=totals[4],
        not_recruiter=totals[5],
        escalated=totals[6],
        by_kind={kind.value: count for kind, count in by_kind},
    )


@router.get("", response_model=RecruiterInboxOut)
def recruiter_inbox(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    view: RecruiterInboxFilter = Query(default=RecruiterInboxFilter.ALL),
    kind: RecruiterEmailKind | None = Query(default=None),
    profile_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    unread: bool = Query(default=False),
    q: str | None = Query(
        default=None,
        max_length=SEARCH_MAX_LENGTH,
        description="Match sender, subject or snippet",
    ),
    page: Page = Depends(page_params),
) -> RecruiterInboxOut:
    """Inbound mail detected for this user, newest first — one page of it.

    Filtering, sorting and counting all happen in the database. They used to
    happen in Python over every row the account had, which made the response
    time and the process's memory a function of how long the mailbox had been
    watched. ``counts.detected`` is the total regardless of the page, so a
    client that does not page can still tell it is looking at a slice.
    """
    stmt = select(RecruiterEmail).where(*_visible(user))

    if view is RecruiterInboxFilter.NEEDS_YOU:
        stmt = stmt.where(_needs_you_clause())
    elif view is RecruiterInboxFilter.DRAFTED:
        stmt = stmt.where(RecruiterEmail.status.in_(tuple(_DRAFTED)))
    elif view is RecruiterInboxFilter.REPLIED:
        stmt = stmt.where(RecruiterEmail.status.in_(tuple(_REPLIED)))
    elif view is RecruiterInboxFilter.NOT_RECRUITER:
        stmt = stmt.where(RecruiterEmail.status == RecruiterEmailStatus.CLASSIFIED)

    if kind is not None:
        stmt = stmt.where(RecruiterEmail.kind == kind)
    if profile_id is not None:
        stmt = stmt.where(RecruiterEmail.matched_profile_id == profile_id)
    if unread:
        stmt = stmt.where(RecruiterEmail.read_at.is_(None))
    if (clause := search_clause(
        q,
        RecruiterEmail.from_address,
        RecruiterEmail.from_name,
        RecruiterEmail.subject,
        RecruiterEmail.snippet,
    )) is not None:
        stmt = stmt.where(clause)

    # ``received_at`` is when the recruiter sent it and ``created_at`` is when we
    # noticed; the first is the truth and the second is the fallback, which is
    # what the old Python sort did by sinking rows with no timestamp. The id
    # breaks ties, without which two pages of a same-second batch can repeat a
    # row and drop another.
    ordered = stmt.order_by(
        func.coalesce(RecruiterEmail.received_at, RecruiterEmail.created_at).desc(),
        RecruiterEmail.id.desc(),
    )
    emails = list(
        db.scalars(
            # A row on this list is a header, not a message: `_row` reads the
            # sender, the subject, the snippet and the verdicts, and never the
            # body. These four are the whole of what it does not read, and they
            # are also the four large ones — `body_text` is the entire recruiter
            # email and `attachments` its manifest. Deferred rather than
            # selected-and-discarded, which is what a page of 500 was doing.
            ordered.options(
                defer(RecruiterEmail.body_text),
                defer(RecruiterEmail.rfc_references),
                defer(RecruiterEmail.attachments),
                defer(RecruiterEmail.last_error),
            )
            .limit(page.limit)
            .offset(page.offset)
        ).all()
    )

    if page.offset == 0 and len(emails) < page.limit:
        # A short first page *is* the whole match — that is what "short" means —
        # so the common case of an inbox that fits costs no extra query.
        total = len(emails)
    else:
        total = (
            db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        )

    counts = _inbox_counts(db, user)
    names = _profile_names(db, user)
    pref = recruiter_reply_service.preference_for(db, user)
    # Built before the commit, not after it. ``preference_for`` may have created
    # the row, so the commit has to happen — but a commit expires every object in
    # the session, and every ``_row`` call below used to run against rows that
    # had just been expired. Each one touched an attribute, SQLAlchemy silently
    # re-SELECTed the whole record to satisfy it, and the page paid one full-row
    # query per message — ``body_text`` and all — for data it had already read.
    # A 30-row page cost 39 queries and a 500-row one cost 509.
    #
    # Committing *after* the response is assembled rather than before it also
    # leaves `user` unexpired, so the request's own auth objects are not
    # re-loaded either.
    set_page_headers(
        response, total=total, returned=len(emails), offset=page.offset
    )
    out = RecruiterInboxOut(
        counts=counts,
        emails=[_row(e, names) for e in emails],
        total=total,
        enabled=pref.enabled,
        auto_reply_enabled=pref.auto_reply_enabled,
        server_enabled=settings.recruiter_reply_enabled,
        last_scan_at=pref.last_scan_at,
    )
    db.commit()
    return out


@router.get("/preferences", response_model=RecruiterPreferenceOut)
def get_preferences(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> RecruiterPreferenceOut:
    """The two switches, plus what the deployment allows."""
    pref = recruiter_reply_service.preference_for(db, user)
    db.commit()
    return RecruiterPreferenceOut(
        enabled=pref.enabled,
        auto_reply_enabled=pref.auto_reply_enabled,
        server_enabled=settings.recruiter_reply_enabled,
        server_auto_enabled=settings.recruiter_reply_auto_enabled,
        last_scan_at=pref.last_scan_at,
        detected_count=pref.detected_count,
        replied_count=pref.replied_count,
    )


@router.patch("/preferences", response_model=RecruiterPreferenceOut)
def update_preferences(
    payload: RecruiterPreferenceIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RecruiterPreferenceOut:
    """Turn inbox watching — and, separately, unreviewed replies — on or off.

    Switching watching off also switches automatic replies off. Leaving the
    second switch armed under a disabled feature is a trap: turning watching back
    on months later would silently resume sending.
    """
    pref = recruiter_reply_service.preference_for(db, user)
    if payload.enabled is not None:
        pref.enabled = payload.enabled
        if not payload.enabled:
            pref.auto_reply_enabled = False
    if payload.auto_reply_enabled is not None:
        pref.auto_reply_enabled = payload.auto_reply_enabled and pref.enabled
    # One event name for every settings surface in the product, with `scope`
    # saying which screen. A name per screen would make "how many people change
    # settings at all" a sum somebody has to remember to compute, and would grow
    # the vocabulary by one every time a preferences form is added — which is
    # the cost that stops anyone instrumenting the next one.
    usage_events.record(
        db,
        "settings.changed",
        user_id=user.id,
        scope="recruiter_inbox",
        enabled=pref.enabled,
        auto_reply=pref.auto_reply_enabled,
    )
    db.commit()
    db.refresh(pref)

    return RecruiterPreferenceOut(
        enabled=pref.enabled,
        auto_reply_enabled=pref.auto_reply_enabled,
        server_enabled=settings.recruiter_reply_enabled,
        server_auto_enabled=settings.recruiter_reply_auto_enabled,
        last_scan_at=pref.last_scan_at,
        detected_count=pref.detected_count,
        replied_count=pref.replied_count,
    )


@router.post(
    "/scan",
    response_model=RecruiterScanOut,
    # Reads the mailbox and classifies what it finds — an LLM call per candidate
    # message. Without a worker it does all of that inline, which is the case
    # this limit is really for.
    dependencies=[Depends(rate_limit(5, 300, scope="recruiter-scan"))],
)
def scan_now(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> RecruiterScanOut:
    """Read the user's mailboxes right now.

    With a worker this dispatches and returns; without one (local dev, a
    single-box install) the scan runs inline so the button still does something —
    the same arrangement ``POST /inbox/sync`` uses.

    **Every connected mailbox, not just the primary.** The beat sweep has fanned
    out over ``live_accounts`` since multi-mailbox landed, so scanning only the
    primary here made the button quietly weaker than the schedule it exists to
    pre-empt: a recruiter who wrote to the second address was found eventually by
    beat, but never by the user pressing "Scan now" and watching nothing appear.

    The counters returned are the sum across mailboxes, and ``query`` names the
    search that was run — it is the same query for each, so summing the numbers
    while reporting the query once loses nothing.
    """
    if not settings.recruiter_reply_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recruiter inbox scanning is not enabled on this server",
        )
    accounts = gmail_accounts.live_accounts(user)
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect a Gmail account first",
        )

    from app.tasks.recruiter_reply_tasks import process_recruiter_email, scan_mailbox

    pending = list(accounts)
    if settings.celery_enabled:
        # A broker that dies partway through the fan-out leaves some mailboxes
        # queued and some not. Dropping the whole list to the inline path would
        # scan the queued ones twice — once here and once when the worker picks
        # them up — so only what did not reach the broker falls through.
        for account in accounts:
            try:
                # `force`: the user pressed the button, so the debounce that keeps
                # beat and push from stacking on each other doesn't apply to them.
                scan_mailbox.apply_async(
                    args=[user.id, account.id], kwargs={"force": True}
                )
                pending.remove(account)
            except Exception as exc:  # noqa: BLE001 - broker down; fall through to inline
                logger.warning(
                    "recruiter scan dispatch failed for %s: %s",
                    mask_email(account.email),
                    exc,
                )
        if not pending:
            return RecruiterScanOut(
                dispatched=True, mailboxes_scanned=len(accounts)
            )

    out = RecruiterScanOut(dispatched=False, mailboxes_scanned=len(accounts))
    errors: list[str] = []
    for account in pending:
        # One mailbox per iteration, and one mailbox's failure stays there.
        # ``inbound_scanner.scan`` only converts ``GmailNotConfigured`` into a
        # reported error; anything else — an API outage, a revoked grant Google
        # has not told us about yet — propagates. With one mailbox that was a
        # 500 and nothing was lost. With several it would throw away the scans
        # that had already succeeded, so the one broken address would make the
        # button appear broken for every address.
        try:
            result, created = recruiter_reply_service.record_scan(
                db, user, account, trigger=TRIGGER_MANUAL
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 - one mailbox must not sink the rest
            logger.warning(
                "recruiter scan failed for %s: %s",
                mask_email(account.email),
                exc,
                exc_info=True,
            )
            db.rollback()
            # The *type*, never ``str(exc)``. This string is returned to the
            # browser, and the exception caught here is unrestricted: a driver
            # error stringifies to the failing statement plus its bound
            # parameters, which on this schema is a recruiter's address and a
            # message body. The mailbox is named — with several connected,
            # "a scan failed" identifies nothing — but masked, because the
            # sentence is also going into a support ticket.
            errors.append(f"{mask_email(account.email)}: {safe_reason(exc)}")
            continue

        for row in created:
            try:
                process_recruiter_email.run(row.id)
            except Exception as exc:  # noqa: BLE001 - one bad message must not 500 the page
                logger.warning(
                    "inline processing failed for recruiter email %s: %s",
                    row.id,
                    exc,
                    exc_info=True,
                )

        out.detected += len(created)
        out.listed += result.listed
        out.examined += result.examined
        out.deferred += result.deferred
        out.skipped_known += result.skipped_known
        out.skipped_own_thread += result.skipped_own_thread
        out.skipped_from_self += result.skipped_from_self
        out.skipped_bounce += result.skipped_bounce
        out.skipped_opt_out += result.skipped_opt_out
        out.skipped_auto_reply += result.skipped_auto_reply
        out.query = result.query or out.query
        # One unreadable mailbox must not present as a clean scan of the rest —
        # but nor should it hide what the others found. Name the mailbox, since
        # with several connected "the scan failed" no longer identifies which.
        if result.error:
            # ``result.error`` is a sentence the scanner wrote on purpose, so
            # it travels as-is — but it quotes what it failed on, and what a
            # mail scan fails on is an address.
            errors.append(f"{mask_email(account.email)}: {scrub(result.error)}")

    out.error = "; ".join(errors) if errors else None
    return out


# --------------------------------------------------------------------------- #
# Stats                                                                        #
# --------------------------------------------------------------------------- #

#: Each skip counter with the words the UI puts next to it. Ordered by how often
#: the reason actually fires, so the list reads sensibly before it is sorted.
_SKIP_LABELS: tuple[tuple[str, str], ...] = (
    ("skipped_known", "Already checked"),
    ("skipped_own_thread", "Conversations you started"),
    ("skipped_from_self", "Sent by you"),
    ("skipped_bounce", "Delivery failures"),
    ("skipped_opt_out", "Asked not to be contacted"),
    ("skipped_auto_reply", "Out-of-office replies"),
    ("skipped_unfetchable", "Couldn't be read"),
)


def _bucket_start(when: datetime, bucket: str) -> date:
    """The day (or the Monday of the week) a timestamp belongs to."""
    day = when.date()
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    return day


def _bucket_label(period: date, bucket: str) -> str:
    return f"w/c {period:%d %b}" if bucket == "week" else f"{period:%d %b}"


@router.get("/stats", response_model=RecruiterStatsOut)
def stats(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    days: int = Query(default=30, ge=1, le=365),
    bucket: str = Query(default="day", pattern="^(day|week)$"),
) -> RecruiterStatsOut:
    """How much mail was scanned, what came of it, and how that moved over time.

    **Two denominators, deliberately not merged.** Scan counters come from
    ``recruiter_scan_runs`` and outcome counters from ``recruiter_emails``, over
    different time axes: a message detected on the 20th and replied to on the
    21st is one detection on the 20th and one reply on the 21st. Presenting them
    as a single funnel would imply a conversion rate that does not exist — the
    same reason ``routers/analytics`` keeps engagement separate from
    applications.

    Every trend point is keyed on the event's own timestamp rather than on when
    a scan happened to notice it, which is the rule ``received_at`` follows
    everywhere else in this feature.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    runs = list(
        db.scalars(
            select(RecruiterScanRun).where(
                RecruiterScanRun.user_id == user.id,
                RecruiterScanRun.created_at >= since,
            )
        ).all()
    )
    emails = list(
        db.scalars(
            select(RecruiterEmail).where(
                RecruiterEmail.user_id == user.id,
                RecruiterEmail.created_at >= since,
            )
        ).all()
    )

    totals = RecruiterStatsTotals(
        scans=len(runs),
        listed=sum(r.listed for r in runs),
        examined=sum(r.examined for r in runs),
        detected=len(emails),
        classified_recruiter=sum(1 for e in emails if e.is_actionable),
        replied=sum(1 for e in emails if e.status in _REPLIED),
        # Only the AUTO band ever went out unread. A DRAFT the user approved is
        # a reply *they* sent, and counting it here would overstate by exactly
        # the number the user cares most about being honest.
        auto_sent=sum(
            1
            for e in emails
            if e.route is not None and e.route.value == "AUTO" and e.status in _REPLIED
        ),
        drafts_pending=sum(1 for e in emails if e.status in _DRAFTED),
        flagged=sum(1 for e in emails if e.status is RecruiterEmailStatus.FLAGGED),
        escalated=sum(1 for e in emails if e.escalated),
        dismissed=sum(1 for e in emails if e.status is RecruiterEmailStatus.IGNORED),
        failed=sum(1 for e in emails if e.status is RecruiterEmailStatus.FAILED),
        deferred=sum(r.deferred for r in runs),
    )

    skipped = [
        SkipReason(reason=field, label=label, count=total)
        for field, label in _SKIP_LABELS
        if (total := sum(getattr(r, field) for r in runs))
    ]
    skipped.sort(key=lambda s: s.count, reverse=True)

    # One point per bucket that has *something* in it, from either source. A
    # bucket present in only one of the two still shows, with zeros on the other
    # side — a day with 400 scans and no detections is a real and useful shape.
    scanned_by: dict[date, int] = {}
    for run in runs:
        when = _aware(run.created_at)
        if when is None:  # pragma: no cover - created_at is never null
            continue
        period = _bucket_start(when, bucket)
        scanned_by[period] = scanned_by.get(period, 0) + run.listed

    points: dict[date, dict[str, int]] = {
        period: {
            "scanned": count,
            "detected": 0,
            "recruiter": 0,
            "replied": 0,
            "auto_sent": 0,
            "flagged": 0,
        }
        for period, count in scanned_by.items()
    }
    for email in emails:
        when = _aware(email.received_at) or _aware(email.created_at)
        if when is None:  # pragma: no cover - one of the two always exists
            continue
        period = _bucket_start(when, bucket)
        point = points.setdefault(
            period,
            {
                "scanned": 0,
                "detected": 0,
                "recruiter": 0,
                "replied": 0,
                "auto_sent": 0,
                "flagged": 0,
            },
        )
        point["detected"] += 1
        if email.is_actionable:
            point["recruiter"] += 1
        if email.status in _REPLIED:
            point["replied"] += 1
            if email.route is not None and email.route.value == "AUTO":
                point["auto_sent"] += 1
        if email.status is RecruiterEmailStatus.FLAGGED:
            point["flagged"] += 1

    trend = [
        StatsTrendPoint(
            period=period, label=_bucket_label(period, bucket), **values
        )
        for period, values in sorted(points.items())
    ]

    # Push is one subscription per mailbox, so this panel is a claim about all of
    # them. Reporting the primary's watch as if it were the user's said "push is
    # healthy" while a second mailbox had no subscription at all — the reassuring
    # half of a split state, and the half that isn't the problem.
    #
    # ``healthy``/``covering`` therefore mean *every* connected mailbox, which is
    # the only reading under which "push is working" implies the beat sweep can
    # stand down. The counts below say how many, so a partial state reads as
    # partial rather than as a flat failure.
    accounts = gmail_accounts.live_accounts(user)
    watches = [a.watch for a in accounts]
    pushing = [w for w in watches if gmail_push.push_is_healthy(w)]
    notified = [w.last_notified_at for w in watches if w is not None and w.last_notified_at]
    push = StatsPushState(
        configured=gmail_push.is_configured(),
        # "Registered" and "delivering" are different claims and the UI should
        # be able to say which one is failing — the whole point of push_covers.
        healthy=bool(accounts) and all(gmail_push.push_is_healthy(w) for w in watches),
        covering=bool(accounts) and all(gmail_push.push_covers(w) for w in watches),
        notifications=sum(
            w.notifications_received for w in watches if w is not None
        ),
        last_notified_at=max(notified) if notified else None,
        mailboxes_total=len(accounts),
        mailboxes_pushing=len(pushing),
        scans_from_push=sum(1 for r in runs if r.trigger == "push"),
        scans_from_beat=sum(1 for r in runs if r.trigger == "beat"),
        scans_manual=sum(1 for r in runs if r.trigger == "manual"),
    )

    return RecruiterStatsOut(
        days=days,
        bucket=bucket,
        totals=totals,
        skipped=skipped,
        trend=trend,
        push=push,
    )


def _inbound_message_for(db: Session, email: RecruiterEmail) -> Email | None:
    """The conversation's copy of this detected message, once it has one.

    A detected row and the ``Email`` row are the same message seen twice: the
    scan writes the first, and threading writes the second when the pipeline
    decides to reply. Only the second can serve attachment bytes — it holds the
    thread the ownership check runs through — so a message still sitting in
    "detected" has names and no way to open them.

    Matched on the Gmail id and scoped to this row's owner. The id is unique
    within a mailbox, and the scope makes that guarantee ours rather than
    Gmail's.
    """
    if not email.gmail_message_id:
        return None
    return db.scalar(
        select(Email)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Email.gmail_message_id == email.gmail_message_id,
            Email.direction == EmailDirection.RECEIVED,
            Application.user_id == email.user_id,
        )
    )


def _recruiter_attachment_names(
    email: RecruiterEmail, inbound: Email | None
) -> list[str]:
    """What this message arrived carrying, by name.

    Prefers the conversation's row, because that is the one the preview
    endpoint resolves against and the two must agree on order — a badge at
    position 1 has to open the file the server calls position 1. Falls back to
    the scan's own record so a message that was never threaded still says what
    it carries.
    """
    if inbound is not None:
        return email_attachments.inbound_filenames_for(inbound)
    return email_attachments.scanned_filenames_for(email.attachments)


def _detail(db: Session, email: RecruiterEmail, names: dict[int, str]) -> RecruiterEmailDetail:
    reply = db.get(Email, email.reply_email_id) if email.reply_email_id else None
    # What the reply will carry. A sent reply already knows what it carried, and
    # re-resolving would answer with today's resume rather than the one that
    # actually went — so the record wins over the plan.
    plan = email_attachments.AttachmentPlan()
    if reply is not None:
        if reply.status is EmailStatus.SENT:
            # The recorded resume, plus whatever the user attached by hand —
            # those rows survive the send and are the only record that they
            # travelled at all.
            sent = [
                email_attachments.PlannedFile(
                    filename=reply.attachment_filename,
                    kind=email_attachments.RESUME,
                )
            ] if reply.attachment_filename else []
            sent.extend(
                email_attachments.PlannedFile(
                    filename=row.filename,
                    kind=email_attachments.UPLOAD,
                    attachment_id=row.id,
                )
                for row in email_attachments.user_attachments_for_email(db, reply)
            )
            plan = email_attachments.AttachmentPlan(files=sent)
        else:
            plan = email_attachments.plan_for_email(db, reply)

    inbound = _inbound_message_for(db, email)

    return RecruiterEmailDetail(
        **_row(email, names).model_dump(),
        attachments=_recruiter_attachment_names(email, inbound),
        attachment_email_id=inbound.id if inbound is not None else None,
        body_text=email.body_text,
        reply_subject=reply.subject if reply else None,
        reply_body=reply.body_text if reply else None,
        reply_status=reply.status.value if reply else None,
        reply_note=reply.draft_note if reply else None,
        thread_id=reply.thread_id if reply else None,
        reply_to_address=email.reply_to_address,
        reply_attachments=plan.filenames,
        reply_attachment_note=plan.reason,
    )


@router.get("/{email_id}", response_model=RecruiterEmailDetail)
def detail(
    email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RecruiterEmailDetail:
    """One detected message: the full body, the verdict, and the reply."""
    email = _owned(db, user, email_id)
    return _detail(db, email, _profile_names(db, user))


@router.get(
    "/{email_id}/intel",
    response_model=OpportunityIntelOut,
    dependencies=[Depends(rate_limit(30, 60, scope="recruiter-intel"))],
)
def intel(
    email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OpportunityIntelOut:
    """What the market pays for the role this message names.

    Deliberately not folded into ``GET /{id}`` even though it describes the same
    row. Modelling a band goes through
    :func:`app.services.salary_service.get_benchmark`, which reads a globally
    shared cache row and writes one when the bucket is new or stale — a write on
    a path the detail endpoint is called from three other ways, including after
    every rematch. Paying for it once, when the user opens the panel, is the
    same bargain ``GET /jobs/{id}/intel`` strikes for the job feed.

    Answers 200 with ``salary: null`` rather than 404 when there is no title to
    model from: the message exists and the honest answer about it is "we can't
    say", which is a body the panel can render and not an error.
    """
    email = _owned(db, user, email_id)
    context = opportunity.market_context(db, user, email)
    if context is None:
        return OpportunityIntelOut(email_id=email.id)

    data = context.as_dict()
    return OpportunityIntelOut(
        email_id=email.id,
        salary={
            "band": data["band"],
            "comparison": data["comparison"],
            "is_estimate": data["is_estimate"],
        },
        floor=data["floor"],
        floor_label=data["floor_label"],
        clears_floor=data["clears_floor"],
    )


@router.post("/{email_id}/read", response_model=RecruiterEmailRow)
def mark_read(
    email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RecruiterEmailRow:
    email = _owned(db, user, email_id)
    if email.read_at is None:
        email.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(email)
    return _row(email, _profile_names(db, user))


@router.post("/{email_id}/rematch", response_model=RecruiterEmailDetail)
def rematch(
    email_id: int,
    payload: RematchIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RecruiterEmailDetail:
    """Re-run profile matching, optionally forcing the profile the user picked.

    A forced profile must belong to the caller. An id that doesn't resolve for
    *this* user is a 404 rather than a 403, for the same reason as everywhere
    else here.
    """
    email = _owned(db, user, email_id)
    profile_id = payload.profile_id if payload else None
    if profile_id is not None:
        owned = db.scalar(
            select(Profile).where(Profile.id == profile_id, Profile.user_id == user.id)
        )
        if owned is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found"
            )

    try:
        recruiter_reply_service.rematch(db, email, profile_id=profile_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    db.commit()
    db.refresh(email)
    return _detail(db, email, _profile_names(db, user))


@router.post(
    "/{email_id}/generate-reply",
    response_model=RecruiterEmailDetail,
    # One LLM call per press, and the 409s above are raised *after* the
    # dependency runs, so a caller cannot spend the budget cheaply on messages
    # that were never going to be drafted.
    dependencies=[Depends(rate_limit(10, 60, scope="recruiter-reply-draft"))],
)
def generate_reply(
    email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RecruiterEmailDetail:
    """Write a reply for a message the product flagged rather than answered.

    Always produces a draft for review, whatever the confidence was: an explicit
    "write me one" is a request for something to read, never for something
    already gone.
    """
    email = _owned(db, user, email_id)
    if email.reply_email_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A reply already exists for this message",
        )
    if not email.is_actionable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This message wasn't from a recruiter",
        )

    try:
        recruiter_reply_service.generate_reply(db, email)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    db.commit()
    db.refresh(email)
    return _detail(db, email, _profile_names(db, user))


@router.post("/{email_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss(
    email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Stop showing a message. Any reply that has not left is stopped with it.

    Dismissing is also the clearest possible statement that this was not worth
    answering, so it is recorded — the next message from the same sender is read
    a little more sceptically. See services/classifier_feedback for the bounds on
    what "a little" can ever mean.

    Which is exactly why an *auto-reply still on the broker* has to be stopped
    too. This used to discard only a ``DRAFT``, on the reasoning that "a reply
    already gone is history" — true, but ``QUEUED`` is neither of those. It is a
    reply this pipeline decided to send without review, handed to the throttled
    sender with a countdown that lands in the recipient's business hours and
    routinely spans a weekend. Dismissing during that window recorded
    ``REJECTED`` — "not worth answering" — and then answered them anyway, hours
    later, in the candidate's name.

    That is the same gap the send task already closes for the recipient's
    opt-out and for an excluded contact: a decision taken while a batch trickles
    has to stop the rest of the batch, or it only governs mail that had not been
    composed yet. This is the third such decision and the user's most explicit.
    """
    email = _owned(db, user, email_id)
    if email.reply_email_id is not None:
        reply = db.get(Email, email.reply_email_id)
        # Whichever branch below retires this row, it stops being outstanding —
        # so the campaign holding it may now have nothing left to send. See
        # `outreach_service.maybe_complete_campaign`.
        campaign = (
            outreach_service.campaign_for_email(db, reply)
            if reply is not None
            else None
        )
        if reply is not None and reply.status is EmailStatus.DRAFT:
            db.delete(reply)
            email.reply_email_id = None
            outreach_service.maybe_complete_campaign(db, campaign)
        elif reply is not None and reply.status is EmailStatus.QUEUED:
            # Written off rather than deleted, and written off rather than put
            # back to DRAFT. A worker may be holding this row under
            # ``_claim_for_send``'s ``FOR UPDATE`` right now, and ``FAILED`` is a
            # state that task already re-reads and obeys — its first act after
            # claiming is to drop anything that is no longer QUEUED. ``DRAFT``
            # would be worse than doing nothing: it would hand a message the user
            # has just dismissed back to the review queue. The row survives as
            # the record of a send that was stopped, the way every other terminal
            # refusal in ``email_tasks._fail_send`` does, and carries no reason
            # for the same reason that one doesn't — the recruiter row beside it
            # says IGNORED, which is the whole explanation.
            reply.status = EmailStatus.FAILED
            outreach_service.maybe_complete_campaign(db, campaign)
    email.status = RecruiterEmailStatus.IGNORED
    try:
        classifier_feedback.record(
            db, email, FeedbackSignal.REJECTED, source="inbox_dismiss"
        )
    except Exception:  # noqa: BLE001 - dismissing must work whatever this does
        logger.warning("feedback not recorded for recruiter email %s", email_id, exc_info=True)
    db.commit()
