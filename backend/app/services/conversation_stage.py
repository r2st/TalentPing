"""Is this message the start of a conversation, or the middle of one?

The inbound pipeline had exactly one answer to that question, and it was the
wrong kind of answer: *first contact* meant "we have no record of this
conversation". :func:`app.services.inbound_scanner.scan` skips Gmail threads
that have an :class:`~app.models.email_thread.EmailThread` row, and
:func:`app.services.recruiter_follow_up.find_prior_engagement` catches a sender
we have already replied to. Everything that got past both was handed to
:func:`app.services.inbound_reply.draft`, whose brief is written for "a recruiter
who contacted them out of the blue" — introduce yourself, name the role, ask
about the compensation band, the remote policy, the team and the stage of the
process.

Those two filters only see conversations **we** took part in. A candidate who
has been mailing a recruiter from their own inbox for a week has neither an
``EmailThread`` row nor a prior ``RecruiterEmail``, so the first message the
product ever sees might be message six — and it was answered as message one. In
production that produced a reply to a thread about booking a second-round
interview which introduced the candidate, explained why they were interested in
the role, and asked what stage the process was at. The recruiter had spent five
messages answering exactly those questions.

The evidence was on the message the whole time and nothing read it:

* the ``References`` / ``In-Reply-To`` headers, which a mail client only sets
  when it is replying to something;
* a reply marker in the subject — ``Re:``, and the ``Re:`` chains that survive a
  trip through a corporate gateway ("Re: EXTERNAL -Re: Interview Discussion");
* the quoted thread in the body, which is a verbatim transcript of everything
  said before we arrived.

**Two signals, not one.** Any single one of these has a plausible innocent
explanation: an ATS mailer that sets ``In-Reply-To`` to its own tracking id, a
recruiter who types "Re: your profile" into a brand-new message, a job
description pasted in under a forwarding banner. Requiring two independent
signals keeps genuine first contact — which must still get the resume and may
still auto-send — out of this path. The one exception is a quoted turn written
by the candidate themselves: that is not evidence of a conversation, it *is*
one, and it stands alone.

**What follows from a mid-thread verdict** is decided by the caller, but the
shape of it is: draft from the reconstructed transcript rather than from the
first-contact brief, never send it unreviewed, and don't re-attach a CV the
recruiter has already been reading for a week.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.services import reply_text
from app.services.reply_agent import ThreadMessage

# A reply marker anywhere in the subject's prefix chain. Deliberately not
# anchored at the start: corporate gateways prepend tags, so the real thing
# looks like "Re: EXTERNAL -Re: Interview Discussion || COVASANT" and a
# ``startswith("re:")`` test misses half of them once one hop has happened.
#
# ``Fwd:`` is *not* here. A forward is somebody handing us a new thing to look
# at, which is much closer to first contact than to a conversation in progress.
_REPLY_PREFIX_RE = re.compile(
    r"(?:^|[\s\-\[\]|(>])(re|aw|sv|antw|antwort|odp|res|回复)\s*[:\uff1a]",
    re.I,
)

# The same markers, anchored — "does this subject line *already* say Re:?" —
# which is a different question from the one above and needs a different answer.
#
# ``_REPLY_PREFIX_RE`` is deliberately unanchored because it is gathering
# evidence: a marker anywhere in the prefix chain says a gateway has been
# through it. Building the subject a reply goes out under cannot use that. It
# would read "Notes on the re: line" as an existing reply and thread a first
# contact under somebody else's conversation.
#
# So this one starts at the beginning, steps over the tags a gateway prepends
# — ``[EXTERNAL]``, ``(EXT)``, ``[SUSPICIOUS]`` — and then requires the marker.
# ``Re[2]:`` and ``Re(2):`` are here because Outlook and several webmail
# clients number a chain that way rather than stacking the word.
_LEADING_REPLY_RE = re.compile(
    r"^\s*(?:[\[(][^\])]{0,40}[\])]\s*)*"
    r"(?:re|aw|sv|antw|antwort|odp|res|回复)"
    r"\s*(?:[\[(]\d{1,3}[\])])?\s*[:\uff1a]",
    re.I,
)


@dataclass(frozen=True)
class Stage:
    """What we concluded about where in a conversation a message sits."""

    is_first_contact: bool
    #: Short phrases naming what was seen, for logs and the review screen.
    evidence: tuple[str, ...] = ()
    #: One sentence for the candidate, explaining the call we made.
    reason: str = ""
    #: The conversation as reconstructed from the quoted thread, oldest first.
    #: Empty on first contact — there is nothing to reconstruct.
    messages: list[ThreadMessage] = field(default_factory=list)

    @property
    def is_mid_thread(self) -> bool:
        return not self.is_first_contact


def is_reply_subject(subject: str | None) -> bool:
    """Whether a subject line says it is answering something."""
    return bool(_REPLY_PREFIX_RE.search(subject or ""))


def strip_reply_prefix(subject: str | None) -> str:
    """*subject* with its reply markers and gateway tags removed.

    "Re: [EXTERNAL] AW: unsubscribe" is "unsubscribe". The chain is stripped to
    a fixed point rather than once, because a subject that has been round a
    gateway and back carries several markers and only the outermost is visible
    to a single pass.

    Exists so a caller can ask what the sender actually *typed* in the subject
    line. :func:`reply_classifier.classify_reply_detailed` is the one that
    needed it: an opt-out whose whole content is the subject — which is exactly
    what the ``mailto:`` half of our own ``List-Unsubscribe`` header asks a mail
    client to send — has to be recognised through however many ``Re:`` markers
    the recipient's client stacked on it.
    """
    cleaned = (subject or "").strip()
    while True:
        stripped = _LEADING_REPLY_RE.sub("", cleaned, count=1).strip()
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


def reply_subject(subject: str | None, *, fallback: str = "your note") -> str:
    """The subject line a reply to *subject* goes out under.

    Three call sites built this string and only one of them was right.
    ``inbound_reply._subject_for`` tested ``startswith("re:")``, which is the
    naive form of the test this module already documents as insufficient:
    a gateway that *prepends* its tag — "[EXTERNAL] Re: Interview Discussion",
    which is the shape half of them use — fails it, and the reply went out as
    "Re: [EXTERNAL] Re: Interview Discussion". A German recruiter's "AW: Ihre
    Bewerbung" came back as "Re: AW: Ihre Bewerbung". The other two call sites
    (``tasks.inbox_tasks._apply_intent`` and
    ``follow_up_service.compose_follow_up``) had no test at all and prepended
    unconditionally.

    That is not only untidy. A stacked prefix is a spam signal in its own
    right, and the subject is what every mail client that does *not* thread on
    ``References`` — which is most of them outside Gmail — matches on: a
    recipient whose client threads by normalised subject sees the reply as a
    conversation of its own if the normalisation and our prefixing disagree.

    An empty subject was the worse half. ``f"Re: {thread.subject or ''}"``
    produced the literal string ``"Re:"`` — a subject line with nothing in it,
    on mail sent from the candidate's own Gmail. A recruiter row whose inbound
    message carried no subject is all it takes, and
    ``recruiter_reply_service`` copies that subject onto the thread. So an
    absent subject falls back to naming something instead.
    """
    cleaned = (subject or "").strip()
    if not cleaned:
        return f"Re: {fallback}"
    return cleaned if _LEADING_REPLY_RE.match(cleaned) else f"Re: {cleaned}"


def threaded_by_headers(
    references: str | None = None, in_reply_to: str | None = None
) -> bool:
    """Whether the sender's own client threaded this onto an earlier message.

    RFC 5322 §3.6.4: both headers name messages that already exist. A client
    composing a genuinely new message has nothing to put in either.
    """
    return bool((references or "").strip() or (in_reply_to or "").strip())


def _normalized(addresses: Iterable[str] | None) -> set[str]:
    return {a.strip().lower() for a in (addresses or ()) if a and a.strip()}


def reconstruct(
    body: str | None, *, own_addresses: Iterable[str] | None = None
) -> list[ThreadMessage]:
    """The conversation inside a quoted body, oldest first.

    Attribution is by address wherever a quote marker names one, because that is
    the only evidence that can't be wrong: a marker reading "Jordan
    <jordan@gmail.com> wrote:" against one of the user's own addresses is the
    candidate's turn no matter what the alternation says.

    Where no address is available it falls back to alternating speakers, seeded
    from the newest turn — which is the recruiter's, since they are the one who
    just wrote to us. A thread genuinely does alternate most of the time, and a
    mislabelled middle turn costs far less than having no transcript at all.
    """
    mine = _normalized(own_addresses)
    turns = [turn for turn in reply_text.split_turns(body) if turn.text]

    messages: list[ThreadMessage] = []
    previous: str | None = None
    for turn in turns:
        if turn.author_email:
            direction = "candidate" if turn.author_email in mine else "recruiter"
        elif previous is None:
            direction = "recruiter"
        else:
            direction = "candidate" if previous == "recruiter" else "recruiter"
        messages.append(
            ThreadMessage(
                direction=direction, body=turn.text, sender=turn.author_email
            )
        )
        previous = direction

    messages.reverse()  # the transcript reads oldest first
    return messages


def detect(
    *,
    subject: str | None,
    body: str | None,
    references: str | None = None,
    in_reply_to: str | None = None,
    own_addresses: Iterable[str] | None = None,
) -> Stage:
    """Where in a conversation an inbound message sits, and why we think so.

    Pure: headers, a subject and a body in, a verdict out. Nothing here reads
    the database, which is the whole point — the previous answer to this
    question was *only* a database lookup, and that is what made a week-old
    conversation look like a stranger's first email.
    """
    mine = _normalized(own_addresses)
    messages = reconstruct(body, own_addresses=mine)

    # A turn the candidate wrote, quoted back at us. Not evidence of a
    # conversation — a conversation.
    candidate_spoke = any(message.direction == "candidate" for message in messages)

    evidence: list[str] = []
    if threaded_by_headers(references, in_reply_to):
        evidence.append("their mail client threaded it onto an earlier message")
    if is_reply_subject(subject):
        evidence.append("the subject line is a reply")
    if len(messages) > 1:
        evidence.append(f"the body quotes {len(messages) - 1} earlier message(s)")

    if candidate_spoke:
        return Stage(
            is_first_contact=False,
            evidence=(*evidence, "you are quoted in the thread"),
            reason=(
                "You have already written in this thread, so this reply answers "
                "the conversation rather than introducing you."
            ),
            messages=messages,
        )

    if len(evidence) >= 2:
        return Stage(
            is_first_contact=False,
            evidence=tuple(evidence),
            reason=(
                "This message is part of a conversation already in progress ("
                + "; ".join(evidence)
                + "), so it was answered in context rather than as a first note."
            ),
            messages=messages,
        )

    return Stage(
        is_first_contact=True,
        evidence=tuple(evidence),
        reason="Nothing on this message says it belongs to an earlier conversation.",
        messages=[],
    )


__all__ = [
    "Stage",
    "detect",
    "is_reply_subject",
    "reconstruct",
    "reply_subject",
    "strip_reply_prefix",
    "threaded_by_headers",
]
