"""Thread-aware recruiter replies — Scout's draft, always for review.

:func:`app.services.ai_composer.compose_reply` drafts from the *latest* message
alone. That is enough for the first exchange and wrong for every one after it: a
recruiter who asked about notice period in message two and salary in message
four gets a reply answering only the salary, as though the earlier question was
never asked. Worse, a thread where the candidate already said "Tuesday works"
produces a draft offering availability again.

So this module drafts from the **whole conversation**. Three things follow from
that, and they are the reason this is its own module rather than another
parameter on ``compose_reply``:

* **The brief is chosen, not inherited.** The classified intent says what the
  recruiter's last message *was*; the template says what the reply should *do*,
  and those come apart. An OFFER whose numbers sit below the candidate's market
  band is a negotiation, not an acknowledgement — see :func:`choose_template`.
* **Negotiation is detected, never performed.** When a thread quotes a number
  the salary benchmarks call low, the draft asks to discuss compensation and
  anchors on the market band. It never states a counter-figure: the candidate's
  real floor isn't in any of our data, and a number invented on their behalf is
  one they have to walk back.
* **Nothing here sends.** Every return value is a draft for the review queue.
  The module has no Gmail dependency at all, which is what makes that guarantee
  checkable rather than promised.

The grounding rule from the tailorer applies unchanged: a draft may only use
facts the thread or the candidate's own profile supports. A model that invents
an availability window, a notice period or a salary figure is discarded in
favour of the deterministic template.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.config import settings
from app.models.email import EmailDirection, ReplyIntent, ReplyTemplate
from app.services import untrusted
from app.services.ai_composer import CandidateContext
from app.services.currency import (
    APOSTROPHE_GROUPED,
    CURRENCY_PATTERN,
    GROUPING_CHARS,
    INDIAN_GROUPED,
    usd_rate,
)
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    llm_is_configured,
    looks_like_reasoning,
    strip_code_fence,
)
from app.services.pay_period import annual_multiplier
from app.services.reply_text import visible_text

logger = logging.getLogger(__name__)

# How much of the conversation the model is shown. Recruiter threads are short;
# this is generous enough to never truncate a real one, and bounded so a mailing
# list that got misfiled can't blow the context window.
MAX_THREAD_MESSAGES = 12
MAX_CHARS_PER_MESSAGE = 1200


@dataclass
class ThreadMessage:
    """One message in a conversation, flattened for the prompt.

    Deliberately not the ORM row: drafting is pure, so it can be tested without
    a database and can't accidentally mutate the thread it is reading.
    """

    direction: str  # "recruiter" | "candidate"
    body: str
    subject: str | None = None
    sender: str | None = None

    @property
    def is_recruiter(self) -> bool:
        return self.direction == "recruiter"


@dataclass
class ReplyDraft:
    """What Scout produced, and the reasoning the UI shows beside it."""

    body: str
    template: ReplyTemplate
    # One line under the "Scout suggests" label explaining the angle taken.
    note: str = ""
    generated_with: str = "heuristic"
    model: str | None = None
    # Why this is the template rather than a generation, when it is. "throttled"
    # means every provider was rate limited, which is temporary and worth
    # waiting out; anything else means the text is the best we will get. Callers
    # that send without review use this to tell those two apart.
    fallback_reason: str | None = None
    # Set when the thread looks like a below-market offer. Drives the
    # negotiation prompt and the badge in the inbox.
    negotiation_detected: bool = False
    # Facts pulled off the thread that the draft is allowed to reference.
    open_questions: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Reading a thread                                                             #
# --------------------------------------------------------------------------- #


def thread_messages(thread) -> list[ThreadMessage]:
    """Flatten an :class:`~app.models.email_thread.EmailThread` for drafting.

    Only messages with a body are kept — a row recording that something was sent,
    with the text never captured, contributes nothing to a draft and would read
    to the model as an empty turn.

    Each body is reduced to what its sender actually typed. Stored bodies carry
    the whole quoted thread underneath them, so without this the transcript is
    every message plus a copy of every message before it — and, worse, the
    recruiter's "latest message" contains our own questions quoted back. The
    open-questions pass reads those as things *they* asked, and the draft
    answers the candidate's own email.
    """
    out: list[ThreadMessage] = []
    for email in getattr(thread, "emails", None) or []:
        body = visible_text(getattr(email, "body_text", None))
        if not body:
            continue
        received = getattr(email, "direction", None) == EmailDirection.RECEIVED
        out.append(
            ThreadMessage(
                direction="recruiter" if received else "candidate",
                body=body,
                subject=getattr(email, "subject", None),
                # ``from_address``, not ``to_address``: this field names who
                # wrote the message. It read ``to_email`` for a long time, which
                # is not a column on this model at all, so ``getattr``'s default
                # made every sender None without anything failing.
                sender=getattr(email, "from_address", None),
            )
        )
    return out


def transcript(messages: list[ThreadMessage]) -> str:
    """Render the conversation the way the model reads it.

    Oldest first, so "what was already said" reads in the order it happened, and
    the last turn — the one being answered — sits closest to the instruction.
    Only the tail is kept when a thread is long: the opening of a twenty-message
    thread is history, and the recent turns are what the reply must fit.
    """
    recent = messages[-MAX_THREAD_MESSAGES:]
    lines: list[str] = []
    for index, message in enumerate(recent, start=1):
        who = "RECRUITER" if message.is_recruiter else "CANDIDATE"
        body = message.body[:MAX_CHARS_PER_MESSAGE].strip()
        lines.append(f"[{index}] {who}:\n{body}")
    return "\n\n".join(lines)


# Questions the recruiter asked that no later candidate message answered. Naive
# on purpose: a sentence ending in "?" in an inbound message is a question, and
# the model is asked to address the ones still open. Precision matters less than
# recall here — a redundant answer is polite, a dropped question is rude.
_QUESTION_RE = re.compile(r"[^.!?\n]*\?")


def open_questions(messages: list[ThreadMessage]) -> list[str]:
    """Recruiter questions the candidate has not answered yet, oldest first.

    Every recruiter turn since the candidate last spoke, not only the newest
    one. Recruiters double-text: "Also — what's your notice period?" arrives as
    its own message the next morning, and reading the last message alone drops
    everything the message before it asked. That is the exact failure this
    module exists to fix, so the list it feeds the prompt has to span the run.

    A question the candidate has already replied to is not open, and the
    cheapest honest test for that is whether they have said anything at all
    since it was asked. So the scan stops at the candidate's last turn: when
    they spoke last there is nothing outstanding, and the brief is a nudge
    rather than an answer.
    """
    blocks: list[list[str]] = []
    for message in reversed(messages):
        if not message.is_recruiter:
            break
        blocks.append(
            [
                q.strip()
                for q in _QUESTION_RE.findall(message.body)
                if 10 < len(q.strip()) <= 300
            ]
        )
    found = [question for block in reversed(blocks) for question in block]
    # The newest five: when a run overflows the cap, the questions closest to
    # the reply are the ones it must not drop.
    return found[-5:]


def latest_recruiter_message(messages: list[ThreadMessage]) -> str:
    for message in reversed(messages):
        if message.is_recruiter:
            return message.body
    return ""


# --------------------------------------------------------------------------- #
# Compensation detection                                                       #
# --------------------------------------------------------------------------- #

# Money as recruiters write it: "$180,000", "180k", "€95k", "120 000 EUR",
# "90000", "¥8,000,000", "₹20,00,000", "CHF 130'000". The lakh grouping comes
# first and the Western grouping second, so "20,00,000" is read whole and
# "150,000" is read whole rather than as "150" with a stray "000" left over.
#
# :data:`~app.services.currency.APOSTROPHE_GROUPED` is here for the same reason
# ``jd_parser`` and ``resume_parser`` both carry it, and this module was the one
# of the three that did not. A Swiss band is written "CHF 130'000", the
# alternation above cannot see an apostrophe, and ``\d{2,7}`` then matched the
# leading "130" — 143 dollars once priced, under the plausibility floor, so the
# figure vanished. That is not merely a missed badge:
# :func:`_states_a_figure` compares the *thread* against the *draft*, so a
# recruiter's "CHF 130'000" extracted as nothing and a model echoing it back as
# "CHF 130,000" extracted as 143,000 read as a salary the model had invented.
# The whole generated reply was thrown away and a template sent in its place —
# the anti-fabrication guard firing on the recruiter's own number.
#
# The currency marker is part of the match on purpose: it is what
# :func:`_figure_rate` prices the figure with, and a marker left outside the
# span is a marker nobody reads.
_MONEY_RE = re.compile(
    rf"(?:(?:{CURRENCY_PATTERN})\s?)?"
    rf"({INDIAN_GROUPED}|{APOSTROPHE_GROUPED}|\d{{1,3}}(?:[,.\s]\d{{3}})+"
    rf"|\d{{2,7}})\s?(k\b)?",
    re.I,
)
#: Every character that separates digits rather than valuing them, so a matched
#: figure is turned into an integer by stripping all of them.
#: :data:`~app.services.currency.GROUPING_CHARS` is the shared answer; the
#: ``\s`` is kept beside it because the patterns here admit any whitespace as a
#: thousands separator, not only the three typographic spaces that constant
#: lists. Missing the apostrophe made ``int()`` read "130'000" as 130.
_GROUPING_RE = re.compile(rf"[{re.escape(GROUPING_CHARS)}\s]")
#: A marker written *after* the figure — "120,000 EUR", "8,000,000 JPY". The
#: money pattern stops at the last digit, so this is the only thing that sees it.
_TRAILING_CURRENCY_RE = re.compile(rf"^\s*(?:{CURRENCY_PATTERN})", re.I)
_CURRENCY_MARKER_RE = re.compile(CURRENCY_PATTERN, re.I)
#: The vocabulary of a message about pay. Matched as **words**, by
#: :data:`_COMP_WORD_RE` — which is the whole point of the pattern existing.
#:
#: These used to be tested with ``word in lowered``, and a bare substring test
#: over a list this short is a gate that is open. "ote" is inside "remote",
#: "note", "quote", "promoted" and "vote"; "rate" is inside "corporate",
#: "accurate" and "separate"; "base" is inside "based in London"; "range" is
#: inside "arranged"; "band" is inside "bandwidth". A recruiter's email
#: containing the word "remote" is not thereby an email about money, and every
#: number in it became a salary figure: "Acme has 45,000 employees worldwide
#: and is fully remote" was extracted as a $45,000 offer and badged in the
#: inbox as "about 75% under the 180,000 USD median for this role and market" —
#: a sentence about a real recruiter, shown to the user, that nobody had said.
#:
#: ``comp`` carried a trailing space for exactly this reason, so that it would
#: not fire on "company". The word boundary is that fix, applied to all of
#: them; the space is gone with it, since it also meant "comp." at the end of a
#: sentence did not count.
_COMP_WORDS = (
    "salary", "salaries", "compensation", "comp", "base", "package", "offer",
    "pay", "paid", "paying", "rate", "budget", "range", "band", "equity",
    "bonus", "ote",
)
#: The plural suffix covers "rates", "packages", "bonuses"; the irregulars
#: ("salaries", "paying") are spelled out in the list above.
_COMP_WORD_RE = re.compile(rf"\b(?:{'|'.join(_COMP_WORDS)})(?:e?s)?\b", re.I)


#: A currency marker written against digits — "$120,000", "USD 120000",
#: "120,000 EUR". Against digits rather than merely present, so "please quote
#: in USD" is not a figure and "we need 30,000 hours" beside it is not a salary.
_MARKED_FIGURE_RE = re.compile(
    rf"(?:{CURRENCY_PATTERN})\s?\d|\d\s?(?:{CURRENCY_PATTERN})", re.I
)


def mentions_compensation(text: str) -> bool:
    """True when a message is talking about money at all.

    The word list is a proxy for "this is about pay", and a currency marker
    written against a figure is not a proxy — it is the thing itself. Without
    that second test the proxy has to be right about vocabulary the recruiter
    chose: "We can offer $85/hr" was read and "We can do $85/hr on this one"
    was not, and neither was "Can you do $120,000?", because "do" is not on the
    list and no other word on it appears. Both are threads quoting a number at
    the candidate, which is the only thing :func:`detect_negotiation` is
    looking for.

    Widening the gate does not widen what counts as a figure: every number
    still has to survive :func:`extract_salary_figures`'s plausibility band,
    and the badge still only appears below the market median.
    """
    if _COMP_WORD_RE.search(text or ""):
        return True
    return _MARKED_FIGURE_RE.search(text or "") is not None


def _marked_span(text: str, match: re.Match[str]) -> str:
    """One money match plus any currency written just after it.

    :data:`_MONEY_RE` takes a *leading* marker into its own span and stops at
    the last digit, so "120,000 EUR" and "8,000,000 JPY" need this to be priced
    at all.
    """
    trailing = _TRAILING_CURRENCY_RE.match(text[match.end() : match.end() + 8])
    return match.group(0) + (trailing.group(0) if trailing else "")


def _figure_rate(marked: str, message_rate: float) -> float:
    """Dollars per unit for one figure: its own marker, else the message's.

    A recruiter writes the currency once — "the band is €90,000 to €110,000",
    or just as often "€90,000 to 110,000". The second figure in that sentence
    carries no mark of its own and is plainly in euros, so an unmarked figure
    inherits whatever the message said. An explicitly marked one never does:
    "$150,000 (about €138,000)" is two currencies in one sentence and the
    dollar figure is a dollar figure.

    ``usd_rate`` returning something other than 1.0 is itself evidence of a
    marker — the lakh grouping is one, without any symbol being written.
    """
    own = usd_rate(marked)
    if own != 1.0 or _CURRENCY_MARKER_RE.search(marked):
        return own
    return message_rate


def extract_salary_figures(text: str) -> list[int]:
    """Annual figures a message quotes, **in US dollars**.

    Only plausible annual salaries are returned. A bare "5" from "a team of 5"
    and a "2024" from a date both fall outside the band, which is the cheapest
    way to avoid reading arbitrary numbers as compensation.

    **The conversion is the same convention the rest of the product enforces**
    — see :mod:`app.services.currency`, which names ``jd_parser`` and
    ``resume_parser`` as the two places a figure gets in. This is the third,
    and it was reading an employer's number straight out of an email and
    handing it to :func:`detect_negotiation` to compare against a dollar
    median.

    **A rate is annualised** the same way and by the same code a posting's is —
    :func:`app.services.pay_period.annual_multiplier`. "We can do $85/hr" is how
    contract work is quoted, and eighty-five is not a salary by any reading, so
    the whole class of thread was invisible: no figure extracted, no
    negotiation badge, and :func:`_states_a_figure` unable to tell a rate the
    recruiter quoted from one the model invented.

    It failed in both directions, and the plausibility band is what made the
    failures silent. ¥8,000,000 is an ordinary Tokyo salary and about $54,000 —
    well under any senior median, exactly the thread worth flagging — but eight
    million is over the ceiling, so it was dropped and no badge appeared.
    ₹20,00,000 is about $24,000 and was not matched at all: a pattern built for
    groups of three reads "20,00,000" as a bare "20". And £95,000 was compared
    to a dollar median as though the pound sign were decorative, which
    overstates the gap by fourteen points and then prints the figure back to
    the user labelled "USD".

    **The message rate is read off the figures, not off the prose.** An
    unmarked figure inherits the currency a marked sibling was written in — see
    :func:`_figure_rate` — and the rate it inherited used to be
    ``usd_rate(text)``, the first currency token anywhere in the message. That
    is the very distinction :data:`_MARKED_FIGURE_RE` exists to draw, applied to
    the gate and not to the arithmetic behind it, and a recruiter's email is
    mostly prose:

    * "The salary range is 130,000 to 150,000. You'll own our CAD pipeline."
      In a mechanical, hardware or manufacturing thread ``CAD`` is the drawing
      package, not the Canadian dollar, and there is no casing that tells them
      apart. Both figures were multiplied by 0.75 and the band was reported to
      the user as 97,500–112,500 "USD", a quarter under what the recruiter
      offered — and then badged as below the market median on the strength of
      it.
    * "Ils confirment le salaire : 90 000 à 110 000 €." ``ils`` is the French
      for "they"; ``ILS`` is the shekel. The euro sign is written once, on the
      second figure, and the first inherited 0.27 — 90,000 euros read as
      $24,300.

    Every span here is a figure with whatever marker was written against it, so
    the euro on the second figure is what the first inherits and a currency code
    loose in a sentence prices nothing. The lakh grouping keeps its say, since
    it is a marker written *as* the figure rather than beside it.
    """
    if not mentions_compensation(text):
        return []

    matches = list(_MONEY_RE.finditer(text or ""))
    marked_spans = [_marked_span(text or "", match) for match in matches]
    message_rate = usd_rate(" ".join(marked_spans))
    figures: list[int] = []
    for match, marked in zip(matches, marked_spans, strict=True):
        raw, suffix = match.group(1), (match.group(2) or "").lower()
        digits = _GROUPING_RE.sub("", raw)
        if not digits.isdigit():
            continue
        value = int(digits)
        if suffix == "k":
            value *= 1000
        value = round(
            value
            * annual_multiplier(text, match.start(), match.end())
            * _figure_rate(marked, message_rate)
        )
        # Tested after conversion, because "above any salary anyone is offered"
        # is a sentence about dollars — see `jd_parser._IMPLAUSIBLE_ANNUAL`.
        if 20_000 <= value <= 2_000_000:
            figures.append(value)
    return figures


def detect_negotiation(text: str, band=None) -> tuple[bool, str]:
    """Whether this thread is a negotiation opportunity, and why.

    A thread qualifies when it quotes a number *and* that number sits below the
    market median for the role. Below-median rather than below-max on purpose:
    half of all real offers land under the max by definition, and flagging those
    would make the badge meaningless.
    """
    figures = extract_salary_figures(text)
    if not figures:
        return False, ""
    if band is None:
        return False, ""

    median = getattr(band, "median", None)
    if not median:
        return False, ""

    offered = max(figures)
    if offered >= median:
        return False, ""

    gap = round((median - offered) / median * 100)
    currency = getattr(band, "currency", "USD") or "USD"
    # "Works out at" rather than "quotes": the figure is annualised and priced
    # in dollars, so on an hourly or non-dollar thread it is a number the
    # recruiter never typed. This sentence is shown to the candidate, and
    # attributing a derived figure to the recruiter as a quotation is the sort
    # of thing they would repeat back in the reply.
    return True, (
        f"The thread works out at {offered:,} {currency} a year, about {gap}% "
        f"under the {median:,} {currency} median for this role and market."
    )


# --------------------------------------------------------------------------- #
# Choosing the brief                                                           #
# --------------------------------------------------------------------------- #


def choose_template(
    intent: ReplyIntent | str | None,
    *,
    negotiation: bool = False,
    messages: list[ThreadMessage] | None = None,
) -> ReplyTemplate:
    """Pick the brief the draft is written to.

    The classified intent describes the recruiter's last message; the template
    describes what the reply should accomplish. They agree most of the time and
    diverge in the cases worth having this function for:

    * a below-market offer becomes ``SALARY_NEGOTIATION`` rather than a
      grateful acknowledgement of a number the candidate shouldn't accept yet;
    * a thread whose last word is the candidate's own becomes ``FOLLOW_UP``,
      because there is nothing to reply *to* — this is a nudge.
    """
    if negotiation:
        return ReplyTemplate.SALARY_NEGOTIATION

    if messages and not messages[-1].is_recruiter:
        return ReplyTemplate.FOLLOW_UP

    key = intent.value if isinstance(intent, ReplyIntent) else (intent or "")
    return {
        "SCHEDULING": ReplyTemplate.SCHEDULING,
        "INTERESTED": ReplyTemplate.INTERESTED,
        "OFFER": ReplyTemplate.INTERESTED,
        "QUESTION": ReplyTemplate.QUESTION,
        "NOT_INTERESTED": ReplyTemplate.DECLINING,
    }.get(key.upper(), ReplyTemplate.INTERESTED)


_TEMPLATE_BRIEF: dict[ReplyTemplate, str] = {
    ReplyTemplate.INTERESTED: (
        "They're positive. Thank them, confirm genuine interest, and propose a "
        "short intro call as the concrete next step. Do not restate anything the "
        "candidate has already told them in this thread."
    ),
    ReplyTemplate.SCHEDULING: (
        "They want to book time. Confirm readiness and offer broad flexibility "
        "(e.g. mornings over the next week) WITHOUT inventing specific dates or "
        "slots. If the candidate already gave availability earlier in the "
        "thread, refer back to it rather than contradicting it. Ask them to pick."
    ),
    ReplyTemplate.SALARY_NEGOTIATION: (
        "They have quoted compensation that sits below the market band for this "
        "role. Stay warm and clearly still interested — this is a negotiation, "
        "not a rejection. Acknowledge the offer, say the number is below what "
        "the candidate is targeting for this scope, and reference the market "
        "range you are given as the basis. You MUST NOT state a counter-figure, "
        "a minimum, or any number the candidate has not already given: propose a "
        "conversation about the range instead. Ask for the full package in "
        "writing (base, equity, bonus) if it hasn't been shared."
    ),
    ReplyTemplate.DECLINING: (
        "The RECRUITER has passed on the candidate, or closed the role. The "
        "candidate is not withdrawing and must not be written as though they "
        "were. Be gracious and brief: thank them for letting you know, leave the "
        "door open for future roles, ask nothing of them, and give no critical "
        "feedback about the process or the decision."
    ),
    ReplyTemplate.FOLLOW_UP: (
        "The candidate sent the last message and has had no answer. Write a "
        "short, low-pressure nudge that adds one line of new value or restates "
        "interest. Do not express frustration and do not imply they are late."
    ),
    ReplyTemplate.QUESTION: (
        "They asked something. Answer only from what the thread and the "
        "candidate's profile support. If a question needs specifics you don't "
        "have, say the candidate will follow up with detail rather than guessing."
    ),
}

_TEMPLATE_NOTE: dict[ReplyTemplate, str] = {
    ReplyTemplate.INTERESTED: "Confirms interest and proposes an intro call.",
    ReplyTemplate.SCHEDULING: "Offers flexible availability without inventing slots.",
    ReplyTemplate.SALARY_NEGOTIATION: (
        "Opens a compensation conversation, anchored on the market band."
    ),
    ReplyTemplate.DECLINING: "Answers a rejection graciously and keeps the door open.",
    ReplyTemplate.FOLLOW_UP: "A light nudge on a thread that went quiet.",
    ReplyTemplate.QUESTION: "Answers what was asked, from the profile only.",
}

_TEMPLATE_FALLBACK: dict[ReplyTemplate, str] = {
    ReplyTemplate.INTERESTED: (
        "Hi,\n\nThank you for getting back to me — I'm genuinely interested. Would "
        "a short intro call in the next week make sense? I'm happy to work around "
        "your schedule.\n\nBest,\n{name}"
    ),
    ReplyTemplate.SCHEDULING: (
        "Hi,\n\nThanks for reaching out — I'd be glad to talk. I'm fairly flexible "
        "over the next week or two, mornings especially. Let me know a time that "
        "works on your end and I'll make it happen.\n\nBest,\n{name}"
    ),
    ReplyTemplate.SALARY_NEGOTIATION: (
        "Hi,\n\nThank you for sharing this — I'm still very interested in the role "
        "and the team. I did want to flag that the figure is below what I'm "
        "targeting for a role at this scope, based on what I'm seeing in the "
        "market for comparable positions.\n\nCould we set up a short call to talk "
        "through the range? It would also help to see the full package in writing "
        "— base, equity and bonus — so I can consider it properly.\n\n"
        "Best,\n{name}"
    ),
    ReplyTemplate.DECLINING: (
        "Hi,\n\nThank you for taking the time to get back to me — I appreciate it. "
        "Should something aligned come up down the line, I'd be glad to reconnect. "
        "Wishing you and the team the best.\n\nBest,\n{name}"
    ),
    ReplyTemplate.FOLLOW_UP: (
        "Hi,\n\nJust following up on my last note — still very interested in the "
        "role and happy to work around your timing. Let me know if there's "
        "anything useful I can send over in the meantime.\n\nBest,\n{name}"
    ),
    ReplyTemplate.QUESTION: (
        "Hi,\n\nThanks for the note and the question. Happy to give you what you "
        "need — I'll follow up shortly with the specifics.\n\nBest,\n{name}"
    ),
}


# --------------------------------------------------------------------------- #
# Drafting                                                                     #
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = (
    "You draft a job seeker's reply to a recruiter, given the ENTIRE email "
    "thread so far. The draft is reviewed by the candidate before it is sent.\n\n"
    "Absolute rules:\n"
    "1. Read the whole thread. Never repeat information the candidate has "
    "already given, never contradict something they already said, and never "
    "ask a question that was already answered.\n"
    "2. Never invent specifics: no exact dates or times, no notice period, no "
    "salary figures, no acceptance of an offer, no claim about the candidate's "
    "experience that the profile does not state.\n"
    "3. Address any question the recruiter asked that is still unanswered.\n"
    "4. Match the recruiter's register. Keep it under 160 words, warm and "
    "direct, with one clear next step.\n"
    "5. Follow the brief exactly.\n\n"
    "Return ONLY the reply body — no subject line, no greeting placeholder, no "
    "commentary about what you wrote."
)


def _build_prompt(
    cand: CandidateContext,
    messages: list[ThreadMessage],
    template: ReplyTemplate,
    questions: list[str],
    negotiation_note: str,
    band=None,
) -> str:
    parts = [
        f"Brief: {_TEMPLATE_BRIEF[template]}",
        "",
        "== CONVERSATION SO FAR (oldest first) ==",
        # Fenced, because half of these turns were typed by the recruiter and
        # the draft built from them can be sent without a human reading it
        # first. The candidate's own facts below are *not* fenced: they are
        # this product's records, and the model is meant to act on them.
        untrusted.fence(
            transcript(messages) or "(no messages captured)",
            label="email thread",
        ),
        "",
        "== CANDIDATE (the only facts about them you may use) ==",
        f"Name: {cand.name}",
        f"Headline: {getattr(cand, 'headline', None) or 'n/a'}",
        f"Skills: {', '.join((getattr(cand, 'skills', None) or [])[:10]) or 'n/a'}",
    ]

    if questions:
        parts += [
            "",
            "== STILL UNANSWERED — address these ==",
            *(f"- {q}" for q in questions),
        ]

    if negotiation_note and band is not None:
        parts += [
            "",
            "== MARKET DATA (basis for the compensation conversation) ==",
            negotiation_note,
            f"Market range for this role: {band.minimum:,}–{band.maximum:,} "
            f"{getattr(band, 'currency', 'USD')}, median {band.median:,}.",
            "Reference this range as market context. Do NOT state a counter-offer "
            "or any figure as the candidate's own number.",
        ]

    return "\n".join(parts)


# Specifics a draft must never contain, because we cannot know them. Each is a
# thing free models reliably volunteer — a Tuesday that isn't in the thread, a
# notice period nobody stated — and each one gets the candidate caught out.
_INVENTED_DATE_RE = re.compile(
    r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
    # The minutes are optional, and that is the whole point: a model proposing a
    # slot writes "3pm" far more often than "3:00pm", and requiring the ":00"
    # let every bare hour through the one guard that exists to stop it.
    r"|\b\d{1,2}(?::\d{2})?\s?[ap]\.?m\.?(?![a-z])"
    r"|\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
    re.I,
)


def _normalize_clock(text: str) -> str:
    """Lowercase *text* and reduce every clock time to a single spelling.

    "3 p.m.", "3 PM" and "3pm" are one slot written three ways. The echo test
    below is a containment check, so without this it compares spellings rather
    than times — and a draft repeating back the hour the recruiter proposed gets
    thrown away as invented.
    """
    lowered = (text or "").lower().replace(".", "")
    return re.sub(r"\s+(?=[ap]m\b)", "", lowered)


def _invents_specifics(draft: str, thread_text: str) -> str | None:
    """Return the first concrete detail the draft asserts but the thread lacks.

    A date the recruiter proposed is fair to echo; one that appears only in the
    draft was invented. Same test either way: is it already in the conversation?
    """
    haystack = _normalize_clock(thread_text)
    for match in _INVENTED_DATE_RE.finditer(draft or ""):
        if _normalize_clock(match.group(0)) not in haystack:
            return match.group(0)
    return None


#: A number in any of the shapes a salary is written in. Shared by both halves
#: of :data:`_MONEY_ANYWHERE_RE` so a marker in front and a marker behind agree
#: about what a number is.
_AMOUNT = (
    rf"{INDIAN_GROUPED}|{APOSTROPHE_GROUPED}"
    rf"|\d{{1,3}}(?:[,.\s]\d{{3}})+|\d{{2,7}}"
)

# Money that is unmistakably money, whichever words surround it: a currency
# marker on either side of the figure, or a "k" behind it. Deliberately
# narrower than :data:`_MONEY_RE` about what counts as a number and
# deliberately broader about where it may appear —
# :func:`extract_salary_figures` only looks once a message is *already* talking
# about pay, which is the right test mid-negotiation, where the subject is
# established, and the wrong one everywhere else. "I'm targeting around
# $250,000" uses no compensation vocabulary at all.
#
# The marker is :data:`~app.services.currency.CURRENCY_PATTERN`, and it used to
# be the three symbols ``$€£`` written in front. Both narrowings cost
# :func:`_names_a_figure` the same way, because that function compares the
# *thread's* figures against the *draft's* and a pattern that reads one
# spelling of a number but not another makes two spellings of one figure look
# like two different figures:
#
# * a recruiter writing "USD 150,000" produced no known figure at all, so a
#   model echoing it back as "$150,000" — the same currency, the ordinary
#   spelling — was reported as a salary the model had invented;
# * "150,000 EUR" against a draft's "€150,000" failed identically.
#
# Both threw the whole generated reply away and sent a template, which is the
# same failure :func:`_normalize_clock` exists to prevent for "3pm" and
# "3 p.m.": one fact, two spellings, and only the comparison is confused.
#
# Widening it strengthens the guard as well as unblocking the echo. A thread
# quoted entirely in yen, rupees or francs had *no* figure this function could
# see, so a model volunteering one into it was unchecked here.
_MONEY_ANYWHERE_RE = re.compile(
    rf"(?:{CURRENCY_PATTERN})\s?(?P<pre>{_AMOUNT})(?P<pre_k>\s?k\b)?"
    rf"|(?P<post>{_AMOUNT})\s?(?:{CURRENCY_PATTERN})"
    rf"|\b(?P<bare_k>\d{{2,3}})\s?k\b",
    re.I,
)


def money_figures(text: str) -> set[int]:
    """Every plausible annual figure a piece of text names, comp words or not."""
    figures: set[int] = set()
    for match in _MONEY_ANYWHERE_RE.finditer(text or ""):
        raw = match.group("pre") or match.group("post") or match.group("bare_k")
        if not raw:
            continue
        digits = _GROUPING_RE.sub("", raw)
        if not digits.isdigit():
            continue
        value = int(digits)
        if match.group("pre_k") or match.group("bare_k"):
            value *= 1000
        if 20_000 <= value <= 2_000_000:
            figures.add(value)
    return figures


def _names_a_figure(draft: str, known_text: str) -> str | None:
    """The first money figure the draft states that its sources do not.

    The companion to :func:`_states_a_figure`, and the one that catches the
    sentence that check is blind to. Both run: this one is stricter about what
    a number has to look like, so it misses a bare "180000" that the comp-word
    test would catch, and it finds "$250,000" in a sentence about nothing in
    particular, which is the version a model volunteers unprompted.
    """
    known = money_figures(known_text)
    for figure in sorted(money_figures(draft)):
        if figure not in known:
            return f"{figure:,}"
    return None


def _states_a_figure(draft: str, thread_text: str) -> str | None:
    """Return the first salary figure the draft states that the thread didn't.

    Free models name a number unprompted — "I was targeting closer to $200k" —
    and that is the candidate's negotiating position invented for them, which is
    the one thing this feature must not do.

    Checked on every brief, not only the negotiation one. The negotiation brief
    is where the model is *most* tempted, but it is not the only place it gives
    in: an INTERESTED reply that volunteers a target salary sets the candidate's
    floor in the first friendly exchange, before they have decided what it is.
    Echoing a figure the recruiter already quoted stays allowed either way.
    """
    known = set(extract_salary_figures(thread_text))
    for figure in extract_salary_figures(draft):
        if figure not in known:
            return f"{figure:,}"
    return None


def draft_reply(
    cand: CandidateContext,
    messages: list[ThreadMessage],
    *,
    intent: ReplyIntent | str | None = None,
    band=None,
) -> ReplyDraft:
    """Draft a reply to the whole conversation. Never sends anything.

    Falls back to the template for the chosen brief whenever the model is
    unavailable, leaks its scratchpad, or asserts a specific the thread doesn't
    support. The fallback is a worse letter than a good generation and a much
    better one than a confident fabrication.
    """
    thread_text = " ".join(m.body for m in messages)
    negotiation, negotiation_note = detect_negotiation(thread_text, band)
    template = choose_template(intent, negotiation=negotiation, messages=messages)
    questions = open_questions(messages)

    note = _TEMPLATE_NOTE[template]
    if negotiation and negotiation_note:
        note = negotiation_note

    fallback = _TEMPLATE_FALLBACK[template].format(name=cand.name)
    draft = ReplyDraft(
        body=fallback,
        template=template,
        note=note,
        negotiation_detected=negotiation,
        open_questions=questions,
    )

    if not llm_is_configured():
        return draft

    try:
        raw = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_SYSTEM_PROMPT)},
                {
                    "role": "user",
                    "content": _build_prompt(
                        cand, messages, template, questions, negotiation_note, band
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.6,
            max_tokens=2000,
        )
    except OpenRouterError as exc:
        logger.info("reply draft fell back to template: %s", exc)
        return draft

    if looks_like_reasoning(raw):
        return draft

    # Before anything reads it, and before it is stored as the body that sends.
    # Nothing else stands between this string and a recruiter: the JSON call
    # sites are fence-proof because `extract_json_object` strips one on the way
    # past, and this one asks for prose. The prompt above says "Return ONLY the
    # reply body"; `recruiter_discovery` says "no markdown fence" and strips one
    # anyway, which is the evidence for doing it here too.
    text = strip_code_fence(raw)
    fabricated = (
        _invents_specifics(text, thread_text)
        or _states_a_figure(text, thread_text)
        or _names_a_figure(text, thread_text)
    )
    if fabricated:
        logger.warning(
            "reply draft rejected: model asserted %r, which the thread does not support",
            fabricated,
        )
        return draft

    draft.body = text
    draft.generated_with = "llm"
    draft.model = settings.openrouter_model
    return draft


def draft_for_thread(
    cand: CandidateContext,
    thread,
    *,
    intent=None,
    band=None,
    latest_inbound: str | None = None,
) -> ReplyDraft:
    """:func:`draft_reply` against a persisted thread row.

    ``latest_inbound`` is the message being replied to. It is passed separately
    because the caller has usually only ``db.add``-ed that row: a pending insert
    is not in ``thread.emails``, so drafting from the relationship alone would
    answer the *previous* message. Appended only when it isn't already the last
    recruiter turn, so a flushed session doesn't duplicate it.

    It goes through the same quote-stripping as the stored bodies, which is what
    keeps that duplicate check working: comparing a raw body against a stripped
    one never matches, and the message would be appended a second time.
    """
    messages = thread_messages(thread)
    body = visible_text(latest_inbound)
    if body and (not messages or messages[-1].body.strip() != body):
        messages.append(ThreadMessage(direction="recruiter", body=body))
    return draft_reply(cand, messages, intent=intent, band=band)


__all__ = [
    "MAX_THREAD_MESSAGES",
    "ReplyDraft",
    "ThreadMessage",
    "choose_template",
    "detect_negotiation",
    "draft_for_thread",
    "draft_reply",
    "extract_salary_figures",
    "latest_recruiter_message",
    "mentions_compensation",
    "money_figures",
    "open_questions",
    "thread_messages",
    "transcript",
]
