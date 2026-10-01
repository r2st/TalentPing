"""Gmail API access for a connected candidate account.

Every call is scoped to one :class:`~app.models.gmail_account.GmailAccount`: we
decrypt that row's refresh token and let google-auth mint access tokens as
needed. Sending as the candidate (rather than from a shared relay) is what gives
the outreach real SPF/DKIM/DMARC alignment — the practical reason these emails
land in the inbox instead of spam.

The module is import-safe when the Google client libraries are missing so the
API still boots and tests still run; calls raise :class:`GmailNotConfigured`
instead.
"""
from __future__ import annotations

import base64
import http.client
import logging
import re
import socket
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import encoders
from email.charset import QP, Charset
from email.header import decode_header, make_header
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from bs4 import BeautifulSoup

from app.core.config import settings
from app.services import crypto, google_oauth

logger = logging.getLogger(__name__)


#: Statuses worth trying again. Everything here is the transport saying "not
#: now": a throttle, a timeout, or one of Google's own backends being unwell.
#:
#: ``408`` and ``504`` were missing and are the two that cost most, because a
#: timeout is the failure a *large* send produces — the one carrying a resume
#: and a cover letter — so the messages most likely to hit it were exactly the
#: ones written off as poison.
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

#: Statuses no amount of waiting fixes. The message as composed will be refused
#: identically forever, so the retry budget spent on one is pure delay: three
#: attempts five minutes apart is fifteen minutes of a campaign held open on a
#: message that was never going to go.
#:
#: ``403`` is deliberately absent. It is both a dead scope (translated in
#: :func:`_execute` before it ever reaches here) *and* an ordinary quota
#: refusal, and the second is transient — see
#: ``test_an_ordinary_403_is_left_alone``. Unclassified is the safe answer for
#: it, and for anything else: an unknown status keeps the benefit of the doubt.
_PERMANENT_STATUSES = frozenset({400, 404, 410, 413, 414, 422, 431})

#: An upper bound on a server-supplied ``Retry-After``. Gmail does not normally
#: send an absurd one, but a gateway in front of it can, and a task that sleeps
#: on the number it was handed is a worker held hostage by a header. Matches the
#: ceiling :mod:`app.services.llm_router` puts on the same header for the same
#: reason.
MAX_RETRY_AFTER = 300.0

#: Gmail's attachment ceiling, in bytes. The API refuses a *raw* message over
#: 35 MB, and base64 inflates every attachment by a third on the way in, so
#: 25 MB of files is the number that describes the same limit from this side —
#: and it is the number Google states to users, which is what makes it the right
#: one to put in an error message.
#:
#: Compared against the attachment bytes alone rather than the assembled MIME.
#: The body and headers of an outreach email are kilobytes against a ceiling in
#: tens of megabytes, and a ceiling that had to be recomputed after building the
#: message would have to build the message first — which is most of the cost
#: this check exists to avoid.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

#: The longest this module will block a worker waiting to try again.
#:
#: The retry below is a ``time.sleep`` inside whichever thread is running the
#: task, so every second of it is a worker held. That is a fine trade for the
#: blip this loop exists for — a second, two, four — and a bad one for a
#: throttle measured in minutes: honouring a ``Retry-After: 300`` here would
#: hold one worker for five minutes per attempt while the queue behind it
#: waited.
#:
#: So a wait longer than this is not waited out. The failure propagates instead,
#: and :func:`app.tasks.email_tasks._send_outreach_email` re-queues the task
#: with that same ``Retry-After`` as a Celery countdown — the identical delay,
#: spent on the broker rather than in a worker.
MAX_INLINE_DELAY = 30.0


def retry_after_seconds(exc: Exception) -> float | None:
    """The ``Retry-After`` the transport asked for, in seconds, if it gave one.

    Read rather than guessed. Exponential backoff is what you do when the server
    has not said — when it *has*, doubling a delay it already told you is both
    slower than necessary and, on a per-user quota, sometimes far too fast.

    ``HttpError.resp`` is an ``httplib2.Response``, which is a ``dict`` of
    lower-cased header names, so the lookup goes through ``get`` rather than an
    attribute. Only the delta-seconds form is honoured: the HTTP-date form is
    legal and Google does not send it, and a half-parsed date is worse than
    falling back to backoff. Clamped to :data:`MAX_RETRY_AFTER`, and a negative
    or unparseable value reads as "not given".
    """
    resp = getattr(exc, "resp", None)
    if resp is None:
        return None
    try:
        raw = resp.get("retry-after") or resp.get("Retry-After")
    except AttributeError:  # a stub response, or one that is not dict-like
        return None
    if raw in (None, ""):
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER)


@dataclass(frozen=True)
class TransportFailure:
    """What one Gmail transport failure means for the message that hit it.

    Three questions every caller of :func:`send_email` was answering by hand, or
    — much more often — not answering at all. Before this existed a 413 and a
    503 arrived at the send task as the same bare ``HttpError`` and were treated
    identically: three retries five minutes apart, then ``FAILED`` carrying
    ``str(exc)`` as the recorded reason. That string is
    ``<HttpError 413 when requesting https://gmail.googleapis.com/... returned
    "Request Entity Too Large">``, which is what the *review queue* then showed
    the candidate about their own message.

    * ``terminal`` — retrying cannot help. The message is written off now
      instead of fifteen minutes from now.
    * ``message`` — a sentence for a human, in the product's own voice. Never
      ``str(exc)``: that leaks a request URL into the UI and explains nothing.
    * ``retry_after`` — the transport's own ``Retry-After``, when it sent one,
      so a throttle is waited out for as long as it asked rather than for
      whatever the backoff curve happened to reach.

    ``status`` is kept for the log line and the metrics; nothing user-facing
    reads it.
    """

    terminal: bool
    message: str
    retry_after: float | None = None
    status: int | None = None


#: The user-facing sentence for each permanent status. Deliberately about *the
#: message*, not about HTTP: the reader is a candidate looking at their own
#: outreach in the review queue, and "Request Entity Too Large" tells them
#: nothing they can act on while "the attachments are too big" tells them
#: exactly what to change.
_PERMANENT_MESSAGES = {
    400: "Gmail refused this message as malformed, so it was not sent.",
    404: "Gmail could not find the conversation this belongs to, so it was not sent.",
    410: "Gmail could not find the conversation this belongs to, so it was not sent.",
    413: (
        "This message is too big for Gmail to send — the attachments are over "
        "the 25 MB limit."
    ),
    414: "Gmail refused this message as malformed, so it was not sent.",
    422: "Gmail refused this message as malformed, so it was not sent.",
    431: "Gmail refused this message as malformed, so it was not sent.",
}


def classify_transport_failure(exc: Exception) -> TransportFailure:
    """Sort one send failure into "try again" or "never going to work".

    Covers both shapes a Gmail failure arrives in, which is the point: an
    ``HttpError`` is Google answering, and a socket error is Google never being
    reached at all. The second kind — DNS not resolving ``googleapis.com``, a
    TLS handshake that failed, a connection dropped mid-upload, a read that
    timed out — never went through any classification, because
    :func:`_retry_transient` only ever caught ``HttpError``. So a thirty-second
    network blip consumed the send task's whole poison-message budget and wrote
    a perfectly good message off, while a genuinely malformed one got the same
    three chances.

    Unknown is not terminal. Anything this cannot place keeps the benefit of the
    doubt and is retried, because the cost of the two mistakes is not symmetric:
    a permanent failure retried wastes a quarter of an hour, while a transient
    one written off destroys a message the candidate meant to send and the
    review queue shows as ``FAILED``.
    """
    if isinstance(exc, MessageTooLarge):
        return TransportFailure(
            terminal=True, message=_PERMANENT_MESSAGES[413], status=413
        )
    status = getattr(getattr(exc, "resp", None), "status", None)
    if isinstance(status, int):
        if status in _PERMANENT_STATUSES:
            return TransportFailure(
                terminal=True,
                message=_PERMANENT_MESSAGES.get(
                    status, "Gmail refused this message, so it was not sent."
                ),
                status=status,
            )
        if status in _TRANSIENT_STATUSES:
            return TransportFailure(
                terminal=False,
                message="Gmail is not accepting mail right now — this will be retried.",
                retry_after=retry_after_seconds(exc),
                status=status,
            )
        return TransportFailure(
            terminal=False,
            message="Gmail could not send this right now — this will be retried.",
            retry_after=retry_after_seconds(exc),
            status=status,
        )
    if isinstance(exc, _NETWORK_ERRORS):
        return TransportFailure(
            terminal=False,
            message="Could not reach Gmail — this will be retried.",
            status=None,
        )
    return TransportFailure(
        terminal=False,
        message="This could not be sent — it will be retried.",
        status=None,
    )


def _retry_transient(func, *, max_retries: int = 3, base_delay: float = 1.0):
    """Retry a Gmail API call while the failure is one that could clear.

    The delay is the server's ``Retry-After`` when it sent one and exponential
    backoff when it did not — see :func:`retry_after_seconds`. Backoff is a
    guess at a number the server is often willing to state.

    Network-level failures are retried here too, and used not to be: only
    ``HttpError`` was caught, so a DNS failure or a dropped connection left this
    helper on the first attempt and fell through to the caller. That is a real
    difference for a send, because the caller's budget is measured in
    five-minute waits and this one is measured in seconds.

    A ``send`` retried after a dropped connection can duplicate a delivery Gmail
    accepted but never acknowledged. That risk is not new — the send task
    already retried the whole task on any exception — and it is bounded the same
    way it was: :func:`app.tasks.email_tasks._claim_for_send` holds the row for
    the duration of the call, and Gmail deduplicates a re-sent identical
    ``raw`` within a short window. The alternative, treating every dropped
    connection as terminal, loses real messages to ordinary network weather.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return _execute(func)
        except (HttpError, *_NETWORK_ERRORS) as exc:
            verdict = classify_transport_failure(exc)
            if verdict.terminal or attempt >= max_retries:
                raise
            delay = (
                verdict.retry_after
                if verdict.retry_after is not None
                else base_delay * (2 ** attempt)
            )
            if delay > MAX_INLINE_DELAY:
                # Too long to hold a worker for. Handed up so the caller can
                # wait it out on the broker instead — see MAX_INLINE_DELAY.
                raise
            logger.warning(
                "Gmail API %s (attempt %d/%d), retrying in %.1fs",
                verdict.status if verdict.status is not None else type(exc).__name__,
                attempt + 1,
                max_retries + 1,
                delay,
            )
            time.sleep(delay)
            last_exc = exc
            continue
    raise last_exc  # pragma: no cover

try:
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    _GOOGLE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in slim envs
    _GOOGLE_AVAILABLE = False

    class HttpError(Exception):  # type: ignore[no-redef]
        """Stand-in so the ``except`` clauses below stay importable."""

        resp = None

    class RefreshError(Exception):  # type: ignore[no-redef]
        """Stand-in so the ``except`` clauses below stay importable."""


#: Failures where Gmail never answered at all, as exception types.
#:
#: One entry per item on the list of things that go wrong between a worker and
#: an API: ``socket.gaierror`` is DNS not resolving ``gmail.googleapis.com``;
#: ``ssl.SSLError`` is a TLS handshake that failed or a renegotiation that went
#: wrong mid-stream; ``http.client.HTTPException`` covers a response that
#: arrived truncated or malformed, ``RemoteDisconnected`` among them, which is
#: the connection dropped mid-send; ``ConnectionError`` is refused, reset or
#: aborted; and ``TimeoutError`` is the read that never came back.
#:
#: ``socket.gaierror`` and ``socket.timeout`` are subclasses of ``OSError`` and
#: ``TimeoutError`` respectively on modern Pythons, so the tuple is wider than
#: it strictly needs to be — deliberately, because it is read as documentation
#: of which failures were considered, and an ``isinstance`` check does not care
#: about redundancy. ``OSError`` itself is *not* in it: that would swallow every
#: file and permission error the composer can raise, and those are not the
#: transport being unreachable.
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    socket.timeout,
    ssl.SSLError,
    http.client.HTTPException,
)


_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailNotConfigured(RuntimeError):
    """Raised when Gmail credentials are missing or the client is unavailable."""


class GmailAuthRevoked(RuntimeError):
    """The stored grant is dead — Google answered ``invalid_grant``.

    Distinct from :class:`GmailNotConfigured`, which means *this deployment* is
    missing credentials and is fixed by an operator. This one means *this
    mailbox's* refresh token no longer works, and the only remedy is the
    candidate re-running the consent flow. Nothing retried can bring it back, so
    callers mark the account and stop rather than raising: google-auth raises
    this lazily on the first API call, so an unhandled one is a fresh traceback
    on every beat tick, forever, while the row still claims to be connected.

    It happens for three reasons, and the copy has to cover all three: the user
    removed DoAide AutoApply at myaccount.google.com/permissions, they changed their
    password, or — the one that catches deployments rather than users — the
    OAuth client is still in "Testing" publishing status, where Google expires
    every refresh token seven days after it is issued.
    """


class GmailScopeInsufficient(RuntimeError):
    """The grant is alive but does not cover the call we just made.

    A third remedy, distinct from both of the above: nothing is misconfigured on
    the deployment and the mailbox has not lost its grant — it was simply
    connected with fewer permissions than the product needs, either because the
    user unticked one on the consent screen or because the OAuth client was
    registered without it.

    Named separately because it is otherwise indistinguishable from any other
    Gmail failure at the call site, and it is not: it never clears on its own and
    no number of retries helps. Left untranslated it arrived at the send task's
    generic backstop as ``HttpError 403``, cost three retries, and wrote the
    message off with a reason nobody could act on — while the mailbox went on
    reporting itself connected and the next message did the same thing.
    """


class MessageTooLarge(RuntimeError):
    """The message is over Gmail's size limit, decided before it is uploaded.

    The 413 this pre-empts is a real answer and is still handled — see
    :data:`_PERMANENT_STATUSES` — but it is an expensive way to be told. Gmail
    reads the whole upload before refusing it, so a candidate sending a 30 MB
    portfolio pays the upload on every attempt, and the send task's own budget
    means that happens more than once.

    Checked here rather than at compose time because compose time does not know
    what will actually travel: attachments are resolved at the transport, from
    ``files_of_record``, precisely so a draft that waited in the review queue
    sends the resume as it stands now.
    """


class GmailThreadNotFound(RuntimeError):
    """The mailbox we asked does not hold this thread.

    A Gmail thread id is only meaningful inside the mailbox that holds it, so
    asking the wrong account for one is a 404 rather than an error worth
    retrying. Named separately from :class:`GmailNotConfigured` because the
    remedy is different: nothing is misconfigured, we simply asked the wrong
    mailbox, and the caller's job is to stop asking rather than to alert.
    """


def _is_invalid_grant(exc: Exception) -> bool:
    """True when a refresh failure means the grant itself is gone.

    google-auth reports every refresh failure as ``RefreshError``; only
    ``invalid_grant`` is terminal. A network blip or a 5xx from Google's token
    endpoint arrives the same way and must stay retryable, so match on the OAuth
    error code rather than on the exception type.
    """
    return "invalid_grant" in str(exc)


def _is_insufficient_scope(exc: Exception) -> bool:
    """True when Gmail refused a call the grant does not cover.

    Google reports this as a 403 ``insufficientPermissions`` or a 401 carrying
    ``ACCESS_TOKEN_SCOPE_INSUFFICIENT``, depending on the endpoint, so the status
    alone is not enough to tell it from an ordinary permission error — and both
    codes are also what a plain quota or auth failure looks like. Matching on the
    reason as well keeps this off failures that are nothing to do with scopes.
    """
    status_code = getattr(getattr(exc, "resp", None), "status", None)
    if status_code not in (401, 403):
        return False
    text = str(exc)
    return (
        "insufficientPermissions" in text
        or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in text
        or "insufficient authentication scopes" in text.lower()
    )


def _execute(func):
    """Run one Gmail API call, translating the two terminal failures.

    google-auth mints access tokens lazily, so the refresh — and therefore the
    ``invalid_grant`` — surfaces from whichever ``execute()`` happens to run
    first, not from building the credentials. Every call in this module goes
    through here so that translation happens once.

    The scope failure is translated in the same place and for the same reason:
    both mean this mailbox will not work again until the candidate re-runs the
    consent flow, and both are worth telling apart from the transient failures
    around them.
    """
    try:
        return func()
    except RefreshError as exc:
        if _is_invalid_grant(exc):
            raise GmailAuthRevoked(
                "Google rejected the stored grant (invalid_grant) — "
                "this mailbox has to be connected again"
            ) from exc
        raise
    except HttpError as exc:
        if _is_insufficient_scope(exc):
            raise GmailScopeInsufficient(
                "This mailbox was connected without the permissions DoAide AutoApply "
                "needs — connect it again and leave every permission ticked"
            ) from exc
        raise


def _is_not_found(exc: Exception) -> bool:
    """True when a Gmail API error is a 404."""
    status_code = getattr(getattr(exc, "resp", None), "status", None)
    return status_code == 404


@dataclass
class SentMessage:
    gmail_message_id: str
    gmail_thread_id: str
    #: The RFC 5322 ``Message-ID`` Gmail stamped on the message, read back from
    #: the API after the send — see :func:`_sent_message_id`. ``None`` when the
    #: read-back failed, which callers must treat as "unknown" rather than
    #: inventing one: see ``inbound_scanner.reply_headers_for``.
    rfc_message_id: str | None = None


#: What each half of a media type may contain — the RFC 2045 token characters,
#: minus the separators. Anything else and the string is not a media type.
_TYPE_TOKEN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,64}$")

#: Maintypes that describe a *structure* rather than a payload, and so must
#: never be claimed for a base64 blob. See :meth:`Attachment.parts`.
_STRUCTURAL_MAINTYPES = frozenset({"multipart", "message"})


@dataclass(frozen=True)
class Attachment:
    """One file to travel with the message.

    Usually a rendered resume or cover letter, and no longer only that: a
    candidate can attach anything a recruiter asks for through
    ``POST /inbox/emails/{id}/attachments``, so ``mime_type`` is a real
    variable rather than decoration. See :meth:`parts`.
    """

    filename: str
    content: bytes
    mime_type: str = "application/pdf"

    @property
    def parts(self) -> tuple[str, str]:
        """``(maintype, subtype)`` this file may be announced under.

        Both halves, and that is the fix. The part used to be built by
        ``MIMEApplication``, which takes only a subtype and hardcodes the
        maintype — reasonable when every attachment this product produced was a
        rendered PDF, and wrong the moment
        ``POST /inbox/emails/{id}/attachments`` let a candidate send whatever a
        recruiter asked for. That endpoint is deliberately unfiltered by type,
        so a portfolio went out as ``application/png`` and a notes file as
        ``application/plain`` — which is not a registered media type at all. No
        thumbnail, no inline preview, and in several clients an opaque blob the
        recipient has to open on faith, on outreach whose whole job is to not
        look machine-generated.

        Parameters are dropped. A browser sends ``text/plain; charset=utf-8``,
        and the bytes are base64-encoded here rather than re-encoded, so a
        charset copied across would be a claim about them that nothing checked.

        Anything that is not a usable type falls back to
        ``application/octet-stream``, which no client interprets. Three shapes
        reach that:

        * a type that is not two tokens — ``""``, ``"pdf"``, ``"/pdf"``;
        * a *structural* maintype. A base64 blob labelled ``multipart/mixed``
          claims parts it does not have, and a client that believes the label
          fails to parse the message around it;
        * anything holding a character a media type cannot. This value is
          ``UploadFile.content_type`` — the browser's word, not ours — and it
          lands in a header on mail we send, so it is matched against a token
          rather than trusted. :func:`_header_safe` covers the values this
          module writes itself; ``MIMEBase`` interpolates this one straight
          into ``Content-Type``.
        """
        bare = self.mime_type.partition(";")[0].strip().lower()
        maintype, _, subtype = bare.partition("/")
        if (
            not _TYPE_TOKEN.match(maintype)
            or not _TYPE_TOKEN.match(subtype)
            or maintype in _STRUCTURAL_MAINTYPES
        ):
            return "application", "octet-stream"
        return maintype, subtype


def _naive_utc(value: datetime | None) -> datetime | None:
    """*value* as the naive-UTC datetime google-auth compares against.

    ``google.auth._helpers.utcnow()`` is naive, and ``Credentials.expired``
    compares ``expiry`` to it directly — so handing google-auth the tz-aware
    datetime SQLAlchemy returns raises ``TypeError: can't compare offset-naive
    and offset-aware datetimes`` from inside the request path. SQLite hands the
    same column back naive, so both shapes have to be accepted.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _usable_access_token(account) -> tuple[str | None, datetime | None]:
    """The stored access token and its expiry, but only while it is still good.

    ``expiry`` was never passed to :class:`Credentials`, and that omission is
    not the harmless-looking thing it reads as. google-auth treats an expiry of
    ``None`` as "never expires": ``Credentials.expired`` short-circuits to
    ``False``, ``valid`` is therefore ``True``, and nothing refreshes. So every
    Gmail call sent the access token captured at OAuth-connect time — which
    Google issues for one hour and this row has held for weeks — collected a
    401, and only *then* did ``AuthorizedHttp`` refresh and replay the request.

    Every single API call, on every mailbox, forever: a wasted round trip to
    Gmail plus a hit on Google's token endpoint, for a token we already knew was
    dead. Polling alone is one such pair per thread per five minutes.

    Passing the real expiry makes google-auth refresh *before* the request, so
    the wasted 401 disappears. A row with a token but no recorded expiry is the
    unknown case, and unknown is treated as expired: the token is dropped so the
    credentials start invalid and refresh cleanly, rather than being spent on a
    401 to find out.
    """
    if not account.access_token_encrypted:
        return None, None
    expiry = _naive_utc(account.token_expiry)
    if expiry is None or expiry <= datetime.now(UTC).replace(tzinfo=None):
        return None, None
    try:
        return crypto.decrypt(account.access_token_encrypted), expiry
    except crypto.TokenCryptoError:
        return None, None


def _credentials(account) -> Credentials:
    """Build google-auth credentials from a stored (encrypted) refresh token."""
    if not _GOOGLE_AVAILABLE:
        raise GmailNotConfigured("Google API client libraries are not installed")
    if account is None:
        raise GmailNotConfigured("No Gmail account is connected")
    if not (settings.google_client_id and settings.google_client_secret):
        raise GmailNotConfigured("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set")

    try:
        refresh_token = crypto.decrypt(account.refresh_token_encrypted)
    except crypto.TokenCryptoError as exc:
        raise GmailNotConfigured(str(exc)) from exc

    access_token, expiry = _usable_access_token(account)

    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        # Carried so google-auth can tell a live token from a stale one before
        # it spends a request finding out. See `_usable_access_token`.
        expiry=expiry,
        token_uri=_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        # Replay exactly the granted scopes — see google_oauth.credential_scopes_for.
        scopes=google_oauth.credential_scopes_for(account.scopes),
    )


def _service(account):
    return build("gmail", "v1", credentials=_credentials(account), cache_discovery=False)


def canspam_footer(unsubscribe_url: str) -> str:
    """Build the CAN-SPAM compliant footer (physical address + opt-out).

    The separator is two ASCII hyphens and not an em dash, and that is a
    deliverability decision rather than a typographic one. This footer is
    appended to **every** unsolicited message, so its one non-ASCII character
    was the reason essentially all of this product's cold outreach failed
    :func:`_text_part`'s ASCII test — and mail that fails it is not sent
    ``7bit``. A single em dash therefore decided the transfer encoding of the
    entire corpus of mail whose deliverability the encoding exists to protect,
    while the replies that carry no footer were the only messages ever sent the
    way Gmail's own composer sends them.

    ``--`` is also the RFC 3676 §4.3 signature separator, which is what this
    line has always been for: clients that understand the convention set the
    trailing block apart on their own.
    """
    return (
        "--\n"
        "You are receiving this because your company's careers page lists this "
        "address for hiring enquiries. If you'd prefer not to be contacted, "
        f"unsubscribe here: {unsubscribe_url}\n"
        f"{settings.compliance_physical_address}"
    )


# Everything a header value may not contain. CR and LF are the ones that matter
# — a header is defined as ending at the newline — and the remaining C0 controls
# are here because none of them have any business in an address, a subject or a
# message id, and a NUL in particular truncates the value for anything reading
# the MIME with C string semantics further down the line.
_HEADER_FORBIDDEN = str.maketrans(
    dict.fromkeys(
        [chr(c) for c in range(0x20) if chr(c) not in "\t"] + ["\x7f"], " "
    )
)


def _header_safe(value: str | None) -> str:
    """*value*, flattened to something that can be one header line.

    Python's ``email`` package refuses to serialise a header holding a newline —
    which is the right answer to the injection this prevents, and the wrong
    place to find out. The refusal is a ``HeaderWriteError`` raised from
    :func:`_build_mime`, which is called from ``send_outreach_email`` and caught
    by nothing there: the exception escapes the task, the claimed row rolls back
    to ``QUEUED``, and the campaign sweep re-dispatches exactly the ``QUEUED``
    rows. So one newline meant that message failed on every sweep, forever, and
    its campaign never reached completion.

    It is reachable without anybody being clever. A reply threads on the
    recruiter's own subject line (``inbound_reply._subject_for``), which is a
    header from mail a stranger sent us and is only ``strip``ped; the LLM
    composers can return a title with a line break in it; and
    ``PATCH /tracker/emails/{id}`` takes a subject as free text, which is one
    paste from a document away.

    Collapsed rather than refused, and applied here rather than only at those
    three call sites, because this is the one function every outgoing message is
    built by — and because "your subject had a newline in it" is not a thing to
    fail somebody's outreach over when the intended value is unambiguous.
    """
    if not value:
        return ""
    # Split-and-rejoin rather than a bare translate: a folded header arriving as
    # "Re: long\r\n subject" means "Re: long subject", so the whitespace that
    # continued the fold must not become a second space.
    return " ".join(value.translate(_HEADER_FORBIDDEN).split())


def _header_safe_url(value: str | None) -> str:
    """A URL reduced to something that cannot terminate the header holding it.

    :func:`_header_safe` replaces a control character with a space, which is the
    right answer for a subject and the wrong one for a URI: whitespace inside
    ``List-Unsubscribe``'s angle brackets is not part of the address, and a mail
    client that honours the header would follow a mangled one. A URL has no
    legitimate whitespace at all, so the offending characters are *removed*
    rather than replaced.
    """
    if not value:
        return ""
    return "".join(value.translate(_HEADER_FORBIDDEN).split())


def _filename_safe(value: str | None) -> str:
    """An attachment's name, flattened for the ``Content-Disposition`` param.

    Same failure as a newline in a subject, arriving through a door
    :func:`_header_safe` does not cover. ``MIMEBase.add_header`` quotes
    and escapes the value — a quote or a backslash in a filename is already
    handled — but it does nothing about a control character, so a name holding
    ``\\r\\n`` raises ``HeaderParseError`` ("header value appears to contain an
    embedded header") the moment the message is serialised. That is thrown from
    inside :func:`_build_mime`, which is the exact path whose consequences
    ``_header_safe`` documents: the task dies, the claimed row rolls back to
    ``QUEUED``, and the campaign sweep re-dispatches it to fail again forever.

    A filename is not something the product invents. It is
    ``UploadFile.filename`` off ``POST /inbox/emails/{id}/attachments`` and off
    a resume upload — both only ``strip``ped, so an embedded newline survives —
    and it is ``part["filename"]`` off a MIME part a stranger mailed in, which
    travels back out whenever that file is carried into a reply. Any of the
    three permanently bricks the message it is attached to.

    Empty names fall back rather than producing a bare ``filename=""``, which
    some clients render as an unnamed part the recipient cannot save.
    """
    return _header_safe(value) or "attachment"


#: RFC 5322 §2.1.1: a line, excluding its CRLF, is at most 998 octets. Nothing
#: enforces it on the way out — Python's ``compat32`` policy hands a 7bit part
#: through unwrapped — and an over-long line is one an MTA may fold wherever it
#: likes, in the middle of a URL as readily as at a space.
_MAX_LINE_OCTETS = 998


def _quoted_printable(charset: str) -> Charset:
    """*charset*, with a body encoding of quoted-printable rather than base64.

    ``email.charset``'s registry is process-global and its ``utf-8`` entry maps
    the body to BASE64, so mutating it would change every part every caller in
    the process builds. These are private copies: constructed once, never
    registered, and handed to :class:`MIMEText` as the charset argument.
    """
    result = Charset(charset)
    result.body_encoding = QP
    return result


#: utf-8 text, quoted-printable rather than base64. See :func:`_text_part`.
_QP_UTF8 = _quoted_printable("utf-8")
#: ASCII text that has a line too long for ``7bit``; QP folds it at 76 columns.
_QP_ASCII = _quoted_printable("us-ascii")


def _text_part(text: str, subtype: str) -> MIMEText:
    """A text part encoded the way mail a human wrote is encoded.

    Every part used to be declared ``charset="utf-8"``, which is correct and has
    a side effect: Python's ``email`` package base64-encodes a utf-8 part
    unconditionally, so a plain ASCII "Hi Sam, quick note about the backend
    role" left as an unreadable block with ``Content-Transfer-Encoding: base64``.

    That is a content-filter signal in its own right — SpamAssassin scores
    ``MIME_BASE64_TEXT``, "message text disguised using base64 encoding" — and
    the reason the rule exists is that legitimate mail does not usually need it.
    Gmail's own composer sends an ASCII body as ``7bit``. So every message this
    product sent was encoded unlike the mail it was claiming to be, on a
    signal cheap enough that filters read it inline, in service of a mailbox the
    candidate cannot re-provision.

    ``us-ascii`` only when the text really is ASCII *and* no line is over
    :data:`_MAX_LINE_OCTETS`. Both conditions matter. The first is obvious. The
    second is the reason this is not a plain "prefer 7bit": a body carrying one
    very long line — a tracking URL, or a paste with no newlines in it — is a
    malformed message as ``7bit`` and a legal one under any encoding that
    wraps.

    **Everything else is quoted-printable, not base64**, and that is the half
    the original fix left on the table. Choosing ``us-ascii`` avoided base64
    only for mail that happened to be pure ASCII, and almost none of this
    product's cold outreach is: :func:`canspam_footer` is appended to every
    unsolicited message, the composers write an em dash into a fallback subject
    and a fallback body, and a recruiter called Zoë is not an edge case. Each of
    those flipped the part to utf-8, and Python's ``email`` package base64s a
    utf-8 part unconditionally — so the ``MIME_BASE64_TEXT`` signal this
    function exists to avoid was still on essentially every message that
    mattered, while the footerless replies that need it least were the only
    mail getting ``7bit``.

    Quoted-printable is what a mail client actually does with mostly-ASCII
    text: Gmail's own composer sends a body with one accent in it as
    ``quoted-printable``, and the ~99% of the message that is ASCII stays
    readable in the raw source rather than becoming an opaque block. It also
    folds at 76 columns, so it is a complete answer to the long-line case as
    well — which is why the over-long ASCII branch below stays ``us-ascii``
    (the octets really are ASCII; only the encoding needs to change) instead of
    relabelling the part as utf-8 to buy a wrapping it can have either way.
    """
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return MIMEText(text, subtype, _QP_UTF8)
    if any(len(line) > _MAX_LINE_OCTETS for line in text.splitlines()):
        return MIMEText(text, subtype, _QP_ASCII)
    return MIMEText(text, subtype, "us-ascii")


def _build_mime(
    sender: str,
    to: str,
    subject: str,
    body_text: str,
    footer: str,
    *,
    display_name: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    unsubscribe_url: str | None = None,
    mailto_unsubscribe: bool = False,
    attachments: Sequence[Attachment] = (),
    body_html: str | None = None,
) -> str:
    full_body = f"{body_text}\n\n{footer}" if footer else body_text
    body_part: MIMEText | MIMEMultipart = _text_part(full_body, "plain")

    if body_html:
        # multipart/alternative carries the tracked HTML (pixel + wrapped links)
        # alongside the plain text. The text part is unchanged, so a client that
        # renders it — or a recipient who blocks images — reads exactly what
        # they read before tracking existed.
        alternative = MIMEMultipart("alternative")
        alternative.attach(body_part)
        alternative.attach(_text_part(body_html, "html"))
        body_part = alternative

    if attachments:
        # multipart/mixed only when there is something to mix in: a single-part
        # text/plain message is what the deliverability work assumed, and every
        # attachment-free, tracking-free send should keep producing exactly that.
        message: MIMEText | MIMEMultipart = MIMEMultipart("mixed")
        message.attach(body_part)
        for item in attachments:
            # `MIMEBase` rather than `MIMEApplication`: the latter takes only a
            # subtype and hardcodes `application` as the maintype. See
            # `Attachment.parts`. The payload is base64-encoded either way, so
            # only the declared type changes.
            maintype, subtype = item.parts
            part = MIMEBase(maintype, subtype)
            part.set_payload(item.content)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=_filename_safe(item.filename),
            )
            message.attach(part)
    else:
        message = body_part

    # Every header value is flattened on the way in — see `_header_safe`. The
    # body is not, and must not be: a newline there is the message.
    sender = _header_safe(sender)
    display_name = _header_safe(display_name)
    message["To"] = _header_safe(to)
    # `formataddr` rather than an f-string: it quotes a display name containing
    # the characters that are structural in an address list. `full_name` reaches
    # here from registration *and* from resume parsing, so a document whose name
    # line reads `Dana <someone@else.example>` would otherwise have produced a
    # `From` naming two addresses.
    message["From"] = formataddr((display_name, sender)) if display_name else sender
    message["Subject"] = _header_safe(subject)
    if in_reply_to:
        message["In-Reply-To"] = _header_safe(in_reply_to)
    if references:
        message["References"] = _header_safe(references)
    if unsubscribe_url:
        # RFC 2369 + RFC 8058 one-click — expected by Gmail/Yahoo bulk guidelines.
        # Flattened like every other header value: this one is built from
        # `settings.app_base_url`, so a stray newline in the deployment's
        # environment file would otherwise brick every unsubscribable message
        # the deployment sends, with nothing on the row to say why.
        https_half = f"<{_header_safe_url(unsubscribe_url)}>"
        # The ``mailto:`` half is advertised only when somebody is reading the
        # mailbox it points at — see `mailto_unsubscribe`.
        message["List-Unsubscribe"] = (
            f"<mailto:{sender}?subject=unsubscribe>, {https_half}"
            if mailto_unsubscribe
            else https_half
        )
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def _sent_message_id(service, gmail_message_id: str) -> str | None:
    """The RFC 5322 ``Message-ID`` Gmail put on the message we just sent.

    Read back rather than chosen. Gmail rewrites the ``Message-ID`` of mail
    submitted through it, so a value this module generated and put in the MIME
    would be an id no recipient ever saw — worse than none, because the next
    message on the thread would then carry an ``In-Reply-To`` naming a message
    that does not exist, and the clients that honour the header are exactly the
    ones that would be misled by it.

    Costs one extra API call per send, asking for one header. Deliberately not
    ``_retry_transient``: the message has already left, so a failure here is a
    lost *label*, not a lost delivery, and retrying it three times with backoff
    would hold the send task open over something the product degrades cleanly
    without. Everything is caught for the same reason — the caller has committed
    a real delivery and must not have it rolled back by a bookkeeping lookup.

    ``None`` means "we do not know", and every consumer treats it as such: a
    follow-up with no parent id to name goes out unthreaded, which is what it
    did before this existed.
    """
    try:
        message = _execute(
            lambda: service.users()
            .messages()
            .get(
                userId="me",
                id=gmail_message_id,
                format="metadata",
                metadataHeaders=["Message-ID"],
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - never lose a send over its label
        logger.warning("could not read back Message-ID for %s: %s", gmail_message_id, exc)
        return None
    headers = (message or {}).get("payload", {}).get("headers", []) or []
    for header in headers:
        # Gmail echoes the header with the capitalisation the message carries,
        # and RFC 5322 field names are case-insensitive.
        if str(header.get("name", "")).lower() == "message-id":
            value = " ".join(str(header.get("value") or "").split())
            return value[:998] or None
    return None


def send_email(
    *,
    account,
    to: str,
    subject: str,
    body_text: str,
    unsubscribe_url: str | None,
    mailto_unsubscribe: bool = False,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Sequence[Attachment] = (),
    body_html: str | None = None,
    footer: str | None = None,
) -> SentMessage:
    """Send an email as *account* via the Gmail API; returns message/thread ids.

    ``unsubscribe_url`` is required but may be ``None``, which sends the message
    with no CAN-SPAM footer and no ``List-Unsubscribe`` headers. That is correct
    for a reply to mail the recipient sent *us* — there is no list to leave, and
    offering to unsubscribe someone from their own conversation reads as bulk
    mail. It is wrong for anything unsolicited, so the argument has no default:
    every caller has to say which kind of message it is sending.

    ``footer`` overrides the CAN-SPAM block that ``unsubscribe_url`` would
    otherwise append, and exists for the one message that needs the headers
    without the text: the weekly digest, which is mail from the user's mailbox
    to its own owner. That footer explains why a *stranger* is being contacted,
    so appending it there would be a lie, while Gmail's native unsubscribe
    control — driven by the headers — is exactly the opt-out that message wants.
    Pass ``""`` for no footer at all; leave it ``None`` for the default.

    ``mailto_unsubscribe`` decides whether ``List-Unsubscribe`` also offers the
    ``mailto:`` route beside the HTTPS one, and it defaults to **off** because
    the two halves are honoured by completely different machinery. The HTTPS
    half lands on ``routers/misc.unsubscribe``, a public route with no feature
    flag in front of it, so it works for every message this product has ever
    sent. The ``mailto:`` half asks the recipient's client to *send us a
    message*, and the only thing in this codebase that reads such a message is
    the recruiter-inbox scanner — which requires ``RECRUITER_REPLY_ENABLED``
    server-side and a per-user preference row that **ships off**.

    So for a user who runs outreach and never turned the inbox scanner on — the
    default — every piece of cold mail advertised a door with nothing behind
    it. Apple Mail, Thunderbird and several Outlook builds prefer the
    ``mailto:`` half when both are offered, so those recipients pressed
    Unsubscribe, watched their client report success, and kept receiving
    follow-ups. :mod:`app.services.opt_out` was written for exactly that
    failure and closed it for the scanner's users; this closes it for everyone
    else, by not making the promise. :mod:`app.services.unsubscribe` states the
    asymmetry this rests on: a header we cannot honour is worse than no header,
    because it converts a working opt-out into a complaint. One route fewer
    costs a recipient a click; a route that does nothing costs the mailbox.

    Off by default rather than on, so a caller that has not thought about it
    ships the header set that works rather than the one that lies.
    """
    oversize = sum(len(item.content or b"") for item in attachments)
    if oversize > MAX_ATTACHMENT_BYTES:
        raise MessageTooLarge(
            f"{oversize} bytes of attachments is over Gmail's "
            f"{MAX_ATTACHMENT_BYTES}-byte limit"
        )
    service = _service(account)
    if footer is None:
        footer = canspam_footer(unsubscribe_url) if unsubscribe_url else ""
    raw = _build_mime(
        account.email,
        to,
        subject,
        body_text,
        footer,
        display_name=account.display_name,
        in_reply_to=in_reply_to,
        references=references,
        unsubscribe_url=unsubscribe_url,
        mailto_unsubscribe=mailto_unsubscribe,
        attachments=attachments,
        body_html=body_html,
    )
    payload: dict = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id

    sent = _retry_transient(
        lambda: service.users().messages().send(userId="me", body=payload).execute()
    )
    return SentMessage(
        gmail_message_id=sent["id"],
        gmail_thread_id=sent.get("threadId", ""),
        rfc_message_id=_sent_message_id(service, sent["id"]),
    )


def list_thread_messages(account, thread_id: str) -> list[dict]:
    """Return raw message dicts for a Gmail thread (for reply polling).

    Raises :class:`GmailThreadNotFound` when this mailbox does not hold the
    thread. Translated here rather than left as a raw ``HttpError`` so callers
    do not have to import the Google client to tell "wrong mailbox" apart from
    "Gmail is down" — and so the module stays importable without it.
    """
    service = _service(account)
    try:
        thread = _execute(
            lambda: service.users().threads().get(userId="me", id=thread_id).execute()
        )
    except HttpError as exc:
        if _is_not_found(exc):
            raise GmailThreadNotFound(
                f"thread {thread_id} is not in {getattr(account, 'email', 'this mailbox')}"
            ) from exc
        raise
    return thread.get("messages", [])


#: Gmail's own ceiling on ``messages.list``. Asking for more is not an error —
#: the API silently returns this many — which is exactly why it has to be named
#: here: a caller passing 750 and getting 500 has no way to tell that the rest of
#: its window exists at all, and a message that is never listed is never skipped,
#: deferred or counted. It is simply lost.
LIST_PAGE_MAX = 500


def list_messages(account, query: str, *, max_results: int = 50) -> list[dict]:
    """Message ids matching a Gmail search query, newest first.

    Reads the *whole* mailbox rather than one thread, which is what inbound
    recruiter detection needs and thread polling never did. Only ids and thread
    ids come back — ``list`` is a cheap call and the bodies are fetched by
    :func:`get_message` for the handful that survive filtering, so a mailbox
    with a thousand unread newsletters costs one request rather than a thousand.

    Pages until *max_results* is met or the results run out. Gmail returns
    newest-first, so an unpaginated read truncates at the *old* end of the
    window — the caller sees a full-looking result and the oldest mail in its
    own search window is invisible. Each extra page is one more cheap request.

    No new consent is involved: ``gmail.readonly`` is already in the scope set
    every connected account granted (see :data:`google_oauth.SCOPES`).
    """
    service = _service(account)
    collected: list[dict] = []
    page_token: str | None = None

    while len(collected) < max_results:
        response = _retry_transient(
            lambda pt=page_token: (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(LIST_PAGE_MAX, max_results - len(collected)),
                    pageToken=pt,
                )
                .execute()
            )
        )
        page = response.get("messages", []) or []
        collected.extend(page)
        page_token = response.get("nextPageToken")
        # No cursor means the query is exhausted. An empty page with a cursor is
        # possible on a filtered query, so the token — not the page — decides.
        if not page_token:
            break

    return collected[:max_results]


# --------------------------------------------------------------------------- #
# Push notifications                                                           #
# --------------------------------------------------------------------------- #


def start_watch(account, topic: str) -> dict:
    """Register a Pub/Sub push subscription for this mailbox.

    Returns Gmail's ``{historyId, expiration}``. Scoped to INBOX so a
    notification means "mail arrived" rather than "a label changed somewhere";
    sent mail is recorded by the sender, not discovered by push.
    """
    service = _service(account)
    return _execute(
        lambda: service.users()
        .watch(
            userId="me",
            body={"topicName": topic, "labelIds": ["INBOX"], "labelFilterAction": "include"},
        )
        .execute()
    )


def stop_watch(account) -> None:
    """Cancel this mailbox's push subscription."""
    service = _service(account)
    _execute(lambda: service.users().stop(userId="me").execute())


HISTORY_PAGE_MAX = 500


def list_history(
    account,
    start_history_id: str,
    *,
    max_results: int = 100,
    max_pages: int = 10,
) -> dict:
    """Changes to the mailbox since *start_history_id*.

    ``messagesAdded`` only: the cursor exists to find mail we haven't seen, and
    label/read-state changes are noise for that purpose. A cursor Gmail has
    aged out raises 404 at the caller, which is the signal to re-watch and fall
    back to a poll for the gap.

    Pages until the changes run out, and that is a correctness requirement
    rather than completeness for its own sake. Gmail's ``historyId`` on the
    response is the *mailbox's* current head, not the last record on the page —
    so a single-page read handed the caller a full-looking result plus a cursor
    pointing past everything it had not fetched. The caller stored that cursor,
    and every message on the unread pages was skipped permanently: the next
    notification starts from the head, and those history records are never
    offered again.

    Nothing downstream could recover it either. The five-minute poller skips any
    thread whose mailbox has a healthy watch (``inbox_tasks._push_covers``), and
    a watch that is delivering notifications perfectly *is* healthy — so the one
    mailbox state that causes the loss is also the one that suppresses the
    fallback. One busy morning, or one bulk import, and a hundred messages are
    simply not there.

    ``truncated`` says the page budget ran out with changes still pending. The
    returned ``historyId`` is then the last record actually read rather than the
    mailbox head, so a caller that stores it resumes from the right place and
    the next run picks up the remainder.
    """
    service = _service(account)
    records: list[dict] = []
    page_token: str | None = None
    head: str | None = None
    truncated = False

    for page in range(max_pages):
        response = _execute(
            lambda pt=page_token: (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=str(start_history_id),
                    historyTypes=["messageAdded"],
                    maxResults=min(HISTORY_PAGE_MAX, max_results),
                    pageToken=pt,
                )
                .execute()
            )
        )
        records.extend(response.get("history", []) or [])
        # The head is reported on every page and is what the cursor should
        # advance to once the whole range has been read.
        head = response.get("historyId") or head
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        if page == max_pages - 1:
            truncated = True

    if truncated:
        # Advance only as far as we actually read. Every record carries its own
        # history id, so the remainder is still reachable on the next run —
        # which is the whole difference between "slow" and "lost".
        last_read = next(
            (r.get("id") for r in reversed(records) if r.get("id")), None
        )
        if last_read is not None:
            head = str(last_read)

    return {
        "history": records,
        "historyId": head or str(start_history_id),
        "truncated": truncated,
    }


def get_message(account, message_id: str) -> dict:
    """One full message resource."""
    service = _service(account)
    return _retry_transient(
        lambda: service.users().messages().get(userId="me", id=message_id).execute()
    )


#: Elements whose text is never something the sender wrote.
_HTML_DROP_TAGS = ("script", "style", "head", "title")

#: Elements that start a new line without a blank one before it — the rows of a
#: list or table, and the ``<br>`` a signature block is written with. Wrapping
#: these in blank lines the way a paragraph is wrapped would double-space
#: "Kind regards,<br>Jane<br>Acme" into three separate-looking turns.
_HTML_LINE_TAGS = ("br", "hr", "li", "tr")

#: Elements that end a paragraph, and so are surrounded by a blank line.
#:
#: Inline markup — ``<b>``, ``<a>``, ``<span>``, ``<font>`` — is deliberately
#: absent from both tuples. An HTML mail wraps half its sentences in it, and a
#: converter that breaks on every tag returns a document with one or two words
#: per line: the shattered shape
#: :func:`app.services.resume_parser._looks_shattered` exists to repair,
#: arriving here instead. It costs more than it looks. ``_CLAUSE_BREAK`` in
#: :mod:`app.services.pay_period` counts a newline as the end of a clause, so
#: "$85\nper hour" is a band with no period beside it; ``reply_text`` splits
#: quoted turns on line shape; and the classifier reads the whole thing as prose.
_HTML_BLOCK_TAGS = (
    "address", "article", "blockquote", "div", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "ol",
    "p", "section", "table", "ul",
)


#: Stands in for a line break while the tree is being flattened, so that a
#: break the markup asked for can be told from whitespace that is only in the
#: source. Chosen because no mail body contains it: BeautifulSoup resolves
#: entities, and a literal NUL in an HTML document is replaced with U+FFFD.
_BREAK = "\x00"


def html_to_text(html: str) -> str:
    """An HTML mail body as the text a person would have typed.

    Block elements become line breaks and inline ones become nothing, so a
    paragraph arrives as a paragraph rather than as one word per line. ``&nbsp;``
    becomes an ordinary space: it is what a mail client emits for the run of
    spaces in "Salary:\u00a0\u00a0$180,000", and every matcher downstream is
    written against the space.

    **A newline in the source is not a newline on the page.** Templated mail —
    which is most HTML mail — is pretty-printed, so the markup for one sentence
    is spread over several source lines, and a converter that keeps them
    produces the shattered document the tag rules above exist to avoid:

        <div>
          The rate for this contract is
          <strong>$85</strong>
          per hour, W2.
        </div>

    Kept verbatim that is "$85" on a line of its own, and
    ``pay_period._CLAUSE_BREAK`` counts a newline as the end of a clause — so
    the rate is a band with no period beside it, annualised at 1 and stored as
    $85,000 rather than $176,800. So the breaks the *markup* asks for are marked
    while the tree is flattened, and every other run of whitespace collapses to
    a single space, which is what a browser renders.

    ``<pre>`` is the exception, because there the source newlines are the
    content: some clients wrap a plain-text body in one.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(list(_HTML_DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all("pre"):
        tag.replace_with(_BREAK + tag.get_text().replace("\n", _BREAK) + _BREAK)
    for tag in soup.find_all(list(_HTML_LINE_TAGS)):
        tag.insert_before(_BREAK)
    for tag in soup.find_all(list(_HTML_BLOCK_TAGS)):
        tag.insert_before(_BREAK)
        tag.insert_after(_BREAK)
    text = soup.get_text("").replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", part).strip() for part in text.split(_BREAK)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


#: What a body is decoded as when it is valid UTF-8, and what an unreadable
#: ``charset=`` falls back to. RFC 2045 makes US-ASCII the default for a part
#: that names nothing; UTF-8 is a superset of it, so it is the wider guess.
_DEFAULT_BODY_CHARSET = "utf-8"

#: The last resort for bytes that are neither UTF-8 nor labelled with a charset
#: Python knows. A superset of Latin-1 over the printable range, so a European
#: body arrives readable instead of as a column of replacement characters.
_FALLBACK_BODY_CHARSET = "cp1252"

_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([\w.:+-]+)""", re.I)


def _body_charset(part: dict) -> str:
    """The charset the part claims, or the default when it claims none."""
    match = _CHARSET_RE.search(_part_headers(part).get("content-type", ""))
    return match.group(1) if match else _DEFAULT_BODY_CHARSET


def _decode_body(raw: bytes, charset: str) -> str:
    """A body's bytes as text, preferring UTF-8 and falling back on the label.

    Gmail hands back the part's bytes with only the transfer-encoding undone —
    the charset is still the sender's, and it is named in the part's own
    ``Content-Type``. This decoded every one of them as UTF-8, so a body in any
    of the single-byte encodings half of Europe still sends in arrived with a
    U+FFFD where each accent and each currency symbol had been: "8 000 \u20ac par
    mois" as "8 000 \ufffd par mois", "Ren\u00e9" as "Ren\ufffd".

    That is not a cosmetic loss. The euro sign is what
    :mod:`app.services.currency` reads a band's currency off, and without it
    there is no band; the accented letters are what
    :func:`app.services.places.fold_diacritics` folds for every matcher in the
    product, and a replacement character folds to nothing. The greeting the
    reply opens with is taken from the same string.

    UTF-8 is tried first rather than the label, because a mislabelled body is
    commoner than an exotic one and non-ASCII bytes that decode cleanly as UTF-8
    were UTF-8. Only when that fails does the label decide, which makes this
    strictly better than decoding everything as UTF-8: the bytes this used to
    mangle are exactly the bytes UTF-8 refuses.
    """
    try:
        return raw.decode(_DEFAULT_BODY_CHARSET)
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        logger.warning("unknown message body charset %r", charset)
        return raw.decode(_FALLBACK_BODY_CHARSET, errors="replace")


def _first_body(part: dict, mime_type: str) -> str | None:
    """The first inline body of *mime_type* in this part tree, decoded.

    A body Gmail sent without its base64 padding, or with bytes that are not
    base64 at all, reads as no body rather than as an exception: this runs
    inside the poll of a whole thread, and one malformed part must not end it.
    """
    if part.get("mimeType") == mime_type:
        data = part.get("body", {}).get("data")
        if data:
            try:
                raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            except ValueError:
                logger.warning("message body part is not valid base64; skipping")
                raw = b""
            if raw:
                return _decode_body(raw, _body_charset(part))
    for sub in part.get("parts", []) or []:
        found = _first_body(sub, mime_type)
        if found:
            return found
    return None


def extract_plain_text(message: dict) -> str:
    """The body of a Gmail message resource, as text.

    ``text/plain`` first, because that is the sender's own rendering and needs no
    interpretation. Where there is none — an Outlook client set to HTML, and most
    of the ATS mailers this product reads — the ``text/html`` alternative is
    converted rather than skipped.

    Skipping it is what this used to do, and the fallback it fell through to was
    ``snippet``: Gmail's own ~200-character preview, whitespace collapsed and cut
    mid-sentence. That string is not a body. It was stored as ``body_text`` and
    then read as one by everything downstream — the intent classifier, the
    opt-out and bounce detectors, the salary and role extractors, the quoted
    transcript :func:`app.services.conversation_stage.reconstruct` rebuilds a
    conversation from, and the prompt the reply is drafted against. A message
    whose quoted thread ran to five turns arrived as one truncated sentence with
    no quotes in it at all, which is exactly the shape "first contact" is
    inferred from.
    """
    payload = message.get("payload", {})
    text = _first_body(payload, "text/plain")
    if text is None:
        html = _first_body(payload, "text/html")
        if html is not None:
            text = html_to_text(html)
    return (text or message.get("snippet", "") or "").strip()


#: Control characters have no business in a name we are about to store, list in
#: the inbox and put in a ``Content-Disposition``. A decoded word can carry
#: anything its encoding could hold, a newline included.
_FILENAME_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def decoded_filename(name: str | None) -> str:
    """*name* with any encoded word read, and nothing a header may not hold.

    Split out of :func:`part_filename` because the same string is read back off
    :attr:`Email.inbound_attachments` long after the MIME part it came from is
    out of reach. Every message ingested before ``part_filename`` existed stored
    the blob, and the scan window means that mail is never seen again — see
    :meth:`InboundAttachment.from_dict`. Idempotent, so a row written either
    side of that reads the same.
    """
    name = (name or "").strip()
    if "=?" in name:
        try:
            decoded = str(make_header(decode_header(name)))
        except Exception:  # noqa: BLE001 - a malformed encoded word stays itself
            decoded = name
        name = decoded
    return " ".join(_FILENAME_CONTROL_RE.sub(" ", name).split()).strip()


@dataclass(frozen=True)
class InboundAttachment:
    """A file hanging off a message *we received*, named but not yet fetched.

    Gmail hands back a message resource with the parts described and the bytes
    withheld — a part carries an ``attachmentId`` you redeem separately. That
    split is worth keeping rather than flattening: recording the description at
    ingest costs nothing and lets the inbox list what a recruiter sent, while
    the bytes stay in Gmail until somebody actually opens the file. A mailbox
    full of 2 MB job specs never lands in our database.
    """

    filename: str
    mime_type: str
    attachment_id: str
    size: int = 0

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "attachment_id": self.attachment_id,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> InboundAttachment | None:
        """Rebuild one from a stored row, or ``None`` if it can't be redeemed.

        Stored JSON is not a schema. A row written by an older build, or with
        the id missing, must read back as "no attachment" rather than raise on
        a page load.

        The name is decoded on the way out as well as on the way in. Every
        message ingested before :func:`part_filename` existed stored the encoded
        word Gmail handed over, and ``inbound_scanner``'s scan window means that
        mail is never looked at again — so a row that is already wrong can only
        be fixed here. :func:`decoded_filename` is idempotent, so a row written
        after the fix passes through untouched.
        """
        if not isinstance(data, dict):
            return None
        filename = decoded_filename(data.get("filename"))
        attachment_id = (data.get("attachment_id") or "").strip()
        if not filename or not attachment_id:
            return None
        return cls(
            filename=filename,
            mime_type=data.get("mime_type") or "application/octet-stream",
            attachment_id=attachment_id,
            size=int(data.get("size") or 0),
        )


def _part_headers(part: dict) -> dict[str, str]:
    return {
        (h.get("name") or "").lower(): (h.get("value") or "")
        for h in part.get("headers", []) or []
    }


def _filename_from_part_headers(part: dict) -> str:
    """The name this part's own MIME headers give it, or ``""``.

    Only consulted when Gmail's ``filename`` field is empty, and that happens
    for two shapes of real mail. A sender that writes the name the RFC 2231 way
    — ``filename*=UTF-8''Lebenslauf%20J%C3%B6rg.pdf``, which is how every
    modern client sends a non-ASCII name — and an older one that puts no name on
    ``Content-Disposition`` at all and leaves it on ``Content-Type; name=``.
    Either way :func:`list_attachments` used to see an empty string and drop the
    part, so a file the recruiter really did send was not merely mislabelled in
    the inbox: it was not there.

    ``Message.get_filename`` is the stdlib's answer to both — it collapses the
    2231 continuations and falls back to ``name`` — and it returns ``None`` when
    no name is stated anywhere, which is what keeps a *body* out of the list.
    Gmail hands back a large ``text/html`` body through an ``attachmentId`` just
    as it does a real attachment, and the only thing separating the two is that
    a body is not named.
    """
    headers = _part_headers(part)
    probe = Message()
    if content_type := headers.get("content-type"):
        probe["Content-Type"] = content_type
    if disposition := headers.get("content-disposition"):
        probe["Content-Disposition"] = disposition
    try:
        return (probe.get_filename() or "").strip()
    except Exception:  # noqa: BLE001 - a malformed parameter is not a crash
        return ""


def part_filename(part: dict) -> str:
    """The name a sender gave a MIME part, as the characters they typed.

    Gmail's API decodes nothing — the same fact ``inbound_scanner`` learned
    about ``From`` and ``Subject``, learned again one field along. A recruiter
    attaching ``Lebenslauf Jörg.pdf`` arrived as
    ``=?UTF-8?B?TGViZW5zbGF1ZiBKw7ZyZy5wZGY=?=`` and that blob was stored, listed
    in the inbox as the file's name, and written into the
    ``Content-Disposition`` of the download.

    Which is worse than ugly. ``email_attachments.served_media_type`` gives an
    ``application/octet-stream`` part — what most mail clients declare when they
    are guessing — the benefit of the *extension*, and an encoded word has no
    extension. So the PDF the preview pane exists for was served opaque and the
    candidate got a download of unnamed bytes instead of a document.

    Never raises: an encoding Python does not know gives back the header as it
    arrived, the same trade :func:`inbound_scanner.decode_header_value` makes.
    """
    name = (part.get("filename") or "").strip()
    if not name:
        name = _filename_from_part_headers(part)
    return decoded_filename(name)


def _is_embedded_image(part: dict) -> bool:
    """True for a signature logo rather than a document.

    A tracking pixel or a company logo in a signature is a MIME part with a
    filename and an ``attachmentId``, structurally identical to the job spec
    the recruiter actually attached. What separates them is the ``Content-ID``
    the HTML body references it by, together with an *inline* disposition —
    both, because a genuine attachment can carry either one alone. Listing
    these would put three logo badges on every recruiter email and bury the
    one file the candidate wants to open.
    """
    headers = _part_headers(part)
    disposition = headers.get("content-disposition", "").lower()
    return bool(headers.get("content-id")) and disposition.startswith("inline")


def list_attachments(message: dict) -> list[InboundAttachment]:
    """Every real file on a Gmail message resource, in MIME order.

    Walks the whole part tree: attachments on a multipart/alternative message
    sit as siblings of the body, and on a forwarded one they can be nested
    several levels down inside a message/rfc822 part.
    """
    found: list[InboundAttachment] = []

    def _walk(part: dict) -> None:
        filename = part_filename(part)
        body = part.get("body", {}) or {}
        attachment_id = body.get("attachmentId")
        if filename and attachment_id and not _is_embedded_image(part):
            found.append(
                InboundAttachment(
                    filename=filename,
                    mime_type=part.get("mimeType") or "application/octet-stream",
                    attachment_id=attachment_id,
                    size=int(body.get("size") or 0),
                )
            )
        for sub in part.get("parts", []) or []:
            _walk(sub)

    _walk(message.get("payload", {}) or {})
    return found


def get_attachment(account, message_id: str, attachment_id: str) -> bytes:
    """Redeem one ``attachmentId`` for the bytes behind it.

    Gmail base64url-encodes the payload, and does so without padding often
    enough that decoding has to tolerate it.
    """
    service = _service(account)
    response = _execute(
        lambda: service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = response.get("data") or ""
    if not data:
        return b""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)
