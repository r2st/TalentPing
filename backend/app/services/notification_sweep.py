"""Deriving notifications from the state the database is already in.

Almost nothing here is an *event*. There is no moment at which a grant becomes
"expiring", no callback when a draft has been waiting two days, and no row
written when a follow-up quietly fails to go out. These are conditions, and a
condition has no edge to hang a handler on — which is exactly why they were the
things the product never told anyone about.

So the sweep re-derives them from scratch on every tick and leans on
``dedupe_key`` to decide what is news. That trade is deliberate:

* Emitters stay stateless. Nothing has to remember what it said last hour, and
  a worker restart loses nothing.
* A condition that resolves and returns says itself again, because its key moves
  with it — a new grant has a new expiry date, a new oldest draft has a new id.
* A condition that persists says itself once. The hundred and sixty-seventh
  hour of "your grant expires Thursday" is not more informative than the first.

**Two guards keep the first run of this on an old account from being a wall.**
Entity-scoped notices only look at rows from the last :data:`NEW_WINDOW_DAYS`,
so deploying this to an account with a year of history announces a day of it,
not a year. And :data:`MAX_PER_KIND` caps how many of any one kind a single
sweep will write, so a burst of forty recruiter replies is ten notifications and
a count, not forty rows nobody reads.

Aggregate notices (drafts, follow-ups) are keyed on the *oldest* row in the
condition rather than on a count. A count-keyed notification re-fires on every
change — four drafts, then five, then four again — which is three notifications
about one situation. The oldest row only changes when the user actually deals
with it, which is precisely when saying something again is useful.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.application import Application, ApplicationStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.follow_up import FollowUp, FollowUpStatus
from app.models.notification import NotificationKind, NotificationSeverity
from app.models.recruiter import Recruiter
from app.models.recruiter_email import (
    ACTIONABLE_KINDS,
    RecruiterEmail,
    RecruiterEmailStatus,
)
from app.models.user import User
from app.services import follow_up_suggestions, gmail_accounts, notifications

logger = logging.getLogger(__name__)

#: How far back an entity-scoped notice will look for rows it has never seen.
#: One day, matched to nothing in particular except that it is comfortably wider
#: than the sweep interval — so a tick that is late, or a worker that was down
#: for an afternoon, still catches everything it missed.
NEW_WINDOW_DAYS = 1

#: Most rows of any one kind a single sweep will write for one user.
MAX_PER_KIND = 10

#: How long a draft has to sit in the review queue before it is worth
#: interrupting someone about. Under a day is not a backlog, it is this morning.
DRAFT_WAIT_HOURS = 24

#: How far past its scheduled time a follow-up has to be before "due" becomes
#: "stuck". Generous on purpose: sends defer into the recipient's business
#: hours, so a step scheduled Friday evening legitimately goes out Monday and a
#: tighter window would cry wolf every weekend.
FOLLOW_UP_OVERDUE_HOURS = 48

#: Statuses that mean an inbound recruiter message is still the user's problem.
_OPEN_RECRUITER_STATUSES = (
    RecruiterEmailStatus.DETECTED,
    RecruiterEmailStatus.CLASSIFIED,
    RecruiterEmailStatus.FLAGGED,
    RecruiterEmailStatus.DRAFTED,
)


@dataclass
class SweepResult:
    """What one user's sweep produced, by kind. Returned for the task's tally."""

    emitted: dict[str, int] = field(default_factory=dict)

    def add(self, kind: NotificationKind, n: int = 1) -> None:
        if n:
            self.emitted[kind.value] = self.emitted.get(kind.value, 0) + n

    @property
    def total(self) -> int:
        return sum(self.emitted.values())


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; every comparison here needs tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


# --------------------------------------------------------------------------
# The individual conditions. Each returns how many notifications it wrote.
# --------------------------------------------------------------------------


def _recruiter_replies(db: Session, user: User, now: datetime) -> dict:
    """Inbound recruiter mail that arrived recently and is still unread.

    Read state, not reply state: a message the user has opened has been seen,
    whatever they then did about it. A message the *agent* auto-replied to is
    still worth naming — the user asked for a conversation to be had on their
    behalf, not for it to be hidden from them — but it is no longer open, so it
    falls outside the statuses below.
    """
    since = now - timedelta(days=NEW_WINDOW_DAYS)
    rows = list(
        db.scalars(
            select(RecruiterEmail)
            .where(
                RecruiterEmail.user_id == user.id,
                RecruiterEmail.kind.in_(ACTIONABLE_KINDS),
                RecruiterEmail.status.in_(_OPEN_RECRUITER_STATUSES),
                RecruiterEmail.read_at.is_(None),
                RecruiterEmail.created_at >= since,
            )
            .order_by(RecruiterEmail.created_at.desc())
            .limit(MAX_PER_KIND)
        )
    )

    written = 0
    for row in rows:
        who = row.from_name or row.from_address or "A recruiter"
        subject = (row.subject or "").strip()
        emitted = notifications.emit(
            db,
            user.id,
            kind=NotificationKind.RECRUITER_REPLY,
            title=f"{who} wrote to you",
            body=subject or None,
            link="/inbox",
            dedupe_key=f"recruiter_reply:{row.id}",
            meta={"recruiter_email_id": row.id},
        )
        written += 1 if emitted is not None else 0
    return {NotificationKind.RECRUITER_REPLY: written}


def _drafts_waiting(db: Session, user: User, now: datetime) -> dict:
    """Mail the agent wrote that nobody has approved.

    Keyed on the oldest draft, so this says itself again only once that draft
    has been dealt with — which is the only moment a second reminder is a
    different sentence rather than the same one.
    """
    cutoff = now - timedelta(hours=DRAFT_WAIT_HOURS)
    # Two columns and a tally is all this notice says, and it used to read every
    # draft in the account to get them — `.all()` on an unfiltered, unlimited
    # select, then `rows[0]` and `len(rows)`. Prod carries a hundred and
    # sixty-two of these, the sweep runs per user on a schedule, and the number
    # only ever goes up: a draft that is never approved is never deleted either.
    # Split into the two bounded queries the answer actually needs.
    waiting = (
        select(Email.id, Email.created_at)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == user.id,
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.DRAFT,
        )
    )
    oldest = db.execute(waiting.order_by(Email.id).limit(1)).first()
    if oldest is None:
        return {NotificationKind.DRAFT_WAITING: 0}

    oldest_id, oldest_created = oldest
    created = _aware(oldest_created)
    if created is None or created > cutoff:
        return {NotificationKind.DRAFT_WAITING: 0}

    waiting_days = max(1, (now - created).days)
    # Counted only once the cutoff is cleared, so an account whose drafts are all
    # fresh pays for one row rather than for a tally it will not use.
    count = db.scalar(select(func.count()).select_from(waiting.subquery())) or 0
    emitted = notifications.emit(
        db,
        user.id,
        kind=NotificationKind.DRAFT_WAITING,
        title=(
            f"{count} {_plural(count, 'draft is', 'drafts are')} waiting for approval"
        ),
        body=(
            f"The oldest has been waiting {waiting_days} "
            f"{_plural(waiting_days, 'day', 'days')}. Nothing sends until you approve it."
        ),
        link="/inbox",
        dedupe_key=f"draft_waiting:{oldest_id}",
        meta={"count": count, "oldest_email_id": oldest_id},
    )
    return {NotificationKind.DRAFT_WAITING: 1 if emitted is not None else 0}


def _follow_ups_overdue(db: Session, user: User, now: datetime) -> dict:
    """Scheduled follow-ups whose time came and went.

    A warning rather than a notice. Sends defer into business hours, so a step
    a day late is the system working; a step two days late is the system
    stopped, and the user's pipeline is quietly going cold while the app shows
    a green tick.
    """
    cutoff = now - timedelta(hours=FOLLOW_UP_OVERDUE_HOURS)
    # Bounded the same way, and for the same reason, as the drafts above.
    overdue = (
        select(FollowUp.id, FollowUp.scheduled_at)
        .join(Application, FollowUp.application_id == Application.id)
        .where(
            Application.user_id == user.id,
            FollowUp.status == FollowUpStatus.SCHEDULED,
            FollowUp.scheduled_at < cutoff,
        )
    )
    oldest = db.execute(
        overdue.order_by(FollowUp.scheduled_at).limit(1)
    ).first()
    if oldest is None:
        return {NotificationKind.FOLLOW_UP_DUE: 0}

    oldest_id, oldest_due = oldest
    count = db.scalar(select(func.count()).select_from(overdue.subquery())) or 0
    due = _aware(oldest_due) or now
    late_days = max(1, (now - due).days)
    emitted = notifications.emit(
        db,
        user.id,
        kind=NotificationKind.FOLLOW_UP_DUE,
        severity=NotificationSeverity.WARN,
        title=(
            f"{count} follow-{_plural(count, 'up is', 'ups are')} overdue"
        ),
        body=(
            f"The oldest was due {late_days} {_plural(late_days, 'day', 'days')} ago "
            "and has not gone out."
        ),
        link="/pipeline",
        dedupe_key=f"follow_up_due:{oldest_id}",
        meta={"count": count, "oldest_follow_up_id": oldest_id},
    )
    return {NotificationKind.FOLLOW_UP_DUE: 1 if emitted is not None else 0}


def _follow_ups_suggested(db: Session, user: User, now: datetime) -> dict:
    """Applications nobody is scheduled to chase.

    The counterpart to :func:`_follow_ups_overdue` and deliberately a *notice*
    rather than a warning: an overdue step is the system having stopped, and
    this is the system never having been asked. Nothing is broken — there is
    simply a pile of outreach that will sit there forever unless somebody looks.

    Keyed on the oldest application id, so the notification re-arms when the
    front of the queue changes and does not re-arm while the same rows keep
    qualifying tick after tick. That is the same rule ``_drafts_waiting`` uses
    and for the same reason: this condition is true continuously for weeks, and
    a key without a moving part in it would be one notification for the entire
    period — or, with the wrong moving part, one an hour.

    Deliberately quiet about the *number* in the dedupe key. A pipeline that
    grows from nine stalled applications to ten has not become news.

    Read uncapped, because the number is said out loud. ``build``'s default
    ``limit`` is a *page* — twenty-five, the length past which a list stops
    being one anybody finishes — and taking ``len`` of a page announced a
    pipeline of forty stalled applications as "25 applications have gone
    quiet", with the same truncated figure in ``meta``. That is the failure
    ``follow_up_suggestions.count`` was written to avoid for the badge, arrived
    at from the other side; the sibling notices here already take an uncapped
    ``func.count()`` beside their oldest row, and this one now reads the same
    way. It costs no extra query — the cap was only ever a slice of a list the
    same three reads had already built.
    """
    rows = follow_up_suggestions.build(
        db, user, now=now, limit=follow_up_suggestions.UNCAPPED
    )
    if not rows:
        return {NotificationKind.FOLLOW_UP_SUGGESTED: 0}

    count = len(rows)
    oldest = rows[0]
    emitted = notifications.emit(
        db,
        user.id,
        kind=NotificationKind.FOLLOW_UP_SUGGESTED,
        severity=NotificationSeverity.INFO,
        title=(
            f"{count} application{_plural(count, '', 's')} "
            f"{_plural(count, 'has', 'have')} gone quiet"
        ),
        body=(
            f"The oldest is {oldest.company or 'one of them'}, silent for "
            f"{oldest.silent_days} days with no follow-up scheduled."
        ),
        link="/pipeline",
        dedupe_key=f"follow_up_suggested:{oldest.application_id}",
        meta={"count": count, "oldest_application_id": oldest.application_id},
    )
    return {NotificationKind.FOLLOW_UP_SUGGESTED: 1 if emitted is not None else 0}


def _mailbox_health(db: Session, user: User, now: datetime) -> dict:
    """Grants that have died, and grants about to.

    Both are keyed so that a *reconnect* re-arms them: a revoked mailbox that is
    reconnected and revoked again is genuinely new news, and a fresh grant has a
    new expiry date. ``granted_at`` is what moves in both cases, so it is what
    the key carries.

    """
    expiring = disconnected = 0
    for account in user.gmail_accounts:
        granted = _aware(account.granted_at)
        stamp = granted.date().isoformat() if granted else "unknown"

        if account.status != gmail_accounts.CONNECTED:
            emitted = notifications.emit(
                db,
                user.id,
                kind=NotificationKind.MAILBOX_DISCONNECTED,
                severity=NotificationSeverity.WARN,
                title=f"{account.email} needs reconnecting",
                body=(
                    "Google has stopped honouring this mailbox, so it cannot send "
                    "outreach or read replies."
                ),
                link="/setup",
                dedupe_key=f"mailbox_disconnected:{account.id}:{stamp}",
                meta={"gmail_account_id": account.id, "email": account.email},
            )
            disconnected += 1 if emitted is not None else 0
            continue

        expiry = gmail_accounts.grant_expiry(account, now=now)
        if expiry is None or not expiry.expiring:
            continue
        days = expiry.days_left
        when = "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"
        emitted = notifications.emit(
            db,
            user.id,
            kind=NotificationKind.MAILBOX_EXPIRING,
            severity=NotificationSeverity.WARN,
            title=f"{account.email} loses Google access {when}",
            # The reason is the fix. Without it the user reconnects every week
            # forever and never learns why — see `gmail_accounts.grant_expiry`.
            body=expiry.reason,
            link="/setup",
            dedupe_key=(
                f"mailbox_expiring:{account.id}:{expiry.expires_at.date().isoformat()}"
            ),
            meta={
                "gmail_account_id": account.id,
                "email": account.email,
                "days_left": days,
            },
        )
        expiring += 1 if emitted is not None else 0

    return {
        NotificationKind.MAILBOX_EXPIRING: expiring,
        NotificationKind.MAILBOX_DISCONNECTED: disconnected,
    }


def _interviews(db: Session, user: User, now: datetime) -> dict:
    """Applications that have reached an interview.

    The best thing that happens in this product, and it was announced by a chip
    changing colour on a page the user had to already be looking at. Scoped to
    the recent window like the other entity-scoped notices, so switching this on
    does not re-announce every interview the account has ever had.
    """
    since = now - timedelta(days=NEW_WINDOW_DAYS)
    rows = list(
        db.execute(
            select(Application.id, Recruiter.company)
            .join(Recruiter, Application.recruiter_id == Recruiter.id)
            .where(
                Application.user_id == user.id,
                Application.status == ApplicationStatus.INTERVIEW_SCHEDULED,
                Application.updated_at >= since,
            )
            .order_by(Application.updated_at.desc())
            .limit(MAX_PER_KIND)
        ).all()
    )

    written = 0
    for application_id, company in rows:
        where = company or "a company"
        emitted = notifications.emit(
            db,
            user.id,
            kind=NotificationKind.INTERVIEW_SCHEDULED,
            title=f"Interview scheduled with {where}",
            body="Open the prep brief before it comes round.",
            link="/pipeline",
            dedupe_key=f"interview:{application_id}",
            meta={"application_id": application_id},
        )
        written += 1 if emitted is not None else 0
    return {NotificationKind.INTERVIEW_SCHEDULED: written}


# --------------------------------------------------------------------------
# The entry points.
# --------------------------------------------------------------------------


def sweep_user(db: Session, user: User, *, now: datetime | None = None) -> SweepResult:
    """Derive and emit every notification *user* is owed right now.

    Each condition is independent and **commits on its own**, which is the
    detail that makes the "one failure must not cost the others" promise true.
    The obvious shape — run all five, commit once at the end — is wrong: the
    handler below has to ``rollback`` to make the session usable again after a
    failed flush, and a rollback in a shared transaction discards every
    notification the earlier conditions had already written. So the fifth
    condition raising would silently cost the user the first four, which is the
    exact failure the try/except was added to prevent.

    Committing per condition costs four extra round trips per user per tick,
    against a sweep that on a healthy account writes nothing at all.

    Each condition returns ``{kind: count}`` rather than a bare integer, because
    one of them (mailbox health) produces two kinds. The tally is merged *after*
    the commit, so a condition whose commit fails is not counted as having
    emitted anything.
    """
    now = now or datetime.now(UTC)
    result = SweepResult()

    conditions = (
        ("recruiter_replies", _recruiter_replies),
        ("drafts_waiting", _drafts_waiting),
        ("follow_ups_overdue", _follow_ups_overdue),
        ("follow_ups_suggested", _follow_ups_suggested),
        ("interviews", _interviews),
        ("mailbox_health", _mailbox_health),
    )
    for name, fn in conditions:
        try:
            written = fn(db, user, now)
            db.commit()
        except Exception:  # noqa: BLE001 - one condition must not cost the others
            logger.exception(
                "notification condition %s failed for user %s", name, user.id
            )
            db.rollback()
            continue
        for kind, count in written.items():
            result.add(kind, count)

    return result


def sweep_all(db: Session, *, now: datetime | None = None) -> dict:
    """Every active user, one at a time. The beat task's whole body.

    Users are swept individually and committed individually so that one bad
    account cannot roll back the notifications of the ninety-nine after it.
    """
    now = now or datetime.now(UTC)
    user_ids = list(db.scalars(select(User.id).where(User.is_active.is_(True))))

    totals: dict[str, int] = {}
    swept = 0
    for user_id in user_ids:
        user = db.get(User, user_id)
        if user is None:
            continue
        try:
            result = sweep_user(db, user, now=now)
        except Exception:  # noqa: BLE001 - the sweep must reach every user
            logger.exception("notification sweep failed for user %s", user_id)
            db.rollback()
            continue
        swept += 1
        for kind, count in result.emitted.items():
            totals[kind] = totals.get(kind, 0) + count

    return {"users": swept, "emitted": totals, "at": now.isoformat()}


def unread_total(db: Session, user_id: int) -> int:
    """Convenience re-export so callers need only one import."""
    return notifications.unread_count(db, user_id)


__all__ = [
    "DRAFT_WAIT_HOURS",
    "FOLLOW_UP_OVERDUE_HOURS",
    "MAX_PER_KIND",
    "NEW_WINDOW_DAYS",
    "SweepResult",
    "sweep_all",
    "sweep_user",
    "unread_total",
]
