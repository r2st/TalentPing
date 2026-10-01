"""Reducing an inbound email to the part a human actually typed.

Every reply we receive carries our own outreach underneath it. Mail clients
quote by convention rather than by standard — Gmail writes ``On <date> <name>
wrote:``, Outlook writes a ``From:``/``Sent:``/``To:`` block under a rule of
underscores, older clients write ``-----Original Message-----`` — and the body
Gmail hands back is all of it concatenated.

That is a correctness problem, not a tidiness one. Our outreach says things like
"I'd love to hear about openings" and "are you available for a short call"; those
are the exact phrases the reply classifier keys on. A recruiter answering "Thanks
— we'll pass" quoted our own invitation back at us, and the classifier read the
quote and called the reply SCHEDULING. The user saw a rejection filed as a
meeting request.

The same is true of the boilerplate a *platform* wraps around a message it
forwards on someone's behalf — see the block above :data:`_FOOTER_RULE`, which
is where the most expensive version of this bug lived.

So: cut at the first quote marker, platform footer or signature, drop ``>``
lines, and — this is the part that matters — **never return nothing**. A
bottom-poster writes *below* the quote, so cutting at the marker would leave an
empty string and lose the reply entirely. Each fallback below is strictly less
aggressive than the one before it, and the last one is the original text: an
over-quoted classification beats a missing one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.places import fold_diacritics

# "On Wed, Jul 30, 2026 at 4:12 PM Jane Doe <jane@acme.com> wrote:" — Gmail and
# Apple Mail write this line above the quote, and they write it in the
# *sender's* language. That is the same localisation problem the Outlook header
# words below have, one client over, and it was left unsolved when they were
# fixed: a recruiter replying from a German Gmail sends "Am ... schrieb Jane
# Doe <jane@acme.com>:", which `^on\b` could never match. The quote stayed in
# the body, and the classifier read our own "are you available for a short
# call" back as the recruiter's answer — a rejection filed as SCHEDULING, which
# is the failure this whole module exists to prevent.
#
# The line has two halves worth naming. The **opener** is the preposition it
# starts with, and the **verb** is some spelling of "wrote". Which side of the
# name the verb falls on is a fact about the language rather than about the
# client: German and Dutch put it in front ("Am ... schrieb Jane Doe:"),
# English, French, Spanish, Portuguese and Italian put it behind ("... Jane Doe
# <jane@acme.com> a écrit :"). So the pattern asks for an opener, then a verb,
# then anything up to a trailing colon — which admits both orders without
# needing to know which language it is looking at.
#
# The colon and the verb together are what keep this tight. An opener alone is
# an ordinary English word ("On the other hand", "Am I still in the running"),
# and matching on one would cut a reply in half.
_ATTRIBUTION_OPENERS = (
    "il giorno",  # Italian, ahead of the bare openers so it is not clipped
    "w dniu",  # Polish
    "on",  # English
    "am",  # German
    "le",  # French
    "el",  # Spanish
    "em",  # Portuguese
    "op",  # Dutch
    "den",  # Swedish / Danish
)

#: Some spelling of "wrote", written the way the language spells it. The
#: unaccented twin of each is added by :func:`_both_spellings` rather than
#: listed here — see there for why one list is safer than two.
_ATTRIBUTION_VERBS = (
    "wrote",  # English
    "schrieb",  # German
    "a écrit",  # French
    "escribió",  # Spanish
    "escreveu",  # Portuguese
    "ha scritto",
    "scrisse",  # Italian
    "schreef",  # Dutch
    "skrev",  # Swedish / Danish / Norwegian
    "napisał",
    "napisała",  # Polish (m./f.)
    "kirjoitti",  # Finnish
)


def _both_spellings(words: tuple[str, ...]) -> tuple[str, ...]:
    """*words*, each followed by its unaccented spelling where that differs.

    Accented and unaccented forms both appear on the wire — a sender whose
    client, gateway or transliterating relay strips diacritics still writes the
    same line — so both have to be matchable.

    This used to be done by writing the twins out by hand beside the originals,
    and the list had drifted: "a écrit"/"a ecrit", "escribió"/"escribio" and
    "napisał"/"napisal" were all paired, and "napisała" was not. So a reply from
    a Polish recruiter whose name takes the feminine verb, sent through anything
    that flattens "ł", was the one attribution line here that went unread.

    That is not a cosmetic miss. An unrecognised marker means the quoted thread
    is never cut off, so :func:`visible_text` hands the caller the recruiter's
    own earlier message as though this sender had just typed it, and
    :func:`split_turns` reports one turn where there were two — which is the
    evidence :func:`app.services.conversation_stage.detect` counts to decide
    whether it is looking at first contact or the middle of a conversation.

    Derived rather than paired by hand so the next language added is complete
    by construction. Offsets are why the fold happens *here* and not on the
    line being matched: :func:`_author_of` slices a name at
    ``_ATTRIBUTION_VERB_RE``'s match end, and folding a haystack can change its
    length ("ß" is two characters unaccented), which would cut the name in the
    wrong place.
    """
    out: list[str] = []
    for word in words:
        for spelling in (word, fold_diacritics(word)):
            if spelling not in out:
                out.append(spelling)
    return tuple(out)


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(words)


# The Nordic clients do not open the line with a preposition at all: they open
# it with the *weekday*. Finnish Gmail writes "to 30. heinäk. 2026 klo 16.12
# Jane Doe (jane@acme.com) kirjoitti:", and Norwegian and Danish write "ons.
# 30. juli 2026 kl. 16:12 skrev Jane Doe <jane@acme.com>:". None of those
# begins with anything in :data:`_ATTRIBUTION_OPENERS`, so none of them could
# ever match — which made "kirjoitti" and half of "skrev" dead entries in the
# verb list above. Swedish was the accident that hid it: Gmail there prefixes
# "Den", so the one Nordic language with a preposition was the one that worked
# and the family looked covered.
#
# Everything the leak costs is what :func:`_both_spellings` describes for
# Polish, one language further north: an unmatched marker means
# :func:`visible_text` hands back the recruiter's own quoted message as though
# this sender had just typed it.
#
# Written as a *shape* rather than as seven weekday abbreviations in three
# languages, because the shape is what is actually distinctive and a list would
# drift the way the verb list did. The shape is a short word, optionally
# abbreviated with a dot, then an ordinal day number, then the year: "to 30.
# heinäk. 2026", "ons. 30. juli 2026", "tor. 30. jul. 2026".
#
# The year is not decoration, it is the whole safety margin. Without it the
# branch was "any short word followed by a number", and "Hi 30. of the month is
# fine, as I wrote:" satisfied that — an ordinary sentence, ending in a verb
# and a colon because ordinary sentences do, cut off at the top and its author
# left holding the bullet list underneath. A preposition opener does not need
# this because "On"/"Am"/"Le" are already a closed set; an *arbitrary* leading
# word is not, so the branch has to earn its narrowness from what follows it.
#
# It stands in for an opener and nothing more: the verb and the trailing colon
# still have to be there, which is what keeps prose out of both branches
# equally.
_DATE_FIRST_OPENER = r"[^\W\d_]{2,4}\.?\s+\d{1,2}\.[^\n]{0,40}?\b\d{4}\b"


def _opener_pattern() -> str:
    """The opener slot: a preposition in some language, or a weekday and date."""
    return (
        r"(?:(?:" + _alternation(_both_spellings(_ATTRIBUTION_OPENERS)) + r")\b"
        r"|" + _DATE_FIRST_OPENER + r")"
    )


#: An email address, as a regex fragment, so the pattern below and
#: :data:`_ADDRESS_IN_MARKER` recognise the same thing.
_ADDRESS_PATTERN = r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+"

# The line wraps in most clients, so the caller matches this against a small
# window of joined lines rather than a single one.
#
# What sits between the verb and the closing colon is where this pattern earns
# its keep, and it is two cases rather than one. When the verb comes last there
# is nothing between them but a stray "(a)" or a space, and demanding that is
# what keeps an ordinary sentence out: "On Monday I wrote to your colleague
# about the following:" opens with an opener, contains a verb and ends with a
# colon, and is not a quote marker. When the verb comes first the name and
# address fill the gap — so that spelling is admitted only with an address in
# it, which no such sentence has.
#
# Five characters in the verb-last gap, not more. It has to cover the Polish
# "napisał(-a):" and the space French leaves before its colon; the shortest
# thing a *sentence* puts there is " to me", which is six.
_ON_WROTE = re.compile(
    r"^\s*" + _opener_pattern() +
    r".{0,300}?\b(?:" + _alternation(_both_spellings(_ATTRIBUTION_VERBS)) + r")\b"
    r"(?:[^:\n]{0,5}|[^:\n]{0,160}" + _ADDRESS_PATTERN + r"[^:\n]{0,20})"
    r":\s*$",
    re.I | re.S,
)

#: Whether a line *could* open an attribution, which is all the callers that
#: join wrapped lines need to know before they start joining. Loose on purpose:
#: :data:`_ON_WROTE` is what actually decides, and a line that fails it is
#: treated exactly as it was before.
_ATTRIBUTION_START = re.compile(r"^\s*" + _opener_pattern(), re.I)

# Single-line quote banners that need no lookahead to be unambiguous.
_QUOTE_BANNERS = (
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*-{2,}\s*forwarded message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*_{10,}\s*$"),  # Outlook's divider above the header block
    re.compile(r"^\s*begin forwarded message:\s*$", re.I),
)

# Outlook writes its quoted-header block in whatever language the *sender's*
# client is set to, and a recruiter in Munich or Lyon is writing to a candidate
# in English out of a German or French Outlook. The header words are the one
# part of the quote that stays localised.
#
# These sets have to agree, and twice now they have not.
#
# The first time it was one language deep: "von" and "de" were spelled into the
# From pattern but the sibling gate only ever knew English, so a "Von:" line
# could never be confirmed and the German block was never recognised at all.
#
# The second time it was four languages wide, and in the other direction. The
# vocabulary here stopped at Dutch while the attribution table above went on to
# Polish, Swedish, Danish, Norwegian and Finnish — so the module could read a
# Polish recruiter's Gmail quote and not their Outlook one. "Od:/Wysłano:/Do:/
# Temat:", "Från:/Skickat:/Till:/Ämne:", "Fra:/Sendt:/Til:/Emne:" and
# "Lähettäjä:/Lähetetty:/Vastaanottaja:/Aihe:" all went straight through, and a
# block that is not recognised is not removed: the classifier reads our own
# "are you available for a short call" back as the recruiter's answer.
#
# So the table is now one row per language and the *row* is the unit that gets
# added, because a partial row is precisely what went wrong both times. A
# language that appears in :data:`_ATTRIBUTION_VERBS` and not here is a gap by
# construction, and visible as a missing line rather than as a word absent from
# the middle of a flat list.
_HEADER_WORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # language: (what opens the block, the siblings that confirm it)
    "en": (("from",), ("sent", "to", "date", "subject", "cc", "bcc")),
    "de": (("von",), ("gesendet", "an", "datum", "betreff")),
    "fr": (("de",), ("envoyé", "à", "objet")),
    "es": (("de",), ("enviado", "para", "asunto", "fecha")),
    "pt": (("de",), ("enviado", "para", "assunto")),
    "it": (("da",), ("inviato", "oggetto")),
    "nl": (("van",), ("verzonden", "aan", "onderwerp")),
    "pl": (("od",), ("wysłano", "do", "temat")),
    "sv": (("från",), ("skickat", "till", "ämne")),
    "da": (("fra",), ("sendt", "til", "emne")),
    "nb": (("fra",), ("sendt", "til", "emne")),
    "fi": (("lähettäjä",), ("lähetetty", "vastaanottaja", "aihe")),
}


def _with_unaccented(words: tuple[str, ...]) -> tuple[str, ...]:
    """*words*, each followed by its unaccented spelling where that differs.

    The same argument :func:`_both_spellings` makes for the attribution verbs,
    for the same wire: a Polish or Finnish Outlook block that reached us through
    a transliterating relay says "Wyslano:" and "Lahetetty:", and those are the
    same headers. Derived rather than paired by hand — the flat list this
    replaces had "envoyé"/"envoye" written out and nothing else, which is how
    hand-pairing always ends.

    The rule applies to the *twin* and never to the word itself, and the one
    place that matters is French. "À:" is French for To; it folds to "A:",
    which is also Italian and Spanish for To and is a line of ordinary prose
    away from a false positive — so the flat list this replaces excluded the
    one-letter spellings in prose and kept the accented one. Dropping "à" along
    with "a" would have been a quiet regression: a French block is still
    *recognised* without it, because "Envoyé:" and "Objet:" confirm it, but
    :data:`_HEADER_ANY` would stop consuming the block at the "À :" line and
    leave that line in the reply.
    """
    out: list[str] = []
    for word in words:
        twin = fold_diacritics(word)
        for spelling in (word, twin if len(twin) > 1 else word):
            if spelling not in out:
                out.append(spelling)
    return tuple(out)


def _header_vocabulary(slot: int) -> tuple[str, ...]:
    """Every word in one column of :data:`_HEADER_WORDS`, deduplicated."""
    words: tuple[str, ...] = ()
    for row in _HEADER_WORDS.values():
        words += tuple(w for w in row[slot] if w not in words)
    return _with_unaccented(words)


_FROM_WORDS = _header_vocabulary(0)
_SIBLING_WORDS = _header_vocabulary(1)


def _header_re(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"^\s*(?:" + "|".join(words) + r")\s*:\s*\S", re.I)


# The head of an Outlook-style quoted header block. On its own a "From:" line is
# not proof of anything — people write "From: the job spec you sent" — so it only
# counts when one of the sibling headers follows it within a few lines.
_HEADER_FROM = _header_re(_FROM_WORDS)
_HEADER_SIBLING = _header_re(_SIBLING_WORDS)
_HEADER_LOOKAHEAD = 4

# Signature delimiters. "-- " is the standards-blessed one; the rest are what
# phones actually append.
_SIG_DELIMITER = re.compile(r"^\s*--\s*$")

# ...and a phone appends it in the language the phone is set to, which is the
# third list in this module to have been written English-only while the tables
# above went on to twelve languages. "Von meinem iPhone gesendet", "Envoyé de
# mon iPhone", "Wysłane z iPhone'a" and "Lähetetty iPhonesta" all stayed on the
# end of the reply, so the sender's words — the one thing `visible_text`
# promises to isolate — arrived with a line of handset boilerplate glued to
# them, and the classifier and the drafted answer both read it.
#
# A false positive here is dearer than a missed one: `_is_signature_start`
# *ends* the reply, so a line wrongly read as a signature takes everything
# under it with it. That is what shapes the pattern below.
#
# Ten of the eleven spellings open with the send word and close on the device
# name. The distance between the two is bounded because it is only ever a
# preposition and a possessive — "from my", "de mon", "desde mi", "vanaf mijn",
# "från min", or in Finnish nothing at all — and, more importantly, the line
# has to *end* on the device, give or take an ending glued to it — Polish
# writes "iPhone'a" and Finnish "iPhonesta". No *space* is allowed after it,
# and that is the whole guard: "Enviado el informe a Samsung ayer" opens with
# the send word, names a device and ends five characters later, and it is a
# sentence about a report. A signature has nothing left to say after the
# handset; a sentence almost always does.
_SIG_DEVICE = (
    r"(?:i(?:phone|pad|pod)|android|samsung|galaxy|huawei|xiaomi|oneplus"
    r"|blackberry|pixel|smartphone|windows\s+phone|outlook|gmail)"
)
#: "Sent", spelled the way each language spells it on a handset. Polish and
#: Swedish differ from their Outlook header word by an ending ("Wysłano:" the
#: header, "Wysłane" the signature), so this is its own list rather than a
#: borrow from :data:`_HEADER_WORDS` — a shared list that needed a suffix rule
#: to be right in two places would be the drift, not the cure.
_SIG_SENT = (
    r"(?:sent|gesendet|envoy[ée]|enviad[oa]|inviato|verzonden"
    r"|wys[lł]an[aeoy]|skickat|sendt|l[aä]hetetty)"
)
_SIG_PHRASES = (
    # English keeps its own shape. "Sent from my <anything>" is unambiguous on
    # the strength of the possessive alone, and it covers the handsets no
    # device list will ever hold.
    re.compile(r"^\s*sent from my \w+", re.I),
    re.compile(r"^\s*get outlook for (ios|android)", re.I),
    # Everywhere else: the send word opens the line, a named device closes it.
    re.compile(rf"^\s*{_SIG_SENT}\b[^\n]{{0,25}}\b{_SIG_DEVICE}[^\s\n]{{0,8}}$", re.I),
    # German is the one that puts the verb last, exactly as it does in the
    # attribution line above.
    re.compile(r"^\s*von\s+mein\w*\b[^\n]{0,30}\bgesendet\s*$", re.I),
)

_QUOTED_LINE = re.compile(r"^\s*>")

# --------------------------------------------------------------------------- #
# Platform boilerplate                                                          #
# --------------------------------------------------------------------------- #
#
# A third kind of text that is not the sender's, and the most expensive one this
# module has had to deal with. A recruiter who writes through LinkedIn InMail
# does not send us their message — LinkedIn sends us a notification *containing*
# their message, wrapped in a footer we did not ask for:
#
#     i will give you Range between 50 CAD - 70 CAD//Hourly+ benefits
#
#     ----------------------------------------
#
#     This email was intended for Subhendu Das
#     You are receiving LinkedIn notification emails.
#
#     Unsubscribe: https://www.linkedin.com/comm/mypreferences/u/emailunsub?...
#
# The classifier scores "unsubscribe" as DECISIVE, and rightly: a person who
# types it means it, and it is too consequential to put to a flaky model. So
# every InMail notification in the mailbox was read as the recruiter asking us
# to stop. `_apply_intent` sets `Recruiter.opted_out`, which is the CAN-SPAM
# consent flag and is checked before every send forever, and returns without
# drafting. In production that had happened to 81 messages and 19 contacts —
# including the salary negotiation quoted above, which the product answered by
# marking the recruiter as never to be contacted again.
#
# Unlike a quote this text sits *below* the message and is not introduced by any
# of the markers above: `--` is two dashes and LinkedIn's rule is forty, so
# `_SIG_DELIMITER` never saw it.

# A horizontal rule above a footer block. Ten is well clear of an em-dash or the
# RFC 3676 `-- ` delimiter and well under what any sender actually draws.
_FOOTER_RULE = re.compile(r"^\s*[-–—_=*]{10,}\s*$")

# Lines that open platform boilerplate and cannot plausibly open a human's
# sentence. Deliberately anchored at the start of the line and deliberately
# specific: this cuts everything below it, so a phrase that could appear in
# something a recruiter typed does not belong here.
_FOOTER_PHRASES = (
    re.compile(r"^\s*this (e-?mail|message) was (intended|sent) (for|to)\b", re.I),
    re.compile(r"^\s*you (are receiving|received) this\b", re.I),
    re.compile(r"^\s*you are receiving\b.{0,60}\b(e-?mails?|notifications?)\b", re.I),
    re.compile(r"^\s*learn why we included this\b", re.I),
    re.compile(r"^\s*(to )?unsubscribe\s*[:|]\s*(https?://|www\.)", re.I),
    re.compile(r"^\s*to stop receiving\b", re.I),
    re.compile(r"^\s*manage (your )?(e-?mail )?(preferences|notifications)\b", re.I),
    re.compile(r"^\s*(©|\(c\))\s*\d{4}\b", re.I),
)


def _is_footer_start(line: str) -> bool:
    """Whether platform boilerplate begins at this line."""
    return bool(_FOOTER_RULE.match(line)) or any(
        pattern.match(line) for pattern in _FOOTER_PHRASES
    )


def _is_quote_start(lines: list[str], index: int) -> bool:
    """Whether the quoted original begins at ``lines[index]``."""
    line = lines[index]
    if any(pattern.match(line) for pattern in _QUOTE_BANNERS):
        return True

    # "On ... wrote:" wraps across up to three lines depending on the client and
    # the window width, so join forward until it either closes or clearly isn't.
    if _ATTRIBUTION_START.match(line):
        window = line
        for extra in lines[index + 1 : index + 3]:
            if _ON_WROTE.match(window):
                break
            window = f"{window} {extra.strip()}"
        if _ON_WROTE.match(window):
            return True

    if _HEADER_FROM.match(line):
        return any(
            _HEADER_SIBLING.match(nxt)
            for nxt in lines[index + 1 : index + 1 + _HEADER_LOOKAHEAD]
        )
    return False


def _is_signature_start(line: str) -> bool:
    return bool(_SIG_DELIMITER.match(line)) or any(p.match(line) for p in _SIG_PHRASES)


def _cut_at_quote(lines: list[str]) -> list[str]:
    """Everything above the first quote marker or signature."""
    out: list[str] = []
    for index, line in enumerate(lines):
        if (
            _is_quote_start(lines, index)
            or _is_signature_start(line)
            or _is_footer_start(line)
        ):
            break
        out.append(line)
    return out


def _drop_quoted_lines(lines: list[str]) -> list[str]:
    """Everything that isn't a ``>`` quote or a quote banner, order preserved.

    Used for bottom-posted replies, where cutting at the first marker would throw
    the reply away. Less precise than cutting — an Outlook header block survives
    it — but it keeps the human's words, which is the whole point.
    """
    return [
        line
        for index, line in enumerate(lines)
        if not _QUOTED_LINE.match(line) and not _is_quote_start(lines, index)
    ]


def _tidy(lines: list[str]) -> str:
    """Join, collapse runs of blank lines, and trim."""
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def visible_text(body: str | None) -> str:
    """The part of ``body`` the sender wrote, with the quoted thread removed.

    Falls back, in order, to: dropping only quoted lines (bottom-posted replies),
    then to the original text (anything else). Never returns an empty string for
    a non-empty input.
    """
    if not body or not body.strip():
        return ""

    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    trimmed = _tidy(_cut_at_quote(lines))
    if trimmed:
        return trimmed

    unquoted = _tidy(_drop_quoted_lines(lines))
    if unquoted:
        return unquoted

    return body.strip()


# --------------------------------------------------------------------------- #
# The other half: reading the quote instead of throwing it away                #
# --------------------------------------------------------------------------- #
#
# :func:`visible_text` exists to *lose* the quoted thread, and everything above
# is tuned for that. But the quote is also the only record we have of a
# conversation that happened before this product could see it — a recruiter and
# a candidate who have been mailing each other for a week, where the first
# message we ever lay eyes on is message six. Cutting the quote away there
# throws away the entire context of the reply we are about to write.
#
# So the same markers are read a second way: as boundaries between turns rather
# than as a point to truncate at. Every marker below is already trusted by
# :func:`_is_quote_start`; nothing new is being recognised, it is only being
# kept.

# Any header line in an Outlook-style quoted block. "From:" alone starts one
# (see :data:`_HEADER_FROM`); this is what the rest of the block looks like, and
# swallowing all of it keeps "Sent: Tuesday" — or "Gesendet: Dienstag" — from
# reading as the first sentence of the quoted message.
_HEADER_ANY = _header_re(_FROM_WORDS + _SIBLING_WORDS)

# One level of ">" quoting. Gmail adds a level per nesting depth, so stripping
# every level normalises a five-deep thread into plain lines that the ordinary
# marker patterns match.
_QUOTE_MARK = re.compile(r"^\s*(?:>\s?)+")

# "Jane Doe <jane@acme.com>" inside a marker line. The address is what decides
# whether a quoted turn was the candidate's or the recruiter's, and it is worth
# far more than the display name: names get abbreviated, addresses don't.
_ADDRESS_IN_MARKER = re.compile(_ADDRESS_PATTERN)

# How far a marker may run. An "On ... wrote:" wraps over at most three lines;
# an Outlook header block is From/Sent/To/Cc/Subject/Date and a stray blank.
_MARKER_MAX_LINES = 8

# The "wrote" verb inside a marker line, wherever the language puts it.
_ATTRIBUTION_VERB_RE = re.compile(
    r"\b(?:" + _alternation(_both_spellings(_ATTRIBUTION_VERBS)) + r")\b", re.I
)

# A clock time inside a marker line, with whatever the language hangs off it.
# The narrow no-break space is what Gmail actually emits between "4:12" and
# "PM"; `\s` covers it, but only because Python's `re` is Unicode-aware here.
_CLOCK_TIME = re.compile(r"\d{1,2}[:.]\d{2}(?:\s*(?:[ap]\.?m\.?|uhr|h))?", re.I)


@dataclass(frozen=True)
class QuotedTurn:
    """One message recovered from a quoted thread.

    ``author_email`` comes from the marker that introduced the turn, so it is
    absent for the newest turn — nothing quotes the message you are holding.
    """

    text: str
    author_email: str | None = None
    author_name: str | None = None


def _strip_quote_marks(body: str) -> list[str]:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [_QUOTE_MARK.sub("", line) for line in normalized]


def _consume_marker(lines: list[str], index: int) -> tuple[str, int]:
    """The marker text at ``lines[index]`` and how many lines it occupies.

    Consuming the whole marker is the difference between a turn that reads as
    the sender wrote it and one that opens with somebody's mail headers.
    """
    line = lines[index]

    if _ON_WROTE.match(line):
        return line, 1

    # "On <date> <who> wrote:" wraps across up to three lines, exactly as
    # :func:`_is_quote_start` allows.
    if _ATTRIBUTION_START.match(line):
        window = line
        for offset in range(1, 3):
            if index + offset >= len(lines):
                break
            window = f"{window} {lines[index + offset].strip()}"
            if _ON_WROTE.match(window):
                return window, offset + 1
        return line, 1

    # A banner or an Outlook "From:" block: take the contiguous run of header
    # lines under it, skipping blanks that sit inside the run.
    text, consumed = line, 1
    for offset in range(1, _MARKER_MAX_LINES + 1):
        position = index + offset
        if position >= len(lines):
            break
        candidate = lines[position]
        if _HEADER_ANY.match(candidate):
            text = f"{text}\n{candidate}"
            consumed = offset + 1
        elif not candidate.strip():
            continue
        else:
            break
    return text, consumed


def _author_of(marker: str | None) -> tuple[str | None, str | None]:
    """``(email, display name)`` for whoever wrote the turn a marker introduces."""
    if not marker:
        return None, None
    match = _ADDRESS_IN_MARKER.search(marker)
    address = match.group(0).lower() if match else None

    name = None
    from_line = next(
        (line for line in marker.split("\n") if _HEADER_FROM.match(line)), None
    )
    if from_line:
        name = from_line.split(":", 1)[1]
    elif match:
        # "On Wed, 30 Jul 2026 at 16:12 Jane Doe <jane@acme.com> wrote:" — the
        # name is what sits between the timestamp and the address.
        name = marker[: match.start()].rstrip(" <(\"'")
        # German and Dutch write the verb in front of the name ("Am ...
        # schrieb Jane Doe <jane@acme.com>:"), so when one is on this side of
        # the address, everything after it is the name and nothing else has to
        # be guessed. The last one, because a date can hold a word that also
        # spells a verb.
        verbs = list(_ATTRIBUTION_VERB_RE.finditer(name))
        if verbs:
            name = name[verbs[-1].end() :]
        else:
            # Otherwise the name trails the timestamp, and the timestamp is the
            # one part of a localised attribution line that is written the same
            # way everywhere: "4:12 PM", "16:12", "16.12 Uhr". Cutting at it
            # beats cutting at the year, which left the clock time glued to the
            # front of every English name we ever read (":12 PM Jane Doe").
            name = _CLOCK_TIME.split(name)[-1]
            name = re.split(r"\bat\s+\d|\d{4}[,]?\s*", name)[-1]
    if name:
        name = name.replace("<", " ").replace(">", " ")
        name = _ADDRESS_IN_MARKER.sub("", name).strip(" \t,;:()\"'-")
    return address, (name or None)


def _turn(buffer: list[str], marker: str | None) -> QuotedTurn:
    text = _tidy(_cut_at_quote(buffer))
    address, name = _author_of(marker)
    return QuotedTurn(text=text, author_email=address, author_name=name)


def split_turns(body: str | None) -> list[QuotedTurn]:
    """Split a body into the messages it contains, **newest first**.

    A message that quotes nothing yields one turn — the whole of it. A reply
    quoting a reply quoting the original yields three, oldest last, each reduced
    to what its own sender typed.

    The quote marks themselves are stripped before anything is matched, so the
    nesting depth a client uses (Gmail adds a ``>`` per level, Outlook adds
    none) makes no difference to what comes back.
    """
    if not body or not body.strip():
        return []

    lines = _strip_quote_marks(body)
    turns: list[QuotedTurn] = []
    buffer: list[str] = []
    marker: str | None = None  # nothing introduces the newest turn

    index = 0
    while index < len(lines):
        if _is_quote_start(lines, index):
            turns.append(_turn(buffer, marker))
            marker, consumed = _consume_marker(lines, index)
            buffer = []
            index += consumed
            continue
        buffer.append(lines[index])
        index += 1

    turns.append(_turn(buffer, marker))
    return turns


def has_quoted_history(body: str | None) -> bool:
    """Whether *body* carries at least one earlier message underneath it."""
    return any(turn.text for turn in split_turns(body)[1:])


__all__ = [
    "QuotedTurn",
    "has_quoted_history",
    "split_turns",
    "visible_text",
]
