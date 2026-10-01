"""Bounce classification and suppression.

Before this module there was exactly one kind of bounce in the codebase, and
every one of them did the same three things: booked a hit against the mailbox's
reputation, set ``Recruiter.opted_out = True``, and cancelled the follow-ups.
That is right for "no such user" and wrong for "mailbox full" — ``opted_out`` is
the CAN-SPAM consent flag, checked before every send forever, so spending it on
a recruiter who was on holiday loses that contact permanently across every future
campaign.

So bounces are now classified:

* **HARD** (5.x.x — user unknown, domain not found) suppresses the address
  permanently and counts toward the mailbox's bounce-rate guardrail.
* **SOFT** (4.x.x — full mailbox, greylisting, throttling) cancels this
  application's follow-ups, is recorded, and does *not* touch the mailbox's
  reputation. Three of them escalate to hard.

Consent and deliverability are separate axes now, which is what makes it safe to
suppress a hard bounce forever without claiming the human asked us to stop.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_bounce import BounceKind, EmailBounce
from app.models.email_thread import EmailThread
from app.models.recruiter import DeliveryState, Recruiter

logger = logging.getLogger(__name__)

# Soft bounces to the same address before we give up on it.
SOFT_BOUNCE_LIMIT = 3

# A domain at or above this rate, with enough volume, is worth telling the user
# about. Nothing is auto-blocked on it — see the module note in
# docs/features/bounce-handling.md §5.
DOMAIN_BOUNCE_ALERT = 0.5
MIN_DOMAIN_SENDS = 3

# The enhanced status code (RFC 3463) is the reliable signal: 5.x.x permanent,
# 4.x.x transient. A bare SMTP reply code is the fallback.
#
# The lookarounds are load-bearing, not tidiness. A DSN names the remote server
# it talked to, and Postfix and Exim both write that as a bracketed IP *before*
# the code they are reporting:
#
#     <talent@acme.com>: host mx1.acme.com[5.161.20.10] said: 452 4.2.2 ...
#
# ``5.161.20`` is a prefix of that address and matched first, so a mailbox that
# was merely full read as ``5.x.x`` — permanent. The contact was suppressed
# forever and the bounce was booked against the sending mailbox's reputation,
# which is precisely the pair of costs this module exists to keep off soft
# bounces. Requiring no digit or dot on either side skips the dotted quad and
# lets the real code be found; a trailing sentence period is still fine, since
# only a *digit* after the dot disqualifies a match.
_ENHANCED_RE = re.compile(r"(?<![\d.])([45])\.\d{1,3}\.\d{1,3}(?!\.?\d)")

# Where a bare reply code is allowed to appear. RFC 5321 §4.2 defines a reply as
# a *line beginning* with the code, and a DSN either reproduces that line as it
# stands or quotes it after a lead-in naming whose answer it is. Anywhere else a
# three-digit number is just a three-digit number — and this pattern used to be
# ``\b([45])\d{2}\b``, run over the whole notice including the copy of our own
# message that a DSN returns underneath it. So an outreach email saying "grew
# throughput 500%" came back as a permanent failure, and one saying "555-0123"
# in a signature did too: ``500`` and ``555`` are the shapes of a 5xx reply.
#
# That is the most expensive way this module can be wrong. A hard verdict
# suppresses the contact across every future campaign *and* books a bounce
# against the sending mailbox, whose rate is the guardrail that pauses all
# outbound mail — the two costs the whole hard/soft split exists to keep off a
# notice that did not earn them. Every real form still matches: Postfix's
# ``said: 452 …``, Exim's ``SMTP error …: 550 …``, and a reply line quoted
# verbatim on a line of its own.
#
# The trailing guard is the other half. ``\b`` ends a match at the dash in
# ``555-0123``, so a phone number read as a reply code with a continuation
# marker; a code may not be followed by more of a number, nor run into a word.
_SMTP_LEAD_IN = (
    # ``said: 452``, ``Diagnostic-Code: smtp; 550``, ``returned '550``.
    r"(?:said|says|say|responded|response|reply|replied|returned|status"
    r"|error|reason|code|diagnostic|smtp)\b[^\S\n]*['\"]?[:;]?[^\S\n]*['\"]?"
    # Exim's ``after RCPT TO:<talent@acme.com>: 550 Unknown user`` — the code
    # follows the recipient it is about, not a word naming the server.
    r"|>[^\S\n]*:[^\S\n]*"
)
# ``code`` is the reply code alone. Reporting ``group(0)`` here would put the
# lead-in in the column too, so the tracker showed "said: 452" where a code
# belongs and two notices from different servers never compared equal.
_SMTP_RE = re.compile(
    rf"(?:^[ \t>]*|{_SMTP_LEAD_IN})(?P<code>(?P<klass>[45])\d{{2}})(?![\w]|-\d)",
    re.IGNORECASE | re.MULTILINE,
)

# Where the notice stops describing the failure and starts quoting the message
# that failed. Everything past this is our own outreach coming back, and reading
# it for a diagnosis means reading the candidate's words as the mail server's —
# which is how a resume bullet became a delivery verdict.
#
# Gmail is the case that matters, because that is what this product sends
# through and therefore what its bounces come back as: the human-readable part
# of a Google DSN carries the explanation *and* the full original beneath it, so
# the copy is not in some MIME part ``extract_plain_text`` skips — it is in the
# very text handed to this function. Postfix, Exim and Exchange delimit it too,
# with their own wording.
#
# Missing a delimiter fails safe by construction: the scan simply covers more
# text than it should, which is exactly today's behaviour. Matching one too
# early costs a diagnosis and falls through to "unreadable", which this module
# already treats as soft.
_ORIGINAL_MESSAGE_RE = re.compile(
    r"^[ \t>]*(?:"
    r"-{2,}[ \t]*(?:original|forwarded)[ \t]+message"
    r"|-{2,}[ \t]*(?:begin|start)[ \t]+(?:of[ \t]+)?(?:the[ \t]+)?(?:returned|original)"
    r"|-{2,}[ \t]*(?:below this line is|this is)[ \t]+a copy of the message"
    r"|begin forwarded message[ \t]*:"
    r"|content-type[ \t]*:[ \t]*message/rfc822"
    r"|(?:original|final|returned)[- \t]+message headers"
    r"|the (?:original|returned) message (?:is|was|follows)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_HARD_PHRASES = (
    "user unknown",
    "no such user",
    "no such recipient",
    "address not found",
    "recipient address rejected",
    "recipient not found",
    "does not exist",
    "unknown recipient",
    "invalid recipient",
    "mailbox not found",
    "domain not found",
    "unrouteable address",
    "unrouteable domain",
    "account has been disabled",
    "account is disabled",
    "no longer with the company",
    "permanent failure",
    "permanent error",
)

_SOFT_PHRASES = (
    "mailbox full",
    "mailbox is full",
    "over quota",
    "quota exceeded",
    "insufficient storage",
    "insufficient system storage",
    "try again later",
    "please retry",
    "temporarily deferred",
    "temporary failure",
    "temporarily unavailable",
    "temporarily rejected",
    "greylist",
    "greylisted",
    "throttled",
    "rate limit",
    "too many messages",
    "service unavailable",
    "connection timed out",
)

# An auto-responder is not a delivery failure. Listed explicitly so a future
# edit to the phrase lists can't quietly start suppressing everyone who went on
# holiday.
_NOT_A_BOUNCE = (
    "out of office",
    "out-of-office",
    "auto-reply",
    "automatic reply",
    "on annual leave",
    "on parental leave",
    "currently on leave",
)


@dataclass(frozen=True)
class BounceVerdict:
    """What a delivery-failure notice actually said."""

    kind: BounceKind | None  # None when this isn't a bounce at all
    code: str | None = None
    reason: str | None = None

    @property
    def is_bounce(self) -> bool:
        return self.kind is not None

    @property
    def is_hard(self) -> bool:
        return self.kind is BounceKind.HARD


def _diagnosis(haystack: str) -> str:
    """*haystack* up to the point where the notice starts quoting our own mail.

    A DSN is two documents: what the mail system decided, and a copy of the
    message it decided about. Only the first is evidence. Reading both meant
    every phrase and every number in the outreach we sent was available to the
    classifier as though a mail server had said it — see
    :data:`_ORIGINAL_MESSAGE_RE`.
    """
    marker = _ORIGINAL_MESSAGE_RE.search(haystack)
    return haystack[: marker.start()] if marker else haystack


def classify_bounce(subject: str | None, body: str | None) -> BounceVerdict:
    """Classify a delivery-status notice as hard, soft, or not a bounce.

    Read over the notice's own diagnosis only — :func:`_diagnosis` drops the
    copy of the original message a DSN returns beneath it.

    Precedence, in order:

    1. **An explicit enhanced status code beats every phrase.** A notice saying
       "mailbox full" that carries ``5.2.2`` is permanent — that server has
       decided the mailbox is over quota for good.
    2. **A bare reply code counts only where a reply code can be**, which is at
       the head of a line or quoted after a lead-in. See :data:`_SMTP_RE`.
    3. **Between phrases, soft wins ties.** The costs are asymmetric: calling a
       hard bounce soft costs two more sends and escalates at the third, while
       calling a soft bounce hard loses a real recruiter contact permanently.
    """
    haystack = _diagnosis(f"{subject or ''}\n{body or ''}".lower())
    if not haystack.strip():
        return BounceVerdict(None)

    if any(phrase in haystack for phrase in _NOT_A_BOUNCE):
        return BounceVerdict(None)

    reason = _first_meaningful_line(subject, body)

    match = _ENHANCED_RE.search(haystack)
    if match:
        kind = BounceKind.HARD if match.group(1) == "5" else BounceKind.SOFT
        return BounceVerdict(kind, code=match.group(0), reason=reason)

    match = _SMTP_RE.search(haystack)
    if match:
        kind = BounceKind.HARD if match.group("klass") == "5" else BounceKind.SOFT
        return BounceVerdict(kind, code=match.group("code"), reason=reason)

    soft = any(phrase in haystack for phrase in _SOFT_PHRASES)
    hard = any(phrase in haystack for phrase in _HARD_PHRASES)
    if soft:  # ties go soft
        return BounceVerdict(BounceKind.SOFT, reason=reason)
    if hard:
        return BounceVerdict(BounceKind.HARD, reason=reason)

    # It looked like a bounce to the caller's gate but says nothing we can read.
    # Soft is the safe reading: it costs two more sends, not a lost contact.
    return BounceVerdict(BounceKind.SOFT, reason=reason)


def _first_meaningful_line(subject: str | None, body: str | None) -> str | None:
    """A short human-readable reason for the tracker, from subject + body."""
    parts = [p.strip() for p in (subject or "", body or "") if p and p.strip()]
    if not parts:
        return None
    return " — ".join(parts)[:500]


def domain_of(address: str | None) -> str:
    if not address or "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].strip().lower()


def is_suppressed(recruiter: Recruiter | None) -> bool:
    """True when mail to this contact must not be sent again."""
    return recruiter is not None and recruiter.delivery_state == DeliveryState.HARD_BOUNCED


def record_bounce(
    db: Session,
    *,
    user_id: int,
    recruiter: Recruiter | None,
    verdict: BounceVerdict,
    address: str | None = None,
    email_id: int | None = None,
    now: datetime | None = None,
) -> EmailBounce | None:
    """Persist a bounce and move the recruiter's delivery state.

    Returns the audit row, or None when there was nothing to record. The caller
    commits, and separately decides whether to book this against the *mailbox's*
    reputation — only hard bounces should be (see ``inbox_tasks``).
    """
    if not verdict.is_bounce:
        return None

    now = now or datetime.now(UTC)
    resolved = (address or (recruiter.email if recruiter else "") or "").strip().lower()
    if not resolved:
        return None

    row = EmailBounce(
        user_id=user_id,
        recruiter_id=recruiter.id if recruiter is not None else None,
        email_id=email_id,
        address=resolved,
        domain=domain_of(resolved),
        kind=verdict.kind,
        code=verdict.code,
        reason=verdict.reason,
        occurred_at=now,
    )
    db.add(row)

    if recruiter is not None:
        recruiter.last_bounce_at = now
        recruiter.last_bounce_reason = (verdict.reason or "")[:500] or None
        if verdict.is_hard:
            recruiter.delivery_state = DeliveryState.HARD_BOUNCED
        else:
            recruiter.soft_bounce_count = (recruiter.soft_bounce_count or 0) + 1
            if recruiter.soft_bounce_count >= SOFT_BOUNCE_LIMIT:
                # Repeatedly undeliverable is indistinguishable from
                # permanently undeliverable, at some point.
                recruiter.delivery_state = DeliveryState.HARD_BOUNCED
                recruiter.last_bounce_reason = (
                    f"{SOFT_BOUNCE_LIMIT} soft bounces — treating as undeliverable"
                )
            elif recruiter.delivery_state == DeliveryState.OK:
                recruiter.delivery_state = DeliveryState.SOFT_BOUNCED

    return row


def clear_soft_bounces(recruiter: Recruiter | None) -> None:
    """A reply proves the address works. Forget the transient failures.

    A hard-bounced contact is never revived here: if mail is genuinely arriving
    again, that is a deliberate decision for the user to make, not a side effect
    of one message getting through.
    """
    if recruiter is None or recruiter.delivery_state == DeliveryState.HARD_BOUNCED:
        return
    recruiter.soft_bounce_count = 0
    recruiter.delivery_state = DeliveryState.OK


@dataclass
class DomainBounceRate:
    domain: str
    sent: int
    bounced: int
    hard: int
    soft: int

    @property
    def rate(self) -> float:
        return round(self.bounced / self.sent, 3) if self.sent else 0.0

    @property
    def alerting(self) -> bool:
        return self.sent >= MIN_DOMAIN_SENDS and self.rate >= DOMAIN_BOUNCE_ALERT


def domain_bounce_rates(
    db: Session,
    user_id: int,
    *,
    min_sent: int = MIN_DOMAIN_SENDS,
    limit: int = 20,
) -> list[DomainBounceRate]:
    """Bounce rate per recipient domain for one user, worst first.

    Domains under *min_sent* are dropped: one bounce out of one send is a 100%
    rate and means nothing — the same ``MIN_SAMPLE`` reasoning the analytics
    module applies to response rates.
    """
    from app.models.application import Application

    sent_rows = db.execute(
        select(Recruiter.email, func.count(Email.id))
        .join(Application, Application.recruiter_id == Recruiter.id)
        .join(EmailThread, EmailThread.application_id == Application.id)
        .join(Email, Email.thread_id == EmailThread.id)
        .where(
            Application.user_id == user_id,
            Email.direction == EmailDirection.SENT,
            Email.status == EmailStatus.SENT,
        )
        .group_by(Recruiter.email)
    ).all()

    sent_by_domain: dict[str, int] = {}
    for address, count in sent_rows:
        domain = domain_of(address)
        if domain:
            sent_by_domain[domain] = sent_by_domain.get(domain, 0) + (count or 0)

    bounce_rows = db.execute(
        select(EmailBounce.domain, EmailBounce.kind, func.count(EmailBounce.id))
        .where(EmailBounce.user_id == user_id)
        .group_by(EmailBounce.domain, EmailBounce.kind)
    ).all()

    hard_by_domain: dict[str, int] = {}
    soft_by_domain: dict[str, int] = {}
    for domain, kind, count in bounce_rows:
        target = hard_by_domain if kind == BounceKind.HARD else soft_by_domain
        target[domain] = target.get(domain, 0) + (count or 0)

    rows = [
        DomainBounceRate(
            domain=domain,
            sent=sent,
            bounced=hard_by_domain.get(domain, 0) + soft_by_domain.get(domain, 0),
            hard=hard_by_domain.get(domain, 0),
            soft=soft_by_domain.get(domain, 0),
        )
        for domain, sent in sent_by_domain.items()
        if sent >= min_sent
    ]
    rows.sort(key=lambda r: (r.rate, r.sent), reverse=True)
    return rows[:limit]


__all__ = [
    "DOMAIN_BOUNCE_ALERT",
    "MIN_DOMAIN_SENDS",
    "SOFT_BOUNCE_LIMIT",
    "BounceVerdict",
    "DomainBounceRate",
    "classify_bounce",
    "clear_soft_bounces",
    "domain_bounce_rates",
    "domain_of",
    "is_suppressed",
    "record_bounce",
]
