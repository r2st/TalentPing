"""Worker-side logging: the same stream as the API, correlated to the request.

:mod:`app.core.logging` is wired into ``create_app``, so the API process emits
one JSON object per line with a request id on every record. The worker had none
of it. Nothing here called ``configure_logging``, so Celery installed its own
handler with its own format, and the half of this product that actually does
things — sending outreach, scanning mailboxes, drafting replies, running the
autopilot — wrote plain text into a different shape from everything else. A log
aggregator given both saw a structured stream from the process that mostly
answers ``GET`` and an unparseable one from the process that sends the mail.

Three signals close that:

``setup_logging``
    Connecting to it at all is the documented way to stop Celery configuring
    logging itself. We install the app's own handler instead, so a worker line
    and an API line are the same kind of object.

``before_task_publish`` / ``task_prerun``
    Carry the request id across the process boundary. The module docstring in
    :mod:`app.core.logging` claims the correlation "follows the request across
    the service and task layers" — true within a process, and previously false
    at exactly the boundary where it matters most. A user reports that pressing
    *Launch* did nothing; the campaign route returns 202 and the work happens in
    a worker minutes later. Without propagation the request id they quote
    matches the one line that says the task was published, and nothing about
    what it went on to do.

``task_prerun`` / ``task_postrun``
    Bracket every task with a start and an outcome, so "did the scan run?" is a
    question the log can answer. It could not before: a task that returned
    normally logged nothing whatsoever, and a beat schedule that had silently
    stopped firing looked identical to one that was running and finding nothing.
    Both of the outages in :mod:`app.services.inbound_scanner`'s history were
    diagnosed from task return values recovered by hand from the journal — this
    writes them down as fields on the way past.

Every handler is wrapped so a fault in observability cannot fail the work being
observed. A logging bug that killed a send would be a strictly worse trade than
the missing log line it was added to fix.
"""
from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from celery.signals import (
    before_task_publish,
    setup_logging,
    task_postrun,
    task_prerun,
)

from app.core.config import settings
from app.core.logging import (
    configure_logging,
    pop_log_context,
    push_log_context,
    request_id_var,
)

logger = logging.getLogger("app.task")

#: Message-header name the request id travels under.
#:
#: Prefixed because protocol-2 headers share a namespace with Celery's own
#: (``id``, ``task``, ``retries``, ``eta``…), and a collision would either be
#: silently dropped or corrupt the field Celery routes on.
REQUEST_ID_HEADER = "talentping_request_id"

#: Per-task bookkeeping between ``prerun`` and ``postrun``, keyed by task id.
#:
#: Not a ``ContextVar``: the two callbacks are separate dispatches, and the
#: token that releases the context is precisely the thing that cannot live in
#: the context it releases.
_in_flight: dict[str, tuple[Any, Any, float]] = {}

#: How much of a task's return value is worth writing down. Task results are
#: summary dicts by convention here (``{"sent": 3, "skipped": 1}``), which is
#: the single most useful thing to have in the log — but nothing *enforces*
#: that convention, and a task returning a parsed job description would put a
#: page of prose on one line.
_MAX_RESULT_FIELDS = 12
_MAX_RESULT_VALUE = 200


def _guard(what: str):
    """Never let an observability fault take down the task it observes.

    The same reasoning as :func:`app.tasks.dead_letter._guard`: these run on
    Celery's signal dispatch, where an exception propagates into the worker's
    own bookkeeping rather than into anything that would retry it.
    """

    def decorate(fn):
        def wrapper(*a: Any, **kw: Any) -> None:
            try:
                fn(*a, **kw)
            except Exception:  # noqa: BLE001 - see the docstring
                logging.getLogger(__name__).warning(
                    "task observability hook %s failed", what, exc_info=True
                )

        wrapper.__name__ = fn.__name__
        return wrapper

    return decorate


@setup_logging.connect
def _configure_worker_logging(**_kwargs: Any) -> None:
    """Give the worker the API's handler.

    Connecting to this signal is what suppresses Celery's own configuration;
    the body then has to do the whole job, because nothing else will.
    """
    configure_logging(level=settings.log_level, fmt=settings.resolved_log_format)


@before_task_publish.connect
@_guard("before_task_publish")
def _propagate_request_id(headers: dict | None = None, **_kwargs: Any) -> None:
    """Stamp the publishing request's id onto the message.

    ``setdefault`` rather than assignment: a task that re-publishes another
    (a retry, a chained follow-up) already carries the id of the request that
    began the chain, and that is the one worth keeping. Overwriting it would
    reset the trace at every hop and leave the interesting end of it — the
    original click — unreachable.
    """
    request_id = request_id_var.get()
    if request_id and isinstance(headers, dict):
        headers.setdefault(REQUEST_ID_HEADER, request_id)


@task_prerun.connect
@_guard("task_prerun")
def _begin_task(
    task_id: str | None = None,
    task: Any = None,
    args: Any = None,
    kwargs: Any = None,
    **_kwargs: Any,
) -> None:
    request = getattr(task, "request", None)
    inherited = getattr(request, REQUEST_ID_HEADER, None)
    # Fall back to the task id so a record emitted by beat-scheduled work — which
    # no request ever published — is still correlated to *something*. A periodic
    # scan has no user behind it, but its lines still need to be separable from
    # the other eleven tasks the worker ran in the same second.
    correlation = _clean(inherited) or (task_id or "")
    id_token = request_id_var.set(correlation or None)

    name = getattr(task, "name", None) or "unknown"
    context_token = push_log_context(
        task=name, task_id=task_id, **_correlating_arguments(task, args, kwargs)
    )
    if task_id:
        _in_flight[task_id] = (id_token, context_token, time.perf_counter())

    logger.info("task started", extra={"task_event": "started"})


@task_postrun.connect
@_guard("task_postrun")
def _end_task(
    task_id: str | None = None,
    task: Any = None,
    retval: Any = None,
    state: str | None = None,
    **_kwargs: Any,
) -> None:
    id_token, context_token, started = (
        _in_flight.pop(task_id, (None, None, None)) if task_id else (None, None, None)
    )

    fields: dict[str, Any] = {"task_event": "finished", "task_state": state}
    if started is not None:
        fields["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    fields.update(_result_fields(retval))

    # WARNING for anything that did not succeed. A failed task also reaches the
    # dead-letter table, but only once it is out of retries; this is the line
    # that shows the attempts in between, which is where a task failing every
    # time for an hour is visible before it becomes a permanent record.
    logger.log(
        logging.INFO if state == "SUCCESS" else logging.WARNING,
        "task finished",
        extra=fields,
    )

    if context_token is not None:
        pop_log_context(context_token)
    if id_token is not None:
        try:
            request_id_var.reset(id_token)
        except ValueError:
            request_id_var.set(None)


def _result_fields(retval: Any) -> dict[str, Any]:
    """Flatten a task's summary dict onto the record, under a safe prefix.

    Prefixed with ``result_`` because a task returning ``{"task": "..."}`` or
    ``{"level": ...}`` would otherwise be renamed by the formatter's collision
    rule and land under a name nobody would think to search for.

    Only scalars, and only a handful: this is a summary line, not a transcript.
    A nested structure is the shape a task returns when it is handing back data
    rather than reporting an outcome, and that data belongs in the database it
    was already written to.
    """
    if not isinstance(retval, dict):
        return {}
    fields: dict[str, Any] = {}
    for key, value in list(retval.items())[:_MAX_RESULT_FIELDS]:
        if isinstance(value, (int, float, bool)) or value is None:
            fields[f"result_{key}"] = value
        elif isinstance(value, str):
            fields[f"result_{key}"] = value[:_MAX_RESULT_VALUE]
    return fields


def _clean(value: Any) -> str | None:
    """Accept an inherited id only if it is one.

    The header arrives from the broker, which means it arrives from whatever
    published the message. It is written into every subsequent log line, so it
    gets the same treatment as the HTTP header it came from — see
    :func:`app.core.logging._sanitize_request_id`, whose alphabet this reuses
    rather than inventing a second, looser one.
    """
    from app.core.logging import _sanitize_request_id

    return _sanitize_request_id(value) if isinstance(value, str) else None


#: Argument names that identify *what* a task is working on.
#:
#: Every task in this package already names its subject in the same handful of
#: ways (``email_id``, ``user_id``, ``campaign_id``…), which is what makes
#: reading them at the boundary possible at all. Bound as fields so a line
#: emitted six frames deep — ``"could not read back Message-ID"`` — is
#: attributable without the call site having been changed to say so, which is
#: the job :func:`app.core.logging.bind_log_context` was written for and had
#: exactly one caller doing.
#:
#: A closed list rather than "every int argument": a task's other arguments are
#: page sizes, limits and windows, and binding those would put three
#: meaningless fields on every record to catch the one that matters.
_CORRELATING_ARGUMENTS = frozenset(
    {
        "account_id",
        "application_id",
        "campaign_id",
        "email_id",
        "recruiter_id",
        "row_id",
        "search_id",
        "thread_id",
        "user_id",
    }
)

#: Ids are ints here, and a task handed something else for one is a bug worth
#: seeing rather than a field worth binding. Strings are allowed but clipped —
#: a few tasks take a Gmail message id, which is short, and nothing stops a
#: caller passing something that is not.
_MAX_ARGUMENT_VALUE = 100


def _correlating_arguments(task: Any, args: Any, kwargs: Any) -> dict[str, Any]:
    """The subject of this task, read off its call.

    Positional arguments have to be resolved through the signature, because
    that is how nearly every task in this package is actually invoked —
    ``send_outreach_email.delay(email_id)``, not ``.delay(email_id=...)``. A
    kwargs-only version of this looked correct and bound nothing on the paths
    that matter.

    ``task.run`` rather than ``task``: for a ``bind=True`` task the callable is
    a method and ``self`` is already bound out of the signature, so the
    positional arguments line up with what the caller actually passed.
    """
    values: dict[str, Any] = {}
    run = getattr(task, "run", None)
    try:
        signature = inspect.signature(run)
        bound = signature.bind_partial(*(args or ()), **(kwargs or {}))
    except (AttributeError, TypeError, ValueError):
        # Three ways this legitimately fails, and the same answer to all of
        # them: fall back to the keywords, which need no signature to read.
        #
        # A signature that will not bind is a task about to fail on its own
        # terms, and observing it must not be what raises first. An object with
        # no usable ``run`` is not a Celery task at all — which happens in
        # tests, and would happen again if Celery ever changed the shape.
        #
        # Not left to :func:`_guard`, even though it would catch this: the
        # guard aborts the *whole* handler, so an unfamiliar task shape would
        # take ``task`` and ``task_id`` down with it and leave every line the
        # task emits uncorrelated. Degrading here costs the subject only.
        bound_arguments = dict(kwargs or {})
    else:
        bound_arguments = dict(bound.arguments)

    for key, value in bound_arguments.items():
        if key not in _CORRELATING_ARGUMENTS:
            continue
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            values[key] = value
        elif isinstance(value, str):
            values[key] = value[:_MAX_ARGUMENT_VALUE]
    return values
