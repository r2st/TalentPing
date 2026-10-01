"""One pass over a mailbox, written down.

:class:`~app.services.inbound_scanner.ScanResult` already carries everything a
stats view needs — how many messages were listed, how many were fetched, how many
were new, and one counter per reason a message was skipped. Then
:func:`app.services.inbound_scanner.scan` logs the whole dict and drops it.

So the product could not answer "how many emails did you scan?" from the database
at all, and "why was that one skipped?" only from a log line nobody keeps. This
table is that same dict, persisted, one row per scan.

``trigger`` is the field that earns this table its place beyond the dashboard. It
records which caller ran the scan — the beat tick, a Gmail push notification,
the user pressing the button, or the backlog catch-up — which is the only way to
answer "is push actually doing the work, or is beat still finding everything?".
That question is the operational cost of making push primary, and it should be
answerable without a log search. ``backlog`` answers the other one: "did the
catch-up ever actually run, and what did it find?"

Rows are cheap and append-only. A watched mailbox at the 5-minute fallback
cadence writes ~288 rows a day and most of them say "listed 40, examined 0".
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

#: The four callers. Kept as plain strings rather than an enum: this is a label
#: on an append-only log row, and a future caller should be able to add itself
#: without a migration — which is exactly what ``backlog`` did.
TRIGGER_BEAT = "beat"
TRIGGER_PUSH = "push"
TRIGGER_MANUAL = "manual"
#: The catch-up sweep, which reads a far wider window than the other three. Worth
#: its own label rather than borrowing ``beat``: a run that lists a month of mail
#: and detects nothing looks alarming beside runs that list a week, and the
#: only thing that explains the difference is which caller ran it.
TRIGGER_BACKLOG = "backlog"


class RecruiterScanRun(Base, TimestampMixin):
    __tablename__ = "recruiter_scan_runs"
    __table_args__ = (
        # Every read of this table is "this user, this window, newest first".
        Index("ix_recruiter_scan_runs_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    gmail_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="SET NULL"), index=True
    )

    # beat | push | manual | backlog — see the module docstring.
    trigger: Mapped[str] = mapped_column(String(16), default=TRIGGER_BEAT, nullable=False)

    # ---- The ScanResult, flattened ----
    listed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    examined: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_known: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_own_thread: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_from_self: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_bounce: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_opt_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The scanner has always counted this one and this table has always dropped
    # it, which is a hole in the "same dict, persisted" promise above rather
    # than an omission of detail: on a busy mailbox it is the *largest* bucket
    # by an order of magnitude, and it is the one that explains a scan that
    # fetched 152 messages and detected none.
    skipped_blocked_sender: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    # An out-of-office notice, refused on its headers before it was ever
    # stored. Its own bucket rather than folded into another: "your mailbox is
    # full of autoresponders" and "your mailbox is full of bounces" are
    # different answers to "why did the scan find nothing?", and this one is
    # the reason a recruiter on holiday produces no work rather than a reply.
    skipped_auto_reply: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    skipped_unfetchable: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Left for the next run because the per-run cap was hit. Reported here for
    # the same reason it is reported everywhere else: never silently dropped.
    deferred: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # The Gmail search that produced all of the above. "Why is that email
    # missing?" is answered by the query far more often than by the filters.
    query: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    @property
    def skipped_total(self) -> int:
        return (
            self.skipped_known
            + self.skipped_own_thread
            + self.skipped_from_self
            + self.skipped_bounce
            + self.skipped_opt_out
            + self.skipped_blocked_sender
            + self.skipped_auto_reply
            + self.skipped_unfetchable
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<RecruiterScanRun user={self.user_id} {self.trigger} "
            f"listed={self.listed} detected={self.detected}>"
        )


__all__ = [
    "TRIGGER_BEAT",
    "TRIGGER_MANUAL",
    "TRIGGER_PUSH",
    "RecruiterScanRun",
]
