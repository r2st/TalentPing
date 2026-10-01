"""Finding mail worth looking at, in a mailbox full of mail that isn't.

This is the half of inbound detection that runs before any model does. It reads
the whole mailbox — which nothing else in the product does — and its entire job
is to hand the expensive stages as few messages as possible, without ever
dropping one that mattered.

Six filters, cheapest first:

1. **Already seen.** ``(user_id, gmail_message_id)`` is unique, so a message we
   have a row for is skipped before it is even fetched. This is what makes the
   scanner safe to re-run, which in turn is what makes it safe to trigger from a
   button, a beat tick and a push notification all at once.
2. **Ours.** A message on a Gmail thread that has an :class:`EmailThread` row is
   a conversation *we* started, and :func:`app.tasks.inbox_tasks.poll_thread`
   owns it. Handling it here as well would classify our own outreach as inbound
   recruiter mail and, worse, reply to it. This filter is the boundary between
   the two ingestion paths.
3. **From us.** Mail from any of the user's own addresses is not inbound.
4. **Bounces.** A delivery-failure notice is reputation data, not an
   opportunity: it is reported in :attr:`ScanResult.hard_bounces` for the caller
   to book against the mailbox, and never classified. It still gets a row of its
   own, because a bounce that is booked but not remembered is booked again by
   every later scan — see :func:`scan`.
5. **Automatic responders.** An out-of-office notice is a mail server talking.
   Answering one is a message sent to a robot that answers it again, so it is
   skipped on the strength of its *headers* — RFC 3834's ``Auto-Submitted`` and
   the pre-standard ones every installed Exchange still sends — which is the
   half of the signal that cannot be a person's prose. The subject half is read
   later, by ``recruiter_classifier._prefilter``, because a stored row keeps a
   subject and does not keep headers.
6. **Opt-outs.** Someone asking us to stop is not offering a job. Asked *after*
   the responder check on purpose: this one has a side effect — it retires the
   address across every future campaign — and a machine's vacation notice is
   not a person withdrawing their consent.

What survives is capped and **the cap is reported**, never silent — the same
contract ``POST /inbox/sync`` honours with ``threads_skipped``. A UI that says
"18 checked, 40 left for the next scan" is honest; one that says "checked" is
not.

The same rule governs the rest of :class:`ScanResult`: every message that comes
back from Gmail leaves through exactly one counter, the search that produced them
is recorded alongside, and the whole dict is logged once per scan. The recurring
question about this feature is never "what did it find?" but "why did it not find
*that* one?", and that is only answerable if nothing leaves silently.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.pii import mask_email
from app.models.application import Application
from app.models.email_thread import EmailThread
from app.models.gmail_account import GmailAccount
from app.models.recruiter_email import RecruiterEmail
from app.models.recruiter_scan_skip import (
    REASON_BLOCKED_SENDER,
    REASON_FROM_SELF,
    RecruiterScanSkip,
)
from app.models.user import User
from app.services import bounce_service, gmail_service
from app.services.recruiter_classifier import looks_like_opt_out
from app.services.reply_classifier import looks_like_auto_reply, looks_like_bounce
from app.services.sender_blocklist import is_blocked as is_blocked_sender

logger = logging.getLogger(__name__)

# How many ids to ask Gmail for, independent of how many we will fetch bodies
# for. Listing is cheap and returns ids only, and asking for barely more than the
# cap makes ``deferred`` a lie: we would report "3 left" purely because we never
# asked about the fourth. Fetching is what the cap governs.
#
# Sized well above a busy week's mail. :func:`gmail_service.list_messages` pages
# to reach it — Gmail returns newest-first and caps a single page at 500, so an
# unpaginated read of a large window silently loses its oldest end.
_LIST_CEILING = 2000

# Everything the scanner must never read, whatever else the query says. Chats
# aren't mail, trash was thrown away, and spam is a decision the user's own
# provider already made — answering out of the spam folder is how a candidate's
# domain reputation gets ruined.
_EXCLUSIONS = "-in:chats -in:trash -in:spam"

# The lookback term inside a hand-written ``RECRUITER_SCAN_QUERY``. Only ever
# used to widen it — see :func:`build_query`.
_NEWER_THAN_RE = re.compile(r"newer_than:\d+d")


def build_query(window_days: int | None = None) -> str:
    """The Gmail search one scan runs, from settings.

    *window_days* widens the lookback for one scan without touching the setting.
    It is what the backlog catch-up passes: the ordinary window is deliberately
    short (a week), and mail that arrived while detection was down — a dead
    OAuth grant, a stopped worker — falls out of it and becomes invisible
    forever, because nothing ever looks further back than the window. See
    :mod:`app.tasks.backlog_tasks`.

    Four filters that used to be here are deliberately gone, each because it was
    losing real opportunities:

    * ``is:unread`` — a candidate who opens a recruiter's email on their phone
      before the beat tick made that email permanently invisible to this scanner.
      Read mail is exactly the mail most likely to need answering. Re-reading it
      costs nothing, because ``known_ids`` is what makes a rescan a no-op.
    * ``-category:promotions`` — agency recruiters mail from platforms Gmail
      files under Promotions, so this excluded a whole class of real
      opportunity.
    * ``-category:social`` — and so does this. A recruiter reaching out through
      LinkedIn, or from any address Gmail has decided is "social", was dropped
      before anything read it. The deterministic pre-filter in
      :mod:`recruiter_classifier` is a far better judge of a digest than the tab
      Gmail guessed, and it costs no model call to run.
    * ``in:inbox`` — now optional and off by default. A Gmail filter that files
      recruiter mail under a label and skips the inbox is a *common* setup for
      exactly the kind of candidate this product is for, and every message it
      touched was invisible. All Mail inside the same window is the same dedup
      and the same cost.

    ``-from:me`` stays, because the alternative is fetching the body of every
    message the user ever sent purely to discard it.
    """
    days = settings.recruiter_scan_window_days if window_days is None else window_days
    override = (settings.recruiter_scan_query or "").strip()
    if override:
        # A deployment that writes its own query owns it, with one exception: a
        # catch-up asking for a wider window must actually get one. Returning
        # the override untouched would make the whole feature a silent no-op on
        # exactly the deployments that customised it, and "silent" is the
        # failure this pipeline keeps re-learning. So the term that bounds the
        # lookback is rewritten, and nothing else is.
        if window_days is None:
            return override
        widened, replaced = _NEWER_THAN_RE.subn(f"newer_than:{days}d", override)
        return widened if replaced else override
    scope = "" if settings.recruiter_scan_include_archived else "in:inbox "
    return f"{scope}{_EXCLUSIONS} -from:me newer_than:{days}d"

# "Alex Recruiter <alex@acme.com>" -> ("Alex Recruiter", "alex@acme.com")
_FROM_RE = re.compile(r"^\s*(?:\"?(?P<name>[^\"<]*?)\"?\s*)?<?(?P<email>[^<>\s]+@[^<>\s]+)>?\s*$")

#: One bare mailbox, with nothing around it. Deliberately not an RFC 5322
#: validator — the question is only "is this a single address, or is it a
#: header we failed to take one out of", and the characters that answer it are
#: the ones that separate addresses from each other and from their display
#: names.
_ONE_ADDRESS_RE = re.compile(r'[^<>\s,;"]+@[^<>\s,;"]+')

#: The same shape found *inside* a header rather than being the whole of it,
#: preferring an angle-bracketed mailbox to a bare one. The preference is the
#: point: this only ever runs on a header the strict parser refused, and the
#: commonest way to get there is a display name that is itself address-shaped
#: ("Recruiting; talent@acme.com <noreply@ats.example>"). Brackets are how the
#: sender said which of the two is the mailbox, and every mail client reads
#: them that way.
_ADDRESS_IN_TEXT_RE = re.compile(
    r'<([^<>\s,;"()]+@[^<>\s,;"()]+)>|([^<>\s,;"()]+@[^<>\s,;"()]+)'
)

SNIPPET_CHARS = 240


@dataclass
class CandidateMessage:
    """One inbound message that survived filtering, flattened for the pipeline."""

    gmail_message_id: str
    gmail_thread_id: str | None
    from_address: str
    from_name: str | None
    subject: str | None
    body_text: str
    received_at: datetime
    # ``Reply-To``, when the sender set one that isn't just the From again. See
    # :attr:`RecruiterEmail.reply_address` for why this is load-bearing.
    reply_to_address: str | None = None
    # The RFC 5322 ``Message-ID`` and ``References`` headers. Read here because
    # this is the only place the raw message passes through, and kept because a
    # reply that carries only Gmail's ``threadId`` threads in Gmail and in no
    # other client. See :func:`reply_headers_for`.
    rfc_message_id: str | None = None
    rfc_references: str | None = None
    # What the message arrived carrying, described but not downloaded — see
    # :class:`app.services.gmail_service.InboundAttachment`. Read here for the
    # same reason the RFC headers are: this is the only place the raw Gmail
    # message resource passes through, and the part list is on it.
    attachments: list[dict] = field(default_factory=list)

    @property
    def snippet(self) -> str:
        return (self.body_text or "")[:SNIPPET_CHARS]


@dataclass
class DismissedMessage:
    """One message dismissed on its sender alone, for the caller to remember.

    Deliberately not a :class:`CandidateMessage`: nothing downstream will ever
    classify or reply to this, so the body is not kept. What is kept is the id
    — which is what filter 1 needs to skip it before the next fetch — and the
    address, so a domain blocked in error can be found again.
    """

    gmail_message_id: str
    reason: str
    from_address: str


@dataclass
class ScanResult:
    """What one pass over a mailbox found, and what it deliberately didn't."""

    messages: list[CandidateMessage] = field(default_factory=list)
    # Hard-bounce notices seen this pass. Separate from ``messages`` because
    # nothing may classify or reply to them, and reported rather than acted on
    # here because booking one is a write and :func:`scan` does not write.
    hard_bounces: list[CandidateMessage] = field(default_factory=list)
    # Opt-out requests seen this pass. Reported rather than acted on for the same
    # reason the bounces are — :func:`scan` does not write — and reported *at
    # all* because they used to be counted and thrown away. The ``mailto:`` half
    # of our own ``List-Unsubscribe`` header asks the recipient's client to send
    # exactly this message, so the door the product advertised was the one it
    # did not answer. See :mod:`app.services.opt_out`.
    opt_outs: list[CandidateMessage] = field(default_factory=list)
    # Messages dismissed on the ``From:`` header alone. Reported for the same
    # reason the bounces are — this function does not write — and remembered by
    # the caller so the next pass skips them before paying for the fetch. See
    # :mod:`app.models.recruiter_scan_skip` for why only these two filters
    # qualify.
    dismissed: list[DismissedMessage] = field(default_factory=list)
    listed: int = 0
    examined: int = 0
    skipped_known: int = 0
    skipped_own_thread: int = 0
    skipped_from_self: int = 0
    skipped_bounce: int = 0
    skipped_auto_reply: int = 0
    skipped_opt_out: int = 0
    skipped_blocked_sender: int = 0
    skipped_unfetchable: int = 0
    # Left for the next run because the per-run cap was hit. Reported, never
    # silently dropped.
    deferred: int = 0
    # The search that produced all of the above. Recorded because "why is that
    # email missing?" is answered by the query far more often than by the
    # filters, and a query nobody can see is a query nobody can question.
    query: str = ""
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "detected": len(self.messages),
            "listed": self.listed,
            "examined": self.examined,
            "skipped_known": self.skipped_known,
            "skipped_own_thread": self.skipped_own_thread,
            "skipped_from_self": self.skipped_from_self,
            "skipped_bounce": self.skipped_bounce,
            "skipped_auto_reply": self.skipped_auto_reply,
            "skipped_opt_out": self.skipped_opt_out,
            "skipped_blocked_sender": self.skipped_blocked_sender,
            "skipped_unfetchable": self.skipped_unfetchable,
            "deferred": self.deferred,
            "query": self.query,
            "error": self.error,
        }


# Control characters have no business in a header value we are about to store
# and later show. ``gmail_service._header_safe`` already stops one reaching an
# *outgoing* header; this stops it reaching the database.
_HEADER_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def decode_header_value(raw: str | None) -> str:
    """An RFC 2047 header value as the words it stands for.

    Gmail's API hands back ``payload.headers`` exactly as the message carried
    them, encoded words and all — it decodes nothing. So a recruiter called
    Jörg Müller arrived, and was stored, as
    ``=?UTF-8?B?SsO2cmcgTcO8bGxlcg==?=``.

    That is not only ugly in the inbox. The display name becomes
    :attr:`Recruiter.name` for the contact created from the message, and
    ``ai_composer._greeting`` opens every later outreach and follow-up with
    ``Hi {name},`` — so the blob went back out, to that recruiter, under the
    candidate's name. The same header is what the inbox's name search matches
    against, and no one searches for their base64. Subjects arrive the same way,
    and a subject is read by the classifier, by the bounce and opt-out
    detectors, and then reused verbatim as the ``Re:`` line of the reply.

    Never raises. A charset Python does not know, or bytes that are not what the
    encoding claims, gives back the raw header — an odd string is a much smaller
    loss than a message, which is the same trade :func:`parse_from` makes below.
    """
    if not raw or "=?" not in raw:
        return raw or ""
    try:
        decoded = str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 - any malformed header falls back to itself
        return raw
    # A decoded word can carry anything the encoding could hold, newlines
    # included; a fold in the original means one space, not two lines.
    decoded = " ".join(_HEADER_CONTROL_RE.sub(" ", decoded).split())
    return decoded or raw


def parse_from(raw: str) -> tuple[str | None, str]:
    """Split a ``From:`` header into (display name, address).

    Falls back to the raw header as the address: a malformed sender is still a
    sender, and losing the message would be worse than storing an odd string.

    The address is read from the header as it arrived and only the *name* is
    decoded, which is deliberate and not merely cheaper. An encoded word cannot
    contain a space, a ``<`` or a ``>`` — but what it decodes to can, and a
    display name that decodes to ``<admin>`` would leave the pattern below with
    two bracketed tokens and no match at all. Splitting first means a hostile
    display name can cost its own legibility and nothing else.
    """
    match = _FROM_RE.match(raw or "")
    if not match:
        return None, (raw or "").strip().lower()
    name = decode_header_value(match.group("name") or "").strip() or None
    return name, match.group("email").strip().lower()


#: Headers whose value is prose, and so may arrive RFC 2047 encoded. Only these
#: are decoded: ``Message-ID`` and ``References`` are structured, never carry an
#: encoded word, and are matched character for character when a reply is
#: threaded — running them through a decoder could only ever damage them. ``From``
#: and ``Reply-To`` are prose *and* structure at once and are handled by
#: :func:`parse_from`, which splits them before decoding anything.
_DECODED_HEADERS = frozenset({"subject"})


def headers_of(message: dict) -> dict[str, str]:
    """Every header on a fetched message, lowercased, prose decoded.

    Shared with :mod:`app.tasks.inbox_tasks`, which built the same dict inline
    and so read the same encoded subjects. Both pipelines see every inbound
    message and both have had to learn the same fact about mail separately more
    than once; this one is learned in a single place.
    """
    return {
        h["name"].lower(): (
            decode_header_value(h.get("value"))
            if h["name"].lower() in _DECODED_HEADERS
            else (h.get("value") or "")
        )
        for h in (message.get("payload", {}).get("headers", []) or [])
        if h.get("name")
    }


def message_time(message: dict) -> datetime:
    """Gmail's own timestamp for a message, falling back to now.

    ``internalDate`` is epoch milliseconds. Using it rather than poll time is
    what lets a week-old message that we only just noticed sort as a week old —
    the same rule :func:`app.tasks.inbox_tasks._message_time` follows.
    """
    raw = message.get("internalDate")
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def own_addresses(user: User) -> set[str]:
    """Every address that counts as "the user themselves"."""
    addresses = {a.email.lower() for a in user.gmail_accounts if a.email}
    addresses.add((user.email or "").lower())
    return {a for a in addresses if a}


def is_from_self(from_address: str, mine: set[str]) -> bool:
    """Whether *from_address* is one of the user's own addresses.

    Exact, not substring. ``"bob@acme.com" in "bob@acme.com.mx"`` is true, and a
    recruiter at a domain that merely *starts with* the user's was being dropped
    as the user's own mail — a silent, unexplainable loss.
    """
    return (from_address or "").strip().lower() in mine


def _header_value(
    headers: dict[str, str], name: str, limit: int | None = None
) -> str | None:
    """One header, whitespace-collapsed and bounded, or ``None`` if absent.

    ``Message-ID`` and ``References`` are folded across lines by most mail
    transports, so the raw value arrives with embedded newlines and runs of
    spaces. They have to be collapsed before the value goes back out as a header
    of ours, or the reply carries a malformed one.

    *limit* of ``None`` means "collapse but do not cut", which is what a value
    made of several ids needs: a character slice is the wrong tool there, and
    :func:`trim_references` is the right one.
    """
    raw = headers.get(name)
    if not raw:
        return None
    collapsed = " ".join(str(raw).split())
    return (collapsed if limit is None else collapsed[:limit]) or None


def message_id_for(headers: dict[str, str]) -> str | None:
    """The sender's ``Message-ID``, at the RFC's own 998-character ceiling."""
    return _header_value(headers, "message-id", 998)


#: How long a ``References`` chain is allowed to get before it is trimmed. Not
#: an RFC number — the RFC gives none — but a header has to stop somewhere, and
#: this is roughly sixty message ids, which is a longer conversation than any
#: recruiting thread has ever been.
MAX_REFERENCES_CHARS = 4000

#: The separators a client may write between message ids. Whitespace is the
#: RFC's answer; commas turn up from clients that borrowed the address-list
#: syntax, and splitting on them keeps two ids from being read as one.
_REFERENCE_SEPARATORS = re.compile(r"[\s,]+")


def _reference_ids(chain: str | None) -> list[str]:
    """The individual message ids in a ``References`` value, in order."""
    return [part for part in _REFERENCE_SEPARATORS.split(chain or "") if part]


def trim_references(ids: Sequence[str], limit: int = MAX_REFERENCES_CHARS) -> str:
    """Join *ids* into a ``References`` value no longer than *limit*.

    Dropping ids **from the middle**, which is what RFC 5322 §3.6.4 asks for and
    the opposite of what a slice does. The chain is oldest-first with the
    message being answered last, so ``chain[:limit]`` — the previous
    implementation — threw away the newest ids first and cut the survivor in
    half on its way out. The header that reached the recruiter therefore ended
    in something like ``<msg78.longer.identifier.padding`` (no closing bracket,
    no domain) and did not contain the id that ``In-Reply-To`` named at all: a
    malformed value, and one that no client walking the chain could match to the
    message it answers.

    Nobody hits this on a short thread, which is why it survived — it needs
    around sixty messages, and the only threads that long are the ones that went
    well.

    The two ids that carry the threading are kept in preference to everything
    between them: the first, which is what most clients group a conversation by,
    and the last, which is the message being replied to. An id is never split;
    if even those two cannot fit, the newest alone comes back, whole.
    """
    kept = [str(value) for value in ids if value]
    if not kept:
        return ""
    if len(" ".join(kept)) <= limit:
        return " ".join(kept)

    root, newest, middle = kept[0], kept[-1], kept[1:-1]
    if len(f"{root} {newest}") > limit:
        # Pathological: two ids alone are over budget. Truncating either would
        # produce the malformed value this function exists to prevent, so the
        # one that matters most goes out whole and on its own.
        return newest
    while middle and len(" ".join([root, *middle, newest])) > limit:
        # Oldest first — the far end of a long thread is the part a client is
        # least likely to still be holding.
        middle.pop(0)
    return " ".join([root, *middle, newest])


def references_for(headers: dict[str, str]) -> str | None:
    """The sender's ``References`` chain, if their client sent one.

    Absent on a first message, which is the common case here — a recruiter
    writing out of the blue starts the conversation, so there is nothing to
    reference yet.

    Trimmed by whole ids rather than by characters, for the reason
    :func:`trim_references` gives — this value is stored and later replayed into
    a header we send, so a chain sliced mid-id here is a malformed header later.
    """
    raw = _header_value(headers, "references", None)
    if not raw:
        return None
    return trim_references(_reference_ids(raw)) or None


def reply_headers_for(
    message_id: str | None, references: str | None
) -> tuple[str | None, str | None]:
    """``(In-Reply-To, References)`` for a reply to a message with these headers.

    RFC 5322 §3.6.4: ``In-Reply-To`` is the single id being answered, and
    ``References`` is the whole chain with that id appended — so a client walking
    the chain sees the conversation in order rather than as two fragments.

    Returns ``(None, None)`` when the original carried no ``Message-ID``. A
    fabricated id would be worse than none: it threads the reply to a message
    that does not exist, in whichever client trusts it.

    A chain past :data:`MAX_REFERENCES_CHARS` is trimmed by whole ids rather
    than sliced — see :func:`trim_references`, which is where the reason lives.
    The id being answered survives that trim by construction, so ``In-Reply-To``
    and the end of ``References`` always agree.
    """
    if not message_id:
        return None, None
    ids = [*_reference_ids(references), message_id]
    return message_id, trim_references(ids)


def first_address(raw: str | None) -> str | None:
    """The first mailbox in an address header, or ``None`` if there isn't one.

    ``Reply-To`` is a *mailbox-list* (RFC 5322 §3.6.2), and :func:`parse_from`
    is not a list parser — it is anchored at both ends and reads everything
    before the last ``@`` token as a display name. Handed two addresses it
    therefore did not fail, which would have been survivable; it answered, and
    answered wrongly, in two different ways:

    * ``"talent@acme.com, careers@acme.com"`` came back as **careers@acme.com**.
      The first address was eaten as a display name and the reply went to the
      second one — the fallback mailbox, not the human the sender listed first.
    * ``"Talent Team <talent@acme.com>, Acme Careers <careers@acme.com>"``
      matched nothing at all, and ``parse_from``'s deliberate fallback — the raw
      header, lower-cased, because a malformed *From* is still a sender — handed
      back the whole forty-character string. It contains an ``@``, which is all
      :func:`reply_to_for` checked, so it was stored as the address to answer.

    A legal RFC comment does the same: ``"<talent@acme.com> (Acme Talent)"``
    became ``"<talent@acme.com> (acme talent)"``.

    That value is not just displayed. It is ``Email.to_address`` for the
    auto-reply, and ``Recruiter.email`` for the contact the message creates —
    so the answer goes to the wrong mailbox, or to a string Gmail rejects, and
    the contact row that outreach later runs off carries the same thing.

    ``email.utils.getaddresses`` is the list parser the header actually needs.
    It is strict by default on this Python, refusing a header it cannot parse
    outright rather than guessing — so a display name holding a bare ``;``
    ("Recruiting; Talent Acquisition <x@y.com>", which is illegal and not rare)
    yields nothing, and for *this* header nothing means falling back to a
    ``noreply@`` From, which is the failure the Reply-To lookup exists to
    prevent. Hence the second pass: when the strict parser declines, the first
    address-shaped token in the header is taken. It runs only on headers that
    have already been refused, so it cannot loosen any of the cases above.
    """
    for _name, address in getaddresses([raw or ""]):
        found = _plain_address(address)
        if found:
            return found
    bracketed = None
    bare = None
    for match in _ADDRESS_IN_TEXT_RE.finditer(raw or ""):
        if bracketed is None and match.group(1):
            bracketed = match.group(1)
        if bare is None and match.group(2):
            bare = match.group(2)
    return _plain_address(bracketed or bare)


def _plain_address(value: str | None) -> str | None:
    """*value* as a lower-cased bare address, or ``None`` if it isn't one."""
    candidate = (value or "").strip().strip(".").lower()
    return candidate if _ONE_ADDRESS_RE.fullmatch(candidate) else None


def reply_to_for(headers: dict[str, str], from_address: str) -> str | None:
    """The ``Reply-To`` address, when the sender set a different one.

    Recruiting platforms send from an unmonitored address and put the human in
    ``Reply-To``. Returns ``None`` when the header is absent, unparseable, or
    just the From again, so the common case stores nothing.

    "Unparseable" is :func:`first_address`'s answer rather than
    :func:`parse_from`'s — see there for the two shapes that used to come back
    as an address and were not one.
    """
    raw = headers.get("reply-to")
    if not raw:
        return None
    address = first_address(raw)
    if not address or address == (from_address or "").strip().lower():
        return None
    return address


def _candidate(
    message_id: str,
    stub: dict,
    message: dict,
    headers: dict,
    from_address: str,
    from_name: str | None,
    subject: str,
    body: str,
) -> CandidateMessage:
    """Flatten one fetched Gmail message into the pipeline's shape."""
    return CandidateMessage(
        gmail_message_id=message_id,
        gmail_thread_id=message.get("threadId") or stub.get("threadId"),
        from_address=from_address,
        from_name=from_name,
        subject=subject or None,
        body_text=body,
        received_at=message_time(message),
        reply_to_address=reply_to_for(headers, from_address),
        rfc_message_id=message_id_for(headers),
        rfc_references=references_for(headers),
        attachments=[
            item.as_dict() for item in gmail_service.list_attachments(message)
        ],
    )


def scan(
    db: Session,
    user: User,
    account: GmailAccount,
    *,
    limit: int | None = None,
    window_days: int | None = None,
) -> ScanResult:
    """Read *account*'s inbox and return the messages worth classifying.

    *window_days* overrides how far back the Gmail query looks, for this call
    only. It widens the dedup set by exactly as much — see below — so a catch-up
    over a month cannot re-detect mail that was answered five weeks ago.

    Persists nothing: the caller owns the transaction, so a scan that finds
    twenty messages and fails on the nineteenth doesn't leave eighteen
    half-processed rows behind.

    That contract is why hard bounces are *reported* in
    :attr:`ScanResult.hard_bounces` rather than booked here. Booking them inline
    was a write in a function that promises not to write, and — because this
    branch returns before the message is stored — the notice never entered
    ``known_ids``, so filter 1 could not suppress it. Every scan over the same
    seven-day window re-read the same few DSNs and booked them again: in
    production one mailbox reached a 20370% bounce rate on 20 sends and sat
    permanently paused, which stopped all outbound mail. The caller now writes a
    row per bounce, so each one is counted exactly once.

    :attr:`ScanResult.dismissed` is the *rest* of that lesson, and it is about
    cost rather than correctness. Every filter below filter 1 runs on a message
    that has already been fetched, because a Gmail list returns nothing but ids;
    a message dismissed by one of them left no trace, so the next pass fetched
    it again. On a production mailbox that was 145 blocked senders re-fetched
    every five minutes — 152 fetches to detect nothing, a 25-second scan, and
    roughly 43,000 wasted ``messages.get`` calls a day against a quota the whole
    deployment shares. Nor does the per-run cap bound it: the cap counts
    messages that *survive* (see ``deferred`` below) and none of these do.

    Only the two filters that decide on the ``From:`` header are reported that
    way. The opt-out and bounce sniffers read the body, and a body-reading
    verdict here has been wrong before, so those stay re-evaluated every pass —
    :mod:`app.models.recruiter_scan_skip` has the argument in full.
    """
    cap = limit if limit is not None else settings.recruiter_scan_max_per_run
    query = build_query(window_days)
    result = ScanResult(query=query)

    try:
        listed = gmail_service.list_messages(
            account, query, max_results=_LIST_CEILING
        )
    except gmail_service.GmailNotConfigured as exc:
        result.error = str(exc)
        return result

    result.listed = len(listed)
    if not listed:
        logger.info(
            "recruiter scan for %s matched nothing: query=%r", account.email, query
        )
        return result

    # Bounded, so an active user's whole history isn't loaded to answer "have we
    # seen this?" — but never narrower than the window the Gmail query itself
    # searches, because an id the query can still return and this set has
    # forgotten is a message re-fetched on every pass forever.
    #
    # That used to be a comment asserting the two could not cross ("messages
    # older than this are never returned by list_messages anyway"), which held
    # only while ``recruiter_scan_window_days`` stayed under thirty. One
    # environment variable took the guarantee away silently, and silently is the
    # problem: the re-fetched message would also fail to re-insert against the
    # unique constraints, so the symptom was cost and not an error. Derived from
    # the setting now, with the same month of slack on top — a mailbox connected
    # long after the mail arrived is recorded today and dated then.
    from datetime import timedelta

    # Derived from the window this call actually searched, not from the
    # setting: a backlog catch-up looks back a month or more, and a dedup set
    # narrower than the search is a message the query still returns and this set
    # has forgotten. The unique constraint would refuse the second row, so the
    # cost is a re-fetch and an IntegrityError per message rather than a second
    # reply — but it is a cost paid on every pass, forever.
    dedup_days = 30 + max(
        0,
        settings.recruiter_scan_window_days if window_days is None else window_days,
    )
    dedup_cutoff = datetime.now(UTC) - timedelta(days=dedup_days)
    known_ids = set(
        db.scalars(
            select(RecruiterEmail.gmail_message_id).where(
                RecruiterEmail.user_id == user.id,
                RecruiterEmail.created_at >= dedup_cutoff,
            )
        )
    )
    # Everything a previous pass fetched and threw away on the sender's address.
    # Folded into the same set as the stored messages because filter 1 asks one
    # question — "have we already dealt with this id?" — and a dismissal is an
    # answer to it, on the same window and for the same reason.
    known_ids.update(
        db.scalars(
            select(RecruiterScanSkip.gmail_message_id).where(
                RecruiterScanSkip.user_id == user.id,
                RecruiterScanSkip.created_at >= dedup_cutoff,
            )
        )
    )
    # Scoped to this user: a Gmail thread id is only meaningful inside the
    # mailbox it came from, so another user's threads must never suppress this
    # user's mail.
    our_threads = set(
        db.scalars(
            select(EmailThread.gmail_thread_id)
            .join(Application, Application.id == EmailThread.application_id)
            .where(
                EmailThread.gmail_thread_id.is_not(None),
                Application.user_id == user.id,
            )
        )
    )
    mine = own_addresses(user)

    for stub in listed:
        message_id = stub.get("id")
        if not message_id:
            continue

        if message_id in known_ids:
            result.skipped_known += 1
            continue
        # A thread we started belongs to poll_thread. Checked on the stub, before
        # the message is fetched, because it is the commonest reason to skip for
        # an active user.
        if stub.get("threadId") and stub["threadId"] in our_threads:
            result.skipped_own_thread += 1
            continue

        if len(result.messages) >= cap:
            result.deferred += 1
            continue

        try:
            message = gmail_service.get_message(account, message_id)
        except gmail_service.GmailNotConfigured as exc:
            result.error = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 - one bad message must not end the scan
            result.skipped_unfetchable += 1
            logger.warning(
                    "recruiter scan could not fetch %s: %s",
                    message_id,
                    exc,
                    exc_info=True,
                )
            continue

        result.examined += 1
        headers = headers_of(message)
        from_name, from_address = parse_from(headers.get("from", ""))

        # The two verdicts that rest on the sender's address alone. Both are
        # reported so the caller can remember them: re-reading this message
        # cannot change either answer, so paying for the fetch again buys
        # nothing. See :class:`DismissedMessage`.
        if is_from_self(from_address, mine):
            result.skipped_from_self += 1
            result.dismissed.append(
                DismissedMessage(message_id, REASON_FROM_SELF, from_address)
            )
            continue

        if is_blocked_sender(from_address):
            result.skipped_blocked_sender += 1
            result.dismissed.append(
                DismissedMessage(message_id, REASON_BLOCKED_SENDER, from_address)
            )
            continue

        subject = headers.get("subject", "")
        body = gmail_service.extract_plain_text(message)

        # A bounce found here is the same fact as a bounce found on one of our
        # threads, and it belongs in the same ledger — but only a *hard* one.
        # A full mailbox is not a deliverability problem for this sender and
        # must not push them toward the bounce-rate pause. This path can't
        # attribute the bounce to a recruiter row (the DSN is loose mail, not a
        # thread we own), so it books reputation only; the audit row is written
        # by the thread poller, which knows who was being written to.
        if looks_like_bounce(from_address, subject, body):
            if bounce_service.classify_bounce(subject, body).is_hard:
                result.hard_bounces.append(
                    _candidate(message_id, stub, message, headers, from_address,
                               from_name, subject, body)
                )
            result.skipped_bounce += 1
            continue

        # The message says a machine generated it. Headers only: they are the
        # sender's own declaration (RFC 3834 §5 exists for exactly this
        # question) and cannot be confused with a recruiter's prose, whereas
        # the subject markers this function also knows are a heuristic and are
        # applied one stage later, where a mistake costs a mislabelled row
        # rather than a message nobody ever sees.
        #
        # Skipped outright rather than stored: an out-of-office holds no
        # opportunity, and a row for one is a row the classifier can read as a
        # recruiter writing about a role — which is how a reply gets sent to an
        # autoresponder that replies to it.
        if looks_like_auto_reply(headers):
            result.skipped_auto_reply += 1
            logger.info(
                "recruiter scan skipped %s from %s: the headers say a machine sent it",
                message_id,
                from_address,
            )
            continue

        if looks_like_opt_out(subject, body):
            result.skipped_opt_out += 1
            # Carried out to the caller so the address is actually opted out.
            # Still skipped from ``messages`` — there is nothing here to
            # classify and nothing to reply to — but "skipped" used to mean
            # "forgotten", and this is the one filter whose input is a human
            # asking us to stop.
            result.opt_outs.append(
                _candidate(message_id, stub, message, headers, from_address,
                           from_name, subject, body)
            )
            logger.info(
                "recruiter scan skipped %s from %s: reads as an opt-out request",
                message_id,
                from_address,
            )
            continue

        result.messages.append(
            _candidate(message_id, stub, message, headers, from_address,
                       from_name, subject, body)
        )

    # One line per scan carrying every number, because the question this feature
    # gets asked is never "what did it find?" — it is "why did it not find
    # *that* one?", and that is answered by the shape of the skips.
    logger.info(
        "recruiter scan for %s: %s", mask_email(account.email), result.as_dict()
    )
    return result


__all__ = [
    "MAX_REFERENCES_CHARS",
    "CandidateMessage",
    "DismissedMessage",
    "ScanResult",
    "build_query",
    "trim_references",
    "is_from_self",
    "message_id_for",
    "message_time",
    "own_addresses",
    "decode_header_value",
    "first_address",
    "headers_of",
    "parse_from",
    "references_for",
    "reply_headers_for",
    "reply_to_for",
    "scan",
]
