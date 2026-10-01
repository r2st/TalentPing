"""Structured logging: one JSON object per line, correlated by request.

Every module in this app already logs. Nothing was ever *configured* to
receive it, which is a quieter problem than it sounds: with no handler on the
root logger, Python installs a last-resort handler that emits at WARNING and
drops the level and the logger name. So ``logger.info("gmail webhook: ...")``
— the line you go looking for when a webhook misfires — was never reaching
production output at all, and the warnings that did arrive were bare strings
with no way to tell which request produced them.

Three pieces fix that:

``configure_logging()``
    Installs a single stdout handler on the root logger. JSON in production
    so the fields survive a log aggregator; plain text in development, where
    a human is reading it.

``RequestContextMiddleware``
    Stamps every request with an id, exposes it to every log record emitted
    while that request is on the stack, and returns it in a response header
    so a user reporting a failure can hand you the exact string to grep.

``bind_log_context()``
    Attaches ambient fields — the task, the campaign, whatever the boundary
    knows and the call site does not — to every record emitted beneath it,
    without changing a single logging call.

The correlation runs through a ``ContextVar``, so it survives ``await`` and
follows the request down through the service layer without a function
signature changing to pass it along.

Across the *process* boundary it needs help, and gets it in
:mod:`app.tasks.observability`: the request id is written onto the Celery
message when it is published and read back when the task runs, so a request id
quoted by a user reaches the worker that did the work minutes later. That
module is also what gives the worker this handler at all — without it Celery
installs its own, and the half of the product that sends the mail logs in a
different shape from the half that answers ``GET``.

The events worth writing down on purpose, rather than as a side effect of
something failing, are in :mod:`app.core.events`.
"""
from __future__ import annotations

import json
import logging
import string
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from app.core.pii import PIIRedactionFilter, mask_field

# Set per request by the middleware, read by the formatter. Defaults to None so
# a record emitted outside a request (Celery task, startup, shell) formats
# cleanly rather than raising.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

#: Ambient fields merged into every record emitted while they are bound.
#:
#: The request id answers "which call was this?", which is the whole story for
#: an API process and about half of it for a worker: a line reading "sending
#: email 4012 failed unexpectedly" is only actionable once you also know which
#: task and which mailbox it came from. Those belong on the record, not in the
#: sentence, and the call site that knows them is rarely the one that logs.
#:
#: So the context is set once at the boundary — :func:`bind_log_context` — and
#: every record beneath it carries the fields without a single logging call
#: being changed. Defaults to ``None`` rather than ``{}`` so no caller can
#: mutate a shared default into a leak between tasks.
log_context_var: ContextVar[dict[str, Any] | None] = ContextVar(
    "log_context", default=None
)

#: Fields discovered *during* a request and wanted on every line of it.
#:
#: Separate from :data:`log_context_var`, and mutable where that one is
#: replaced, because of where the fields come from. The request id is known at
#: the middleware; the *user* is not known until an authentication dependency
#: has run, and in FastAPI a ``def`` dependency runs in the threadpool via
#: ``run_in_threadpool``. That copies the context into the worker thread, so a
#: ``ContextVar.set`` inside such a dependency is discarded when it returns —
#: which is every authentication dependency in this app, and the reason the
#: obvious version of this does nothing at all.
#:
#: A copied context still shares the *objects* it holds. So the middleware puts
#: one fresh dict here per request and :func:`bind_request_fields` mutates it,
#: which the threadpool copy and the request coroutine both see because it is
#: the same dict. The per-request freshness is what makes mutation safe: two
#: concurrent requests never hold the same one, so there is no sibling to leak
#: into — the hazard that makes :func:`bind_log_context` replace instead.
request_fields_var: ContextVar[dict[str, Any] | None] = ContextVar(
    "request_fields", default=None
)

REQUEST_ID_HEADER = "X-Request-ID"

# Attributes the stdlib puts on every LogRecord. Anything *not* in here arrived
# via `logger.info("...", extra={...})` and is a field worth emitting, which is
# what makes structured logging structured: call sites add context by name
# instead of interpolating it into a sentence nobody can query.
_STANDARD_RECORD_FIELDS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object.

    One line per record, because every log shipper in existence splits on
    newlines and a pretty-printed exception would arrive as forty unrelated
    events.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        if record.exc_text or record.exc_info:
            # ``exc_text`` first: it is the stdlib's cache of the rendered
            # traceback, and :class:`app.core.pii.PIIRedactionFilter` fills it
            # in deliberately so the frames it quotes have been through the
            # masking. Re-rendering from ``exc_info`` here would put the
            # unmasked original back.
            payload["exception"] = record.exc_text or self.formatException(
                record.exc_info
            )
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in _record_fields(record).items():
            # A field must never quietly redefine the four an aggregator indexes
            # on — `extra={"level": "DEBUG"}` at one call site would otherwise
            # make that record unfindable by severity. Keep the value, under a
            # name that cannot collide.
            if key in payload:
                key = f"extra_{key}"
            payload[key] = value if _is_jsonable(value) else repr(value)

        # `default=str` is a backstop, not the main path: the per-field check
        # above has already replaced anything exotic. Without it a single
        # unserialisable extra would raise *inside* logging and lose the record.
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable lines for development, with the request id kept.

    Same information, arranged for a person rather than a parser.
    """

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = _record_fields(record)
        if fields:
            # Appended as `k=v` rather than interpolated into the message: the
            # same fields the JSON formatter emits, in the one shape a terminal
            # can show them.
            line += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        request_id = request_id_var.get()
        return f"{line} [{request_id}]" if request_id else line


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Everything this record carries beyond the four fixed fields.

    Three layers, least specific first: fields bound to the whole request
    (:func:`bind_request_fields`), then ambient context
    (:func:`bind_log_context`), then the record's own extras. The call site is
    the most specific of the three, so a task that binds ``email_id`` at the
    boundary and logs ``extra={"email_id": ...}`` deeper down should report the
    deeper one rather than two fields disagreeing about the same name.

    Shared by both formatters so a line read in development shows exactly the
    fields the aggregator will index in production. Kept separate, the text
    formatter dropped extras entirely — which made every structured event
    (:mod:`app.core.events`) print as a bare name with all of its content
    missing, and made "it looked fine locally" a property of the formatter.
    """
    # Masked here, and only for the two ambient layers.
    #
    # ``PIIRedactionFilter`` runs on the handler, which is *before* formatting,
    # and it masks what it can reach: the message and ``record.__dict__``. The
    # ambient layers are not on the record — they are read out of their context
    # variables at this moment, several steps after the filter has finished —
    # so anything bound through :func:`bind_log_context` or
    # :func:`bind_request_fields` reached the log unmasked while the identical
    # value passed as ``extra={...}`` was masked. A `mailbox` bound once at a
    # task boundary is the whole point of those functions, which made the gap
    # bigger the more the mechanism was used.
    #
    # The record's own extras are deliberately *not* re-masked below: the
    # filter has already done them, and masking a masked value again is work
    # that can only lose information.
    ambient: dict[str, Any] = dict(request_fields_var.get() or {})
    ambient.update(log_context_var.get() or {})
    fields: dict[str, Any] = {
        key: mask_field(key, value) for key, value in ambient.items()
    }
    for key, value in record.__dict__.items():
        if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
            continue
        fields[key] = value
    return fields


def _is_jsonable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), list, dict, tuple))


def bind_request_fields(**fields: Any) -> None:
    """Add *fields* to every log record emitted for the rest of this request.

    The counterpart to :func:`bind_log_context` for the API process, and the
    reason it exists separately is written up on :data:`request_fields_var`:
    this one survives being called from a threadpooled ``def`` dependency,
    and that one does not.

    Used at the authentication boundary, so ``user_id`` is on the access line,
    on anything the handler logs, and on the traceback of a 500 — without a
    single call site naming it. Which user a failure belongs to is the first
    question asked of a per-account outage, and it was previously answerable
    only by matching the request id back through the database.

    A no-op outside a request, so a service function that calls it is equally
    safe to call from a Celery task or a shell.
    """
    current = request_fields_var.get()
    if current is None:
        return
    current.update({k: v for k, v in fields.items() if v is not None})


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    """Attach *fields* to every record emitted inside the block.

    Nests: an inner bind adds to the outer one rather than replacing it, and
    leaving the block restores exactly what was there before. That is the
    property a worker needs — the task boundary binds the task name, a service
    beneath it binds the campaign, and the campaign disappears at the right
    moment without the task name going with it.

    A brand-new dict every time, never a mutation of the one already bound:
    ``ContextVar`` copies the *reference* into child contexts, so mutating in
    place would write the current task's fields into a sibling that had already
    copied it.
    """
    token = push_log_context(**fields)
    try:
        yield
    finally:
        pop_log_context(token)


def push_log_context(**fields: Any) -> Token[dict[str, Any] | None]:
    """The two halves of :func:`bind_log_context`, for callers that cannot use
    a ``with`` block.

    Celery's ``task_prerun`` and ``task_postrun`` are two separate callbacks
    with the task's whole body between them, so the bind and the release cannot
    share a stack frame. They share the token instead.
    """
    current = log_context_var.get() or {}
    return log_context_var.set({**current, **fields})


def pop_log_context(token: Token[dict[str, Any] | None]) -> None:
    try:
        log_context_var.reset(token)
    except ValueError:
        # The token belongs to a different context — which happens when the
        # bind and the release genuinely ran in different ones, e.g. a task
        # whose prerun and postrun were dispatched across a pool boundary.
        # Clearing is the honest recovery: better an empty context than one
        # task's fields leaking onto the next task's log lines.
        log_context_var.set(None)


def configure_logging(
    level: str | int = "INFO",
    fmt: str = "json",
    *,
    force: bool = True,
) -> None:
    """Point the root logger at stdout with one formatter. Idempotent.

    Idempotence matters more than it looks: uvicorn's reloader and the Celery
    worker both import the app more than once per process, and a handler
    appended each time is every line duplicated that many times.
    """
    root = logging.getLogger()

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            # Only close handlers we own; closing a pytest/uvicorn handler we
            # merely detached would break the owner still holding a reference.
            if getattr(handler, "_talentping", False):
                handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    # The last thing between this application's personal data and a log
    # aggregator with a different retention policy. On the handler rather than
    # on the root logger so it also covers records that propagate up from
    # libraries — googleapiclient quotes the mailbox it failed against, and
    # nothing in this codebase writes that line.
    handler.addFilter(PIIRedactionFilter())
    handler._talentping = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(_coerce_level(level))

    # uvicorn installs its own handlers on these; left alone, every access line
    # is emitted twice — once by uvicorn's handler and once by ours via
    # propagation. Ours is the one carrying the request id, so uvicorn's go.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True

    # SQLAlchemy at INFO echoes every statement, including parameters. That is
    # both unreadable and a way to write user data to disk by accident.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    # An unparseable level is a typo in an env var, which should not stop the
    # process from starting — or, worse, from logging.
    return logging.getLevelNamesMapping().get(str(level).upper(), logging.INFO)


class RequestContextMiddleware:
    """Assign a request id, log the outcome, return the id to the caller.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` because the latter
    runs the downstream app in a separate task, which makes reasoning about
    which context a ``ContextVar`` was set in unnecessarily subtle. Here the
    set and the call happen in one coroutine, so the value is unambiguously
    visible to everything below.
    """

    def __init__(
        self,
        app: Callable,
        *,
        header: str = REQUEST_ID_HEADER,
        quiet_paths: Iterable[str] = ("/api/v1/health", "/health"),
    ) -> None:
        self.app = app
        self.header = header
        self.header_bytes = header.lower().encode()
        # Health checks run every few seconds forever. Logging them buries the
        # requests someone actually wants to find.
        self.quiet_paths = frozenset(quiet_paths)
        self.logger = logging.getLogger("app.request")

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header_value(scope.get("headers") or [], self.header_bytes)
        # Honour an upstream id so a trace spans the proxy, but never trust its
        # shape — this string ends up in every log line for the request, and an
        # unbounded caller-controlled value is a log-injection primitive.
        request_id = _sanitize_request_id(incoming) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        # One fresh dict per request. Everything discovered later — the user,
        # above all — is mutated into this rather than re-`set`, because the
        # dependencies that discover it run in a threadpool where a `set` is
        # thrown away. See :data:`request_fields_var`.
        fields_token = request_fields_var.set({})

        path = scope.get("path", "")
        method = scope.get("method", "")
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((self.header_bytes, request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Log it here — where the request id, path and timing are still in
            # hand — then re-raise so the error still reaches the ASGI server
            # and the client still gets its 500.
            self.logger.exception(
                "request failed",
                extra={
                    "http_method": method,
                    "http_path": path,
                    "duration_ms": _elapsed_ms(started),
                },
            )
            raise
        else:
            if path not in self.quiet_paths:
                self.logger.log(
                    logging.WARNING if status_code >= 500 else logging.INFO,
                    "%s %s %s",
                    method,
                    path,
                    status_code,
                    extra={
                        "http_method": method,
                        # Path only, never the query string: the Gmail OAuth
                        # callback arrives as `?code=<grant>`, and an access log
                        # is the last place that should be written down.
                        "http_path": path,
                        "http_status": status_code,
                        "duration_ms": _elapsed_ms(started),
                    },
                )
        finally:
            request_id_var.reset(token)
            request_fields_var.reset(fields_token)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _header_value(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


# Long enough for a UUID hex or a trace id, short enough not to be a payload.
_MAX_REQUEST_ID = 64

#: The whole alphabet an upstream id may be spelt in. Written out rather than
#: tested with ``str.isalnum()``, which is Unicode-aware: "日本", "①" and "café"
#: are all alnum to Python, and none of them is a trace id.
#:
#: Being let through mattered because the two ends of this round trip do not
#: agree on an encoding. ``_header_value`` decodes the incoming header as
#: latin-1 (which is what the header grammar allows) and ``send_wrapper``
#: encodes the id back out as UTF-8, so a caller's ``\xe9`` came back as
#: ``\xc3\xa9`` — a *different id* in the response header from the one the
#: proxy set, on the one header whose entire job is to be the same on both
#: sides. The trace it exists to join is broken precisely for the requests that
#: carried one. Restricted to ASCII the two encodings cannot disagree, so the
#: id echoed is byte-for-byte the id received.
_REQUEST_ID_CHARS = frozenset(string.ascii_letters + string.digits + "-_.")


def _sanitize_request_id(value: str | None) -> str | None:
    """Keep an upstream id only if it is boring: safe characters, bounded length.

    A newline here would let a caller forge whole log lines; anything longer
    than a trace id is not one; anything outside :data:`_REQUEST_ID_CHARS` is
    not an id we can echo back unchanged.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_REQUEST_ID:
        return None
    if not _REQUEST_ID_CHARS.issuperset(candidate):
        return None
    return candidate
