"""Answering an ATS's screening questions — Scout's judgement, on a short leash.

Every ATS puts questions between the candidate and the submit button, and they
fall into three very different kinds:

1. **Facts only the candidate can state.** Work authorization, sponsorship,
   salary expectations, notice period. These are legally significant and a wrong
   answer is a misrepresentation, so they come *verbatim* from the candidate's
   :class:`~app.models.form_apply.FormApplyProfile` and from nowhere else.
2. **Facts the resume already contains.** "How many years of Python?" — read
   off the parsed resume, deterministically, no model involved.
3. **Everything else.** "Why do you want to work here?", "Describe a system you
   scaled." Scout answers these, grounded in the resume, and is told to return
   ``null`` rather than invent anything. That instruction is load-bearing: the
   same no-invention rule as resume tailoring, applied to a text box that goes
   to a real employer.

Demographic/EEO questions are never *answered* — where the form offers a
"decline to self-identify" option that is selected, and otherwise the question
is left alone. They exist for the employer's reporting, not for a robot.

Anything none of the above resolves comes back with ``source="unanswered"``, and
the run stops in ``NEEDS_INPUT`` rather than submitting a form with a guess in
it. The whole module is pure — questions in, answers out — so it is tested
without a browser.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.core.config import settings
from app.models.form_apply import FormApplyProfile
from app.models.resume import Resume
from app.services import untrusted
from app.services.openrouter_client import OpenRouterError, chat_completion
from app.services.places import fold_diacritics

logger = logging.getLogger(__name__)

# Question kinds. `source` on an Answer says which of these produced it.
SOURCE_BANK = "bank"
SOURCE_RESUME = "resume"
SOURCE_LLM = "llm"
SOURCE_DECLINED = "declined"
SOURCE_UNANSWERED = "unanswered"


#: The apostrophes and quotes a rendered page uses where this module's tables
#: are written with the typewriter forms.
#:
#: Every string here arrives from somebody else's HTML by way of the browser
#: runner, and a form typeset by a CMS carries U+2019 rather than U+0027 —
#: "I don’t wish to answer", "Master’s Degree", "Years’ experience". Nothing
#: in this module could see those: the hint lists, the option comparison and
#: :data:`_YEARS_RE` are all written with the straight forms, so each one
#: silently stopped matching on the pages that use the curly ones.
#:
#: The two parsers this module's own comments name as its twins already fold
#: it — :data:`app.services.jd_parser._YEARS_REQUIRED_RE` and
#: :data:`app.services.resume_parser._YEARS_RE` both admit ``’`` inside the
#: experience phrase. This is the third reader of the same text, and it was the
#: one that did not.
_QUOTE_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‛": "'", "ʼ": "'", "´": "'",
    "“": '"', "”": '"', "‟": '"',
})


def fold_quotes(text: str) -> str:
    """Typographic apostrophes and quotes as their typewriter forms.

    Public because the comparison and the *storage* of an answer are different
    jobs: everything this module returns is the option string exactly as the
    form wrote it, so the fold may only ever touch the key a match is made on.
    """
    return (text or "").translate(_QUOTE_FOLD)


def match_key(text: str) -> str:
    """The form the whole module compares under: quote-folded and unaccented.

    The accent fold is the same argument as the quote fold, and it arrives
    through the same door. Two people spell these strings independently — the
    employer writing the option into their ATS, and the candidate typing the
    answer into their profile — and neither of them is the canonical one. A
    country select offering "Mexico" against a stored address of "México", a
    city list offering "Montréal" against a profile saying "Montreal",
    "Zürich" against "Zurich": each of those is one place written two ways,
    and a bare comparison called them different places.

    :func:`pick_option` returning ``None`` is a safe failure by construction —
    the question goes to :func:`blocking_questions` and the run stops for the
    candidate rather than submitting a guess — which is exactly why it went
    unnoticed. What the candidate sees is an application halted on the
    question of which city they live in, with the right answer sitting in the
    dropdown.

    Only ever the *key*. Every string this module hands back to an adapter is
    the form's own spelling, because that is the only string a select will
    accept.
    """
    return fold_diacritics(fold_quotes(text)).strip().lower()


@dataclass(frozen=True)
class ScreeningQuestion:
    """One question as read off the form."""

    text: str
    field_type: str = "text"  # text|textarea|number|select|radio|checkbox
    options: tuple[str, ...] = ()
    required: bool = True
    # Opaque handle back to the control, so the adapter can write the answer.
    key: str = ""

    @property
    def is_choice(self) -> bool:
        return bool(self.options) or self.field_type in ("select", "radio", "checkbox")

    @property
    def normalized(self) -> str:
        """The question text as the hint tables are written: folded and squashed.

        See :func:`fold_quotes`. A label reading "Do you now or in the future
        require sponsorship?" carries no apostrophe, but a candidate's own
        ``custom`` override is matched against this string and theirs may.
        """
        return re.sub(r"\s+", " ", match_key(self.text))


@dataclass
class Answer:
    """What to put in a control, and where the value came from."""

    question: ScreeningQuestion
    value: str | None
    source: str
    note: str | None = None

    @property
    def answered(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict:
        """The audit-trail shape stored on :class:`FormApplication.answers`."""
        return {
            "question": self.question.text,
            "answer": self.value,
            "source": self.source,
            "note": self.note,
        }


@dataclass
class AnswerBank:
    """The candidate's own answers, flattened off their profile and resume."""

    work_authorized: bool | None = None
    requires_sponsorship: bool | None = None
    willing_to_relocate: bool | None = None
    earliest_start: str | None = None
    notice_period_days: int | None = None
    desired_salary: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    website_url: str | None = None
    github_url: str | None = None
    address_city: str | None = None
    address_country: str | None = None
    years_experience: int | None = None
    custom: dict[str, str] = field(default_factory=dict)
    llm_enabled: bool = True

    @classmethod
    def from_models(
        cls, profile: FormApplyProfile | None, resume: Resume | None
    ) -> AnswerBank:
        links = list(getattr(resume, "links", None) or [])
        linkedin = next((u for u in links if "linkedin.com" in u.lower()), None)
        github = next((u for u in links if "github.com" in u.lower()), None)
        website = next(
            (u for u in links if "linkedin.com" not in u.lower() and "github.com" not in u.lower()),
            None,
        )
        if profile is None:
            return cls(
                phone=getattr(resume, "phone", None),
                linkedin_url=linkedin,
                github_url=github,
                website_url=website,
                years_experience=getattr(resume, "years_experience", None),
            )
        return cls(
            work_authorized=profile.work_authorized,
            requires_sponsorship=profile.requires_sponsorship,
            willing_to_relocate=profile.willing_to_relocate,
            earliest_start=profile.earliest_start,
            notice_period_days=profile.notice_period_days,
            desired_salary=profile.desired_salary,
            phone=profile.phone or getattr(resume, "phone", None),
            linkedin_url=profile.linkedin_url or linkedin,
            website_url=profile.website_url or website,
            github_url=profile.github_url or github,
            address_city=profile.address_city or getattr(resume, "location", None),
            address_country=profile.address_country,
            years_experience=getattr(resume, "years_experience", None),
            custom=dict(profile.custom_answers or {}),
            llm_enabled=profile.llm_answers_enabled,
        )


# --------------------------------------------------------------------------- #
# Question classification                                                      #
# --------------------------------------------------------------------------- #

# Demographic questions. Never answered from the bank or by a model — at most,
# the form's own "decline" option is selected.
_EEO_HINTS = (
    "gender",
    "race",
    "ethnicity",
    "hispanic",
    "latino",
    "disability",
    "veteran",
    "protected",
    "sexual orientation",
    "self-identif",
    "self identif",
    "pronoun",
)

_DECLINE_HINTS = (
    "decline",
    "prefer not",
    "do not wish",
    "don't wish",
    "not to answer",
    "not to disclose",
    "choose not",
)

_YES = ("yes", "y", "true")
_NO = ("no", "n", "false")

# When no option *is* "Yes" or "No", the answer is read off the option's leading
# word — and it used to be read off a `startswith`, which is not the same test.
# "Now or in the future, I will require sponsorship" starts with "no", so a
# candidate who needs no sponsorship was recorded on a real employer's form as
# needing it. "Not applicable" outranked "No, I am not" for the same reason, and
# "Nationwide" answered "are you willing to relocate?" with a no that reads as a
# yes.
#
# Two tiers, because the loose words are the ones that go wrong. A leading "no"
# or "yes" is the answer; a leading "not", "never" or "none" is *usually* the
# answer but must never outrank one, so "No, I am not" wins over "Not
# applicable" whichever order the select lists them in. Bare "y"/"n" are loose
# too: "N/A" leads with an "n" and is a decline, not a no.
#
# Nothing matches nothing. A select this can't map comes back unanswered and
# `blocking_questions` stops the run for the user, which is the entire point of
# returning None here.
_YES_LEAD = (("yes", "true", "yeah", "yep"), ("y",))
_NO_LEAD = (("no", "false", "nope"), ("n", "not", "never", "none"))

_LEAD_WORD_RE = re.compile(r"[a-z]+")


def _lead_word(option: str) -> str:
    """The first word of an option, past any numbering or tick character."""
    found = _LEAD_WORD_RE.search(option)
    return found.group(0) if found else ""

#: The two hint lists the compound rule below has to reason about, named
#: rather than written inline so a phrase can only ever be added to one place.
_SPONSORSHIP_HINTS = ("sponsorship", "sponsor", "visa support", "require a visa")
_AUTHORIZED_HINTS = (
    "legally authorized",
    "authorized to work",
    "authorised to work",
    "eligible to work",
    "right to work",
    "work authorization",
    "work authorisation",
)

# Bank rules, most specific first. Each maps hint substrings to a resolver that
# takes the bank and returns a raw value (bool | str | int | None).
_BANK_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    # Sponsorship must be tested before the broader "authorized to work" rule:
    # "will you now or in the future require sponsorship" contains neither
    # "authorized" nor "eligible", but plenty of forms phrase it both ways.
    #
    # Where a question carries *both* hints this ordering is not a tie-break,
    # it is a guess — and :func:`_authorization_reading` intercepts those
    # before the loop runs, because the guess was inverting the answer.
    (_SPONSORSHIP_HINTS, "requires_sponsorship"),
    (_AUTHORIZED_HINTS, "work_authorized"),
    (("relocate", "relocation"), "willing_to_relocate"),
    (
        ("notice period", "how much notice", "period of notice"),
        "notice_period_days",
    ),
    (
        ("start date", "when can you start", "earliest start", "available to start"),
        "earliest_start",
    ),
    # "Desired compensation" is the label Greenhouse ships by default, and it
    # was not on this list — nor was "compensation requirements", "target
    # salary" or "pay expectations". Only the spellings built around the word
    # "salary" were, plus two of the "compensation" ones.
    #
    # A miss here is not a blank. It falls through to the model tier, and the
    # guard that was supposed to stop a model answering about money — see
    # :data:`_LLM_FORBIDDEN` — had the same hole, so a *fabricated* figure was
    # written into a real employer's compensation field on the candidate's
    # behalf. The candidate's own number was sitting in the bank the whole
    # time.
    (
        (
            "salary expectation",
            "expected salary",
            "desired salary",
            "target salary",
            "salary requirement",
            "compensation expectation",
            "expected compensation",
            "desired compensation",
            "target compensation",
            "compensation requirement",
            "pay expectation",
            "expected pay",
            "desired pay",
            "remuneration expectation",
            "expected remuneration",
        ),
        "desired_salary",
    ),
    (("linkedin",), "linkedin_url"),
    (("github",), "github_url"),
    (("portfolio", "personal website", "website", "personal site"), "website_url"),
    (("phone", "mobile number", "telephone"), "phone"),
)

# "How many years of X" — answered off the resume rather than a model.
# A years-of-experience question, however the board words its label.
#
# The middle alternative used to be the two literals "years of experience" and
# "years' experience", so a label that put anything at all between them missed:
# "Years of relevant work experience", "Years of professional software
# engineering experience" and "Total years of industry experience" are the
# shapes Greenhouse and Lever ship, and all three read as questions this module
# knows nothing about.
#
# The cost is paid at the end of the chain rather than here.
# :func:`answer_from_resume` returns None, the question falls through to the
# LLM tier, and when that is off or down — which
# :func:`app.services.llm_router.llm_is_configured` makes an ordinary state,
# not an outage — it comes back ``unanswered``. A *required* one then lands in
# :func:`blocking_questions` and stops the run in NEEDS_INPUT, over a number
# the resume states outright.
#
# Same allowance and same shape as
# :data:`app.services.jd_parser._YEARS_REQUIRED_RE`, which learned this on the
# posting side: up to four words may sit in the gap, each starting with a
# letter, each separated by spaces on the same line, so a label cannot run
# across a line break into the next question.
#
# The last alternative is the same label with the two words the other way
# round, which is the only way half the ATSs on the market print it:
# "Experience (years)", "Experience (in years)", "Total experience in years".
# Every branch above reads left to right — "years" first, then "experience" —
# so a label that leads with "experience" matched nothing at all, and the
# question came back ``unanswered`` over a number the resume states outright.
#
# It is the identical omission :data:`app.services.jd_parser._YEARS_LABEL_RE`
# was added for on the posting side, and it costs more here. There an absent
# floor scores the dimension neutral; here a *required* one lands in
# :func:`blocking_questions` and stops the run in NEEDS_INPUT, so the
# application is not submitted at all.
#
# Narrower than the forward branch on purpose. That one allows any four words
# in the gap, which is safe when "years" has already been seen; leading with
# "experience" it is not — "please list your experience over the years" would
# be read as a request for a number and answered with one, on a real
# employer's form. So the gap admits a bracket, a separator and the single
# word "in", and nothing else.
_YEARS_RE = re.compile(
    r"how many years"
    r"|years[ \t’'`]*(?:of[ \t]+)?(?:[A-Za-z][\w\-/&+]*[ \t]+){0,4}?experience"
    r"|year[s]? do you have"
    r"|experience[ \t]*[(\[:/|–—-]*[ \t]*(?:in[ \t]+)?years?(?![a-z])",
    re.I,
)


# Every ATS ships one question that names work authorization and sponsorship in
# the same breath, and there are three of them wearing the same clothes:
#
#   a. "Are you legally authorized to work in the US **without sponsorship**?"
#   b. "Do you **require** visa sponsorship to be authorized to work here?"
#   c. "Are you authorized to work in the US? (We do not sponsor visas.)"
#
# All three contain a sponsorship hint, so :data:`_BANK_RULES` answered all
# three off ``requires_sponsorship`` — and (a) and (c) ask the opposite
# question, so the value written was the *negation* of the truth. A candidate
# who is authorized and needs no sponsorship told a real employer, on a
# legally significant field, that they are not authorized to work; a candidate
# who does need sponsorship told them they are. The wrong answer here is worse
# than any blank this module produces, because it contradicts the resume
# attached to the same application and nobody sees it before it is submitted.
#
# So a both-hints question is read rather than assumed:
#
# * a negation of the requirement ("without sponsorship", "without the need
#   for sponsorship", "and do not require sponsorship") makes it a *compound*
#   question — true only when the candidate is authorized **and** needs no
#   sponsorship;
# * an actual requirement verb attached to sponsorship ("require sponsorship",
#   "sponsorship is needed") makes it the sponsorship question, which is what
#   the rule ordering was written for;
# * anything else — sponsorship named without either, i.e. the employer's
#   parenthetical note — leaves it the authorization question it asks.
#
# A question carrying only one of the two hints never reaches here and the
# rules answer it exactly as before.
_NO_SPONSORSHIP_RE = re.compile(
    r"without\s+(?:the\s+)?"
    r"(?:need\s+(?:for|of)\s+|requiring\s+|requirement\s+(?:for|of)\s+)?"
    r"(?:any\s+|a\s+|an\s+|current\s+|future\s+|further\s+|ongoing\s+"
    r"|employer\s+|company\s+|work\s+|employment\s+|visa\s+|immigration\s+)*"
    r"sponsor"
    r"|(?:do(?:es)?\s+not|do\s?n't|does\s?n't|will\s+not|wo\s?n't|never)\s+"
    r"(?:\w+\s+){0,3}?requir\w*\s+(?:\w+\s+){0,2}?sponsor"
    r"|no\s+(?:\w+\s+){0,2}?sponsorship\s+(?:is\s+)?(?:required|needed|necessary)",
    re.I,
)

_SPONSORSHIP_REQUIRED_RE = re.compile(
    r"(?:requir\w*|need\w*|seek\w*|request\w*)\s+(?:\w+\s+){0,3}?sponsor"
    r"|sponsorship\s+(?:\w+\s+){0,3}?(?:required|needed|necessary)",
    re.I,
)

#: What :func:`_authorization_reading` decided a both-hints question is asking.
_READ_COMPOUND = "compound"
_READ_SPONSORSHIP = "sponsorship"
_READ_AUTHORIZATION = "authorization"


def _authorization_reading(question: ScreeningQuestion) -> str | None:
    """Which question a work-authorization/sponsorship label is really asking.

    ``None`` when the label names only one of the two, which is every question
    :data:`_BANK_RULES` was already right about.
    """
    haystack = question.normalized
    if not any(hint in haystack for hint in _AUTHORIZED_HINTS):
        return None
    if not any(hint in haystack for hint in _SPONSORSHIP_HINTS):
        return None
    # Negation first: "without requiring sponsorship" satisfies both patterns
    # and only one of them reads it correctly.
    if _NO_SPONSORSHIP_RE.search(haystack):
        return _READ_COMPOUND
    if _SPONSORSHIP_REQUIRED_RE.search(haystack):
        return _READ_SPONSORSHIP
    return _READ_AUTHORIZATION


def _authorized_without_sponsorship(bank: AnswerBank) -> bool | None:
    """The compound answer, or ``None`` when the profile does not settle it.

    Either half can settle it alone on the way to "no" — an unauthorized
    candidate is not authorized without sponsorship whatever their sponsorship
    field says. "Yes" needs both, because it is the claim an employer relies
    on. Anything short of that is left for the candidate: the question is
    required on every form that asks it, so ``None`` stops the run in
    NEEDS_INPUT rather than submitting a half-known answer.
    """
    if bank.work_authorized is False or bank.requires_sponsorship is True:
        return False
    if bank.work_authorized is True and bank.requires_sponsorship is False:
        return True
    return None


def _answer_authorization(
    question: ScreeningQuestion, bank: AnswerBank, reading: str
) -> Answer | None:
    if reading == _READ_SPONSORSHIP:
        raw: object | None = bank.requires_sponsorship
        note = None
    elif reading == _READ_AUTHORIZATION:
        raw = bank.work_authorized
        note = None
    else:
        raw = _authorized_without_sponsorship(bank)
        note = (
            "authorized to work and needs no sponsorship"
            if raw is True
            else "not authorized without sponsorship"
        )
    formatted = _format(raw, question)
    if formatted is None:
        return None
    return Answer(question, formatted, SOURCE_BANK, note=note)


def is_eeo(question: ScreeningQuestion) -> bool:
    """True for demographic questions, which are never answered substantively."""
    return any(hint in question.normalized for hint in _EEO_HINTS)


def pick_option(options: tuple[str, ...] | list[str], wanted: object) -> str | None:
    """The option that expresses *wanted*, or ``None`` if none of them does.

    Booleans match yes/no options; a string matches case-insensitively, then by
    substring. Returning ``None`` (rather than the first option) is the point:
    a select we can't map is a question we haven't answered.

    Blank options are dropped before any of that. ``<option value=""></option>``
    heads most ATS selects, and the substring pass matched it against
    everything — ``"" in text`` is true for every text there is. So the first
    answer that mapped to nothing selected the placeholder and came back
    ``answered``, which took the question out of :func:`blocking_questions` and
    let the run submit a form with a required select untouched. That is the one
    outcome this function exists to prevent.
    """
    if not options:
        return None
    # Folded on the comparison side only: `original` is what goes back to the
    # adapter, and the form's own spelling is the only string a select will
    # accept. See :func:`match_key`.
    normalized = [
        (opt, lowered)
        for opt, lowered in ((o, match_key(o)) for o in options)
        if lowered
    ]
    if not normalized:
        return None

    if isinstance(wanted, bool):
        targets = _YES if wanted else _NO
        for original, lowered in normalized:
            if lowered in targets:
                return original
        for tier in _YES_LEAD if wanted else _NO_LEAD:
            for original, lowered in normalized:
                if _lead_word(lowered) in tier:
                    return original
        return None

    text = match_key(str(wanted))
    if not text:
        return None
    for original, lowered in normalized:
        if lowered == text:
            return original
    numeric = text.isdigit()
    for original, lowered in normalized:
        if numeric:
            # A number is not a substring-matchable token, and the banded years
            # dropdown every ATS ships is where that bites. "1" is inside
            # "6-10 years", so a candidate with one year of experience selected
            # six-to-ten on a real employer's form — an overstatement of up to
            # nine years, made on their behalf, because of the "1" in "10".
            #
            # `_band_for` is the function that actually reads these labels, and
            # it is already the fallback when this returns None. So the numeric
            # pass here is narrowed to the only shape it can be trusted on: an
            # option naming exactly this number and saying nothing about a range
            # around it. "8 years" still answers 8; "0-2 years", "10+ years" and
            # "Under 2 years" are all handed to the function that understands
            # what they mean.
            if _states_only(lowered, text):
                return original
            continue
        if _holds_phrase(lowered, text) or _holds_phrase(text, lowered):
            return original
    return None


def _holds_phrase(haystack: str, needle: str) -> bool:
    """Whether *needle* sits in *haystack* as whole words rather than anywhere.

    A bare ``in`` is what this used to be, and on a select of place names it is
    a coin flip. Two-letter answers are the ones that hurt, because every ATS
    ships a state dropdown and the answer to it is often a code: "MA" is inside
    "AlabaMa", "OR" is inside "CalifORnia", "IA" is inside "CalifornIA" and
    "IN" is inside "IllINois". Each of those was selected on a real employer's
    form as the candidate's home state, and — this is the part that makes it
    worse than a blank — it came back *answered*, so it never reached
    :func:`blocking_questions` and the run submitted without anyone seeing it.
    "US" against a country list picked Australia the same way.

    Requiring word alignment keeps what the loose test was for — a bank value
    of "Master's Degree in Computer Science" still finds the "Master's Degree"
    option — and gives back ``None`` for the rest, which is this module's
    answer to a select it cannot map: leave it to the candidate.
    """
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


_DIGIT_RUN_RE = re.compile(r"\d+")


def _states_only(option: str, number: str) -> bool:
    """Whether *option* names *number* and no range or comparison around it."""
    found = _DIGIT_RUN_RE.findall(option)
    if len(found) != 1 or int(found[0]) != int(number):
        return False
    return _open_floor(option) is None and _open_ceiling(option) is None


def decline_option(options: tuple[str, ...] | list[str]) -> str | None:
    """The form's own "prefer not to say" option, when it offers one."""
    for option in options:
        lowered = match_key(option)
        if any(hint in lowered for hint in _DECLINE_HINTS):
            return option
    return None


def _format(value: object, question: ScreeningQuestion) -> str | None:
    """Render a bank value for the control it is going into."""
    if value is None:
        return None
    if isinstance(value, bool):
        if question.is_choice:
            return pick_option(question.options, value)
        return "Yes" if value else "No"
    text = str(value) if isinstance(value, int) else str(value).strip()
    if not text:
        return None
    if question.is_choice:
        return pick_option(question.options, text)
    return text


def answer_from_bank(question: ScreeningQuestion, bank: AnswerBank) -> Answer | None:
    """The candidate's own stated answer, if this question is one of those."""
    haystack = question.normalized
    if not haystack:
        return None

    # A user-written override wins over every rule below — it exists precisely
    # for the question our rules got wrong last time.
    #
    # Longest needle first, and folded on both sides. Neither was true, and
    # each cost the override the one job it has.
    #
    # *Folded*, because `haystack` is :attr:`ScreeningQuestion.normalized` —
    # quote-folded, as everything this module compares against is — and the
    # needle was not. The needle is the side a *person typed*, on a phone or in
    # Word, where "Master's" is autocorrected to U+2019 before it is ever
    # saved. So the override the candidate wrote specifically to answer a
    # question was the one string in the comparison that could not match it,
    # and the question came back unanswered and stopped the run.
    #
    # *Longest first*, because a dict has an insertion order and specificity is
    # not it. A candidate with a general "salary" → "Negotiable" and a
    # role-specific "salary expectations for this role" → "$210,000" got
    # whichever they happened to save first. The needle that matches more of
    # the question is the more specific instruction, and the general one is the
    # fallback it was written as.
    for needle, value in sorted(
        (bank.custom or {}).items(), key=lambda kv: (-len(kv[0] or ""), kv[0] or "")
    ):
        folded = match_key(needle or "")
        if folded and folded in haystack:
            formatted = _format(value, question)
            if formatted is not None:
                return Answer(question, formatted, SOURCE_BANK, note="custom answer")

    if is_eeo(question):
        chosen = decline_option(question.options)
        if chosen is not None:
            return Answer(question, chosen, SOURCE_DECLINED, note="EEO question")
        # No decline option and nothing to say: leave it to the candidate.
        return Answer(question, None, SOURCE_DECLINED, note="EEO question, left blank")

    # Before the rules, not inside them: a label naming both work authorization
    # and sponsorship has to be read, and the first rule that matches it is the
    # wrong one two times in three. See :func:`_authorization_reading`.
    reading = _authorization_reading(question)
    if reading is not None:
        return _answer_authorization(question, bank, reading)

    for hints, attr in _BANK_RULES:
        if any(hint in haystack for hint in hints):
            raw = getattr(bank, attr, None)
            if attr == "notice_period_days" and isinstance(raw, int):
                raw = f"{raw} days"
            formatted = _format(raw, question)
            return (
                Answer(question, formatted, SOURCE_BANK)
                if formatted is not None
                else None
            )
    return None


def answer_from_resume(
    question: ScreeningQuestion, resume: Resume | None, bank: AnswerBank
) -> Answer | None:
    """Years-of-experience questions, read straight off the parsed resume.

    Capped at the resume's total: a candidate with 8 years of experience has not
    had 8 years of a framework that shipped last year, but claiming *more* than
    their total is the failure mode worth ruling out here.
    """
    if not _YEARS_RE.search(question.text or ""):
        return None
    years = bank.years_experience or getattr(resume, "years_experience", None)
    if not years:
        return None

    # A named skill doesn't get its own number — we don't have per-skill years,
    # and the resume's total is the only figure we can honestly stand behind.
    skill = _skill_in(question.text, resume)
    formatted = _format(years, question)
    if formatted is None:
        # A select of ranges ("3-5 years"): find the band it falls in.
        formatted = _band_for(question.options, years)
    if formatted is None:
        return None
    return Answer(
        question,
        formatted,
        SOURCE_RESUME,
        note=f"{years} years on the resume" + (f" (asked about {skill})" if skill else ""),
    )


def _skill_in(text: str, resume: Resume | None) -> str | None:
    """The resume skill a years-of-experience question is asking about.

    Named as a word, not found as a substring. A one-letter skill — "R", "C"
    — is a substring of nearly every question ever written ("years of
    expe-r-ience"), and the answer's note is the sentence that tells the user
    why the box was filled. Longest match first so a question about
    "JavaScript" is not credited to the "Java" listed next to it.
    """
    lowered = (text or "").lower()
    skills = [s for s in (getattr(resume, "skills", None) or []) if s and s.strip()]
    for skill in sorted(skills, key=lambda s: (-len(s), s)):
        needle = re.escape(skill.strip().lower())
        if re.search(rf"(?<![a-z0-9]){needle}(?![a-z0-9])", lowered):
            return skill
    return None


# A banded option, in the shapes ATS forms actually print them. The dash class
# includes the em dash — some forms use it, and "0—2 years" matched nothing
# without it. "Between 3 and 5 years" is the other common spelling, and "and"
# is only a separator behind "between": on its own it would invent a range out
# of any label that happened to name two numbers.
_BAND_RE = re.compile(
    r"between\s+(\d+)\s*(?:years?|yrs?)?\s*and\s+(\d+)"
    r"|(\d+)\s*(?:[-–—~]|to|through)\s*(\d+)"
)


def _band_bounds(text: str) -> tuple[int, int] | None:
    """The low and high ends of a banded option, or ``None`` if it isn't one."""
    match = _BAND_RE.search(text)
    if not match:
        return None
    numbers = [int(g) for g in match.groups() if g is not None]
    if len(numbers) != 2 or numbers[0] > numbers[1]:
        return None
    return numbers[0], numbers[1]

# The top option of a years dropdown is open-ended, and ATSs write it every way
# English allows. Only "10+" was recognised, so "More than 10 years", "10 or
# more years" and "At least 7 years" answered nothing: `_band_for` returned
# None, the question came back ``unanswered``, and the run stopped in
# NEEDS_INPUT over a number the resume states.
#
# Two patterns because the two readings differ by one. "10+" and "at least 10"
# admit a candidate with exactly ten years; "more than 10" does not. The
# distinction is worth keeping — a range option ("6-10 years") is always
# preferred over an open one, but where the form offers no range, answering
# "more than 10" with exactly ten years would be a claim the resume does not
# support.
#
# **Negation is why both open-ended readings need a guard.** "No more than 5
# years" is a *ceiling*, and "more than 5" sits inside it; "no less than 3
# years" is a *floor*, and "less than 3" sits inside that. Each phrase matched
# the opposite reading's pattern, and because :func:`_band_for` consults the
# floor first and moves on when it finds one, the wrong reading won outright: a
# candidate with twenty years on their resume answered "No more than 5 years" on
# a real employer's form. The literal was already listed as a ceiling phrasing
# three lines down — it was simply never reached.
#
# The same shape as every other bare-containment bug in this codebase, in a
# module where the cost is a false statement under the candidate's name rather
# than a missed match. A phrase this cannot read stays unanswered, which is the
# failure this file is built around.
_NOT_NEGATED = r"(?<!\bno\s)(?<!\bnot\s)"

_OPEN_INCLUSIVE = re.compile(
    r"(\d+)\s*\+"
    r"|(?:at least|minimum(?:\s+of)?|no(?:t)? less than)\s+(\d+)"
    r"|(\d+)\s*(?:years?\s*)?or\s+(?:more|above|greater|over)"
)
_OPEN_EXCLUSIVE = re.compile(
    _NOT_NEGATED + r"(?:more than|over|greater than|above)\s+(\d+)"
)

# The *bottom* option of a years dropdown is open-ended too, and the fix above
# only ever looked upwards. A form offering "Less than 2 years / 2-4 years /
# 5+ years" answered nothing for a candidate with one year on their resume:
# no range contains 1, no floor admits it, `_band_for` returned None, and the
# run stopped in NEEDS_INPUT over a number the resume states — the identical
# failure, at the other end of the same dropdown.
#
# Split the same way and for the same reason: "under 2 years" excludes a
# candidate with exactly two, "2 years or less" includes them, and answering
# either one wrongly is a claim the resume does not support.
_CEIL_EXCLUSIVE = re.compile(
    _NOT_NEGATED + r"(?:less than|fewer than|under|below)\s+(\d+)"
)
_CEIL_INCLUSIVE = re.compile(
    r"(?:up to|at most|maximum(?:\s+of)?|no(?:t)? more than)\s+(\d+)"
    r"|(\d+)\s*(?:years?\s*)?or\s+(?:less|fewer|under|below)"
)


def _open_floor(text: str) -> int | None:
    """The fewest years an open-ended option admits, or ``None`` if it isn't one."""
    match = _OPEN_INCLUSIVE.search(text)
    if match:
        return int(next(group for group in match.groups() if group))
    match = _OPEN_EXCLUSIVE.search(text)
    if match:
        return int(match.group(1)) + 1
    return None


def _open_ceiling(text: str) -> int | None:
    """The most years an open-ended option admits, or ``None`` if it isn't one."""
    match = _CEIL_INCLUSIVE.search(text)
    if match:
        return int(next(group for group in match.groups() if group))
    match = _CEIL_EXCLUSIVE.search(text)
    if match:
        return int(match.group(1)) - 1
    return None


def _band_for(options: tuple[str, ...] | list[str], years: int) -> str | None:
    """The "3-5 years" style option that contains *years*.

    A range always wins: it is the narrowest true statement available. Failing
    that, the *highest* open-ended option the candidate clears — not the last
    one in the list, which is what this used to take. Dropdowns usually run
    low-to-high and the two agree; where one runs the other way, a candidate
    with twenty years picked "5+" over "10+" and understated themselves on a
    form going to an employer, on nothing but the order the options happened to
    be written in.

    An open-ended *bottom* option ("Less than 2 years") is the last resort, and
    only a last resort: where the form offers both, a floor is the stronger
    claim and never understates the candidate. Among several ceilings the
    lowest one wins — the narrowest true statement again.
    """
    floor: int | None = None
    fallback: str | None = None
    ceiling: int | None = None
    under: str | None = None
    for option in options:
        text = (option or "").lower()
        band = _band_bounds(text)
        if band and band[0] <= years <= band[1]:
            return option
        edge = _open_floor(text)
        if edge is not None and years >= edge and (floor is None or edge > floor):
            floor, fallback = edge, option
            continue
        top = _open_ceiling(text)
        if top is not None and years <= top and (ceiling is None or top < ceiling):
            ceiling, under = top, option
    return fallback or under


# --------------------------------------------------------------------------- #
# The LLM tier                                                                 #
# --------------------------------------------------------------------------- #

_LLM_SYSTEM = (
    "You are {agent}, the job-application digital robot inside DoAide AutoApply. You are "
    "filling in a real employer's application form on behalf of a real "
    "candidate.\n"
    "Absolute rules:\n"
    "1. Answer ONLY from the candidate's resume below. Never invent an "
    "employer, a skill, a qualification, a date or a number that is not there.\n"
    "2. If the resume does not support an answer, return null for that "
    "question. A missing answer is always better than a fabricated one.\n"
    "3. Never answer questions about salary, compensation, pay, visa status, "
    "work authorization, sponsorship, or demographics — return null for "
    "those; the candidate answers them personally.\n"
    "4. When a question lists options, your answer must be one of them, "
    "copied exactly.\n"
    "5. Keep free-text answers under 90 words, first person, plain and "
    "specific.\n"
    'Return JSON only: {{"answers": [{{"index": 0, "answer": "..."}}]}} — use '
    "null for the answer when you cannot ground it in the resume."
)

# Every phrase :data:`_BANK_RULES` routes, so a question the bank claims can
# never be answered by anything else.
#
# This is the load-bearing half of the guard below, and it is derived rather
# than transcribed because a hand-written copy is what kept going wrong. The
# guard was written out beside the rules, and drifted from them twice: once on
# the compensation spellings, and once — still open until now — on work
# authorization. "Work Authorization" is the label Greenhouse and Lever both
# ship, `_BANK_RULES` lists it, and the guard did not; nor did it list
# "eligible to work", "work authorisation", the notice-period phrasings, the
# start-date phrasings, or any of the contact fields.
#
# What that cost is not a blank. A bank rule returns ``None`` — falling the
# question through to the model tier — in two ordinary situations: the
# candidate left that field of their profile empty, and the form used a select
# whose options :func:`pick_option` could not map ("Authorized to work for any
# employer" / "Requires sponsorship now or in the future"). Both are exactly
# the moment the docstring's rule 1 applies: a fact only the candidate can
# state, where they have not stated it.
#
# And the model is in no position to fill the gap even in principle.
# :func:`answer_with_llm` sends it the role, the resume and the questions —
# never the bank. It has not been told the candidate's work authorization, so
# an answer to "Work Authorization" is not a grounded answer at all; it is a
# guess read off which countries the resume's employers are in, written into a
# legally significant field on a real employer's form, under the candidate's
# name.
_BANK_HINTS: tuple[str, ...] = tuple(
    sorted({hint for hints, _ in _BANK_RULES for hint in hints})
)

# Questions the model is forbidden from answering even if it is asked to. Belt
# and braces around rule 3 above: a model that ignores the instruction still
# cannot get a fabricated visa answer past this filter.
#
# The literals are the ones no bank rule needs. A rule only has to be specific
# enough to pick the right profile field, so it asks for "salary expectation";
# the guard has to catch the question however it is put, so it asks for
# "salary" — and "compensation", "remuneration", "sponsor" and "visa" are
# there for the same reason.
#
# Bare is the right width. Over-blocking costs one question the candidate
# answers themselves — the outcome this module treats as correct whenever it
# cannot ground an answer — and under-blocking costs a fact they never stated,
# sent to an employer, in a field the employer will rely on.
_LLM_FORBIDDEN = (
    *_BANK_HINTS,
    "sponsor",
    "visa",
    "salary",
    "compensation",
    "remuneration",
    *_EEO_HINTS,
)


def _resume_brief(resume: Resume | None, limit: int = 2500) -> str:
    if resume is None:
        return "(no resume on file)"
    experience = "; ".join(
        f"{row.get('title', '')} at {row.get('company', '')} "
        f"({row.get('start', '')}–{row.get('end', '')})".strip()
        for row in (resume.experience or [])[:6]
    )
    parts = [
        f"Name: {resume.full_name or 'unknown'}",
        f"Headline: {resume.headline or 'unknown'}",
        f"Seniority: {resume.seniority or 'unknown'}",
        f"Years of experience: {resume.years_experience or 'unknown'}",
        f"Location: {resume.location or 'unknown'}",
        f"Skills: {', '.join((resume.skills or [])[:40]) or 'unknown'}",
        f"Experience: {experience or 'unknown'}",
        f"Summary: {(resume.summary or '')[:600]}",
    ]
    return "\n".join(parts)[:limit]


def _question_payload(questions: list[ScreeningQuestion]) -> str:
    return json.dumps(
        [
            {
                "index": index,
                "question": question.text,
                "type": question.field_type,
                "options": list(question.options),
            }
            for index, question in enumerate(questions)
        ],
        ensure_ascii=False,
    )


def answer_with_llm(
    questions: list[ScreeningQuestion],
    resume: Resume | None,
    *,
    job_title: str | None = None,
    company: str | None = None,
    completion=chat_completion,
) -> dict[int, str]:
    """Ask Scout for the leftover questions, in one batched call.

    One call for the whole form rather than one per question: an application can
    carry a dozen questions, and the free tier this product runs on would not
    survive that. Returns a sparse ``{index: answer}`` — anything the model
    declined, fabricated-looking, or was forbidden from answering is simply
    absent, and the caller treats it as unanswered.
    """
    if not questions:
        return {}

    system = untrusted.guarded(_LLM_SYSTEM.format(agent=settings.agent_name))
    # Two of these three blocks came off a page this product did not write: the
    # role line is a scraped job title, and the questions are the employer's own
    # form text. The resume between them is the candidate's record, and it is
    # deliberately *not* fenced — it is the only material the model is allowed
    # to ground an answer in, and telling it to disregard directions in there
    # would be telling it to disregard the answer.
    user = "\n\n".join(
        [
            "ROLE",
            untrusted.fence(
                f"{job_title or 'unknown'} at {company or 'unknown'}",
                label="job posting",
            ),
            "CANDIDATE RESUME",
            _resume_brief(resume),
            "QUESTIONS",
            untrusted.fence(_question_payload(questions), label="form questions"),
        ]
    )

    try:
        raw = completion(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=900,
        )
    except OpenRouterError as exc:
        logger.info("screening answers unavailable (no LLM): %s", exc)
        return {}

    return _parse_llm_answers(raw, questions)


def _parse_llm_answers(
    raw: str, questions: list[ScreeningQuestion]
) -> dict[int, str]:
    """Validate the model's reply against the questions it was asked."""
    from app.services.openrouter_client import extract_json_object

    payload = extract_json_object(raw or "")
    if not payload:
        return {}
    rows = payload.get("answers")
    if not isinstance(rows, list):
        return {}

    out: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(questions):
            continue

        answer = row.get("answer")
        if answer is None or not str(answer).strip():
            continue
        text = str(answer).strip()
        question = questions[index]

        if any(hint in question.normalized for hint in _LLM_FORBIDDEN):
            # The model answered something it was told to leave alone. Drop it.
            logger.debug("dropped model answer to a restricted question")
            continue
        if question.options:
            matched = pick_option(question.options, text)
            if matched is None:
                continue
            text = matched
        out[index] = text
    return out


# --------------------------------------------------------------------------- #
# The public entry point                                                       #
# --------------------------------------------------------------------------- #


def answer_questions(
    questions: list[ScreeningQuestion],
    *,
    bank: AnswerBank,
    resume: Resume | None = None,
    job_title: str | None = None,
    company: str | None = None,
    completion=chat_completion,
) -> list[Answer]:
    """Answer a whole form's screening questions, cheapest tier first.

    Bank → resume → Scout, and anything left over comes back with
    ``source="unanswered"`` so the caller can stop rather than submit a guess.
    """
    answers: list[Answer | None] = [None] * len(questions)
    leftovers: list[int] = []

    for index, question in enumerate(questions):
        resolved = answer_from_bank(question, bank) or answer_from_resume(
            question, resume, bank
        )
        if resolved is not None:
            answers[index] = resolved
        else:
            leftovers.append(index)

    if leftovers and bank.llm_enabled:
        asked = [questions[i] for i in leftovers]
        llm = answer_with_llm(
            asked,
            resume,
            job_title=job_title,
            company=company,
            completion=completion,
        )
        for position, index in enumerate(leftovers):
            value = llm.get(position)
            if value:
                answers[index] = Answer(questions[index], value, SOURCE_LLM)

    return [
        answer
        if answer is not None
        else Answer(questions[index], None, SOURCE_UNANSWERED)
        for index, answer in enumerate(answers)
    ]


def blocking_questions(answers: list[Answer]) -> list[str]:
    """The required questions that came back unanswered — why a run stops."""
    return [
        answer.question.text
        for answer in answers
        if answer.question.required and not answer.answered
    ]


__all__ = [
    "Answer",
    "AnswerBank",
    "ScreeningQuestion",
    "answer_from_bank",
    "answer_from_resume",
    "answer_questions",
    "answer_with_llm",
    "blocking_questions",
    "decline_option",
    "fold_quotes",
    "match_key",
    "is_eeo",
    "pick_option",
]
