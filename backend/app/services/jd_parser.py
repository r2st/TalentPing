"""Job-description parsing — a URL or a wall of text into structured requirements.

Both the tailoring engine and the fit scorer need the same view of a posting:
what skills it asks for, how senior it is, where it is, what it pays. This module
is the single place that derives it.

Same two-layer shape as the resume parser, for the same reason:

1. **Heuristics** (:func:`parse_heuristic`) — regex and taxonomy matching over
   the text. Deterministic, offline, and the only path exercised in tests.
2. **LLM enrichment** (:func:`enrich_with_llm`) — an OpenRouter free model fills
   in title/company/responsibilities and sharpens the required-skill list. Any
   failure degrades silently to layer 1.

Fetching a URL is best-effort: a posting behind a login or heavy JS gives us
nothing, and the caller is expected to fall back to pasted text.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import requests
from bs4 import BeautifulSoup

from app.core import outbound
from app.core.config import settings
from app.services import untrusted
from app.services.currency import (
    APOSTROPHE_GROUPED,
    CURRENCY_PATTERN,
    GROUPING_CHARS,
    INDIAN_GROUPED,
    SPACE_GROUPED,
    usd_rate,
)
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
    llm_is_configured,
)
from app.services.pay_period import (
    PERIOD_MULTIPLIERS,
    period_near,
    period_span_after,
)
from app.services.resume_parser import extract_skills, marker_pattern

logger = logging.getLogger(__name__)

# Requirement lines usually start with one of these, or a bullet glyph.
_BULLET_RE = re.compile(r"^\s*(?:[-*•·‣▪◦]|\d+[.)])\s+(.{4,300})$", re.M)

# "5+ years", "at least 3 years", "3-5 years of experience", and the far more
# common shape the fixed qualifier list used to miss: "6+ years of professional
# software engineering experience". Up to four words may sit between "years of"
# and "experience", but each must start with a letter and be separated by spaces
# on the same line — a full stop or a newline ends the phrase, so "5 years.\nWe
# value experience" is not a five-year requirement.
#
# :data:`_YEARS_RANGE_SEP` is the whole reason a range is written out here. The
# separator used to be a bare ASCII hyphen, so "3-5 years of experience" read
# as three and every other way of writing the same range read as *five*: a
# posting pasted out of a word processor carries an en dash, and "3 to 5 years"
# and "between 3 and 5 years" are how a person writes it by hand. In each of
# those the match starting at "3" failed, the engine moved on, and the one
# starting at "5" succeeded — so the floor came out at the *top* of the band.
#
# That is the dangerous direction. `fit_scorer.score_experience` prints "3 yrs
# vs 5 required (short by 2)" and scores the dimension down for a candidate the
# posting explicitly invites, and `infer_seniority` falls back to the same
# number, so a "5-8 years" role written with an en dash was filed as senior
# rather than mid. Nothing about the posting looks wrong.
#
# Letting the separator win also *prevents* the double reading rather than
# merely correcting it: one match now spans the whole range, so `finditer`
# never offers the top of the band as a second, larger count for
# `extract_years_required`'s max() to prefer.
_YEARS_RANGE_SEP = r"(?:[-–—]|to\b|and\b)"
_YEARS_REQUIRED_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*"
    rf"(?:{_YEARS_RANGE_SEP}[ \t]*\d{{1,2}}[ \t]*)?(?:years?|yrs?)"
    r"[ \t’'`]*(?:of[ \t]+)?(?:[A-Za-z][\w\-/&+]*[ \t]+){0,4}?"
    r"(?:experience|exp\b)",
    re.I,
)

# The same requirement written as a *field* rather than as a sentence, which is
# how every structured board renders one: "Experience: 2-4 years", "Years of
# experience: 5+", "Experience Required: 5-7 years", "Minimum Experience: 3
# years", "Exp: 4 yrs".
#
# The pattern above reads left to right — a number, then "years", then
# "experience" — so a label that puts "experience" *first* matched nothing at
# all, and the posting set no floor. That is the quiet direction (an absent
# floor scores the dimension neutral rather than wrongly), which is why it went
# unnoticed on the boards that write every posting this way.
#
# The number needs a "+" or a "years" word behind it, so "Experience: 3" — which
# could be three anything — is not read as three years.
_YEARS_LABEL_RE = re.compile(
    r"\b(?:experience|exp)\b[^\n:]{0,20}:[ \t]*(?:between[ \t]+)?"
    r"(\d{1,2})\s*"
    r"(?:(?:\+|plus)\s*(?:years?|yrs?)?"
    rf"|(?:{_YEARS_RANGE_SEP}[ \t]*\d{{1,2}}[ \t]*)?(?:years?|yrs?))",
    re.I,
)

#: A year count that belongs to the *employer* rather than to the candidate.
#:
#: Every posting opens with a paragraph about the company, and companies
#: measure themselves in years: "Founded in 1998, Acme has over 25 years of
#: experience helping enterprises scale", "Our leadership team brings more than
#: 40 years of combined experience", "We've been in business 30 years".
#:
#: :func:`extract_years_required` takes the **largest** count in the posting,
#: for the good reason its docstring gives — the small ones are per-skill asks.
#: That rule hands the About Us paragraph the answer outright: a role asking
#: for "5+ years of experience building backend services" came out requiring
#: *forty*, because the founders' careers add up. `fit_scorer.score_experience`
#: then scores an eight-year candidate `max(0, 1 - 32 * 0.2)` — zero — and
#: prints "8 yrs vs 40 required (short by 32)" on the card. The dimension is
#: pinned at the floor for every applicant alive, and nothing about the posting
#: looks wrong.
#:
#: `resume_parser._shared_claim` is the same guard on the other side of the
#: comparison, learned there first: "Led a team with 25 years of combined
#: experience" is a fact about the team. The collective words are its list. The
#: rest are the shapes only an employer writes, and each is deliberately a
#: *subject* rather than a topic — "we have" and "our team" are the company
#: talking about itself, where "we are looking for", "we want" and "our ideal
#: candidate has" are the company talking about the candidate and still set the
#: floor.
_COLLECTIVE_YEARS_RE = re.compile(
    r"\b(?:combined|collective|cumulative|aggregate)\b", re.I
)
_EMPLOYER_SUBJECT_RE = re.compile(
    r"\bwe(?:\s+(?:have|bring|boast|offer)|'ve|\u2019ve)\b"
    r"|\bour\s+(?:team|teams|company|firm|founders?|leadership|staff|people"
    r"|experience|track\s+record|history)\b"
    r"|\bfounded\s+in\b",
    re.I,
)

#: How far back to look for that subject, measured from the **number** rather
#: than from the start of the match. The two are the same place for the
#: sentence pattern, whose first group is the count; for the label pattern the
#: count sits at the far end, and looking back from the label would put "Our"
#: in "Our experience: 25 years in the market" outside the window.
_EMPLOYER_LOOKBEHIND = 60

#: A heading that opens an optional block. Anchored at the start of the line and
#: allowed only a short tail, so it is a heading and not the word "preferred"
#: trailing a real requirement.
_OPTIONAL_HEAD_RE = re.compile(
    r"^[\s\-*•·]*(?:nice[\s-]?to[\s-]?haves?|bonus(?:\s+points)?|pluses"
    r"|preferred(?:\s+(?:qualifications|skills|experience))?|good[\s-]to[\s-]have"
    r"|desirable|optional|even\s+better)\b[^\n]{0,20}$",
    re.I,
)

#: A heading that closes one again. Only known section names count: a stray
#: unclosed block would quietly demote real requirements to wishes.
_SECTION_HEAD_RE = re.compile(
    r"^[\s\-*•·]*(?:requirements?|qualifications|basic\s+qualifications"
    r"|minimum\s+qualifications|responsibilities|what\s+you'?ll\s+do"
    r"|what\s+you'?ll\s+need|who\s+you\s+are|must\s+have|about\s+(?:us|the\s+role)"
    r"|benefits|compensation|perks|skills|your\s+profile)\b[^\n]{0,30}$",
    re.I,
)

#: The same idea inline, for postings that mark a single bullet rather than open
#: a section for it: "Bonus: 2 years of Rust".
_OPTIONAL_CUE_RE = re.compile(
    r"nice[\s-]to[\s-]have|bonus|(?:is|are)\s+a\s+(?:big\s+)?plus|preferred"
    r"|not\s+required|desirable|good[\s-]to[\s-]have",
    re.I,
)

_SENIORITY_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("exec", ("chief", "vp of", "vice president", "head of", "director of", "cto", "cio")),
    ("lead", ("staff", "principal", "lead", "architect", "manager")),
    ("senior", ("senior", "sr.", "sr", "senior-level")),
    ("junior", ("junior", "jr.", "entry level", "entry-level", "graduate", "intern")),
]

#: Each marker as a whole-word pattern. Matching "intern" as a substring filed
#: "Internal Tools Engineer" as a junior role, and the overqualified penalty
#: then cost every senior candidate who looked at it.
_SENIORITY_MARKER_RES: list[tuple[str, tuple[re.Pattern[str], ...]]] = [
    (level, tuple(marker_pattern(m) for m in markers))
    for level, markers in _SENIORITY_MARKERS
]

#: Nouns a level word can be *hiring for*. A marker in the body counts only when
#: one of these follows it within a couple of words: "Senior Backend Engineer",
#: "a senior-level position" and "our next Staff Engineer" name this job.
#: "lead the redesign", "help us architect scalable systems" and "reports to the
#: VP of Engineering" do not — they describe the work, or somebody else's job,
#: and reading them as the level turned ordinary IC postings into exec ones.
#: Note "engineering" is deliberately absent: it is the department in "VP of
#: Engineering", not the seat being filled.
_ROLE_NOUN = (
    r"(?:engineer|developer|programmer|scientist|analyst|designer|architect"
    r"|administrator|consultant|specialist|researcher|technician|manager"
    r"|director|marketer|recruiter|writer|role|position|opening|opportunity"
    r"|vacancy|candidate|applicant|hire|level|seat)s?\b"
)

#: `marker` then at most two more words, then the role noun.
_NAMES_THE_ROLE = re.compile(
    r"[\s\-,/]*(?:[\w+#./&]+[\s\-,/]+){0,2}" + _ROLE_NOUN, re.I
)

#: Somebody else's seat. "Reporting to the Senior Director" clears the role-noun
#: bar above — "director" is a role noun — but it is the boss's level, not this
#: job's, so the phrase in front of the marker vetoes the match.
_SOMEONE_ELSES_SEAT = re.compile(
    r"(?:report(?:s|ing|ed)?\s+(?:in\s+)?to|work(?:s|ing)?\s+(?:closely\s+)?with"
    r"|partner(?:s|ing)?\s+with|collaborat\w+\s+with|alongside|mentored\s+by"
    r"|supported\s+by|guidance\s+(?:of|from)|paired\s+with)"
    r"[^.\n]{0,40}$",
    re.I,
)

# A posting offering remote work. "anywhere" only counts attached to a verb
# about where you are: bare, it is prose — "you will make changes anywhere in
# the stack" is not a remote role, and it used to be read as one.
_REMOTE_RE = re.compile(
    r"\bremote\b|\bwork\s+from\s+home\b|\bwfh\b|\bdistributed\s+team\b"
    r"|\b(?:from|work|live|based|located)\s+anywhere\b",
    re.I,
)

# The same word, denied. "This role is not remote", "no remote work" and "not
# eligible for remote work" all contain "remote" and all mean the opposite of
# it. The word chain cannot cross a full stop, so a genuine offer earlier in the
# posting is not cancelled by a negation in a later sentence.
_NOT_REMOTE_BEFORE = re.compile(
    r"\b(?:not|non|no|never|without|cannot|can't|isn't|aren't)\b[-\s]?"
    r"(?:[\w'’]+\s+){0,3}$",
    re.I,
)
_NOT_REMOTE_AFTER = re.compile(
    r"\s*(?:work|working|option|roles?|positions?)?\s*"
    r"(?:is|are|will\s+be)?\s*(?:not|no\s+longer)\b",
    re.I,
)

# On-site, in every spelling. "on site" with a space and "in person" were both
# missing, so a posting saying "we work on site in Austin three days a week"
# stated nothing about location as far as this function was concerned.
# "in office" excludes the Microsoft one: "proficiency in Office 365" is a skill.
_ONSITE_RE = re.compile(
    r"\bon[-\s]?site\b|\bin[-\s]person\b|\bhybrid\b|\boffice[-\s]based\b"
    r"|\bon[-\s]?premises?\b|\brequired\s+to\s+relocate\b"
    r"|\brelocation\b[^.\n]{0,30}\brequired\b"
    # A posting almost never says "in our office" — it names the office: "based
    # in our NYC office", "from our London office three days a week", "at our
    # Austin headquarters", "in the San Francisco office". The bare form was
    # the only one recognised, so the single most common way a job ad states
    # where the desk is said nothing at all, and `detect_remote` returned None
    # — unstated — for a posting that had stated it plainly.
    #
    # Up to two words in the gap, which covers every city that needs them
    # ("San Francisco") without reaching across a clause: "in our efforts to
    # build a great office" is four words and stays out.
    r"|\b(?:in|at|from|out\s+of)\s+(?:the|our|its|their)\s+"
    r"(?:[\w\'\u2019-]+\s+){0,2}(?:offices?|headquarters|hq)\b"
    # Denying remote work is itself a statement that the role is on-site, and
    # it is often the only one a posting makes.
    # Not the hyphenated compound: "no remote-work stipend" is a benefit a
    # remote company can decline to offer, not a statement about the desk.
    r"|\bnon[-\s]?remote\b|\b(?:not|no)\s+(?:a\s+)?remote\b(?!-)"
    r"|(?<!microsoft )\bin[-\s]office\b(?!\s*(?:365|suite|\d))",
    re.I,
)

_CURRENCY = rf"(?:{CURRENCY_PATTERN})"
_RANGE_SEP = r"\s*(?:-|–|—|to)\s*"
# Splitting a band that has already matched, which is not the same job as
# finding one. "and" is here and not in `_RANGE_SEP` because the two are asked
# different questions: as a *separator* it is far too cheap — "$5,000 and 20
# days holiday" would read as a band of $5,000-$20,000 — so `_SALARY_RE` only
# accepts it with a currency marker on the figure that follows. By the time a
# span exists, an "and" inside it can only be the one that passed that test.
_RANGE_SPLIT = r"\s*(?:-|–|—|to|and)\s*"
# Cents, which every Workday posting writes and none of them mean: "$70,000.00
# - $95,000.00". Two digits, so the European thousands separator is untouched —
# "€70.000" is seventy thousand and its three digits do not match this.
#
# Without it the amount stopped at "$70,000", the ".00" sat where the range
# separator was expected, and the upper half of the band was dropped. The value
# ignores the cents either way: `_money_to_int` reads the integer part.
_CENTS = r"(?:\.\d{2})?"
# Amount shapes, longest first so "155000" isn't clipped to "155" — and the lakh
# shape ahead of the Western one, for the reason
# :data:`~app.services.currency.INDIAN_GROUPED` gives.
#
# `SPACE_GROUPED` is safe in here and nowhere near `_BARE_AMOUNT`: every use of
# this fragment sits behind a leading currency marker, and it is the marker
# that stops "we serve 500 000 users" from reading as pay.
_AMOUNT = (
    rf"(?:{SPACE_GROUPED}|{INDIAN_GROUPED}|{APOSTROPHE_GROUPED}|\d{{1,3}}(?:[.,]\d{{3}})+"
    rf"|\d{{4,7}}|\d{{2,3}}){_CENTS}(?:\s?k)?"
)
# A currency marker written *after* the figure, which is how most of the world
# outside the dollar writes one: "60,000 - 80,000 GBP", "850,000 - 950,000 SEK",
# "140,000 CAD - 170,000 CAD". Only ever attached to `_BARE_AMOUNT`, because a
# figure that already carries a leading marker has nothing to gain from it.
#
# Kept *inside* the matched band rather than read from the text after it, and
# that is the whole point. `extract_salary` prices a band by calling
# `currency.usd_rate` on the span it matched, and the display string it returns
# is re-read by `salary_service.parse_offered` later — so a marker outside the
# span is a marker that exists for neither call. "850,000 - 950,000 SEK" was
# read as eight hundred and fifty *thousand dollars* rather than eighty-five:
# ten times the band, which clears any floor a candidate set, scores
# `fit_scorer.score_salary` a confident 1.0, and puts a posting at nine times
# the market median on the intel card. GBP and EUR fail the same way and more
# quietly, understating by a fifth to a quarter in the direction that hides a
# good role rather than the one that oversells a bad one.
#
# Optional and repeated per figure, because a range may mark either half, both,
# or only the end: "140,000 CAD - 170,000 CAD" found no band at all before this
# — the marker sat exactly where `_RANGE_SEP` expected the separator — and
# "60,000 GBP - 80,000 GBP" matched the first figure and dropped the top.
_TRAILING_CURRENCY = rf"(?:\s?{_CURRENCY})?"

# Without a currency marker, only money-shaped figures count: "155,000",
# "155000", "155k". A bare two-to-four-digit number is a year, a headcount or
# "12 years of experience" far more often than it is pay.
_BARE_AMOUNT = (
    rf"(?:{INDIAN_GROUPED}|{APOSTROPHE_GROUPED}|\d{{1,3}}(?:[.,]\d{{3}})+|\d{{5,7}}|\d{{2,3}}\s?k)"
    rf"{_CENTS}{_TRAILING_CURRENCY}"
)

# The pay period, written *between* the two halves of a range rather than after
# them. "$45/hr to $60/hr" is how American contract work is advertised and
# "€8.000 per month - €10.000 per month" is how European postings are; in both
# the unit sits where `_RANGE_SEP` expects the separator, so the match stopped
# at the first figure and the band's upper half was dropped. "$45/hr to $60/hr"
# — a $93,600-$124,800 contract — was read as a flat $93,600 and displayed as
# one, which reads as an employer's final number rather than the floor of a
# range the candidate could negotiate inside.
#
# Only ever matched *inside* the optional range group below, never on its own.
# Consuming it from a single-rate posting would move the period words out of
# the tail `period_near` reads and into the span, and "$45/hr" with no period
# found is $45 scaled to $45,000 a year.
_PERIOD_UNITS = r"(?:hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?|annum)\b"
_INLINE_PERIOD = rf"(?:\s*/\s*{_PERIOD_UNITS}|\s+(?:per|an|a)\s+{_PERIOD_UNITS})?"

# "$150,000 - $180,000", "£90k", "USD 120k-160k", "€70.000", "$45/hr to $60/hr"
_SALARY_RE = re.compile(
    rf"{_CURRENCY}\s?{_AMOUNT}"
    rf"(?:{_INLINE_PERIOD}{_RANGE_SEP}{_CURRENCY}?\s?{_AMOUNT}"
    # "Between $100,000 and $130,000". The currency marker is required on the
    # second figure here and optional above, because "and" is a word rather
    # than a separator: without it, "$5,000 and 20 days holiday" reads as a
    # band of $5,000-$20,000.
    rf"|\s+and\s+{_CURRENCY}\s?{_AMOUNT})?",
    re.I,
)
# Same figures with the currency left off: "155000 - 185000", "Salary: 120k".
# A bare range stands on its own; a bare single figure only counts when a pay
# word introduces it, so "Trusted by 250,000 users" doesn't become a band.
_BARE_RANGE_RE = re.compile(rf"(?P<band>{_BARE_AMOUNT}{_RANGE_SEP}{_BARE_AMOUNT})", re.I)
# The same shapes plus the space grouping, for the cued fallback alone.
#
# `SPACE_GROUPED` is kept out of `_BARE_AMOUNT` because `_BARE_RANGE_RE` reads
# that fragment with nothing in front of it, and "we serve 500 000 - 600 000
# users" is a range of exactly this shape. The cue word is the guard that
# sentence cannot pass, and it is the same guard the rest of this module
# already trusts: a leading currency marker for `_AMOUNT`, a trailing one for
# `_MARKED_AMOUNT`, a pay word within forty characters here.
#
# Without it the hole was in English too. "Salary: 55 000" — no marker, and a
# space where the comma usually goes — matched nothing at all, because
# `\d{1,3}(?:[.,]\d{3})+` stops at the space and `\d{5,7}` cannot span it
# either. That is a board's salary field read as an empty one.
_CUED_AMOUNT = (
    rf"(?:{SPACE_GROUPED}|{INDIAN_GROUPED}|{APOSTROPHE_GROUPED}"
    rf"|\d{{1,3}}(?:[.,]\d{{3}})+|\d{{5,7}}|\d{{2,3}}\s?k){_CENTS}{_TRAILING_CURRENCY}"
)
_CUED_SALARY_RE = re.compile(
    r"(?:salary|compensation|\bcomp\b|base pay|\bpay\b|package|budget|remuneration)"
    rf"[^\n]{{0,40}}?(?P<band>{_CUED_AMOUNT}(?:{_RANGE_SEP}{_CUED_AMOUNT})?)",
    re.I,
)
# A lone figure whose currency marker is written *after* it: "90,000 EUR per
# year", "90.000 €", "1.200.000 ₺". `_SALARY_RE` cannot see these — it requires
# the marker in front — and the bare fallbacks above cannot either: one needs a
# range and the other needs an English pay word within forty characters. So the
# format most of Europe writes a single figure in was found only when the
# posting happened to say "salary" beside it, and a board whose salary field is
# the bare string "90.000 €" round-tripped through
# `salary_service.parse_offered` to nothing at all. A band that parses to
# nothing is a band `meets_floor` waves through and `score_salary` reports as
# "the posting doesn't publish a salary band" — on a posting that does.
#
# The marker is *required* here, which is what makes a cue word unnecessary:
# this module's own rule is that a currency-marked figure is unambiguous, and
# the side the marker is written on does not change that.
#
# Tried last of all the fallbacks, so nothing that matched before still
# matches: a cued figure keeps priority over a marked one further down the
# posting ("Salary: 120,000 ... relocation budget 5,000 EUR").
#
# The range half matters as much as the lone figure: "45 000 - 55 000 €" is the
# ordinary French posting, and the marker sits after the *second* figure, where
# neither `_SALARY_RE` nor `_BARE_RANGE_RE` can require it. The marker on the
# first figure is optional so "45 000 € - 55 000 €" reads the same way.
_MARKED_AMOUNT = (
    rf"(?:{SPACE_GROUPED}|{INDIAN_GROUPED}|{APOSTROPHE_GROUPED}"
    rf"|\d{{1,3}}(?:[.,]\d{{3}})+|\d{{5,7}}|\d{{2,3}}\s?k){_CENTS}"
)
_MARKED_AFTER_RE = re.compile(
    rf"(?P<band>{_MARKED_AMOUNT}(?:\s?{_CURRENCY})?"
    rf"(?:{_INLINE_PERIOD}{_RANGE_SEP}{_MARKED_AMOUNT})?\s?{_CURRENCY})",
    re.I,
)
_SALARY_NUM_RE = re.compile(
    rf"({SPACE_GROUPED}|{INDIAN_GROUPED}|{APOSTROPHE_GROUPED}"
    rf"|\d{{1,3}}(?:[.,]\d{{3}})+|\d+)\s*(k)?",
    re.I,
)

# US retirement plans, which are named after the tax-code section that created
# them and are therefore shaped exactly like money. "401k" satisfies
# `_BARE_AMOUNT` ("120k" is the same shape), and the phrase it almost always
# appears in — "competitive salary, 401k matching" — puts a pay cue within the
# forty characters `_CUED_SALARY_RE` looks back over. So the single most common
# benefits line in an American job posting was read as a published band of
# $401,000: it rendered on the card as a salary of "401k", it cleared any floor
# the candidate set, and `fit_scorer.score_salary` scored the dimension 1.0 on a
# number no employer had offered. That score gates autopilot sending.
#
# Scrubbed from the bare-number fallbacks only. A figure with a currency on it
# is unambiguous by this module's own rule, and "$401k" is somebody's pay.
_RETIREMENT_PLAN_RE = re.compile(
    r"(?<![$€£₹\d])\b(?:401\s*\(?\s*k\s*\)?|40[37]\s*\(?\s*b\s*\)?|457\s*\(?\s*b\s*\)?)"
    r"(?!\w)",
    re.I,
)


def _to_usd(value: int | None, rate: float) -> int | None:
    """*value* at *rate*, rounded to a round hundred.

    The hundred is honesty about the rate rather than arithmetic: converting
    ₹12,00,000 at a figure as coarse as :data:`~app.services.currency.CURRENCY_TO_USD`
    does not know its own last two digits, so reporting them would be inventing
    them.

    A figure that converts to nothing comes back as ``None``, keeping this
    module's rule that zero is never a published band — see
    :func:`_money_to_int`, and the 500 that reading a zero as a real figure
    caused there.
    """
    if not value:
        return value
    return round(value * rate / 100) * 100 or None


# Above this, an annualised figure is a misread rather than a salary.
#
# The failure it catches is an annual band with a period word loose near it —
# "$150,000 base; on-call hours are paid separately" puts "hour" inside the
# lookahead, and multiplying by 2080 turns a real salary into $312,000,000. A
# number that large poisons everything downstream in a way the original never
# could: `compare_to_market` reports it as two thousand times the median, and
# `score_salary` clears every floor a candidate could set.
#
# Set well above any pay anyone will ever be offered, so it only ever fires on
# arithmetic that has already gone wrong. When it does, the figure is kept as
# written — the period is the part we got wrong, not the number.
_IMPLAUSIBLE_ANNUAL = 5_000_000




# Section headers that introduce the "what we need" part of a posting.
_REQUIREMENT_HEADS = (
    "requirements", "qualifications", "what you'll need", "what you need",
    "who you are", "you have", "must have", "we're looking for",
    "we are looking for", "skills", "your profile", "about you",
)
_RESPONSIBILITY_HEADS = (
    "responsibilities", "what you'll do", "what you will do", "the role",
    "your role", "day to day", "day-to-day", "you will", "about the role",
)

# Industry keywords, counted by density — see :func:`detect_industry`.
#
# Matched on word boundaries by :data:`_INDUSTRY_PATTERNS`, not by containment.
# Containment made three of these fire on words that have nothing to do with the
# industry, and one of them is a word almost every job ad contains:
#
#   "start immediately", "your immediate manager" -> media
#   "Chennai India", "Shanghai office"            -> ai   (from the "ai " needle)
#   "blending two teams"                          -> lending
#
# The industry is not a decoration. It is 10% of the fit score, and
# `cover_letter_service` writes it into the letter as "<Company> operates in
# <industry>." — so a fintech that happened to say "immediately" was told it was
# a media company, and then told the employer so.
#
# "shipping" is gone rather than bounded. Its problem is not the boundary: in a
# software job ad "shipping" overwhelmingly means deploying — "we ship daily",
# "shipping features" — and it was the only needle an ordinary engineering
# posting tripped, so it decided the label on its own. "last mile" and "courier"
# are the senses that only ever mean freight.
_INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fintech": ("fintech", "payments", "banking", "trading", "lending", "insurtech"),
    "healthcare": (
        "healthcare", "health tech", "medical", "clinical", "biotech", "pharma",
        "pharmaceutical",
    ),
    "ecommerce": ("ecommerce", "e-commerce", "retail", "retailer", "marketplace", "d2c"),
    # "ai" rather than "ai ": the padding space was a boundary check spelled the
    # only way containment allows, and it both over-matched — any word ending in
    # "ai", so Chennai, Mumbai, Shanghai and Dubai all read as AI companies —
    # and under-matched, since "AI/ML" and "AI," have no space after the acronym.
    "ai": ("artificial intelligence", "machine learning", "ai", "llm", "generative"),
    "security": ("cybersecurity", "security", "infosec", "appsec"),
    "gaming": ("gaming", "game studio", "video game"),
    "data": ("data platform", "analytics", "data warehouse", "business intelligence"),
    "edtech": ("edtech", "education technology", "e-learning"),
    "logistics": ("logistics", "supply chain", "freight", "last mile", "last-mile",
                  "courier"),
    "media": ("media", "streaming", "publishing", "advertising", "adtech"),
}

# The left guard refuses a needle that starts mid-word ("Chenn|ai", "b|lending");
# the right guard refuses one that ends mid-word ("media|te", "retail|er") while
# admitting a plural "s", so "video games" and "retailers" still count.
_INDUSTRY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        industry,
        re.compile(
            "|".join(rf"(?<![a-z0-9]){re.escape(k)}(?!s?[a-z])" for k in keywords)
        ),
    )
    for industry, keywords in _INDUSTRY_KEYWORDS.items()
)


@dataclass
class ParsedJob:
    """The structured view of a posting that tailoring and scoring both consume."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    remote: bool | None = None
    seniority: str | None = None  # junior|mid|senior|lead|exec
    years_required: int | None = None

    required_skills: list[str] = field(default_factory=list)
    # Skills named as "nice to have" — they count for less when scoring.
    preferred_skills: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    industry: str | None = None
    salary_text: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None

    raw_text: str = ""
    parsed_with: str = "heuristic"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def jd_hash(text: str) -> str:
    """Stable cache key for a job description, insensitive to whitespace churn."""
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Fetching                                                                     #
# --------------------------------------------------------------------------- #

# Nodes that never carry posting content but do carry a lot of text.
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "noscript", "svg", "form")


class JobFetchError(RuntimeError):
    """Raised when a job URL could not be turned into usable text."""


def fetch_job_text(url: str, *, timeout: float | None = None) -> tuple[str, str | None]:
    """Fetch a posting URL and return ``(text, page_title)``.

    Best-effort by design: postings behind a login or rendered entirely in JS
    yield little or nothing, and the caller falls back to pasted text. Raises
    :class:`JobFetchError` rather than returning an empty string so the API can
    tell the user what happened.

    *url* is a caller's own string — ``job_url`` off a Smart Apply request — and
    the text this returns is handed back to them, so the fetch goes through
    :func:`app.core.outbound.safe_get`, which refuses anything resolving inside
    our own network and re-checks every redirect hop. Without it the endpoint is
    a read proxy onto loopback and the cloud metadata service; see that module.
    """
    headers = {
        "User-Agent": settings.scraper_user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = outbound.safe_get(
            None,
            url,
            headers=headers,
            timeout=timeout or settings.scraper_timeout_seconds,
        )
        resp.raise_for_status()
    except outbound.BlockedURLError as exc:
        # Deliberately quotes the reason: a candidate who pasted a file:// path
        # or an intranet link needs to know the URL was refused rather than
        # unreachable. It says nothing a caller could not learn by resolving the
        # name themselves.
        raise JobFetchError(f"That job URL can't be fetched — {exc}.") from exc
    except requests.RequestException as exc:
        raise JobFetchError(f"Could not fetch the job posting: {exc}") from exc

    soup = BeautifulSoup(outbound.page_text(resp), "html.parser")
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()

    page_title = soup.title.get_text(strip=True) if soup.title else None
    # Prefer the semantic content container; fall back to the whole body.
    node = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))

    if len(text) < 200:
        raise JobFetchError(
            "That page didn't return readable job text — it may need a login or "
            "render with JavaScript. Paste the description instead."
        )
    return text[:20000], page_title


# --------------------------------------------------------------------------- #
# Heuristic parsing                                                            #
# --------------------------------------------------------------------------- #


def _section_after(text: str, heads: tuple[str, ...], limit: int = 2500) -> str:
    """Return the slice of *text* following the first matching section header."""
    lowered = text.lower()
    for head in heads:
        idx = lowered.find(head)
        if idx != -1:
            return text[idx : idx + limit]
    return ""


def extract_bullets(text: str, limit: int = 12) -> list[str]:
    """Pull bulleted lines out of a block, de-duplicated and trimmed."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _BULLET_RE.finditer(text):
        line = re.sub(r"\s+", " ", match.group(1)).strip(" .;")
        key = line.lower()
        if len(line) < 8 or key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _employer_claim(chunk: str, match: re.Match[str]) -> bool:
    """Whether a year count is the employer's own rather than the candidate's ask."""
    if _COLLECTIVE_YEARS_RE.search(match.group(0)):
        return True
    start = match.start(1)
    head = chunk[max(0, start - _EMPLOYER_LOOKBEHIND) : start]
    return bool(_EMPLOYER_SUBJECT_RE.search(head))


def _years_in(chunk: str) -> list[int]:
    matches = [*_YEARS_REQUIRED_RE.finditer(chunk), *_YEARS_LABEL_RE.finditer(chunk)]
    values = [int(m.group(1)) for m in matches if not _employer_claim(chunk, m)]
    return [v for v in values if 0 < v <= 40]


def extract_years_required(text: str) -> int | None:
    """The experience floor the posting sets, in years.

    Within one phrase the floor is the low end: "3-5 years" asks for three, and
    the pattern captures only that number. Across the posting it is the *largest*
    figure that is actually required, because the small ones are per-skill asks —
    a role wanting "6+ years of backend experience" and "2 years of experience
    with Kubernetes" wants six years, not two. Reading the smallest told an
    eight-year requirement it needed one year, and every candidate short of the
    real bar was scored as comfortably over it.

    Counts on a nice-to-have line never set the floor. If those are the only
    counts in the posting they are all there is to go on, so the largest of them
    is used rather than giving up.
    """
    required: list[int] = []
    optional: list[int] = []
    in_optional_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if _OPTIONAL_HEAD_RE.match(stripped):
            in_optional_section = True
        elif _SECTION_HEAD_RE.match(stripped):
            in_optional_section = False
        bucket = (
            optional
            if in_optional_section or _OPTIONAL_CUE_RE.search(stripped)
            else required
        )
        bucket.extend(_years_in(line))
    values = required or optional
    return max(values) if values else None


def _level_in_title(title: str) -> str | None:
    """A level word anywhere in the title is that title's level."""
    for level, patterns in _SENIORITY_MARKER_RES:
        if any(p.search(title) for p in patterns):
            return level
    return None


def _level_in_body(body: str) -> str | None:
    """A level word in the body counts only where it names the seat being filled.

    The body is prose: it says what you will do and who you will do it with, and
    both readily contain level words that have nothing to do with this job's
    level. Requiring a role noun just after the marker, and no reporting cue just
    before it, keeps "we are hiring a Staff Engineer" and drops "you will lead
    the redesign" and "reports to the VP of Engineering".
    """
    for level, patterns in _SENIORITY_MARKER_RES:
        for pattern in patterns:
            for match in pattern.finditer(body):
                if not _NAMES_THE_ROLE.match(body, match.end()):
                    continue
                if _SOMEONE_ELSES_SEAT.search(body[max(0, match.start() - 60) : match.start()]):
                    continue
                return level
    return None


def infer_seniority(title: str | None, text: str, years: int | None) -> str | None:
    """Seniority from the title first, then body markers, then the year count."""
    from_title = _level_in_title(title or "")
    if from_title:
        return from_title
    from_body = _level_in_body(text[:1500])
    if from_body:
        return from_body
    if years is None:
        return None
    if years >= 10:
        return "lead"
    if years >= 6:
        return "senior"
    if years >= 3:
        return "mid"
    return "junior"


def _offers_remote(text: str) -> bool:
    """Whether the posting offers remote work, rather than merely saying the word.

    Every negated mention used to read as an offer, and the location dimension
    gives a remote role full marks — so a remote-only candidate was shown "This
    role is not remote" at 1.0, which is the one thing that filter exists to
    prevent.
    """
    for match in _REMOTE_RE.finditer(text):
        if _NOT_REMOTE_BEFORE.search(text[max(0, match.start() - 40) : match.start()]):
            continue
        if _NOT_REMOTE_AFTER.match(text, match.end()):
            continue
        return True
    return False


def detect_remote(text: str) -> bool | None:
    """True for remote, False for explicitly on-site/hybrid, None when unstated."""
    remote_hit = _offers_remote(text)
    onsite_hit = bool(_ONSITE_RE.search(text))
    if remote_hit and not onsite_hit:
        return True
    if onsite_hit and not remote_hit:
        return False
    if remote_hit and onsite_hit:
        # "Remote-friendly hybrid" — treat the stricter signal as the truth.
        return False
    return None


def _money_to_int(chunk: str, *, scale_bare: bool = True) -> int | None:
    match = _SALARY_NUM_RE.search(chunk)
    if not match:
        return None
    digits, k = match.groups()
    value = int(re.sub(rf"[{re.escape(GROUPING_CHARS)}]", "", digits))
    # "120k" and a bare "120" are both 120,000 — nobody advertises a salary of
    # one hundred and twenty.
    #
    # Except per hour, per day or per week, which is what *scale_bare* turns
    # off: "$85 per hour" is eighty-five dollars, and reading it as $85,000 an
    # hour would annualise to a hundred and seventy-six million. The `k` suffix
    # is honoured either way — somebody writing "$8k per month" means eight
    # thousand, and said so.
    if k or (scale_bare and value < 1000):
        value *= 1000
    # Zero is not a figure anyone published; it is what an all-zeros span parses
    # to, and the scaling above cannot rescue it (0 * 1000 is still 0). Returned
    # as "no figure" rather than as the number nought, because every reader
    # downstream tests these with `if value` — truthiness that reads 0 as absent
    # in some places and as a real band in others. `compare_to_market` was in
    # the second group: it filtered `[v for v in (low, high) if v]`, got an
    # empty list from a (0, 0) band, and divided by its length. That is a 500 on
    # `GET /jobs/{id}/intel`, reachable from the text of a scraped posting.
    return value or None


#: A magnitude written onto the end of a figure: "$40M", "€30M", "$40 million".
#:
#: No salary is written this way and every company blurb's funding round is.
#: "We raised $40M last year" is on a large share of the startup postings this
#: feed carries, and `_AMOUNT` matched the "$40" out of it and left the "M"
#: behind — so `_money_to_int` applied the rule that nobody advertises a salary
#: of forty dollars, and the posting was filed with a band of $40,000.
#:
#: That is worse than the wrong number it looks like, for two reasons.
#:
#: The blurb is at the *top* of a posting and the pay is at the bottom, and
#: this function returned the first match it found — so the fabricated band beat
#: the real "$150,000 - $190,000" further down. And a posting with a band is
#: not a posting without one: `fit_scorer.score_salary` scores the dimension
#: against it instead of reporting no published band, `meets_floor` measures the
#: candidate's floor against it, and the intel card quotes it. A candidate
#: targeting $150,000 was shown a $40,000 role and scored down for it, on a
#: posting that pays what they asked for.
#:
#: Anchored at the start of the tail so the magnitude has to be *touching* the
#: figure. A single space is allowed, for "$40 million", and no more: "$40,000
#: and 5 million users" is a salary beside a sentence, not a figure with a
#: magnitude on it. "monthly" cannot match `m` — the word boundary needs the
#: `m` to end there — and neither can a currency code like "MXN".
_MAGNITUDE_AFTER_RE = re.compile(
    r"^\s?(?:m|mm|mn|bn|b|million|billion|milliard)s?\b", re.I
)


def _priceable(tail: str) -> bool:
    """Whether a matched figure is money rather than a magnitude."""
    return not _MAGNITUDE_AFTER_RE.match(tail)


def _first_priceable(
    pattern: re.Pattern[str], haystack: str, group: str | int
) -> tuple[str, str, str] | None:
    """The first match of *pattern* that isn't a funding figure, with neighbours.

    ``finditer`` rather than ``search`` because skipping one magnitude has to
    leave the rest of the posting readable: a blurb saying "$40M Series B" and a
    pay line saying "$150,000" are both in this text, and the point is to reach
    the second one.
    """
    for match in pattern.finditer(haystack):
        tail = haystack[match.end(group) :]
        if _priceable(tail):
            return match.group(group), haystack[: match.start(group)], tail
    return None


def _find_salary_span(text: str) -> tuple[str, str, str] | None:
    """``(band, text_before, text_after)``, or None when no band is present.

    A currency-marked figure wins wherever it appears — it is unambiguous. Only
    when the posting names no currency at all do we fall back to bare numbers,
    which are read conservatively (see :data:`_BARE_AMOUNT`) and with the
    retirement-plan names blanked out first (see :data:`_RETIREMENT_PLAN_RE`).

    A figure carrying a magnitude is skipped rather than priced — see
    :data:`_MAGNITUDE_AFTER_RE`.

    The neighbours come back with the band because the pay period is written
    beside it and nowhere else, and they are sliced from **whichever haystack
    the band was found in**. That matters for the bare fallbacks: scrubbing the
    retirement plans shifts every offset after the first one, so an index taken
    from the scrubbed text would point somewhere else in the original.
    """
    found = _first_priceable(_SALARY_RE, text, 0)
    if found:
        return found
    # Replaced by a space rather than deleted, so "401k-403b" can't be spliced
    # into a number.
    bare_text = _RETIREMENT_PLAN_RE.sub(" ", text)
    for pattern in (_BARE_RANGE_RE, _CUED_SALARY_RE, _MARKED_AFTER_RE):
        found = _first_priceable(pattern, bare_text, "band")
        if found:
            return found
    return None


#: A single figure the posting has named as the *top* of what it will pay.
#:
#: "Up to $150,000" was read the way every other single figure is read — as the
#: band's lower end, which `salary_service.as_band` then mirrors into the upper
#: end as well. So a posting that published only a ceiling was stored with a
#: **floor** of $150,000 and shown to the candidate as one: `compare_to_market`
#: reports it as `offered_min`, and the fit score's salary line quotes it. The
#: employer said what they would not go above; we told the candidate it was
#: what they would not go below.
#:
#: Anchored hard against the figure — `\W*$` before it, `^\W*` after — so the
#: phrase has to be touching the money. "We have up to 500 staff and pay
#: $150,000" does not qualify, and neither does anything a sentence away.
_CEILING_BEFORE_RE = re.compile(
    r"(?:up\s+to|no\s+(?:more|higher)\s+than|not\s+exceeding|at\s+most|"
    r"max(?:imum)?(?:\s+of)?)\W*$",
    re.I,
)
_CEILING_AFTER_RE = re.compile(
    r"^\W*(?:max(?:imum)?|or\s+(?:less|below)|and\s+under|at\s+most)\b", re.I
)


def _ceiling_match(head: str, tail: str) -> tuple[re.Match[str], bool] | None:
    """The words naming a lone figure as the band's top, and which side they sit.

    ``(match, True)`` when the phrase is written in front of the figure,
    ``(match, False)`` when it follows.

    One helper for both readers, for the reason
    :func:`app.services.pay_period._match_ahead` gives about the period: two
    callers computing the same answer two ways is how the display string and
    the figures behind it come to disagree.
    """
    before = _CEILING_BEFORE_RE.search(head)
    if before is not None:
        return before, True
    after = _CEILING_AFTER_RE.search(tail)
    if after is not None:
        return after, False
    return None


def _is_ceiling(head: str, tail: str) -> bool:
    """Whether the words touching a lone figure name it as the band's top."""
    return _ceiling_match(head, tail) is not None


def _with_ceiling(raw: str, head: str, tail: str) -> str:
    """*raw* with the words that named it a ceiling restored beside it.

    The span the regex matched is the money and nothing else, so "Up to
    $150,000" produced the display string "$150,000" — the *same string* an
    ordinary $150,000 salary produces. That is the failure
    :data:`_CEILING_BEFORE_RE` exists to prevent, arriving one step later:
    :func:`extract_salary` puts the figure in the upper slot correctly and the
    row is stored with no floor, and then every reader that re-reads the
    display string reads a floor back out of it.
    :func:`app.services.salary_service.compare_to_market` is that reader on
    a live page — the intel card reported ``offered_min`` of $150,000 against a
    row whose ``salary_min`` is NULL. The employer said what they would not go
    above; the card said it was what they would not go below.

    The same fix, and the same reasoning, as :func:`_with_period`: the display
    string is what gets stored and re-read, so anything the figures were
    derived from has to survive in it. Taken verbatim from the posting rather
    than generated, so "Up to" stays "Up to" and "$150,000 max" keeps its own
    word order.

    The punctuation the anchors swept up is dropped and the words are not: the
    phrase is re-read as text, and a stray mark between it and the money is
    noise at best. Nothing in either pattern can carry a currency marker, so
    restoring it cannot change the rate the round-trip prices the band at.
    """
    found = _ceiling_match(head, tail)
    if found is None:
        return raw
    match, before = found
    phrase = re.sub(r"\s+", " ", match.group(0)).strip()
    if before:
        phrase = re.sub(r"\W+$", "", phrase)
        return f"{phrase} {raw}" if phrase else raw
    phrase = re.sub(r"^\W+", "", phrase)
    return f"{raw} {phrase}" if phrase else raw


def extract_salary(text: str) -> tuple[str | None, int | None, int | None]:
    """Return ``(display_text, annual_min, annual_max)`` for the first band found.

    **The two numbers are always a year's pay, in US dollars.** A posting quoting an hourly,
    daily, weekly or monthly rate is annualised at :data:`PERIOD_MULTIPLIERS`,
    because every reader of these — the fit score's salary dimension, the
    market comparison, the feed's salary floor — holds an annual figure on the
    other side of the comparison. Handing them a monthly number to compare
    against an annual one is not a smaller answer, it is a wrong one.

    **A band quoted in another currency is converted** at
    :data:`~app.services.currency.CURRENCY_TO_USD`, for exactly the same reason
    and with exactly the same trade-off: every number on the other side of every
    comparison is a dollar figure, so a rupee one handed over unconverted is
    wrong by eighty times rather than merely imprecise.

    **The display text keeps the period the employer wrote.** It is what the
    card shows and what gets stored, so "$85 - $105 per hour" stays as it was
    said rather than being restated as a figure nobody published. It is also
    what :func:`app.services.salary_service.parse_offered` re-reads later, and
    a display string that had lost its period would annualise a second time on
    the way back in. It keeps the employer's currency marker for the same
    reason, and re-reading it is idempotent for the same reason: both
    conversions are computed from the figures as written, which the display text
    still holds.

    **And it keeps the words that named a lone figure a ceiling**, for the
    third time for the same reason — see :func:`_with_ceiling`. Period,
    currency, side: everything the two numbers were derived from has to survive
    in the string they are stored beside, because that string is read back.
    """
    found = _find_salary_span(text)
    if found is None:
        return None, None, None
    span, head, tail = found
    raw = re.sub(r"\s+", " ", span).strip()

    period = period_near(head, tail, raw)
    multiplier = PERIOD_MULTIPLIERS.get(period or "year", 1)
    # Only a rate quoted per hour, day or week can be a two-or-three digit
    # figure and mean it. Everything else keeps the rule that a bare "120" is
    # a hundred and twenty thousand.
    scale_bare = multiplier <= 12

    parts = re.split(_RANGE_SPLIT, raw)
    low = _money_to_int(parts[0], scale_bare=scale_bare) if parts else None
    high = _money_to_int(parts[1], scale_bare=scale_bare) if len(parts) > 1 else None
    if low is None and high is None:
        # The span matched the shape of money and held none — "$00", "€0.000".
        # Returning `raw` here would put a salary line on the card ("$00") with
        # no band behind it, which reads as a published figure of zero rather
        # than as the parse artifact it is.
        return None, None, None
    if low and high and high < low:
        low, high = high, low

    # Read before the guard below, because the guard is a claim about dollars.
    # Reading it here cannot change the answer: `_with_period` only appends the
    # employer's period words to the end of `raw`, which cannot introduce a
    # currency marker or displace the leftmost one.
    rate = usd_rate(raw)

    # `_IMPLAUSIBLE_ANNUAL` is a figure in dollars — "above any pay anyone will
    # ever be offered" is not a sentence about yen. Tested against the raw
    # number it rejected the annualisation of every ordinary salary quoted in a
    # low-value currency: ¥700,000 a month is a normal Tokyo wage and
    # 700,000 × 12 is over the bar, so the multiplier was dropped as a misread
    # period and the *monthly* figure was stored as the year's pay. About
    # $4,700, filed as an annual salary — under every floor and every median
    # this product models, which is the direction that empties a feed.
    #
    # ₹5,00,000 a month failed the same way and predates the yen entirely.
    annualised = (high or low or 0) * multiplier * rate
    if multiplier != 1 and annualised <= _IMPLAUSIBLE_ANNUAL:
        low = low * multiplier if low else low
        high = high * multiplier if high else high
        raw = _with_period(raw, tail, head)

    if rate != 1.0:
        low, high = _to_usd(low, rate), _to_usd(high, rate)
        if low is None and high is None:
            return None, None, None

    # Last, so the figure has already been annualised and converted and only
    # its *side* is in question. A lone figure the posting called a ceiling
    # belongs in the upper slot; leaving it in the lower one publishes a floor
    # the employer never offered. See `_CEILING_BEFORE_RE`.
    if high is None and low is not None and _is_ceiling(head, tail):
        return _with_ceiling(raw, head, tail), None, low
    return raw, low, high


def _with_period(raw: str, tail: str, head: str) -> str:
    """*raw* with the period phrase the posting used restored onto the end.

    The span the regex matched stops at the last digit, so a band read as
    hourly would otherwise be displayed as a bare "$85 - $105" — the same string
    an $85,000 salary produces, on a card whose numbers are now annualised
    behind it. Appending the words the employer wrote is what keeps the display
    and the stored figures telling the same story.

    Taken verbatim from the posting rather than generated from the period name,
    so "/hr" stays "/hr" and "per month" stays "per month".

    **Read through the same helper that priced the band.** This used to run
    `PERIOD_RE` over its own slice of the tail, and the two slices were not the
    same string: a phrase beginning inside the lookahead but running past it
    was priced by `period_near` and invisible here, so the numbers were
    annualised and the card showed the bare figure they were annualised from —
    "$4,000" beside a stored $48,000, which is the one thing this function
    exists to prevent.
    """
    end = period_span_after(tail)
    if end is None:
        # The period was written in front of the figure ("hourly rate: $85").
        # The words are already on the page above the band, so the card reads
        # correctly without repeating them.
        return raw
    phrase = re.sub(r"\s+", " ", tail[:end]).strip()
    joiner = "" if phrase.startswith("/") else " "
    return f"{raw}{joiner}{phrase}"


def detect_industry(text: str) -> str | None:
    """Best-guess industry label, by keyword density in the posting."""
    # Collapsed first: every phrase here is multi-word, and a posting wraps
    # wherever its column width ran out.
    lowered = re.sub(r"\s+", " ", text.lower())
    best: tuple[str, int] | None = None
    for industry, pattern in _INDUSTRY_PATTERNS:
        hits = len(pattern.findall(lowered))
        if hits and (best is None or hits > best[1]):
            best = (industry, hits)
    return best[0] if best else None


def extract_title(text: str, page_title: str | None = None) -> str | None:
    """The role title: an explicit label, else the page title, else line one."""
    match = re.search(
        r"^\s*(?:job\s+title|position|role)\s*[:\-]\s*(.{3,120})$", text, re.I | re.M
    )
    if match:
        return match.group(1).strip(" .-|")
    if page_title:
        # Page titles are usually "Senior Engineer at Acme | Greenhouse" — the
        # first segment is the role.
        head = re.split(r"\s+[|\-–—]\s+", page_title)[0].strip()
        if 3 < len(head) < 120:
            return head
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    # Headlines are usually "Senior Engineer at Acme" — the title is the left half.
    first = re.split(r"\s+at\s+[A-Z]", first, maxsplit=1)[0].strip(" .-|,")
    return first[:120] or None


def extract_company(text: str, page_title: str | None = None) -> str | None:
    """The hiring company, from an explicit label or an 'X at Y' page title."""
    match = re.search(
        r"^\s*(?:company|employer|organization)\s*[:\-]\s*(.{2,80})$", text, re.I | re.M
    )
    if match:
        return match.group(1).strip(" .-|")
    for source in (page_title or "", text[:400]):
        at_match = re.search(r"\bat\s+([A-Z][\w&.\- ]{1,50})", source)
        if at_match:
            return at_match.group(1).strip(" .-|")
    return None


def extract_location(text: str) -> str | None:
    match = re.search(r"^\s*location\s*[:\-]\s*(.{2,80})$", text, re.I | re.M)
    if match:
        return match.group(1).strip(" .-|")
    # "San Francisco, CA" / "Berlin, Germany" in the first few lines.
    head = "\n".join(text.splitlines()[:12])
    place = re.search(r"\b([A-Z][a-zA-Z.\- ]{2,30},\s*[A-Z][a-zA-Z.\- ]{1,30})\b", head)
    return place.group(1).strip() if place else None


def parse_heuristic(text: str, page_title: str | None = None) -> ParsedJob:
    """Structure a posting with regex + taxonomy matching only. Never fails."""
    text = text or ""
    requirements_block = _section_after(text, _REQUIREMENT_HEADS)
    responsibilities_block = _section_after(text, _RESPONSIBILITY_HEADS)

    # Skills named in the requirements section are required; anything else the
    # taxonomy finds elsewhere in the posting is treated as preferred.
    required = extract_skills(requirements_block or text)
    everywhere = extract_skills(text)
    preferred = [s for s in everywhere if s not in required]

    nice_block = _section_after(text, ("nice to have", "bonus", "preferred", "plus if"), 1200)
    if nice_block:
        nice = extract_skills(nice_block)
        # A "nice to have" skill is not a requirement, whichever section it was
        # first seen in.
        required = [s for s in required if s not in nice]
        preferred = sorted({*preferred, *nice})

    years = extract_years_required(text)
    title = extract_title(text, page_title)
    salary_text, salary_min, salary_max = extract_salary(text)

    return ParsedJob(
        title=title,
        company=extract_company(text, page_title),
        location=extract_location(text),
        remote=detect_remote(text),
        seniority=infer_seniority(title, text, years),
        years_required=years,
        required_skills=required,
        preferred_skills=preferred,
        responsibilities=extract_bullets(responsibilities_block or text),
        keywords=extract_keywords(text),
        industry=detect_industry(text),
        salary_text=salary_text,
        salary_min=salary_min,
        salary_max=salary_max,
        raw_text=text,
        parsed_with="heuristic",
    )


# Words that appear in every posting and carry no signal for keyword matching.
# Kept as prose and split at import — a 120-element list literal is unreadable
# and this runs exactly once.
_STOPWORD_TEXT = """
a an the and or but if then than that this these those for to of in on at by with
from as is are was were be been being have has had do does did will would shall should
can could may might must you your we our us they them their it its not no yes about
into over under more most some any each other such own same so only just also very
who whom which what when where why how all both few here there team role work working
job position candidate company experience years year strong ability able across
including etc via using use used help helps within while during per plus
"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def extract_keywords(text: str, limit: int = 30) -> list[str]:
    """Highest-signal terms in the posting, for ATS keyword matching.

    Taxonomy skills always win a slot; the rest of the list is filled with the
    most frequent non-stopword terms so domain vocabulary ("claims adjudication",
    "GDPR") survives even when it isn't a recognised technical skill.
    """
    skills = extract_skills(text)
    counts: dict[str, int] = {}
    for word in re.findall(r"[a-zA-Z][a-zA-Z+#.\-]{2,}", text.lower()):
        token = word.strip(".-")
        if len(token) < 3 or token in _STOPWORDS or token in skills:
            continue
        counts[token] = counts.get(token, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    extra = [word for word, hits in ranked if hits >= 2][: max(0, limit - len(skills))]
    return [*skills[:limit], *extra][:limit]


# --------------------------------------------------------------------------- #
# LLM enrichment                                                               #
# --------------------------------------------------------------------------- #

_LLM_SYSTEM_PROMPT = (
    "You extract structured data from job postings. Return ONLY a JSON object "
    "with these keys and no prose:\n"
    '{"title": str, "company": str|null, "location": str|null, '
    '"remote": true|false|null, "seniority": "junior"|"mid"|"senior"|"lead"|"exec"|null, '
    '"years_required": int|null, "required_skills": [str], "preferred_skills": [str], '
    '"responsibilities": [str], "industry": str|null, "salary_text": str|null}\n'
    "Rules: copy facts from the posting only — never invent a company, salary or "
    "requirement. Skills must be short noun phrases (e.g. 'kubernetes', 'python', "
    "'stakeholder management'), lowercase, at most 20 items. Responsibilities are "
    "at most 8 short phrases. Use null when the posting does not say."
)

_ALLOWED_SENIORITY = {"junior", "mid", "senior", "lead", "exec"}


def _str_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            cleaned = re.sub(r"\s+", " ", item.strip())[:120]
            if cleaned.lower() not in {o.lower() for o in out}:
                out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _merge_llm(base: ParsedJob, data: dict[str, Any]) -> ParsedJob:
    """Overlay validated LLM fields on the heuristic result.

    The LLM only ever *adds* — a field it gets wrong can't erase a value the
    heuristics were confident about, because every assignment is guarded.
    """
    merged = ParsedJob(**{**base.as_dict()})

    for attr in ("title", "company", "location"):
        value = data.get(attr)
        if isinstance(value, str) and value.strip():
            setattr(merged, attr, re.sub(r"\s+", " ", value.strip())[:255])

    # ``salary_text`` is not overlaid like the others, because it is not a field
    # on its own: it is one third of a fact whose other two thirds are
    # ``salary_min`` and ``salary_max``, and the model is only ever asked for
    # the text. Setting it alone split the pair — ``score_salary`` computes off
    # the *figures* and prints the *text*, so a model that re-spelled the band
    # produced the sentence "Band ($200,000) clears your 150,000 floor" over a
    # score worked out from the heuristic's 97,000. A number the candidate was
    # never shown, explained by a band that never scored it.
    #
    # So the triple moves together or not at all. The model's spelling is read
    # back for figures through the same extractor the heuristics used; when it
    # yields none — which is the ordinary case for "Salary: 155000", where the
    # pay word that licensed the bare number stayed behind in the description —
    # the heuristic's whole triple is kept rather than half-replaced.
    salary_text = data.get("salary_text")
    if isinstance(salary_text, str) and salary_text.strip():
        cleaned = re.sub(r"\s+", " ", salary_text.strip())[:255]
        _text, low, high = extract_salary(cleaned)
        if low is not None or high is not None:
            merged.salary_text = cleaned
            merged.salary_min, merged.salary_max = low, high
        elif merged.salary_min is None and merged.salary_max is None:
            # Nothing to contradict: the heuristics found no band either, so the
            # model's phrasing is the only description of the pay there is.
            merged.salary_text = cleaned

    if isinstance(data.get("remote"), bool):
        merged.remote = data["remote"]

    seniority = data.get("seniority")
    if isinstance(seniority, str) and seniority.lower() in _ALLOWED_SENIORITY:
        merged.seniority = seniority.lower()

    years = data.get("years_required")
    if isinstance(years, int) and 0 < years <= 40:
        merged.years_required = years

    required = [s.lower() for s in _str_list(data.get("required_skills"), 20)]
    if required:
        merged.required_skills = required
    preferred = [
        s.lower() for s in _str_list(data.get("preferred_skills"), 15) if s.lower() not in required
    ]
    if preferred:
        merged.preferred_skills = preferred

    responsibilities = _str_list(data.get("responsibilities"), 8)
    if responsibilities:
        merged.responsibilities = responsibilities

    industry = data.get("industry")
    if isinstance(industry, str) and industry.strip():
        merged.industry = industry.strip().lower()[:60]

    # Keywords stay derived from the text: they feed ATS matching, where the
    # posting's own vocabulary matters more than the model's paraphrase.
    merged.keywords = sorted(
        {*merged.keywords, *merged.required_skills, *merged.preferred_skills}
    )[:40]
    merged.parsed_with = "llm"
    return merged


def enrich_with_llm(text: str, base: ParsedJob) -> ParsedJob:
    """Ask a free model on the provider chain to sharpen the heuristic parse."""
    if not llm_is_configured():
        return base
    try:
        raw = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_LLM_SYSTEM_PROMPT)},
                {
                    "role": "user",
                    # The whole message is a posting: either scraped off a page
                    # or pasted in from one. What comes back is not display
                    # copy — `required_skills` feeds the fit scorer, and
                    # `keywords` feeds the ATS matching that decides which jobs
                    # the user is shown at all.
                    "content": untrusted.fence(text[:6000], label="job posting"),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.0,
            max_tokens=900,
        )
    except OpenRouterError as exc:
        logger.info("jd enrichment skipped: %s", exc)
        return base

    data = extract_json_object(raw)
    return _merge_llm(base, data) if data else base


def parse_job(
    text: str, *, page_title: str | None = None, use_llm: bool = True
) -> ParsedJob:
    """Parse a job description; heuristics always run, the LLM only refines."""
    base = parse_heuristic(text, page_title)
    if not use_llm:
        return base
    return enrich_with_llm(text, base)


def parse_job_input(
    *, description: str | None = None, url: str | None = None, use_llm: bool = True
) -> ParsedJob:
    """Parse from whichever the caller supplied, preferring pasted text.

    Pasted text wins over a URL because it is what the user actually saw — a
    fetch can silently land on a cookie wall and parse *that* instead.
    """
    page_title: str | None = None
    text = (description or "").strip()
    if not text:
        if not url:
            raise JobFetchError("Provide a job description or a job URL")
        text, page_title = fetch_job_text(url)
    return parse_job(text, page_title=page_title, use_llm=use_llm)


__all__ = [
    "JobFetchError",
    "ParsedJob",
    "extract_keywords",
    "fetch_job_text",
    "jd_hash",
    "parse_heuristic",
    "parse_job",
    "parse_job_input",
]
