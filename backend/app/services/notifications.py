"""Writing, reading and expiring in-app notifications.

The interesting half of this module is :func:`emit`, and the interesting thing
about :func:`emit` is that it is designed to be called *speculatively*, from a
sweep that has no memory of what it did last time. Callers say "this is true
right now" and the dedupe key decides whether that is news. Nothing upstream has
to track state, which is what keeps the emitters — a Celery sweep, an inbound
scanner, a router — from each growing their own half-correct copy of "have we
already told them".

Three rules make that safe:

1. **A muted kind is never written.** Not written-then-filtered: a user who has
   switched a kind off should not accumulate rows they will never see, and a
   filter at read time means the unread count is computed from rows the list
   does not show. The count and the list disagreeing is the one bug a
   notification centre cannot survive.
2. **A duplicate is a no-op, not an error.** The unique constraint is the
   arbiter, and losing the race returns the existing row.
3. **A failed emit never fails its caller.** These are told to the user as a
   courtesy; a notification that cannot be written must not roll back the
   inbound message that prompted it. Every write is inside a savepoint for the
   reason ``digest_service.get_or_create`` gives — a failed flush poisons the
   session, and the session belongs to whoever called us.
4. **A dismissed key stays claimed.** Dismissal hides the row; it does not
   free the key. Otherwise "dismiss" means "hide until the sweep runs again",
   because the sweep has no memory beyond the key and the condition that
   produced the row is still true — see :func:`dismiss`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.notification import (
    NOTIFICATION_KINDS,
    Notification,
    NotificationKind,
    NotificationPreference,
    NotificationSeverity,
)
from app.models.user import User

logger = logging.getLogger(__name__)

#: How long a notification lives. Long enough that a user who opens the app
#: fortnightly still sees the week they missed; short enough that the list is
#: never a year of history nobody scrolls to.
RETENTION_DAYS = 30

#: The most rows any one list read will return. The client pages; this is the
#: backstop for a client that does not.
MAX_LIST = 100


def get_preference(db: Session, user: User) -> NotificationPreference:
    """This user's preferences row, created on first read.

    Same shape and same race as ``digest_service.get_or_create``: ``user_id`` is
    unique, two concurrent first-reads both insert, and the loser adopts the
    winner's row rather than raising out of a function whose caller is merely
    rendering a settings page.
    """
    pref = user.notification_preference
    if pref is not None:
        return pref

    savepoint = db.begin_nested()
    pref = NotificationPreference(user_id=user.id, muted_kinds=[])
    db.add(pref)
    try:
        db.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = db.scalar(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user.id
            )
        )
        if existing is None:
            raise
        logger.info(
            "notification preferences for user %s were created concurrently", user.id
        )
        return existing
    savepoint.commit()
    db.commit()
    db.refresh(pref)
    return pref


def _allows(db: Session, user_id: int, kind: str) -> bool:
    """Whether *user_id* wants to hear about *kind*.

    Reads the row directly rather than through :func:`get_preference` because
    this runs on the emit path, which must not create rows: an emitter sweeping
    a thousand users would otherwise insert a preferences row for every one of
    them the first time it ran, including the users it then decides to say
    nothing to.

    No row means no opinion, and no opinion means yes.
    """
    pref = db.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    if pref is None:
        return True
    return pref.allows(kind)


def emit(
    db: Session,
    user_id: int,
    *,
    kind: NotificationKind | str,
    title: str,
    dedupe_key: str,
    body: str | None = None,
    link: str | None = None,
    severity: NotificationSeverity | str = NotificationSeverity.INFO,
    meta: dict | None = None,
) -> Notification | None:
    """Tell *user_id* something, unless we already have or they asked us not to.

    Returns the row it **wrote**, or ``None`` when it wrote nothing — because
    the kind is muted, or because this key has already been claimed.

    That the deduped case is ``None`` rather than the existing row is the whole
    reason callers can count their return values. The sweep re-derives the same
    conditions every fifteen minutes; if a repeat emit handed back the row it
    found, every tick would report having emitted everything it had ever
    emitted, and the tally a deployment reads to tell "quiet" from "broken"
    would be a constant.

    Does **not** commit. The caller owns the transaction, because the common
    case is an emitter part-way through persisting the thing being announced,
    and a commit here would publish that half-written work.
    """
    kind_value = kind.value if isinstance(kind, NotificationKind) else str(kind)
    severity_value = (
        severity.value if isinstance(severity, NotificationSeverity) else str(severity)
    )

    if kind_value not in NOTIFICATION_KINDS:
        # A typo in a dedupe key is invisible; a typo in a kind would render as
        # an unstyled row forever. Refused loudly here, where the stack still
        # names the emitter.
        raise ValueError(f"unknown notification kind: {kind_value!r}")

    if not _allows(db, user_id, kind_value):
        return None

    # Deliberately not filtered on `dismissed_at`: a dismissed row still holds
    # its key. That is the whole of rule 4 — the sweep re-derives a standing
    # condition every tick and would otherwise re-announce whatever the user
    # just cleared. `prune` is what eventually frees the key.
    existing = db.scalar(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return None

    row = Notification(
        user_id=user_id,
        kind=kind_value,
        severity=severity_value,
        title=title[:200],
        body=body[:500] if body else None,
        link=link[:300] if link else None,
        dedupe_key=dedupe_key[:200],
        meta=meta or {},
    )
    savepoint = db.begin_nested()
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # Somebody else emitted the same key between the select above and this
        # flush. That is the constraint doing its job, not a failure — and it
        # is *their* row, not ours, so nothing was written here.
        savepoint.rollback()
        return None
    savepoint.commit()
    return row


def _visible(stmt, user_id: int):
    """Narrow *stmt* to the rows this user can still see.

    A dismissed row is off the list in every sense — not listed, not counted,
    not markable, not dismissable twice — and survives only to keep its
    ``dedupe_key`` claimed. One helper rather than the same clause written out
    at each of the seven places that need it, because a filter applied to six
    of them is a count and a list that disagree, which is the one bug a
    notification centre cannot survive.

    :func:`emit` is the one read that deliberately does *not* use this. See the
    comment there.
    """
    return stmt.where(
        Notification.user_id == user_id, Notification.dismissed_at.is_(None)
    )


def unread_count(db: Session, user_id: int) -> int:
    return (
        db.scalar(
            _visible(select(func.count(Notification.id)), user_id).where(
                Notification.read_at.is_(None)
            )
        )
        or 0
    )


def total_count(db: Session, user_id: int, *, unread_only: bool = False) -> int:
    """How many rows the same filters select, ignoring the page.

    Read alongside the list so the client can tell a short page from the end of
    the list. See ``app/core/pagination.py`` for why a silently truncated list
    is worse than a slow one.

    ``unread_only`` has to be here rather than only on :func:`list_for`, and
    that is the whole point of the argument: the count answers "is this page all
    of it", so a count taken over a *different* filter than the list answers a
    question nobody asked. With a hundred read notifications and three unread
    ones, ``?unread_only=true&limit=50`` returned three rows and a total of a
    hundred, and a client comparing the two paged forward into nothing.
    """
    stmt = _visible(select(func.count(Notification.id)), user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return db.scalar(stmt) or 0


def list_for(
    db: Session,
    user_id: int,
    *,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Notification]:
    """One page of *user_id*'s notifications, newest first.

    Ordered by ``id`` after ``created_at`` because a sweep emits several rows
    inside one transaction and they land on the same timestamp — an unordered
    tie is how a paged read repeats one row and drops another.
    """
    stmt = _visible(select(Notification), user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
    return list(db.scalars(stmt.limit(min(limit, MAX_LIST)).offset(offset)))


def mark_read(db: Session, user_id: int, notification_id: int) -> Notification | None:
    """Mark one row read. Returns ``None`` when it is not this user's row.

    The ownership check is a ``where`` clause rather than a load-then-compare so
    that "not yours" and "does not exist" are the same answer — a 404 that only
    fires for ids belonging to somebody else is an enumeration oracle.
    """
    row = db.scalar(
        _visible(select(Notification), user_id).where(
            Notification.id == notification_id
        )
    )
    if row is None:
        return None
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
    db.commit()
    return row


def mark_all_read(db: Session, user_id: int) -> int:
    """Mark every unread row read. Returns how many changed."""
    result = db.execute(
        _visible(update(Notification), user_id)
        .where(Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    db.commit()
    return int(result.rowcount or 0)


def dismiss(db: Session, user_id: int, notification_id: int) -> bool:
    """Clear one row off the list. Returns whether anything changed.

    Stamped rather than deleted, and the distinction is the difference between
    dismissing something and hiding it for fifteen minutes.

    This used to be a ``DELETE``, on the argument that keeping the row would
    keep the dedupe key claimed and so silence the condition forever —
    "dismissing 'your grant expires Thursday' would silence next week's warning
    too". That argument is wrong about its own example: next week's warning
    carries next week's date in its key (``mailbox_expiring:7:2026-09-06``) and
    re-arms whatever happens to this row. Every entity-scoped notice is the
    same — a new recruiter email has a new id.

    What the delete actually did was free the keys of the *state*-scoped
    notices, which are the ones nobody wants twice. Those name a condition that
    is still true on the next tick: prod carries a hundred and sixty-two drafts
    that are never going to be approved, so ``draft_waiting:<oldest id>`` does
    not move. Dismissing it un-claimed the key, and the sweep — which has no
    memory beyond the key — re-derived the condition and wrote the identical
    notification back within the sweep interval. The user's only way to be rid
    of it was to mute the kind.

    The key is freed by :func:`prune` instead, on the same clock as everything
    else here. A condition still true a month later gets one more sentence,
    which is a reminder rather than a loop.
    """
    row = db.scalar(
        _visible(select(Notification), user_id).where(
            Notification.id == notification_id
        )
    )
    if row is None:
        return False
    now = datetime.now(UTC)
    row.dismissed_at = now
    # Also read, so nothing has to remember that a dismissed row is excluded
    # from the unread count by a second rule. It is excluded by both.
    if row.read_at is None:
        row.read_at = now
    db.commit()
    return True


def dismiss_all(db: Session, user_id: int) -> int:
    """Clear the whole list. Returns how many rows changed.

    Same tombstone as :func:`dismiss`, for the same reason — "clear all" on an
    account with a standing condition is otherwise the shortest path back to
    the row the user just cleared.
    """
    now = datetime.now(UTC)
    result = db.execute(
        _visible(update(Notification), user_id).values(
            dismissed_at=now,
            read_at=func.coalesce(Notification.read_at, now),
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def prune(db: Session, *, now: datetime | None = None, days: int = RETENTION_DAYS) -> int:
    """Delete notifications older than *days*, read or not, dismissed or not.

    Unread rows age out too, and deliberately so. A three-week-old "a recruiter
    replied" that the user has not opened is not news any more — it is a row
    keeping an unread badge lit for something they have long since dealt with
    somewhere else in the app.

    This is also the only thing that ever frees a ``dedupe_key``, which makes
    it the release valve on :func:`dismiss`. A dismissed row holds its key so
    the sweep cannot re-announce it on the next tick; deleting the row here a
    month later means a condition that is *still* true gets said once more.
    That is the intended cadence, not a leak: monthly is a reminder, and every
    fifteen minutes was the bug.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    result = db.execute(delete(Notification).where(Notification.created_at < cutoff))
    db.commit()
    return int(result.rowcount or 0)
