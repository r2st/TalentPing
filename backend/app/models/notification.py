"""In-app notifications — the surface between "the agent noticed" and "the user knows".

The product has had exactly two ways to tell its user something, and both of
them require the user to already be looking. A toast lasts four seconds and only
fires for an action the user just took. The weekly digest (:mod:`app.models.digest`)
is real mail, but it is *weekly* — a recruiter who writes on Tuesday morning is
news the user learns on Monday, six days after the recruiter stopped waiting.

Everything in between fell in the gap: a grant that expires on Thursday, a
follow-up that came due on Wednesday, a draft that has been sitting in review
since last week. All of it was visible in the app, on a page the user had no
reason to open, which is the same thing as invisible.

A notification is one row that says *something happened, here is where to go*.
It is deliberately not a message queue and not an event log:

``dedupe_key``
    is what stops the sweep that produces most of these from producing them
    again on the next tick. The whole design rests on it, so it is worth being
    precise about the two shapes it takes:

    * **Entity-scoped** — ``recruiter_reply:email:412``. Names a thing that
      happened once. The key can never recur, so neither can the notification.
    * **State-scoped** — ``mailbox_expiring:7:2026-08-30``. Names a *condition*
      plus the fact that would have to change for it to be worth saying again.
      Re-fires when the state moves and stays silent while it does not, which is
      what lets an hourly sweep emit "your grant expires Thursday" once rather
      than a hundred and sixty-eight times.

    A key that encodes neither — ``drafts_waiting`` on its own — is a bug: the
    notification fires once, ever, and the second week of drafts is silent.

``read_at`` and ``dismissed_at``
    are the only mutable fields, and there is no "unread again" for either.

    Dismissal is a *tombstone* rather than a delete, and that is the half of
    the dedupe contract the key alone cannot carry. Most of these notices name
    a condition that is still true at the next tick — a hundred and sixty-two
    drafts nobody is going to approve, a pipeline of stalled applications — so
    a state-scoped key does not move when the user reads the row and decides to
    live with it. Deleting the row handed the key back, and the sweep, which
    has no memory beyond the key, wrote the identical notification again
    fifteen minutes later. "Dismiss" meant "hide this until the next tick".

    Keeping the row keeps the key claimed, so the answer stays dismissed. It
    does not keep it forever: :func:`app.services.notifications.prune` deletes
    by age like every other row here, and a condition that is *still* true a
    month later is worth one more sentence.

Rows are pruned by age (:func:`app.services.notifications.prune`) rather than by
count, because the thing that must not happen is an account that has been
running for a year rendering a thousand-row list.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class NotificationKind(str, enum.Enum):
    """What happened. One member per thing the product can interrupt a user for.

    Stored as its *value* in a plain ``String`` column rather than a database
    enum, for the reason ``users.role`` gives: this list will grow, and growing
    a Postgres enum is a migration with a lock on it. An unknown value read back
    from an older or newer deployment renders as itself instead of raising,
    which is the behaviour a notification list should have.

    Values are SCREAMING_CASE to match every other enum the web client styles
    from a lookup table — ``tests/test_enum_drift.py`` reads those tables and
    checks them against these members, and its parser only accepts upper-case
    keys. A lower-case value here would have been a kind that renders as a raw
    grey chip with nothing checking that it does.
    """

    #: A recruiter replied and nothing has answered them yet.
    RECRUITER_REPLY = "RECRUITER_REPLY"
    #: Mail the agent wrote that is waiting for the user to approve it.
    DRAFT_WAITING = "DRAFT_WAITING"
    #: A follow-up step whose send date has passed.
    FOLLOW_UP_DUE = "FOLLOW_UP_DUE"
    #: A Gmail grant with days left on it. See ``gmail_accounts.grant_expiry``.
    MAILBOX_EXPIRING = "MAILBOX_EXPIRING"
    #: A Gmail grant Google has already refused. Sending has stopped.
    MAILBOX_DISCONNECTED = "MAILBOX_DISCONNECTED"
    #: Applications that went quiet with nobody scheduled to chase them. Not
    #: the same as FOLLOW_UP_DUE, which is a *scheduled* step that did not go
    #: out — a system failure. This one is the opposite: nothing failed, because
    #: nothing was ever planned.
    FOLLOW_UP_SUGGESTED = "FOLLOW_UP_SUGGESTED"
    #: An application reached the interview stage. The best thing that happens
    #: in this product, and until now it was announced by a chip changing
    #: colour on a page the user had to already be looking at.
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"


#: Every legal ``kind`` value, for validation at the edges.
NOTIFICATION_KINDS: tuple[str, ...] = tuple(k.value for k in NotificationKind)


class NotificationSeverity(str, enum.Enum):
    """How loud. Two levels, because three would be a taxonomy nobody applies.

    ``INFO`` is "there is work waiting". ``WARN`` is "something has stopped, or
    is about to". Only ``WARN`` earns a colour in the list and a dot in the nav.
    """

    INFO = "info"
    WARN = "warn"


SEVERITIES: tuple[str, ...] = tuple(s.value for s in NotificationSeverity)


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        # The dedupe contract, enforced by the database rather than by the
        # sweep that relies on it. Two overlapping sweeps — `task_acks_late`
        # redelivers a killed worker's tick while the original may still be
        # running — both read "no notification for this key" and both insert.
        # Without this constraint that is a duplicate in the user's list; with
        # it, it is an IntegrityError the emitter swallows.
        UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_user_dedupe"),
        # The list query: one user's rows, newest first.
        Index("ix_notifications_user_created", "user_id", "created_at"),
        # The unread count, which is a different query from the list and was
        # being served by the index above on the argument that "unread rows are
        # a small and recent subset by construction". They are — but that index
        # cannot tell which rows those are, so the count still had to walk every
        # entry the user had ever been sent and test two columns on each. And
        # the row count only grows: see ``dismissed_at`` below, a dismissed row
        # stays on file to keep its ``dedupe_key`` claimed.
        #
        # The app shell polls this every thirty seconds on every route, for the
        # whole life of every session, to render one integer. Partial so the
        # index holds only the rows the poll is asking about — which on a
        # settled account is nearly none of them — and so the rest cost nothing
        # to keep it current.
        Index(
            "ix_notifications_user_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL AND dismissed_at IS NULL"),
            sqlite_where=text("read_at IS NULL AND dismissed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # No ``index=True``: ``ix_notifications_user_created`` above already leads
    # with this column, so a plain ``(user_id)`` tree serves no read the
    # composite does not and is maintained on every insert for nothing. It also
    # made this table the one place where two indexes covered an identical
    # column list — ``ix_notifications_user_unread`` is partial and therefore
    # *not* the duplicate ``test_no_two_indexes_cover_the_same_columns`` was
    # naming, but the plain index genuinely was redundant, just against a
    # different index than the failure said.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(10), default=NotificationSeverity.INFO.value, nullable=False
    )

    #: One line, in the user's language, saying what happened.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The sentence under it. Optional — a title that needs no elaboration
    #: should not be padded with one.
    body: Mapped[str | None] = mapped_column(String(500))

    #: Where to go. An in-app path (``/inbox?email=412``), never an absolute
    #: URL: this is rendered into a client-side router link, and an origin here
    #: would be an open redirect wearing a notification's clothes.
    link: Mapped[str | None] = mapped_column(String(300))

    #: See the module docstring. Not nullable — a notification with no dedupe
    #: key cannot be re-emitted safely, so there is no such thing.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)

    #: Anything the client needs that is not a sentence — a count, an id, a
    #: date. Free-form on purpose; nothing server-side branches on it.
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When the user cleared this row off their list. See the module docstring:
    #: the row survives dismissal so its ``dedupe_key`` stays claimed, and every
    #: read path filters it out. Nothing un-sets this — a dismissed row leaves
    #: only by ageing out.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="notifications")

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<Notification user={self.user_id} kind={self.kind} "
            f"read={self.is_read}>"
        )


class NotificationPreference(Base, TimestampMixin):
    """Which kinds this user has switched off, and whether any of it is on.

    Muting is stored as a list of *silenced* kinds rather than a column per
    kind, because a column per kind is a migration every time the product
    learns to notice something new — and the release that adds the column is
    the release where every existing row defaults to whatever the migration
    said, rather than to "on", which is the only sensible default for a
    notification the user has never seen.

    So absence means enabled. A user with no row at all gets everything, a user
    with an empty ``muted_kinds`` gets everything, and the only way to be quiet
    is to have said so.
    """

    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    #: The master switch. Off silences everything without losing which
    #: individual kinds the user had muted — turning it back on restores the
    #: selection rather than resetting it.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: ``NotificationKind`` values this user does not want. Unknown values are
    #: tolerated and ignored: a kind removed from the code should not make an
    #: old preferences row unreadable.
    muted_kinds: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    user: Mapped[User] = relationship(back_populates="notification_preference")

    def allows(self, kind: str) -> bool:
        """Whether a notification of *kind* may be written for this user."""
        if not self.enabled:
            return False
        return kind not in (self.muted_kinds or [])

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<NotificationPreference user={self.user_id} enabled={self.enabled} "
            f"muted={len(self.muted_kinds or [])}>"
        )
