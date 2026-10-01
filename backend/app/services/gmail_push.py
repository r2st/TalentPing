"""Gmail push notifications — the mailbox tells us, instead of us asking.

Polling every mailbox every five minutes has two costs that both get worse as
the product grows: a reply sits unseen for up to five minutes, and a candidate
whose inbox is quiet all day still burns a full sweep of API calls per tick.
Gmail's ``users.watch`` inverts it — Google publishes to a Cloud Pub/Sub topic
the moment a mailbox changes, Pub/Sub POSTs the webhook, and a quiet mailbox
costs nothing.

What a notification actually contains is the thing that shapes this module:
**not the message**. It carries the mailbox address and a ``historyId``, and
nothing else. So the webhook's job is to identify the account and enqueue a
fetch; the message bodies come from ``users.history.list`` starting at the
cursor we stored last time.

Three consequences, each of which is a rule here:

* **The cursor advances only after the work is done.** A crash between "read
  the notification" and "store the messages" must replay, not skip — a skipped
  history range is a reply the candidate never sees, and nothing later would
  notice it was missing.
* **Watches expire after seven days,** silently. A lapsed watch produces no
  error anywhere; push simply stops. :func:`due_for_renewal` is what makes that
  visible, and the beat task renews well before the deadline.
* **Push is an optimization, never the only route.** Anything that fails here
  marks the watch non-active, and :mod:`app.tasks.inbox_tasks` polls that
  mailbox exactly as it did before. The feature degrades to the old behaviour
  rather than to silence.

The webhook endpoint itself is unauthenticated by necessity — Pub/Sub has no
user session — so it is deliberately incurious: it verifies the shared token,
resolves the mailbox, and enqueues. It never trusts the payload for anything
beyond "which mailbox changed".
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.pii import mask_email
from app.models.gmail_account import GmailAccount
from app.models.gmail_watch import WATCH_TTL_DAYS, GmailWatch
from app.services import gmail_service

logger = logging.getLogger(__name__)

# Renew this far ahead of expiry. Google's guidance is to re-watch at least
# daily; two days of slack means a worker outage over a weekend still doesn't
# drop push, and re-watching is idempotent so an early renewal costs nothing.
RENEW_BEFORE = timedelta(days=2)


class PushNotConfigured(RuntimeError):
    """Push is not set up on this deployment — polling covers the mailbox."""


@dataclass(frozen=True)
class Notification:
    """The decoded contents of one Pub/Sub push message."""

    email_address: str
    history_id: str


def is_configured() -> bool:
    """True when this deployment has a Pub/Sub topic to watch against."""
    return bool(settings.gmail_pubsub_topic)


# --------------------------------------------------------------------------- #
# Decoding the webhook payload                                                 #
# --------------------------------------------------------------------------- #


def decode_notification(payload: dict) -> Notification | None:
    """Pull the mailbox and history id out of a Pub/Sub push envelope.

    The shape is ``{"message": {"data": "<base64 of a JSON object>"}}``. Every
    malformed variant returns ``None`` rather than raising: the webhook is a
    public endpoint, so a bad body is an expected input, and the correct
    response to one is a 204 that stops Pub/Sub retrying it forever.
    """
    message = (payload or {}).get("message")
    if not isinstance(message, dict):
        return None

    raw = message.get("data")
    if not isinstance(raw, str) or not raw:
        return None

    try:
        # Pub/Sub uses standard base64; padding is sometimes stripped in transit.
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        data = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        logger.warning("gmail webhook: undecodable message data")
        return None

    if not isinstance(data, dict):
        return None

    address = str(data.get("emailAddress") or "").strip().lower()
    history_id = str(data.get("historyId") or "").strip()
    if not address or not history_id:
        return None

    return Notification(email_address=address, history_id=history_id)


def token_is_valid(token: str | None) -> bool:
    """Whether a webhook request carries the configured shared secret.

    When no token is configured the endpoint accepts anything — appropriate for
    local development, and the reason :func:`is_configured` gates registration
    in the first place. A deployment that sets the topic should set this too.

    ``compare_digest`` rather than ``==``: this is the only secret a caller gets
    to guess against, the endpoint is public by necessity, and ``==`` on ``str``
    returns as soon as two bytes differ. That leaks the shared secret a character
    at a time to anyone willing to time the responses — and what it buys is the
    ability to forge Gmail notifications for any address, which is a free
    inbox-poll trigger against arbitrary connected mailboxes.
    """
    expected = settings.gmail_pubsub_token
    if not expected:
        return True
    return bool(token) and hmac.compare_digest(token, expected)


# --------------------------------------------------------------------------- #
# Registering and renewing                                                     #
# --------------------------------------------------------------------------- #


def _expiration_to_datetime(value) -> datetime | None:
    """Gmail returns ``expiration`` as epoch milliseconds, as a string."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None


def start_watch(db: Session, account: GmailAccount) -> GmailWatch:
    """Register (or re-register) a push subscription for one mailbox.

    Idempotent at both ends: calling ``watch()`` again replaces the previous
    subscription at Google's end, and the row is upserted here. A failure is
    recorded on the row rather than raised, because the caller's correct
    response to "push didn't start" is to keep polling — not to fail.
    """
    watch = db.scalar(
        select(GmailWatch).where(GmailWatch.gmail_account_id == account.id)
    ) or GmailWatch(gmail_account_id=account.id)
    if watch.id is None:
        db.add(watch)

    watch.topic = settings.gmail_pubsub_topic

    if not is_configured():
        watch.status = "stopped"
        watch.last_error = "No Pub/Sub topic configured; polling this mailbox"
        db.flush()
        return watch

    try:
        result = gmail_service.start_watch(account, settings.gmail_pubsub_topic)
    except Exception as exc:  # noqa: BLE001 - any failure means "keep polling"
        logger.warning(
            "gmail watch failed for %s: %s",
            mask_email(account.email),
            exc,
            exc_info=True,
        )
        watch.status = "failed"
        watch.last_error = str(exc)[:500]
        db.flush()
        return watch

    watch.status = "active"
    watch.last_error = None
    watch.last_renewed_at = datetime.now(UTC)
    watch.history_id = str(result.get("historyId") or "") or watch.history_id
    watch.expires_at = _expiration_to_datetime(result.get("expiration")) or (
        datetime.now(UTC) + timedelta(days=WATCH_TTL_DAYS)
    )
    db.flush()
    logger.info(
        "gmail push active for %s until %s",
        mask_email(account.email),
        watch.expires_at,
    )
    return watch


def stop_watch(db: Session, account: GmailAccount) -> None:
    """Cancel push for a mailbox and fall back to polling."""
    watch = db.scalar(
        select(GmailWatch).where(GmailWatch.gmail_account_id == account.id)
    )
    if watch is None:
        return
    try:
        gmail_service.stop_watch(account)
    except Exception as exc:  # noqa: BLE001 - best effort; the row is what matters
        logger.info("gmail stop-watch failed for %s: %s", mask_email(account.email), exc)
    watch.status = "stopped"
    db.flush()


def due_for_renewal(db: Session, *, now: datetime | None = None) -> list[GmailWatch]:
    """Watches close enough to expiry that they should be re-registered.

    Includes watches already marked failed: a mailbox whose registration failed
    an hour ago (rate limit, transient 503) should be retried on the next sweep
    rather than left on polling forever.
    """
    now = now or datetime.now(UTC)
    deadline = now + RENEW_BEFORE

    rows = db.scalars(select(GmailWatch)).all()
    due: list[GmailWatch] = []
    for watch in rows:
        if watch.status == "stopped":
            continue
        if watch.status == "failed":
            due.append(watch)
            continue
        expires = watch.expires_at
        if expires is None:
            due.append(watch)
            continue
        if expires.tzinfo is None:  # SQLite round-trips naive datetimes
            expires = expires.replace(tzinfo=UTC)
        if expires <= deadline:
            due.append(watch)
    return due


# --------------------------------------------------------------------------- #
# Handling a notification                                                      #
# --------------------------------------------------------------------------- #


def account_for_address(db: Session, email_address: str) -> GmailAccount | None:
    """The connected account a notification belongs to."""
    return db.scalar(
        select(GmailAccount).where(GmailAccount.email == email_address)
    )


def record_notification(db: Session, watch: GmailWatch, history_id: str) -> None:
    """Book the fact that Google told us something changed.

    Deliberately does **not** advance ``history_id``: that cursor marks how far
    the mailbox has been *processed*, and the fetch hasn't run yet. Advancing it
    here would make a crash mid-fetch look like completed work and silently skip
    the messages in between.
    """
    watch.last_notified_at = datetime.now(UTC)
    watch.notifications_received = (watch.notifications_received or 0) + 1
    if watch.status != "active":
        # A notification is proof push is working, whatever we thought.
        watch.status = "active"
        watch.last_error = None
    db.flush()


def advance_cursor(db: Session, watch: GmailWatch, history_id: str, ingested: int) -> None:
    """Move the processed-up-to cursor after the messages it covers are stored."""
    watch.history_id = str(history_id)
    watch.messages_ingested = (watch.messages_ingested or 0) + max(0, ingested)
    db.flush()


def push_is_healthy(watch: GmailWatch | None, *, now: datetime | None = None) -> bool:
    """Whether a mailbox's push subscription can be trusted right now.

    An active-but-silent watch is the failure this guards. Renewal keeps
    ``expires_at`` in the future even when Google has quietly stopped
    delivering, so "active" alone is not evidence; a watch past its expiry is
    not healthy regardless of what its status column says.
    """
    if watch is None or not watch.is_active:
        return False
    expires = watch.expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > (now or datetime.now(UTC))


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; every comparison here needs UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def push_covers(watch: GmailWatch | None, *, now: datetime | None = None) -> bool:
    """Whether push is doing this mailbox's job well enough for beat to stand down.

    Two conditions, and the second one is the entire reason this function exists
    separately from :func:`push_is_healthy`:

    * **Registered.** There is an active watch that has not passed its expiry.
    * **Delivering.** Google said something within
      ``recruiter_push_trust_seconds``, or the watch was registered that recently
      and simply has had nothing to report yet.

    ``push_is_healthy`` alone would be a trap. A subscription can sit at
    ``status="active"`` with a future ``expires_at`` while Google has quietly
    stopped publishing — renewal keeps the row looking healthy either way — so
    "active" is not evidence of delivery. Trusting it alone would mean a silently
    dead subscription switches a mailbox *off* rather than falling back to
    polling, which is the one thing this feature must never do.

    A quiet mailbox with working push therefore gets scanned by beat every so
    often anyway. That is the cost, it is one ``messages.list`` per fifteen
    minutes, and it buys the guarantee that push failing is slow rather than
    silent.
    """
    now = now or datetime.now(UTC)
    if not push_is_healthy(watch, now=now):
        return False

    trust = settings.recruiter_push_trust_seconds
    if trust <= 0:
        # Configured never to trust push. Beat covers every mailbox, every tick.
        return False

    deadline = now - timedelta(seconds=trust)
    last_signal = _aware(watch.last_notified_at) or _aware(watch.last_renewed_at)
    if last_signal is None:
        return False
    return last_signal > deadline


__all__ = [
    "RENEW_BEFORE",
    "Notification",
    "PushNotConfigured",
    "account_for_address",
    "advance_cursor",
    "decode_notification",
    "due_for_renewal",
    "is_configured",
    "push_covers",
    "push_is_healthy",
    "record_notification",
    "start_watch",
    "stop_watch",
    "token_is_valid",
]
