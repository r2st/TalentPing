"""Gmail connection schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AuthorizeResponse(BaseModel):
    authorization_url: str
    state: str


class GmailAccountUpdate(BaseModel):
    """The one thing a mailbox row can be told to do: become the sender.

    A single optional field rather than a bare ``POST .../primary`` so demoting
    has somewhere to live if it is ever wanted — though today it has no meaning,
    since a user with mailboxes always has exactly one primary among them.
    """

    is_primary: bool | None = None


class GmailWatchOut(BaseModel):
    """A mailbox's push subscription, as the settings UI shows it.

    Declared before :class:`GmailAccountOut` because that model embeds it.
    """

    id: int
    # active | failed | stopped. Anything but active means this mailbox is
    # polled instead — slower, but never silent.
    status: str
    topic: str | None = None
    expires_at: datetime | None = None
    last_renewed_at: datetime | None = None
    last_notified_at: datetime | None = None
    notifications_received: int = 0
    messages_ingested: int = 0
    # Why push isn't running, when it isn't. Shown verbatim: a user whose
    # replies are arriving slowly deserves the actual reason.
    last_error: str | None = None

    model_config = {"from_attributes": True}


class GmailAccountOut(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    status: str
    is_primary: bool
    last_used_at: datetime | None = None
    created_at: datetime

    # ---- Per-mailbox facts ----
    # These used to be reported once, for the primary, at the top of GmailStatus.
    # With several mailboxes that is not an abbreviation, it is a wrong answer:
    # one mailbox's failed watch would read as "replies have stopped" for all of
    # them, and a second mailbox's own warm-up ramp would look like a bug in the
    # first. Each row carries its own.
    watch: GmailWatchOut | None = None
    # Distinct from ``watch.status``: a lapsed subscription still says "active"
    # because renewal keeps the row looking fine. See GmailStatus.push_healthy.
    push_healthy: bool = False
    # reputation_service.warmup_progress(): where this mailbox is on its ramp.
    # Surfacing it per account is what stops "my second mailbox only sends 5 a
    # day" reading as a fault rather than the deliberate ramp it is.
    warmup: dict | None = None
    paused_until: datetime | None = None
    pause_reason: str | None = None

    # ---- The grant's own clock ----
    # When this mailbox's refresh token lapses of its own accord, on a
    # deployment that has said its Google consent screen is unpublished. Null
    # whenever that is not known — which includes rows connected before the
    # issue date was recorded — and null is "we cannot say", never "it is fine".
    grant_expires_at: datetime | None = None
    # Whole days remaining, floored: 1 means at least another day.
    grant_expires_in_days: int | None = None
    # True once it is close enough to interrupt the user for. A separate field
    # rather than a threshold the client reapplies, so the two screens that
    # warn cannot drift apart about when to.
    grant_expiring: bool = False
    # Why it expires and what stops it happening again, in one sentence. The
    # fix is a Google Cloud setting, not anything in this product, so the copy
    # has to say so or the user reconnects weekly forever.
    grant_expiry_reason: str | None = None

    model_config = {"from_attributes": True}


class GmailStatus(BaseModel):
    # Whether the *server* has OAuth credentials at all.
    configured: bool
    # Whether *this user* has a live connection.
    connected: bool
    accounts: list[GmailAccountOut] = []
    # Whether this deployment has a Pub/Sub topic at all.
    push_configured: bool = False
    # The primary mailbox's subscription and whether it can be trusted *right
    # now*. Both are per-mailbox facts and both now live on each entry of
    # ``accounts`` as well; these two stay because they are what the older
    # client reads, and a deploy should not blank a working panel mid-flight.
    # New UI should read the per-account fields — with several mailboxes, the
    # primary's watch is not the user's watch.
    push_healthy: bool = False
    watch: GmailWatchOut | None = None
