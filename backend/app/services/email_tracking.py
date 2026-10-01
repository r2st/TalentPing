"""Open and click tracking for outbound outreach.

Builds the HTML alternative part of a message — the body plus wrapped links plus
a 1x1 pixel — and records the events that come back. Everything user-facing about
the *reliability* of this data is documented in docs/features/email-tracking.md
§2; the short version is that Gmail proxies images and Outlook blocks them, so an
open is evidence, not proof. Two mitigations live here:

* opens inside :data:`DEDUPE_WINDOW` of the previous one count once, so one
  reader refreshing their mail client isn't five opens;
* an open within :data:`PREFETCH_GRACE` of the send is flagged ``is_prefetch``
  and excluded from rates, because that is a mail proxy caching the pixel rather
  than anyone reading anything.

The two interact, and the order matters: the dedupe merges repeats *of the same
kind*, and never merges a prefetch with the first genuine read after it. See
:func:`record_open`.

The write path is reachable unauthenticated (a recruiter's mail client has no
session), which shapes every function below: unknown tokens are silently
ignored rather than 404'd, and the row's ``user_id`` always comes from the email
we looked up, never from the request.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import quote, urlparse

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.email import Email
from app.models.email_event import EmailEvent, EmailEventType

logger = logging.getLogger(__name__)

# A repeat open inside this window is the same open.
DEDUPE_WINDOW = timedelta(minutes=10)
# An open this soon after the send is a mail proxy, not a person.
PREFETCH_GRACE = timedelta(seconds=60)

# A 1x1 fully transparent GIF. The smallest thing that is still an image.
TRANSPARENT_GIF = base64.b64decode(
    b"R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# Bare URLs in the plain-text body. Trailing punctuation is excluded so a link
# ending a sentence doesn't swallow the full stop.
_URL_RE = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]]")


def _api_base() -> str:
    return settings.tracking_base_url.rstrip("/")


def ensure_token(email: Email) -> str:
    """The message's tracking token, minting one on first use. Idempotent."""
    if not email.tracking_token:
        email.tracking_token = secrets.token_urlsafe(32)
    return email.tracking_token


def pixel_url(token: str) -> str:
    return f"{_api_base()}/t/o/{quote(token, safe='')}.gif"


def encode_target(url: str) -> str:
    """urlsafe-base64 the click target so `&` and `#` survive the round trip."""
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def decode_target(encoded: str) -> str | None:
    """Reverse :func:`encode_target`, or None when it isn't valid."""
    if not encoded:
        return None
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def is_safe_target(url: str | None) -> bool:
    """True for plain http(s) URLs only.

    The scheme half of the redirect guard: it is what stops a ``javascript:``
    URL reaching a ``Location`` header. It says nothing about *where* an http
    URL points, which is :func:`is_tracked_target`'s job — a click endpoint that
    checked only this is still an open redirect, just a well-formed one.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_tracked_target(email: Email, url: str | None) -> bool:
    """Whether *url* is a link the message identified by this token actually carried.

    The click endpoint took its destination from a query parameter and followed
    it on the strength of ``is_safe_target`` alone, which only ever asked
    "is this http?". So ``/t/c/<anything>?u=<base64 of https://evil.example>``
    redirected off the product's own domain, from a public unauthenticated
    route, whether or not the token named a real message. That is the textbook
    open redirect, and it is worth more to an attacker here than most: the link
    a victim inspects is on the domain their recruitment mail already comes
    from, and the domain wearing the phishing is the one whose deliverability
    this whole subsystem exists to protect.

    Nothing needs signing to close it. The wrapped links were built from
    ``build_tracked_html``, which wraps exactly the URLs it found in
    ``body_text`` — so the set of destinations this endpoint is ever supposed to
    emit is already stored, and a target that is not in it was not in the
    message. Derived rather than signed on purpose: every link already sitting
    in a recruiter's mailbox keeps working, which a signature scheme could not
    offer without stranding the recipients it most matters to.

    Matched exactly, against the same regex that produced the wrapping, so a
    target that merely *starts with* a real one (``https://acme.com.evil.test``
    beside a genuine ``https://acme.com``) is not accepted for it.
    """
    if not url:
        return False
    return any(match.group(0) == url for match in _URL_RE.finditer(email.body_text or ""))


def wrap_link(token: str, url: str) -> str:
    return f"{_api_base()}/t/c/{quote(token, safe='')}?u={encode_target(url)}"


def _should_wrap(url: str, unsubscribe_url: str | None, *, in_body: bool) -> bool:
    """Never wrap the opt-out link, our own tracking URLs, or a footer link.

    Breaking one-click unsubscribe in order to measure a click would be both
    illegal under CAN-SPAM and stupid.

    *in_body* is the third rule and it is the one that keeps this function
    honest with :func:`is_tracked_target`. The two are a pair: this decides
    which links get a ``/t/c/`` URL, and that decides which targets the click
    endpoint will follow — and they read *different strings*. Wrapping ran over
    the body **and the footer**; the allowlist is derived from ``body_text``
    alone, because the footer is boilerplate rebuilt at send time and is not
    stored on the row.

    So a URL in the footer was wrapped into a tracking link and then refused by
    the endpoint it pointed at: the recipient landed on this product's own
    front page instead of wherever the link said, from a message we sent them.
    ``canspam_footer`` interpolates ``COMPLIANCE_PHYSICAL_ADDRESS`` verbatim,
    and a compliance line that names the sender's website — an ordinary way to
    write one — is a dead link in every piece of cold outreach the deployment
    sends.

    Not wrapped rather than newly allowed, because the footer is the same on
    every message: a click on it measures nothing about this email, and the
    endpoint's allowlist is exactly the guarantee that stops ``/t/c/`` being an
    open redirect. Widening it to reproduce the footer would trade a real
    property for a statistic nobody wants.
    """
    if not is_safe_target(url):
        return False
    if not in_body:
        return False
    if unsubscribe_url and url.split("?")[0] == unsubscribe_url.split("?")[0]:
        return False
    return not url.startswith(_api_base())


def build_tracked_html(
    body_text: str,
    token: str,
    *,
    footer: str = "",
    unsubscribe_url: str | None = None,
) -> str:
    """The HTML alternative: the body, links wrapped, pixel last.

    The body is escaped before any markup is inserted, so a recruiter's name
    containing ``&`` or a body containing ``<`` cannot produce broken — or
    injected — HTML.
    """
    full = f"{body_text}\n\n{footer}" if footer else body_text
    # Where the message stops and the boilerplate starts. Only the first half is
    # stored on the row, so only the first half can be wrapped — see
    # `_should_wrap`. No match can straddle it: `_URL_RE` spans no whitespace
    # and the join is two newlines.
    body_end = len(body_text)

    # Escaping must happen *around* the URLs, not before them: escaping first
    # would turn every `&` in a query string into `&amp;`, and the wrapped
    # target would then encode that corrupted URL.
    pieces: list[str] = []
    cursor = 0
    for match in _URL_RE.finditer(full):
        pieces.append(escape(full[cursor : match.start()], quote=False))
        url = match.group(0)
        wrap = _should_wrap(
            url, unsubscribe_url, in_body=match.start() < body_end
        )
        href = wrap_link(token, url) if wrap else url
        pieces.append(f'<a href="{escape(href, quote=True)}">{escape(url, quote=False)}</a>')
        cursor = match.end()
    pieces.append(escape(full[cursor:], quote=False))
    linked = "".join(pieces)

    paragraphs = [
        p.replace("\n", "<br>\n") for p in re.split(r"\n\s*\n", linked) if p.strip()
    ]
    body_html = "\n".join(f"<p>{p}</p>" for p in paragraphs)

    pixel = (
        f'<img src="{pixel_url(token)}" width="1" height="1" '
        'alt="" style="display:block;border:0" />'
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.5;color:#111">\n'
        f"{body_html}\n{pixel}\n</div>"
    )


def hash_ip(ip: str | None) -> str | None:
    """A salted, truncated hash of the caller's IP — never the address itself."""
    if not ip:
        return None
    digest = hashlib.sha256(f"{ip}{settings.jwt_secret}".encode()).hexdigest()
    return digest[:64]


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; the arithmetic here needs tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def email_for_token(db: Session, token: str) -> Email | None:
    """The message a tracking token belongs to, or None."""
    if not token:
        return None
    return db.scalar(select(Email).where(Email.tracking_token == token))


def _owner_id(db: Session, email: Email) -> int | None:
    """The user who sent *email*, walked through thread -> application."""
    from app.models.application import Application
    from app.models.email_thread import EmailThread

    thread = db.get(EmailThread, email.thread_id)
    if thread is None:
        return None
    application = db.get(Application, thread.application_id)
    return application.user_id if application is not None else None


def _is_genuine(event: EmailEvent) -> bool:
    """Whether one event is engagement by a person rather than a mail proxy."""
    return event.event_type == EmailEventType.CLICK or not event.is_prefetch


def _has_genuine_engagement(db: Session, email: Email) -> bool:
    """Whether a person — not a mail proxy — has already engaged with this message.

    Two decisions read this: whether the subject variant may be credited, and
    whether :data:`DEDUPE_WINDOW` may swallow an open. Both need the same answer,
    and neither can decide it from the counters on ``Email``, because those count
    things the experiment deliberately ignores:

    * ``open_count`` includes proxy prefetches, so "is this the first open?" is
      not the same question as "is this the first open by a person";
    * ``first_opened_at`` is set by a prefetch too, and set again by
      :func:`record_click` synthesising an open.

    So the answer comes from the event rows, which record what each engagement
    actually *was* — the source of truth these counters are derived from.

    Both halves are load-bearing. Sessions here are built with ``autoflush=
    False``, so the query sees only what has been flushed; an event recorded
    earlier in the same uncommitted transaction is still sitting in ``db.new``
    and would otherwise be invisible, which is a second credit for the same
    reader. Scanning ``db.new`` rather than flushing keeps this read-only —
    a service that flushes on a caller's behalf writes out whatever else that
    caller happened to have pending.
    """
    already_written = db.scalar(
        select(EmailEvent.id)
        .where(
            EmailEvent.email_id == email.id,
            or_(
                EmailEvent.event_type == EmailEventType.CLICK,
                EmailEvent.is_prefetch.is_(False),
            ),
        )
        .limit(1)
    )
    if already_written is not None:
        return True

    return any(
        isinstance(obj, EmailEvent) and obj.email_id == email.id and _is_genuine(obj)
        for obj in db.new
    )


def _credit_variant(db: Session, email: Email) -> None:
    """Book this message's open against its subject-line variant."""
    if not email.subject_variant_id:
        return
    from app.services import subject_ab_service

    subject_ab_service.record_open(db, email.subject_variant_id)


def record_open(
    db: Session,
    email: Email,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> EmailEvent | None:
    """Record an open, or None when it was deduplicated inside the window."""
    now = now or datetime.now(UTC)
    user_id = _owner_id(db, email)
    if user_id is None:
        return None

    sent_at = _aware(email.sent_at)
    is_prefetch = sent_at is not None and (now - sent_at) < PREFETCH_GRACE

    # Decided before the event row exists, for the autoflush reason in
    # `_has_genuine_engagement`.
    first_genuine = not is_prefetch and not _has_genuine_engagement(db, email)

    # The dedupe merges two fetches of the same kind. It must not merge a proxy
    # prefetch with the human read that follows it, and the two windows overlap
    # by design: the proxy fires within seconds of delivery, and a recruiter who
    # reads promptly does so well inside the following ten minutes. Swallowing
    # that read left the message with no genuine open event at all — so it fell
    # out of the open-rate numerator (`routers/analytics` filters prefetches out)
    # and its subject variant scored nothing, for the recipients who engaged
    # *fastest*. The prefetch flag exists precisely to tell these two apart;
    # letting the window override it threw the distinction away again.
    last = _aware(email.last_opened_at)
    if last is not None and now - last < DEDUPE_WINDOW and not first_genuine:
        return None

    event = EmailEvent(
        user_id=user_id,
        email_id=email.id,
        event_type=EmailEventType.OPEN,
        user_agent=(user_agent or "")[:255] or None,
        ip_hash=hash_ip(ip),
        is_prefetch=is_prefetch,
        occurred_at=now,
    )
    db.add(event)

    email.open_count = (email.open_count or 0) + 1
    email.last_opened_at = now
    if email.first_opened_at is None:
        email.first_opened_at = now

    # A genuine first open is the signal the subject-line experiment converges
    # on. A proxy prefetch fires for every delivered message and would score
    # every variant identically, so it is deliberately excluded — but only it.
    # Keying this off `open_count == 1` also excluded the *human* open that
    # follows a prefetch, which is nearly every Gmail recipient there is: the
    # proxy takes the first slot at delivery, the real read arrives as open two,
    # and the arm scored nothing for a message that was genuinely read.
    if first_genuine:
        _credit_variant(db, email)

    return event


def record_click(
    db: Session,
    email: Email,
    url: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> EmailEvent | None:
    """Record a click on *url*."""
    now = now or datetime.now(UTC)
    user_id = _owner_id(db, email)
    if user_id is None:
        return None

    # A click is a person, never a proxy — no prefetch test to apply. Same
    # autoflush ordering as the open path.
    credit_variant = not _has_genuine_engagement(db, email)

    event = EmailEvent(
        user_id=user_id,
        email_id=email.id,
        event_type=EmailEventType.CLICK,
        url=url[:2000],
        user_agent=(user_agent or "")[:255] or None,
        ip_hash=hash_ip(ip),
        occurred_at=now,
    )
    db.add(event)

    email.click_count = (email.click_count or 0) + 1
    if email.first_clicked_at is None:
        email.first_clicked_at = now
    # A click is a strictly stronger signal than an open, and image blocking
    # means it routinely arrives without one. Count it as an open too, or a
    # recipient who read the message in Outlook and clicked through reads as
    # "never opened it".
    if email.first_opened_at is None:
        email.first_opened_at = now
        email.last_opened_at = now
        email.open_count = (email.open_count or 0) + 1

    # Credited even when the message already has an open on it, because the one
    # it has may be a prefetch — and a prefetch is exactly what the arm is not
    # allowed to score. This is the only signal an image-blocking recipient ever
    # produces, so without it the experiment is blind to every Outlook reader.
    if credit_variant:
        _credit_variant(db, email)

    return event


__all__ = [
    "DEDUPE_WINDOW",
    "PREFETCH_GRACE",
    "TRANSPARENT_GIF",
    "build_tracked_html",
    "decode_target",
    "email_for_token",
    "encode_target",
    "ensure_token",
    "hash_ip",
    "is_safe_target",
    "is_tracked_target",
    "pixel_url",
    "record_click",
    "record_open",
    "wrap_link",
]
