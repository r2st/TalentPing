"""Personal data, and the two places it must not end up.

This product's whole subject matter is personal: a resume is a career history,
an inbox is a correspondence, and the address at the top of both identifies a
named human being. Storing that is the job. The failure mode is not storing it
— it is *copying* it into the two systems that were never scoped to hold it:

**The log stream.** Logs leave the application. They go to a file on the VPS,
to whatever aggregator is pointed at stdout, and into the terminal of anyone
running ``journalctl``. Those places have a different retention policy from the
database (usually "forever"), a different access list (usually "wider"), and no
delete path at all — so a line reading ``gmail history fetch failed for
alice@example.com`` survives the account it names being deleted, and survives it
in a system nobody thinks to look in when honouring the deletion.

**An error response.** A 500 body is read by a browser, pasted into a support
ticket, and screenshotted. :mod:`app.core.errors` already replaces the
unhandled case with a constant string, but a *handled* ``except Exception as
exc`` that interpolates ``exc`` into ``detail`` walks straight past it — and a
SQLAlchemy exception stringifies to the failing statement plus its bound
parameters, which for this schema means message bodies and addresses.

So this module gives both problems the same two answers.

:func:`mask_email` and :func:`mask_name` are what a *call site* should use. A
masked address keeps everything an operator reads a log line for — which
mailbox, which provider, is it the same one as last time — and drops the part
that identifies a person. ``alice@example.com`` becomes ``a***e@example.com``:
still greppable against itself, still tells you it is Gmail or not, no longer a
contact record.

:class:`PIIRedactionFilter` is the net under that. Call-site discipline is a
thing you have to keep doing, and the failure is silent: nobody notices the log
line that leaked, because leaking looks exactly like working. The filter runs
on every record on the way out and rewrites anything shaped like an address,
whether it came from a call site that forgot, from ``str(exc)`` on a Gmail
error quoting the recipient, or from a traceback frame's local. It cannot mask
what it cannot recognise — a bare full name is not a detectable shape — which
is why it is the second line and not the first.

:func:`scrub` is the same redaction for a string that is about to become an
HTTP response, and :func:`safe_reason` is what to use instead of ``str(exc)``
when an ``except Exception`` has to say *something* to the client.

What is deliberately not here: encryption, hashing, or tokenisation of the
stored data. The database holds this data because the product cannot work
without it, and :mod:`app.services.crypto` already covers the one class —
OAuth tokens — where storing the plaintext is indefensible. Masking is about
copies, not about the record of origin.
"""
from __future__ import annotations

import logging
import re
from contextlib import suppress
from typing import Any

#: Every field name whose value is personal data, as it appears in this
#: codebase's ``extra={...}`` payloads and task kwargs. Matched as a substring
#: on the lowercased key by :func:`mask_field`, so ``to_email``,
#: ``account_email`` and ``EMAIL`` all resolve.
#:
#: ``_id`` suffixed names are *not* here on purpose: ``email_id`` is a row id
#: and the single most useful field in the whole log stream. The check below
#: excludes them explicitly rather than relying on the substring list being
#: careful, because "email" is a substring of "email_id".
_PII_KEY_HINTS = (
    "email",
    "address",
    "recipient",
    "sender",
    "from_addr",
    "to_addr",
    "mailbox",
    "full_name",
    "candidate_name",
    "contact_name",
    "phone",
    "linkedin_url",
)

#: Keys that contain a hint but are not personal data. An id is a number and a
#: domain is already the redacted form — re-masking either loses the field's
#: entire value for nothing.
_PII_KEY_EXEMPT_SUFFIXES = ("_id", "_ids", "_count", "_domain", "_at")

#: Deliberately looser than a validating address grammar. This runs on text
#: nobody is checking — an exception message, a provider error body — where the
#: cost of missing an address is a leak and the cost of masking something that
#: merely looks like one is a slightly odd log line. So: any run of
#: address-legal characters, an ``@``, a dotted host.
#:
#: The lookbehind is what makes masking *idempotent*, which is load-bearing
#: rather than tidy: :class:`PIIRedactionFilter` is attached per handler, so a
#: record reaching two of them is scrubbed twice. Without it the second pass
#: reads ``a***e@example.com``, cannot match from the ``a`` (``*`` is not an
#: address character), and happily matches the *suffix* ``e@example.com`` — so
#: the mask degrades to ``a****@example.com`` and a third pass degrades it
#: again. Refusing to start a match immediately after an address character or a
#: mask character means a match is always a whole token or nothing.
_EMAIL_RE = re.compile(
    r"(?<![*A-Za-z0-9._%+\-@])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"
)

#: The replacement for a value too short to partially reveal. Showing the first
#: and last character of a three-character local part is disclosure, not
#: masking — the same reasoning as :func:`app.services.credential_store.mask`.
_OPAQUE = "***"


def mask_email(address: str | None) -> str | None:
    """An address with the person removed and the mailbox still identifiable.

    ``alice@example.com`` -> ``a***e@example.com``. The domain survives in full
    because every operational question about mail is domain-shaped — is Outlook
    deferring us, is this the Gmail grant or the SMTP one — and because a
    domain is not personal data. The local part is reduced to its first and
    last character, which is enough to tell two mailboxes on the same domain
    apart in a log without naming either.

    A local part of three characters or fewer is replaced outright: ``bo`` has
    no middle to hide.

    ``None`` in, ``None`` out, so a call site can pass an optional column
    without a conditional. A string that is not an address is returned masked
    whole rather than as-is — the caller believed it was one, and the safe
    reading of that disagreement is that it holds something personal.
    """
    if address is None:
        return None
    value = address.strip()
    if not value:
        return value
    if "@" not in value:
        return _OPAQUE
    local, _, domain = value.rpartition("@")
    if not local or not domain:
        return _OPAQUE
    return f"{_mask_span(local)}@{domain}"


def mask_name(name: str | None) -> str | None:
    """A person's name reduced to initials.

    ``Ada Lovelace`` -> ``A. L.``. Enough to correlate two lines about the same
    contact within one log stream, not enough to be a contact.
    """
    if name is None:
        return None
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return name.strip()
    return " ".join(f"{part[0]}." for part in parts)


def _mask_span(value: str) -> str:
    if len(value) <= 3:
        return _OPAQUE
    return f"{value[0]}{_OPAQUE}{value[-1]}"


def scrub(text: str | None) -> str | None:
    """Every email-shaped run in *text*, masked. Everything else untouched.

    For strings assembled somewhere else and about to be logged or returned:
    an exception message, a provider's error body, a scan result. The sentence
    around the address is what makes the line useful, so it is preserved.
    """
    if not text:
        return text
    return _EMAIL_RE.sub(lambda m: mask_email(m.group(0)) or _OPAQUE, text)


def mask_field(key: str, value: Any) -> Any:
    """*value*, masked if *key* names a field that holds personal data.

    The key-driven half of the protection, for structured fields where the
    value alone gives nothing away — a bare ``"Ada Lovelace"`` is not a
    detectable shape, but ``full_name="Ada Lovelace"`` says what it is.

    Recurses into containers, because a structured extra is routinely a dict —
    a scan summary, a task result — and an address one level down is exactly as
    exported as one at the top. The recursion re-keys on the *inner* name, so
    ``{"result": {"mailbox": ...}}`` is masked by ``mailbox`` rather than
    escaping because ``result`` means nothing.
    """
    if isinstance(value, dict):
        return {k: mask_field(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_field(key, v) for v in value]
    if not isinstance(value, str) or not value:
        return value
    lowered = key.lower()
    if lowered.endswith(_PII_KEY_EXEMPT_SUFFIXES):
        return value
    if not any(hint in lowered for hint in _PII_KEY_HINTS):
        # Not a declared PII field, but the value may still *contain* an
        # address — an error string, a subject line, a scan summary.
        return scrub(value)
    if "@" in value:
        return mask_email(value)
    if "name" in lowered:
        return mask_name(value)
    return _OPAQUE


# The generic sentence a client gets for a failure nobody wrote a message for.
# Same reasoning as `app.core.errors.GENERIC_DETAIL`: an exception message is
# written for a developer.
UNEXPECTED = "an unexpected error"


def safe_reason(exc: BaseException) -> str:
    """What a bare ``except Exception`` may tell the client.

    The exception *type*, never its message. A ``ValueError`` raised by this
    codebase says something a user can act on and is caught by name at the call
    sites that want it; a ``ProgrammingError`` from the driver stringifies to
    the statement, the column names and the bound parameters — which on this
    schema is a message body, an address, or a resume. There is no way to tell
    those apart at the point of catching, and the request id is already on the
    response and the full traceback already in the log, so naming the class is
    the most that can be said without guessing.
    """
    name = type(exc).__name__
    return f"{name} (see request id)" if name else UNEXPECTED


#: A bare formatter, used only for its ``formatException``. Instantiated once
#: because building one per record to render a traceback is measurable on the
#: error path, which is exactly where the process is already unwell.
_BASE_FORMATTER = logging.Formatter()


class PIIRedactionFilter(logging.Filter):
    """Mask personal data on every record leaving the process.

    A ``Filter`` rather than a ``Formatter`` because there are two formatters —
    JSON in production, text in development — and a redaction that lived in one
    of them would be a redaction that development could not see working. It is
    also why this cannot be done in the formatter's ``format``: the extras are
    read there, but a filter runs before *both*, on one path.

    Three things are rewritten:

    * the formatted message, after ``%``-interpolation, so an address that
      arrived as a ``%s`` argument is caught;
    * every ``extra={...}`` field, by name and by shape;
    * the exception message, which is where a Gmail or SMTP error puts the
      recipient it refused.

    Never raises. A filter that throws drops the record, and losing the log
    line is a strictly worse outcome than emitting it unmasked — the whole
    point of the module is that logs are how failures are found.
    """

    #: Record attributes the stdlib owns. Mirrors the formatter's list; kept
    #: here rather than imported to avoid a cycle between the two modules.
    _RESERVED = frozenset(
        {
            "args", "asctime", "created", "exc_info", "exc_text", "filename",
            "funcName", "levelname", "levelno", "lineno", "message", "module",
            "msecs", "msg", "name", "pathname", "process", "processName",
            "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        }
    )

    def filter(self, record: logging.LogRecord) -> bool:
        with suppress(Exception):  # see the class docstring
            self._redact(record)
        return True

    def _redact(self, record: logging.LogRecord) -> None:
        # Interpolate first, then mask, then drop the args: an address passed
        # as a `%s` argument is only visible once the two are combined, and
        # leaving `args` in place would let the formatter re-interpolate the
        # unmasked originals over the top of the masked message. Doing it
        # unconditionally also makes the filter idempotent, which matters
        # because it is attached per *handler* and a record reaching two of
        # them is filtered twice.
        record.msg = scrub(record.getMessage())
        record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in self._RESERVED or key.startswith("_"):
                continue
            record.__dict__[key] = mask_field(key, value)

        # ``exc_text`` is the stdlib's own cache of the rendered traceback, and
        # it is normally empty at filter time — the formatter fills it in
        # afterwards, well past anything that could mask it. Rendering it here
        # is what puts the traceback (whose frames quote the arguments that
        # caused the failure, addresses included) on the maskable side of the
        # line; both formatters then read the cache rather than re-rendering.
        if record.exc_info and not record.exc_text:
            record.exc_text = _BASE_FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)


__all__ = [
    "PIIRedactionFilter",
    "UNEXPECTED",
    "mask_email",
    "mask_field",
    "mask_name",
    "safe_reason",
    "scrub",
]
