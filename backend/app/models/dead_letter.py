"""A task that failed for the last time, written down.

``task_ignore_result=True`` is set globally and for good reasons (see
:mod:`app.tasks.celery_app`): nothing reads a task result, and touching the
result store makes ``apply_async`` block for ~20s whenever Redis is unwell. The
cost of that decision is that a task which exhausts its retries leaves behind
exactly one thing — a traceback on stderr of whichever worker happened to run
it. There is no way to ask the deployment "what has been failing?", and no way
to run the work again once the cause is fixed.

That is the gap this table fills. One row per permanently-failed task, holding
enough to answer *what broke*, *how often*, and *can I just re-run it*.

Three decisions are load-bearing:

**Identical failures collapse.** ``poll_all_inboxes`` fails every five minutes
while Gmail is down; a row per occurrence is 288 rows a day that all say the
same sentence, and the real signal — that a *second*, different thing also
started failing — is buried. Rows are keyed by :attr:`fingerprint`, which mixes
the task name, its arguments and the exception, so a repeating failure
increments :attr:`occurrences` and moves :attr:`last_failed_at`. Arguments are
*in* the fingerprint deliberately: one user's broken row must not collapse into
another's, because the two are replayed separately.

**Arguments are stored, but only when storing them is honest.** Every task in
this codebase takes scalar ids, so recording the arguments is both safe and the
thing that makes replay possible. A future task taking a token must not have it
land here in plaintext, so :mod:`app.tasks.dead_letter` redacts by key name and
truncates by length — and sets :attr:`replayable` to ``False`` when it did,
because arguments that were rewritten for storage would be replayed as the
rewritten version. Replay is offered only where it would be faithful.

**No foreign key to ``users``.** :attr:`user_id` is a plain integer, unlike
every other table here. It is copied out of a task's ``user_id`` argument on a
best-effort basis, so its value is whatever the caller passed — including, on
the failure path this table exists to record, a stale or nonexistent id. A
constraint would mean the row recording that failure is itself rejected, which
is the one thing a dead-letter queue may never do.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

#: Awaiting an operator. The only status a capture ever writes.
STATUS_NEW = "new"
#: Re-dispatched onto the broker. Keeps the row as the audit trail of the retry.
STATUS_REPLAYED = "replayed"
#: Triaged and consciously dropped — a failure that needed no re-run.
STATUS_IGNORED = "ignored"

STATUSES = (STATUS_NEW, STATUS_REPLAYED, STATUS_IGNORED)

#: Why the task stopped. ``failed`` is an exception that outlived its retries;
#: ``revoked`` is a task killed from outside (``worker_lost``, a terminate).
REASON_FAILED = "failed"
REASON_REVOKED = "revoked"


class DeadLetterJob(Base, TimestampMixin):
    __tablename__ = "dead_letter_jobs"
    __table_args__ = (
        # The collapse lookup: "is this exact failure already open?". Ordered
        # fingerprint-first because status has three values and is not
        # selective on its own.
        Index("ix_dead_letter_jobs_fingerprint_status", "fingerprint", "status"),
        # The operator's list: open failures, newest first.
        Index("ix_dead_letter_jobs_status_last_failed", "status", "last_failed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Dotted Celery task name, e.g. ``app.tasks.inbox_tasks.poll_all_inboxes``.
    task_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    #: The Celery id of the *most recent* occurrence. Not unique: a collapsed row
    #: has had many, and this is the one worth grepping the worker log for.
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    queue: Mapped[str | None] = mapped_column(String(64))

    #: See the module docstring — best effort, no constraint.
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)

    #: JSON, redacted and length-capped by :mod:`app.tasks.dead_letter`.
    args_json: Mapped[str | None] = mapped_column(Text)
    kwargs_json: Mapped[str | None] = mapped_column(Text)

    #: failed | revoked
    reason: Mapped[str] = mapped_column(
        String(16), default=REASON_FAILED, nullable=False
    )
    exception_type: Mapped[str | None] = mapped_column(String(255))
    exception_message: Mapped[str | None] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text)
    #: ``request.retries`` at the point of final failure — how much budget the
    #: task had already spent, which distinguishes "died once" from "died 24
    #: times over two hours".
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Stable hash over task name, arguments and the normalised exception.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: False when redaction or truncation rewrote the arguments, so replaying
    #: would dispatch something other than what failed.
    replayable: Mapped[bool] = mapped_column(default=True, nullable=False)

    #: new | replayed | ignored
    status: Mapped[str] = mapped_column(
        String(16), default=STATUS_NEW, index=True, nullable=False
    )
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The Celery id handed out by the replay, so the retry can be followed.
    replayed_task_id: Mapped[str | None] = mapped_column(String(64))
    #: Email of the administrator who replayed or ignored it.
    resolved_by: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<DeadLetterJob {self.task_name} {self.exception_type} "
            f"x{self.occurrences} {self.status}>"
        )


__all__ = [
    "REASON_FAILED",
    "REASON_REVOKED",
    "STATUSES",
    "STATUS_IGNORED",
    "STATUS_NEW",
    "STATUS_REPLAYED",
    "DeadLetterJob",
]
