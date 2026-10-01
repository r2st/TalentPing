"""Capturing a task that failed for the last time.

Wired to Celery's ``task_failure`` and ``task_revoked`` signals, which fire only
once a task is genuinely finished: ``self.retry()`` raises ``Retry`` and emits
``task_retry`` instead, so a task still working through its budget does not
appear here. What lands is what a worker has given up on.

The whole module obeys one rule, and :func:`_guard` is that rule made
mechanical: **nothing in here may raise into the worker.** A dead-letter queue
that throws while recording a failure turns one broken task into two, and does
it on the path where the process is already in trouble — the database may be
the very thing that failed. Every handler is wrapped, every exception is logged
and swallowed.

The redaction below is defensive rather than necessary today. Every task in this
codebase takes scalar ids (``user_id``, ``posting_id``, a ``history_id``
string), so nothing sensitive currently reaches these columns. It is here so
that the first task to take a token does not quietly start writing it to a table
the admin UI renders — and, because a rewritten argument would be replayed as
the rewritten version, redacting anything also clears
:attr:`~app.models.dead_letter.DeadLetterJob.replayable`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from celery.signals import task_failure, task_revoked
from sqlalchemy import select

from app.models.dead_letter import (
    REASON_FAILED,
    REASON_REVOKED,
    STATUS_NEW,
    DeadLetterJob,
)

logger = logging.getLogger(__name__)

#: A kwarg whose name contains one of these has its value replaced. Matched as a
#: substring on the lowercased key, so ``refresh_token`` and ``API_KEY`` both go.
_SECRET_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "private",
    "authorization",
    "cookie",
    "signature",
)

_REDACTED = "[redacted]"

#: Per-value and whole-document caps. A task argument that needs more than this
#: is not an id, which is the only thing tasks here take.
_MAX_VALUE_CHARS = 500
_MAX_DOC_CHARS = 4000

#: Runs of digits and long hex blobs are collapsed before an exception message
#: becomes part of a fingerprint, so "connection refused to 10.0.0.5:6379" and
#: the same message about a different port stay one row rather than two.
_DIGITS = re.compile(r"\d+")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)


class _Redactor:
    """Sanitises task arguments, remembering whether it had to change anything."""

    def __init__(self) -> None:
        self.modified = False

    def value(self, value: Any, *, key: str | None = None) -> Any:
        if key and any(hint in key.lower() for hint in _SECRET_HINTS):
            self.modified = True
            return _REDACTED
        if isinstance(value, str):
            if len(value) > _MAX_VALUE_CHARS:
                self.modified = True
                return value[:_MAX_VALUE_CHARS] + "…"
            return value
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): self.value(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        # A datetime, a model instance, anything else: keep a bounded repr. Not
        # replayable, because `repr` does not round-trip back to the object.
        self.modified = True
        return self.value(repr(value))


def _dump(payload: Any, redactor: _Redactor) -> str | None:
    """*payload* as JSON, or ``None`` if it cannot be represented at all."""
    try:
        text = json.dumps(redactor.value(payload), default=str, sort_keys=True)
    except Exception:  # pragma: no cover - json.dumps with default= is total
        redactor.modified = True
        return None
    if len(text) > _MAX_DOC_CHARS:
        redactor.modified = True
        return text[:_MAX_DOC_CHARS] + "…"
    return text


def _normalise(message: str) -> str:
    """An exception message with the parts that vary per-occurrence removed."""
    return _DIGITS.sub("#", _HEX.sub("#", message))[:300]


def fingerprint(
    task_name: str, args_json: str | None, kwargs_json: str | None, exc_type: str,
    message: str,
) -> str:
    """A stable key for "this same failure, again".

    Arguments are included so that two users failing the same way stay two rows:
    they are replayed independently, so collapsing them would make one of the
    two un-runnable.
    """
    digest = hashlib.sha256()
    for part in (task_name, args_json or "", kwargs_json or "", exc_type,
                 _normalise(message)):
        digest.update(part.encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()[:64]


def _extract_user_id(args: Any, kwargs: Any) -> int | None:
    """The task's ``user_id`` kwarg, when it has one and it looks like an id."""
    if isinstance(kwargs, dict):
        candidate = kwargs.get("user_id")
        if isinstance(candidate, bool):
            return None
        if isinstance(candidate, int):
            return candidate
    return None


def record(
    *,
    task_name: str,
    task_id: str | None = None,
    args: Any = None,
    kwargs: Any = None,
    exc: BaseException | None = None,
    traceback_text: str | None = None,
    retries: int = 0,
    queue: str | None = None,
    reason: str = REASON_FAILED,
    session_factory: Any = None,
    now: datetime | None = None,
) -> DeadLetterJob | None:
    """Write (or collapse into) the dead-letter row for one finished failure.

    Returns the row for the benefit of tests; callers on the signal path ignore
    it. Opens and owns its own session: the failing task's session is, by the
    time this runs, of unknown health.
    """
    if session_factory is None:
        from app.core.database import SessionLocal

        session_factory = SessionLocal

    moment = now or datetime.now(UTC)
    redactor = _Redactor()
    args_json = _dump(list(args) if isinstance(args, (list, tuple)) else args, redactor)
    kwargs_json = _dump(kwargs if isinstance(kwargs, dict) else None, redactor)

    exc_type = type(exc).__name__ if exc is not None else ""
    message = str(exc) if exc is not None else ""
    key = fingerprint(task_name, args_json, kwargs_json, exc_type, message)

    session = session_factory()
    try:
        existing = session.scalar(
            select(DeadLetterJob)
            .where(
                DeadLetterJob.fingerprint == key,
                DeadLetterJob.status == STATUS_NEW,
            )
            .order_by(DeadLetterJob.id.desc())
        )
        if existing is not None:
            # Same failure, again. Keep the first sighting, refresh everything
            # that describes the latest one.
            existing.occurrences += 1
            existing.last_failed_at = moment
            existing.task_id = task_id
            existing.retries = retries
            existing.traceback = _truncate(traceback_text)
            existing.exception_message = _truncate(message)
            session.commit()
            return existing

        row = DeadLetterJob(
            task_name=task_name,
            task_id=task_id,
            queue=queue,
            user_id=_extract_user_id(args, kwargs),
            args_json=args_json,
            kwargs_json=kwargs_json,
            reason=reason,
            exception_type=exc_type[:255] or None,
            exception_message=_truncate(message),
            traceback=_truncate(traceback_text),
            retries=retries,
            fingerprint=key,
            occurrences=1,
            first_failed_at=moment,
            last_failed_at=moment,
            replayable=not redactor.modified,
            status=STATUS_NEW,
        )
        session.add(row)
        session.commit()
        return row
    finally:
        session.close()


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= _MAX_DOC_CHARS else text[:_MAX_DOC_CHARS] + "…"


def _guard(what: str):
    """Run *fn*, log anything it raises, and never let it out.

    See the module docstring: the caller is a Celery signal dispatched from the
    worker's failure path, and an exception raised here would replace the
    failure being recorded with one from the recorder.
    """

    def decorate(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception:
                logger.exception("dead-letter capture failed (%s)", what)
                return None

        wrapper.__name__ = getattr(fn, "__name__", "wrapper")
        return wrapper

    return decorate


def _queue_of(sender: Any) -> str | None:
    delivery = getattr(getattr(sender, "request", None), "delivery_info", None)
    if isinstance(delivery, dict):
        value = delivery.get("routing_key")
        return str(value)[:64] if value else None
    return None


@task_failure.connect
@_guard("task_failure")
def _on_task_failure(
    sender: Any = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    args: Any = None,
    kwargs: Any = None,
    einfo: Any = None,
    **_: Any,
) -> None:
    task_name = getattr(sender, "name", None) or "<unknown>"
    request = getattr(sender, "request", None)
    record(
        task_name=task_name,
        task_id=task_id,
        args=args,
        kwargs=kwargs,
        exc=exception,
        traceback_text=str(einfo) if einfo is not None else None,
        retries=int(getattr(request, "retries", 0) or 0),
        queue=_queue_of(sender),
        reason=REASON_FAILED,
    )
    logger.warning(
        "dead-lettered %s (%s): %s", task_name, task_id, type(exception).__name__
    )


@task_revoked.connect
@_guard("task_revoked")
def _on_task_revoked(
    sender: Any = None,
    request: Any = None,
    terminated: bool = False,
    signum: Any = None,
    expired: bool = False,
    **_: Any,
) -> None:
    """A task killed from outside — ``worker_lost``, a terminate, an expiry.

    Expiries are *not* recorded. Several beat entries set ``expires`` on purpose
    (see :mod:`app.tasks.celery_app`): a scan that sat in the queue longer than
    the gap to the next one is meant to be dropped, and recording each of those
    as a dead letter would fill the table with the system working correctly.
    """
    if expired:
        return
    task_name = getattr(request, "task", None) or getattr(sender, "name", None) or "<unknown>"
    record(
        task_name=str(task_name),
        task_id=getattr(request, "id", None),
        args=getattr(request, "args", None),
        kwargs=getattr(request, "kwargs", None),
        exc=None,
        traceback_text=f"revoked (terminated={bool(terminated)}, signum={signum})",
        retries=0,
        reason=REASON_REVOKED,
    )
    logger.warning("dead-lettered revoked task %s", task_name)


__all__ = ["fingerprint", "record"]
