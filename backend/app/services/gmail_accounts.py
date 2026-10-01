"""Which mailbox does this piece of work belong to?

The data model has always allowed a user several connected Gmail accounts. What
the product did with that was read ``user.primary_gmail`` everywhere, which is
the right answer to exactly one question — "what is this user's default sending
identity?" — and the wrong answer to most of the questions that were asking it.

Wrong, not merely limited. A recruiter who writes to the candidate's second
address and gets an answer from their first sees a broken thread and a sender
they have no record of contacting, which is what a spoof looks like. A thread
that lives in mailbox B, polled with mailbox A's credentials, returns a Gmail
404. A bounce on B's outreach that lands on A's ledger can pause the wrong
mailbox.

So this module answers the narrower questions, and ``user.primary_gmail`` stays
as what every resolver here falls back *to*:

* :func:`resolve_for_thread` — the mailbox a conversation happens in.
* :func:`resolve_for_new_outreach` — the mailbox a new conversation starts from.
* :func:`live_accounts` — every mailbox worth doing background work on.
* :func:`set_primary` — move the sending identity.

**Nothing here ever returns a mailbox that is not ``connected``.** A revoked
grant cannot send, and handing one back so the caller can fail later just moves
the error somewhere less legible. Callers get ``None`` and treat it the same way
they already treat "no Gmail connected", which is a path every one of them
already has.

**Single-mailbox users see no change.** Every branch below collapses to
``primary_gmail`` when there is one account, which is the account it always was.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.pii import mask_email
from app.models.application import Application
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.gmail_account import GmailAccount
from app.models.profile import Profile
from app.models.user import User

logger = logging.getLogger(__name__)

CONNECTED = "connected"


def live_accounts(user: User | None) -> list[GmailAccount]:
    """This user's connected mailboxes, primary first, then oldest first.

    The order is the one the UI shows and the one background fan-out follows, so
    "the first mailbox" means the same thing in both places.
    """
    if user is None:
        return []
    live = [a for a in user.gmail_accounts if a.status == CONNECTED]
    return sorted(live, key=lambda a: (not a.is_primary, a.id))


def account_for_address(user: User | None, address: str | None) -> GmailAccount | None:
    """The connected mailbox an address names, if it names one of this user's.

    The value being matched is usually a raw ``To:`` header — ``"Jane Doe"
    <jane@gmail.com>``, sometimes several addresses — so the header is parsed
    and the addresses in it compared exactly.

    It used to be a substring test, on the stated grounds that parsing was the
    caller's job. Substring is not "equals, allowing for a display name": it is
    also true of an address the user's merely sits *inside*, on both sides.
    ``"jane@acme.com" in "mary.jane@acme.com"`` and ``"jane@acme.com" in
    "jane@acme.com.mx"`` are the two shapes, and
    :func:`app.services.inbound_scanner.is_from_self` carries a docstring about
    the second of them — it was fixed there and left here.

    What it costs is the mailbox a reply argues from. This is the fallback that
    decides which of the user's addresses answers a thread whose
    ``gmail_account_id`` was never written, so a candidate holding both
    ``jane@`` and ``mary.jane@`` on one domain had replies to one conversation
    sent from the other — a different signature, a different sending identity,
    and a thread the recruiter sees split across two people.
    """
    if user is None or not address:
        return None
    named = {
        parsed.strip().lower()
        for _name, parsed in getaddresses([address])
        if parsed and parsed.strip()
    }
    if not named:
        return None
    for account in live_accounts(user):
        if account.email and account.email.strip().lower() in named:
            return account
    return None


def sent_header_variants(
    db: Session, user_id: int, address: str | None, *, since: datetime | None = None
) -> list[str]:
    """Every ``from_address`` spelling in this user's sent mail that *is* *address*.

    ``from_address`` holds whatever went into the header — bare ``jane@x.com`` on
    some rows, ``Jane Doe <jane@x.com>`` on others — so neither equality nor the
    obvious ``LIKE '%addr%'`` is a correct test. Equality misses the display-name
    rows; ``LIKE`` answers *yes* for ``jane@x.com`` when the stored value is
    ``not-jane@x.com``, which would credit one mailbox with another's sending.

    The distinct-header set is small, so it is pulled back and parsed with
    :func:`email.utils.parseaddr`, and only exact address matches are returned.
    The result is meant to be fed to an ``IN`` clause, which keeps the parsing in
    Python and the counting in the database.

    *since* bounds the scan to recent mail, for the callers that only care about
    a window.
    """
    if not address:
        return []
    wanted = address.strip().lower()
    if not wanted:
        return []

    query = (
        select(Email.from_address)
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == user_id,
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.SENT,
            Email.from_address.is_not(None),
        )
        .distinct()
    )
    if since is not None:
        query = query.where(Email.sent_at >= since)
    headers = db.scalars(query).all()
    return [h for h in headers if parseaddr(h or "")[1].strip().lower() == wanted]


def sent_in_last_24h(
    db: Session,
    user_id: int,
    account: GmailAccount | None = None,
    *,
    now: datetime | None = None,
) -> tuple[int, datetime | None]:
    """Sends in the last 24h for one mailbox, and the ``sent_at`` of the oldest.

    The rolling window the warm-up ceiling is enforced against. It lives here,
    beside the resolvers, because it answers the same question they do — *which
    mailbox does this belong to?* — and because it had been written twice, in
    :mod:`app.tasks.email_tasks` and :mod:`app.services.auto_apply_service`, with
    the same defect in both copies.

    **A mailbox is identified by its address as well as by its row.** The thread
    stamp alone is not enough, and the way it fails is not a rounding error: the
    ``email_threads.gmail_account_id`` foreign key is ``ON DELETE SET NULL``, so
    disconnecting a mailbox blanks the stamp on every thread it ever sent from.
    Reconnecting the same address then builds a *new* row with a new id, and the
    window it can see starts empty — while
    :func:`app.services.reputation_service.adopt_send_history` has just restored
    the ramp to that address's real, fully-warmed ceiling.

    The two compose into a burst: a mailbox that had already spent its thirty
    sends for the day came back able to spend thirty more immediately, which is
    exactly the pattern a rolling window exists to stop (see
    :func:`app.services.reputation_service.roll_daily_counter` on why enforcement
    is deliberately not on the calendar day). ``Email.from_address`` is stamped
    with the sending address on every successful send and is a plain column, so
    it survives the row being deleted — it is the part of the ledger that belongs
    to the *address* rather than to the row.

    Matching is on parsed addresses, never a substring: see
    :func:`sent_header_variants` for why ``LIKE '%addr%'`` would credit one
    mailbox with another's sending.

    The oldest send comes back with the count because it is what says *when*
    capacity next frees up — a row ages out of a rolling window exactly 24h after
    it went out. It is the same scan, so it costs nothing, and it saves the
    reputation gate from guessing.

    *account* of ``None`` gives the user-wide count, which is the right answer
    when there is no mailbox to attribute the send to.

    *now* is injectable because everything this number is weighed against is —
    :func:`app.services.reputation_service.evaluate`, ``warmup_day_limit`` and
    ``adopt_send_history`` all take one, and this was the last link in the chain
    that could only read the wall clock. That asymmetry is not a style
    complaint. A test that pins the rest of the chain to a fixed instant has to
    place *these* rows against the real clock, and a row written at the pinned
    instant silently ages out of the window once real time moves past it: the
    assertion that a mailbox counted twenty sends starts failing a day later,
    and — far worse — an assertion that a *different* address counted zero goes
    on passing for the wrong reason, having stopped exercising the address
    matching at all. Both had happened here; see ``test_warmup_history``.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=24)
    query = (
        select(func.count(Email.id), func.min(Email.sent_at))
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == user_id,
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.SENT,
            Email.sent_at >= since,
        )
    )
    if account is not None:
        belongs = EmailThread.gmail_account_id == account.id
        variants = sent_header_variants(db, user_id, account.email, since=since)
        if variants:
            belongs = or_(belongs, Email.from_address.in_(variants))
        query = query.where(belongs)

    count, oldest = db.execute(query).one()
    if oldest is not None and oldest.tzinfo is None:
        # SQLite hands back naive datetimes; the arithmetic downstream is aware.
        oldest = oldest.replace(tzinfo=UTC)
    return int(count or 0), oldest


def observed_send_history(
    db: Session, user_id: int, address: str | None
) -> tuple[datetime | None, int]:
    """What *address* has actually sent, as ``(first_sent_at, count)``.

    The warm-up ramp's clock is a column on the mailbox row, and that row is
    lost whenever a mailbox is removed and re-added. This is where the answer
    survives: ``emails`` keeps every delivered message with the address it went
    out as, and those rows outlive any number of reconnections. See
    :func:`app.services.reputation_service.adopt_send_history` for what is done
    with it and why that cannot be used to fake warmth.

    **Addresses are compared parsed, not by substring.** ``from_address`` holds
    whatever went into the header — bare ``jane@x.com`` on some rows,
    ``Jane Doe <jane@x.com>`` on others — and the obvious ``LIKE '%addr%'``
    answers *yes* for ``jane@x.com`` when the stored value is
    ``not-jane@x.com``. Crediting one mailbox with another's history would hand
    a brand-new address a ramp it never earned, which is the one thing this must
    not do. The distinct-header set is small, so it is pulled back and parsed
    with :func:`email.utils.parseaddr`, and only exact matches count.

    Scoped to *user_id*: another user's sends from the same address — which
    happens when a shared mailbox moves between accounts — are not this
    mailbox's reputation to inherit.
    """
    matching = sent_header_variants(db, user_id, address)
    if not matching:
        return None, 0

    row = db.execute(
        select(func.min(Email.sent_at), func.count(Email.id))
        .join(EmailThread, Email.thread_id == EmailThread.id)
        .join(Application, EmailThread.application_id == Application.id)
        .where(
            Application.user_id == user_id,
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.SENT,
            Email.from_address.in_(matching),
        )
    ).one()
    first = row[0]
    if first is not None and first.tzinfo is None:
        # SQLite hands back naive datetimes. Normalising here rather than at the
        # caller keeps "when did this address first send" a tz-aware answer on
        # every backend, which is what the ramp arithmetic compares against.
        first = first.replace(tzinfo=UTC)
    return first, int(row[1] or 0)


def resolve_for_thread(
    db: Session, thread: EmailThread | None, *, user: User | None = None
) -> GmailAccount | None:
    """The mailbox this conversation belongs to.

    In order:

    1. the thread's own ``gmail_account_id`` — the stored fact, and the only one
       of these that is not an inference;
    2. the address an inbound message on the thread was delivered to, which is
       the rule :func:`app.services.email_attachments._account_for_inbound`
       already uses and what makes rows written before the column existed
       resolve correctly;
    3. the user's primary.

    *user* is optional and purely a shortcut for callers that already loaded it.
    """
    if thread is None:
        return None
    if user is None:
        user = _user_for_thread(db, thread)
    if user is None:
        return None

    if thread.gmail_account_id is not None:
        account = db.get(GmailAccount, thread.gmail_account_id)
        # A mailbox that has since been revoked is not a usable answer, but it is
        # a real one — fall through rather than pretending the column is empty.
        if account is not None and account.status == CONNECTED:
            return account

    delivered = db.scalar(
        select(Email.to_address)
        .where(
            Email.thread_id == thread.id,
            Email.direction == EmailDirection.RECEIVED,
            Email.to_address.is_not(None),
        )
        .order_by(Email.id)
    )
    account = account_for_address(user, delivered)
    if account is not None:
        return account

    return user.primary_gmail


def _usable(
    db: Session, user: User, account_id: int | None
) -> GmailAccount | None:
    """*account_id* as a mailbox this user can actually send from, or ``None``.

    Both callers below hold an id that was valid when it was stored and may not
    be now — the mailbox can have been disconnected, or its grant revoked — so
    "the column is set" is never on its own an answer.
    """
    if account_id is None:
        return None
    account = db.get(GmailAccount, account_id)
    if account is None or account.user_id != user.id or account.status != CONNECTED:
        return None
    return account


def resolve_for_new_outreach(
    db: Session,
    user: User | None,
    *,
    campaign: Campaign | None = None,
    profile: Profile | None = None,
    profile_id: int | None = None,
) -> GmailAccount | None:
    """The mailbox to start a new conversation from.

    Most specific statement wins: the campaign's chosen mailbox, else the
    profile's, else the user's primary. A campaign is something the user built
    and named by hand, so when it carries a from-address that is a direct
    instruction; a profile's is a standing preference for a whole class of work.

    Adding a mailbox buys a *separate identity*, not extra throughput — nothing
    here rotates between accounts to raise volume, and doing so would defeat the
    warm-up ramp by construction and put a real person's personal Gmail at risk
    of suspension.
    """
    if user is None:
        return None

    chosen = _usable(db, user, campaign.gmail_account_id if campaign else None)
    if chosen is not None:
        return chosen

    if profile is None and profile_id is not None:
        profile = db.get(Profile, profile_id)
    chosen = _usable(db, user, profile.gmail_account_id if profile else None)
    if chosen is not None:
        return chosen

    return user.primary_gmail


def set_primary(db: Session, user: User, account_id: int) -> GmailAccount:
    """Move the sending identity to *account_id*, clearing it on every sibling.

    Raises :class:`LookupError` when the account is not this user's, so the
    router can answer 404 without a second query. A revoked account is refused
    too: promoting a grant that cannot send would leave the user with no working
    sender and a UI insisting otherwise.
    """
    account = db.get(GmailAccount, account_id)
    if account is None or account.user_id != user.id:
        raise LookupError("Gmail account not found")
    if account.status != CONNECTED:
        raise ValueError("That mailbox is not connected")

    for sibling in user.gmail_accounts:
        sibling.is_primary = sibling.id == account.id
    db.flush()
    return account


REVOKED = "revoked"


def mark_revoked(db: Session, account: GmailAccount, reason: str) -> GmailAccount:
    """Record that Google has stopped honouring this mailbox's grant.

    Called when a Gmail call raises :class:`~app.services.gmail_service.
    GmailAuthRevoked`. Until this existed, a dead grant left the row saying
    ``connected`` forever: the setup page showed a healthy mailbox, the resolvers
    handed it out as a sender, and every beat tick raised the same
    ``invalid_grant`` traceback — a mailbox that was simultaneously advertised as
    working and incapable of a single API call.

    Flipping the status is what makes the failure legible everywhere at once:
    :func:`live_accounts` stops returning it, so background work skips it instead
    of retrying it to no end, and the UI can finally offer the one thing that
    fixes it — connecting the mailbox again.

    Promotes a sibling when the dead mailbox was the primary, for the same reason
    :func:`set_primary` refuses a revoked one: a user with another working
    mailbox should keep a sending identity rather than silently lose one.
    """
    if account.status == REVOKED:
        return account

    logger.warning("Gmail grant revoked for %s: %s", mask_email(account.email), reason)
    account.status = REVOKED
    # The stored access token is as dead as the refresh token behind it. Drop it
    # so a reconnect cannot half-work off a stale cached value.
    account.access_token_encrypted = None
    account.token_expiry = None

    if account.is_primary:
        account.is_primary = False
        db.flush()
        replacement = next(
            (a for a in live_accounts(account.user) if a.id != account.id), None
        )
        if replacement is not None:
            replacement.is_primary = True
        else:
            # Nothing left to promote — keep the flag where it was so the row
            # still names which mailbox to reconnect first.
            account.is_primary = True
    db.flush()
    return account


# --------------------------------------------------------------------------- #
# When this grant is going to die on its own                                   #
# --------------------------------------------------------------------------- #

#: How long Google honours a refresh token issued by a client whose consent
#: screen is still in "Testing". Google's number, not a knob: the deployment
#: says *whether* it is in testing (``settings.google_oauth_testing_mode``), and
#: this says what that costs. Publishing the consent screen is what changes it.
TESTING_GRANT_DAYS = 7

#: How close to the end a grant has to be before the product says anything.
#: Two days, so a warning raised on a Friday is still actionable on Monday — a
#: banner that appears the hour before the mailbox stops is a post-mortem with
#: better timing.
GRANT_WARNING_DAYS = 2


@dataclass(frozen=True)
class GrantExpiry:
    """When a mailbox's grant lapses of its own accord, and why.

    Only ever describes an expiry the deployment *knows about*. Google gives no
    signal that a token is short-lived — a grant that dies on Friday is
    indistinguishable, until it does, from one good for a year — so this is
    built from a stated deployment fact plus the issue date on the row, and is
    absent whenever either is missing.
    """

    expires_at: datetime
    days_left: int
    reason: str

    @property
    def expiring(self) -> bool:
        """Whether it is close enough to be worth interrupting the user."""
        return self.days_left <= GRANT_WARNING_DAYS


def grant_expiry(account: GmailAccount, *, now: datetime | None = None) -> GrantExpiry | None:
    """When *account*'s grant will lapse on its own, or ``None`` if unknown.

    ``None`` in four cases, and all four are "we cannot say" rather than "it is
    fine":

    * the deployment has not said its consent screen is unpublished, so nothing
      known expires this token on a clock;
    * the row predates :attr:`GmailAccount.granted_at` and carries no issue
      date. Guessing from ``created_at`` would report a mailbox as fresh on the
      morning it stops working, which is the failure this exists to prevent;
    * the grant is already revoked, where the reactive banner is the true and
      more useful thing to show;
    * the clock has already run out but Google has not been asked yet. Reporting
      a negative countdown would claim knowledge of a refusal that has not
      happened — a token past its seventh day is very likely dead and this
      module does not get to decide that on Google's behalf. ``days_left`` of
      zero is the last thing said, and then the next API call settles it.
    """
    if not settings.google_oauth_testing_mode:
        return None
    if account.status != CONNECTED or account.granted_at is None:
        return None

    granted = account.granted_at
    if granted.tzinfo is None:  # SQLite hands back naive datetimes.
        granted = granted.replace(tzinfo=UTC)

    expires_at = granted + timedelta(days=TESTING_GRANT_DAYS)
    remaining = expires_at - (now or datetime.now(UTC))
    if remaining.total_seconds() < 0:
        return None

    return GrantExpiry(
        expires_at=expires_at,
        # Floored, so "1 day left" means at least one more day and never
        # "sometime in the next twenty minutes".
        days_left=remaining.days,
        reason=(
            "This deployment's Google consent screen is still unpublished, so "
            "Google expires every grant seven days after it is issued. "
            "Publishing the consent screen is what stops it."
        ),
    )


def _user_for_thread(db: Session, thread: EmailThread) -> User | None:
    from app.models.application import Application

    application = db.get(Application, thread.application_id)
    if application is None:
        return None
    return db.get(User, application.user_id)


__all__ = [
    "CONNECTED",
    "GRANT_WARNING_DAYS",
    "REVOKED",
    "TESTING_GRANT_DAYS",
    "GrantExpiry",
    "account_for_address",
    "grant_expiry",
    "live_accounts",
    "mark_revoked",
    "observed_send_history",
    "resolve_for_new_outreach",
    "resolve_for_thread",
    "sent_header_variants",
    "set_primary",
]
