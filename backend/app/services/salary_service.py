"""Salary intelligence — what the market pays, next to what the posting offers.

A band on a job ad is only useful relative to something. "$120k–$150k" is a good
offer for a mid-level frontend role in Lisbon and a poor one for a staff
infrastructure role in San Francisco, and the candidate reading the feed rarely
has both numbers in their head.

**The estimate is a deterministic model, not a scrape.** Three multiplied
factors: a base median per role family, a seniority multiplier, and a cost-of-
market multiplier for the location. That choice is deliberate:

* it works on a fresh install with no keys and no network,
* the same role in the same city always returns the same band, which matters
  because a candidate uses it to decide whether to negotiate,
* levels.fyi and Glassdoor both prohibit scraping in their terms, and the
  research rules for this product (§3.3) say we don't do that.

The trade-off is honest and surfaced: rows are stored with ``source="modelled"``
and ``sample_size=0``, and the UI says "estimated" rather than quoting a survey.
:class:`~app.models.salary_benchmark.SalaryBenchmark` has the columns a real
data source would fill, so swapping one in later is a change of writer, not of
schema or of every read site.

Bands are stored in USD regardless of where the role is. Converting the model's
output into local currency would imply a precision it doesn't have, and the
comparison the candidate wants — "is this offer low?" — needs both numbers in
the same unit anyway.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.salary_benchmark import SalaryBenchmark
from app.services.jd_parser import extract_salary
from app.services.places import fold_diacritics
from app.services.resume_parser import marker_pattern

logger = logging.getLogger(__name__)

# Base annual median in USD for a **mid-level** role in a baseline US market.
# Ordered most-specific first: the first family whose keywords hit wins, so
# "machine learning engineer" is never classified as a plain engineer.
_ROLE_FAMILIES: tuple[tuple[str, str, int, tuple[str, ...]], ...] = (
    ("engineering_manager", "Engineering Manager", 185_000,
     ("engineering manager", "director of engineering", "vp of engineering",
      "head of engineering", "cto")),
    ("ml_engineer", "Machine Learning Engineer", 158_000,
     ("machine learning", "ml engineer", "deep learning", "nlp engineer",
      "ai engineer", "research scientist")),
    ("security_engineer", "Security Engineer", 148_000,
     ("security engineer", "application security", "appsec", "infosec",
      "penetration test", "security analyst")),
    ("devops_sre", "DevOps / SRE", 142_000,
     ("site reliability", "devops", "platform engineer", "infrastructure engineer",
      "cloud engineer", "kubernetes engineer")),
    ("data_engineer", "Data Engineer", 138_000,
     ("data engineer", "analytics engineer", "etl", "data platform")),
    ("data_scientist", "Data Scientist", 136_000,
     ("data scientist", "quantitative", "statistician")),
    ("product_manager", "Product Manager", 142_000,
     ("product manager", "product owner", "technical program manager",
      "program manager")),
    ("mobile_engineer", "Mobile Engineer", 130_000,
     ("ios engineer", "android engineer", "mobile engineer", "react native",
      "flutter", "swift developer", "kotlin developer")),
    ("backend_engineer", "Backend Engineer", 132_000,
     ("backend", "back end", "back-end", "server side", "api engineer")),
    ("frontend_engineer", "Frontend Engineer", 122_000,
     ("frontend", "front end", "front-end", "ui engineer", "react developer",
      "javascript developer", "web developer")),
    ("fullstack_engineer", "Full-stack Engineer", 128_000,
     ("full stack", "fullstack", "full-stack")),
    ("qa_engineer", "QA Engineer", 102_000,
     ("qa engineer", "quality assurance", "test engineer", "sdet", "automation test")),
    ("designer", "Product Designer", 118_000,
     ("product designer", "ux designer", "ui designer", "design lead",
      "graphic designer", "user experience")),
    ("data_analyst", "Data Analyst", 96_000,
     ("data analyst", "business analyst", "bi analyst", "reporting analyst")),
    ("sales", "Sales", 98_000,
     ("account executive", "sales representative", "sales manager",
      "business development", "sdr", "bdr")),
    ("marketing", "Marketing", 92_000,
     ("marketing", "growth", "seo", "content strategist", "brand manager")),
    ("recruiter", "Recruiter", 88_000,
     ("recruiter", "talent acquisition", "sourcer", "people operations")),
    ("support", "Customer Support", 62_000,
     ("customer support", "customer success", "technical support", "help desk")),
    ("finance", "Finance", 104_000,
     ("accountant", "financial analyst", "controller", "bookkeeper", "finance manager")),
    # The catch-all for anything with "engineer" or "developer" in it.
    ("software_engineer", "Software Engineer", 126_000,
     ("software engineer", "software developer", "programmer", "engineer", "developer")),
)

_GENERIC_FAMILY = ("generic", "Professional", 88_000)

# Tokens that are only ever an acronym, and so must match a whole word or
# nothing. Every one of them is a substring of an ordinary English word, and
# bare-substring matching turned that into money: "cto" is inside *director*,
# *contractor*, *vector*, *inspector* and *collector*, so "Debt Collector" was
# read as an Engineering Manager — and, since the exec seniority list carries
# "cto" too, as an *executive* one. That is a $62k role modelled at a $516k
# median in San Francisco, whose real $70–85k band the card then called "85%
# below the market median". "seo" is inside *Seoul*, which priced
# "Software Engineer, Seoul" as Marketing.
_WHOLE_WORD_ONLY = frozenset({"cto", "cfo", "ceo", "seo", "etl", "sdr", "bdr", "sdet"})


def _matcher(words: tuple[str, ...]) -> re.Pattern[str]:
    """A matcher for *words* that will not fire in the middle of a longer word.

    Three shapes, because the lists hold three kinds of token:

    * anything already hand-anchored with a space (``" iii"``, ``"vp "``,
      ``" 3"``) carries its own boundaries and is kept verbatim — the callers
      pad the haystack with spaces so those mean exactly what they say;
    * an acronym from :data:`_WHOLE_WORD_ONLY` is bounded on both sides;
    * every other word gets a leading boundary and a free tail, so "engineer"
      still matches "Engineering" and "designer" still matches "Designers".
      The leading ``\\b`` is what does the work — there is none before the
      "cto" in "director".
    """
    parts = []
    for word in words:
        # Folded, because the haystack is: see :func:`_haystack`. A needle
        # written with an accent would otherwise be the one spelling this
        # matcher could never see.
        word = fold_diacritics(word)
        if word != word.strip():
            parts.append(re.escape(word))
        elif word in _WHOLE_WORD_ONLY:
            parts.append(rf"\b{re.escape(word)}\b")
        else:
            parts.append(rf"\b{re.escape(word)}\w*")
    return re.compile("|".join(parts))


_FAMILY_MATCHERS: tuple[tuple[str, str, int, re.Pattern[str]], ...] = tuple(
    (key, label, base, _matcher(words)) for key, label, base, words in _ROLE_FAMILIES
)

# Applied to the family's mid-level base.
_SENIORITY_MULTIPLIER = {
    "junior": 0.66,
    "mid": 1.0,
    "senior": 1.32,
    "lead": 1.58,
    "exec": 2.05,
}

_SENIORITY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("exec", ("chief", "cto", "cfo", "ceo", "vp ", "vice president", "head of",
              "director")),
    ("lead", ("staff", "principal", "lead", "architect", "manager", "distinguished")),
    ("senior", ("senior", "sr.", "sr ", "snr", " iii", " iv", " 3", " 4")),
    ("junior", ("junior", "jr.", "jr ", "intern", "graduate", "entry level",
                "entry-level", "trainee", "apprentice", " i ", " 1")),
)

_SENIORITY_MATCHERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (level, _matcher(patterns)) for level, patterns in _SENIORITY_PATTERNS
)

# Cost-of-market multipliers, relative to a US national baseline of 1.0.
# Keyed by market, matched on city/region/country words in the location string.
_MARKETS: tuple[tuple[str, str, float, tuple[str, ...]], ...] = (
    ("us_bay_area", "San Francisco Bay Area", 1.36,
     ("san francisco", "bay area", "palo alto", "mountain view", "san jose",
      "sunnyvale", "menlo park", "cupertino", "oakland")),
    ("us_nyc", "New York City", 1.26, ("new york", "nyc", "manhattan", "brooklyn")),
    ("us_seattle", "Seattle", 1.20, ("seattle", "bellevue", "redmond")),
    ("us_boston", "Boston", 1.13, ("boston", "cambridge, ma", "massachusetts")),
    ("us_la", "Los Angeles / San Diego", 1.12,
     ("los angeles", "san diego", "santa monica", "irvine")),
    # Washington the state, before Washington the city. "Tacoma, Washington" was
    # priced as the DC market at 1.10, and "Vancouver, WA" reached the
    # `ca_major` row below and was priced against a Canadian band at 0.80 — a US
    # salary reported as a fifth above market by a number a candidate reads
    # while deciding whether to negotiate. Seattle and its suburbs match above
    # this row and keep their own market.
    ("us_other", "United States", 0.97,
     ("washington state", ", wa", ", washington", "spokane", "tacoma")),
    ("us_dc", "Washington DC", 1.10, ("washington", "arlington", "bethesda", "reston")),
    ("us_austin", "Austin", 1.06, ("austin", "dallas", "houston", "texas")),
    ("us_chicago", "Chicago", 1.05, ("chicago", "illinois")),
    ("us_denver", "Denver", 1.03, ("denver", "boulder", "colorado")),
    # "us" and not " us". The leading space was written as an anchor — it is
    # how `_matcher` above spells one, and that function keeps such a needle
    # verbatim — but this table is compiled by `marker_pattern`, which anchors
    # on ``\b`` and *splits the needle on whitespace first*. The space was
    # therefore dropped and nothing replaced it on the left: the compiled
    # pattern was ``us\b``, which matches the end of any word.
    #
    # So every location whose string carries a word ending in "us" was priced
    # as the United States, and this row sits above every non-US market in the
    # table, so it won before the real one was ever tried. "Vilnius, Lithuania"
    # is Central & Eastern Europe at 0.48 and was modelled at 0.97 — the market
    # median doubled, which tells a candidate an ordinary Lithuanian offer is
    # half what the market pays. "Aarhus, Denmark", "Nicosia, Cyprus",
    # "Piraeus, Greece" and "Minsk, Belarus" all read as American the same way.
    ("us_other", "United States", 0.97,
     ("united states", "usa", "u.s.", "us", "atlanta", "miami", "phoenix",
      "portland", "minneapolis", "nashville", "raleigh", "detroit")),
    ("ch", "Switzerland", 1.22, ("zurich", "zürich", "geneva", "switzerland", "basel")),
    ("uk_london", "London", 0.86, ("london",)),
    ("uk_other", "United Kingdom", 0.70,
     # ", wales" and not "wales": Sydney is in New South Wales, and priced as a
     # British city it lost 18% of its band.
     ("united kingdom", "england", "scotland", ", wales", "manchester", "bristol",
      "edinburgh", "leeds", "cambridge, uk")),
    ("ie", "Ireland", 0.82, ("dublin", "ireland", "cork")),
    ("nl", "Netherlands", 0.80, ("amsterdam", "netherlands", "utrecht", "rotterdam")),
    ("de_munich", "Munich", 0.79, ("munich", "münchen", "stuttgart", "frankfurt")),
    ("de_berlin", "Berlin", 0.73, ("berlin", "germany", "deutschland", "hamburg",
                                   "cologne", "köln", "leipzig")),
    ("fr", "France", 0.72, ("paris", "france", "lyon", "toulouse")),
    ("nordics", "Nordics", 0.82,
     ("stockholm", "sweden", "copenhagen", "denmark", "oslo", "norway",
      "helsinki", "finland")),
    ("ca_major", "Canada", 0.80,
     ("toronto", "vancouver", "montreal", "canada", "ottawa", "waterloo")),
    ("au_nz", "Australia / NZ", 0.88,
     ("sydney", "melbourne", "australia", "brisbane", "auckland", "new zealand")),
    ("es_pt_it", "Southern Europe", 0.54,
     ("madrid", "barcelona", "spain", "lisbon", "porto", "portugal", "milan",
      "rome", "italy", "athens", "greece")),
    ("cee", "Central & Eastern Europe", 0.48,
     ("warsaw", "krakow", "poland", "prague", "czech", "bucharest", "romania",
      "budapest", "hungary", "sofia", "bulgaria", "belgrade", "serbia",
      "ukraine", "kyiv", "lithuania", "vilnius", "estonia", "tallinn")),
    ("sg_hk", "Singapore / Hong Kong", 0.84, ("singapore", "hong kong")),
    ("jp_kr", "Japan / Korea", 0.70, ("tokyo", "japan", "seoul", "korea")),
    ("in", "India", 0.26, ("bangalore", "bengaluru", "india", "hyderabad", "pune",
                           "mumbai", "delhi", "gurgaon", "noida", "chennai")),
    ("latam", "Latin America", 0.34,
     ("brazil", "são paulo", "sao paulo", "mexico", "argentina", "buenos aires",
      "colombia", "bogota", "chile", "santiago", "lima", "peru", "uruguay")),
    # Israel and the Gulf are split out of the row below rather than sharing it.
    # Tel Aviv is one of the highest-paying technology markets outside the US
    # and was priced beside Cairo and Lagos at 0.36, which is not a rough
    # estimate of it — it is the wrong market. An ordinary senior salary there,
    # ₪450,000, came back as "93% above the market median", and this number is
    # read by a candidate deciding whether to negotiate: it told them to take an
    # ordinary offer as an exceptional one. Dubai reported 114% for the same
    # reason.
    #
    # The multipliers are estimates like every other row in this table, and
    # deliberately conservative ones — Israel beside Australia/NZ, the Gulf
    # beside France — because overstating a market costs the candidate the same
    # way in the other direction.
    ("il", "Israel", 0.88, ("israel", "tel aviv", "herzliya", "haifa")),
    ("gulf", "Gulf states", 0.72,
     ("dubai", "abu dhabi", "uae", "united arab emirates", "qatar", "doha",
      "saudi", "riyadh")),
    ("mena_africa", "MENA / Africa", 0.36,
     ("egypt", "cairo", "nigeria", "lagos", "kenya", "nairobi",
      "south africa", "cape town", "johannesburg", "morocco")),
)

#: Every needle above as a whole-word pattern. Matched as substrings, "rome"
#: found Romeoville, Illinois and "india" found Indianapolis — so a US salary
#: was compared against a Southern European band at 0.54 and an Indian one at
#: 0.26. That number is read by a candidate deciding whether to negotiate.
#:
#: Needles carrying punctuation keep it: "cambridge, ma" and "cambridge, uk"
#: are two different cities and the comma is what tells them apart.
_MARKET_MATCHERS: tuple[tuple[str, str, float, tuple[re.Pattern[str], ...]], ...] = tuple(
    (key, label, multiplier, tuple(marker_pattern(fold_diacritics(n)) for n in needles))
    for key, label, multiplier, needles in _MARKETS
)


# US states, as a location string's last word. A posting that says "Rome, GA"
# names a town in Georgia, and matched on the city name alone it was priced as
# Southern Europe at 0.54; "Paris, TX" as France, "Berlin, NH" as Germany,
# "Lima, OH" as Latin America. The same suffix also rescues the many US cities
# no row above names at all — "Charlotte, NC" was simply unknown.
_US_STATES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
    "al", "ak", "az", "ct", "dc", "fl", "ga", "hi", "ia", "id", "ks", "ky",
    "la", "me", "mi", "mn", "mo", "ms", "mt", "nc", "nd", "ne", "nh", "nj",
    "nm", "nv", "ny", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx",
    "ut", "va", "vt", "wa", "wi", "wv", "wy",
)

# The seven codes left out of the list above, because each is also the country
# code of a market in this table: CA/Canada, DE/Germany, IN/India, IL/Israel,
# AR/Argentina, CO/Colombia, MA/Morocco. "Berlin, DE" is priced as Germany here
# and "Tel Aviv, IL" as Israel. They still read as their state when nothing
# above claimed the string: "Wilmington, DE" and "Springfield, IL" have no
# foreign city in them.
#
# This is deliberately *not* what `places.locate` does with the same string.
# That function resolves "Berlin, DE" to Delaware, because it answers a
# different question — is this the same place as that one — where reading an
# American board's mail the American way is the safe default, and where
# `_contradicts` re-opens the ambiguity before anything acts on it. Pricing has
# no second look: the multiplier is the answer, and here the two readings are a
# third apart — 0.73 for Berlin against 0.97 for the US. So the tie is broken on
# whichever reading the rest of the string supports rather than on a default:
# a city this table names outright wins, and otherwise the code is the state.
_AMBIGUOUS_STATE_CODES = ("ca", "de", "in", "il", "ar", "co", "ma")

#: A US postcode, which is what a board writes straight after the state.
#:
#: Five digits, with a digit check on each side, and both of those are load
#: bearing. The other thing that occupies this position is a *foreign*
#: postcode, and reading one as American would hand the string to the state
#: table — the very failure this fragment exists to undo, run backwards. An
#: Indian PIN is six digits ("Chennai, TN 600001" is Tamil Nadu, not
#: Tennessee) and an Australian postcode is four ("Perth, WA 6000" is Western
#: Australia); without the lookarounds, ``\d{5}`` finds five of a PIN's six
#: and prices Chennai as an American city at a third of the market.
_US_POSTCODE = r"(?<!\d)\d{5}(?:-\d{4})?(?!\d)"

#: What a location string may carry between its state and its end.
#:
#: Both patterns below anchor on ``$``, because a state is only a *suffix* when
#: it is one: a "GA" in the middle of a sentence is not Georgia. But the thing
#: a board routinely writes after the state is not the sentence the anchor is
#: guarding against — it is the **postcode**, and the postcode defeated the
#: whole mechanism.
#:
#: Indeed's location field is "Charlotte, NC 28202" and always has been, and
#: every posting syndicated out of it carries the number. So "Rome, GA 30161"
#: was priced as Southern Europe at 0.54 — a modelled median of $90,000
#: against the $161,000 the identical string without its postcode gets — and an
#: ordinary $170,000 offer in Rome, Georgia was reported to the candidate as
#: "89% above the market median", which is the number they read while deciding
#: whether to negotiate. "Lima, OH 45801" came out as Latin America at 0.34,
#: "Dublin, OH 43017" as Ireland and "Paris, TX 75460" as France; "Charlotte,
#: NC 28202", which no row names, came out as "Unspecified location" at 1.0 —
#: an American salary priced against no market at all. Each is the homonym
#: failure the suffix was added to settle, arriving through the five digits
#: behind it.
#:
#: The country tail was already here and keeps its meaning; it now also admits
#: the spacing a board uses when it writes no comma ("Rome, GA 30161 USA").
#:
#: A *bracketed* tail — "Boise, ID (Hybrid)", "Charlotte, NC (Remote)" — is
#: deliberately still refused, and the reason is the same country tail read the
#: other way round. The other thing a board writes in brackets after the code
#: is where the job is: "Chennai, TN (India)" and "Milan, MI (Italy)" name
#: their own continent in there, and a tail that swallowed whatever sat between
#: the brackets would read those codes as Tennessee and Michigan and price both
#: against the US — 0.97 where Chennai's own market is 0.26. The bracketed
#: arrangement costs 1.0 against 0.97 on an American string; reading a foreign
#: one as American costs a factor of four.
_STATE_TAIL = (
    r"(?:(?:\s*,\s*|\s+)" + _US_POSTCODE + r")?"
    r"(?:(?:\s*,\s*|\s+)(?:usa|u\.s\.a\.|u\.s\.|us|united states))?"
    r"\s*$"
)

_US_STATE_SUFFIX = re.compile(
    r",\s*(?:" + "|".join(_US_STATES + _AMBIGUOUS_STATE_CODES) + r")\.?" + _STATE_TAIL,
    re.I,
)
_UNAMBIGUOUS_US_STATE_SUFFIX = re.compile(
    r",\s*(?:" + "|".join(_US_STATES) + r")\.?" + _STATE_TAIL,
    re.I,
)

_US_OTHER = ("us_other", "United States", 0.97)

# Fully-remote roles are usually priced against a company's home market rather
# than the candidate's city. Slightly under the US baseline is the honest middle.
_REMOTE_MARKET = ("remote", "Remote", 0.95)
_UNKNOWN_MARKET = ("unknown", "Unspecified location", 1.0)

_REMOTE_WORDS = ("remote", "anywhere", "worldwide", "distributed", "work from home")

# How wide the band is around the median. Junior pay is tightly banded; senior
# and above spreads out as equity and level bands widen.
_SPREAD = {
    "junior": (0.86, 1.18),
    "mid": (0.84, 1.22),
    "senior": (0.82, 1.28),
    "lead": (0.80, 1.34),
    "exec": (0.76, 1.45),
}

# How far from the median an offer has to be before the card calls it.
_MATERIAL_DELTA = 0.08


# --------------------------------------------------------------------------- #
# Classification                                                               #
# --------------------------------------------------------------------------- #


def _haystack(value: str | None) -> str:
    """*value* as the tables below are written: lowercase, unaccented, padded.

    Every needle in this module is spelled in ASCII, and ``marker_pattern`` and
    :func:`_matcher` both anchor on ``\\b`` — which Python evaluates over the
    Unicode word class, so an accented letter is a word character and carries no
    boundary beside its unaccented neighbours. ``\\bmontreal\\b`` therefore matched
    "Montreal" and not "Montréal", and the miss was silent in both directions
    this module is read in:

    * a location that named no market took ``_UNKNOWN_MARKET``'s multiplier of
      1.0, so "Montréal" was modelled a quarter above the Canadian band the same
      city gets when a board spells it without the accent, and "Kraków" more
      than twice above its own;
    * a title whose level word carried an accent lost the level rather than the
      role — "Développeur Backend Sénior" classified as a backend engineer at
      *mid*, which is 32% under the band that title actually describes.

    Both numbers are read by a candidate deciding whether to negotiate, and both
    changed with nothing but the spelling a board happened to use.

    The comparison key is folded, never a value: nothing here returns the string
    it was given, only a row from a table, so folding cannot reach anything the
    user sees.

    A few needles were added with their accents over time — "zürich",
    "münchen", "köln", "são paulo" — because the ASCII spelling beside them did
    not match. They fold to the same key as the haystack now and go on working;
    they are simply no longer the only accented cities that do.
    """
    return f" {_folded(value)} "


def _folded(value: str | None) -> str:
    """The same normalisation without the padding — what a ``$``-anchored
    pattern such as :data:`_US_STATE_SUFFIX` has to be matched against."""
    return re.sub(r"\s+", " ", fold_diacritics((value or "").lower())).strip()


def classify_role(title: str | None, description: str | None = None) -> tuple[str, str, int]:
    """``(family_key, label, base_median_usd)`` for a job title.

    The title decides. The description is only consulted when the title is
    uselessly generic ("Engineer", "Analyst") — job ads are full of stack
    keywords, and letting them outvote a clear title turns a backend role into
    whatever tool the posting mentions most.
    """
    haystack = _haystack(title)
    for key, label, base, matcher in _FAMILY_MATCHERS:
        if matcher.search(haystack):
            return key, label, base

    if description:
        body = _haystack(description[:4000])
        for key, label, base, matcher in _FAMILY_MATCHERS:
            if matcher.search(body):
                return key, label, base

    return _GENERIC_FAMILY


def classify_seniority(title: str | None, *, fallback: str = "mid") -> str:
    """Seniority implied by a title, defaulting to mid.

    Mid is the right default rather than junior: most postings that don't say a
    level are mid-level, and guessing low would flatter every band on the page.
    """
    haystack = _haystack(title)
    for level, matcher in _SENIORITY_MATCHERS:
        if matcher.search(haystack):
            return level
    return fallback if fallback in _SENIORITY_MULTIPLIER else "mid"


def _first_market(text: str, *, us_only: bool = False) -> tuple[str, str, float] | None:
    """The first market whose needles appear in *text*, in table order."""
    for key, label, multiplier, matchers in _MARKET_MATCHERS:
        if us_only and not key.startswith("us_"):
            continue
        if any(m.search(text) for m in matchers):
            return key, label, multiplier
    return None


def _in_the_united_states(text: str, match: tuple[str, str, float] | None) -> bool:
    """Whether a US state on the end of *text* should outrank *match*.

    An unambiguous state always does. One of the seven codes that doubles as a
    country code only does when no foreign city claimed the string, so "Berlin,
    DE" stays in Germany while "Wilmington, DE" is in Delaware.
    """
    if _UNAMBIGUOUS_US_STATE_SUFFIX.search(text):
        return True
    return match is None and bool(_US_STATE_SUFFIX.search(text))


def classify_market(location: str | None, *, remote: bool | None = None) -> tuple[str, str, float]:
    """``(market_key, label, multiplier)`` for a location string."""
    text = _folded(location)
    if not text:
        return _REMOTE_MARKET if remote else _UNKNOWN_MARKET

    # A location that names a city *and* says remote is priced on the city: the
    # employer anchored it somewhere, which is what sets the band.
    match = _first_market(text)
    if match and match[0].startswith("us_"):
        return match

    # The city name lost, or found nothing. A US state on the end of the string
    # outranks either: "Rome, GA" is in Georgia and "Charlotte, NC" is in the US
    # even though no row above names it.
    if _in_the_united_states(text, match):
        return _first_market(text, us_only=True) or _US_OTHER

    if match:
        return match

    if remote or any(w in text for w in _REMOTE_WORDS):
        return _REMOTE_MARKET
    return _UNKNOWN_MARKET


# --------------------------------------------------------------------------- #
# Estimation                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class SalaryBand:
    """A modelled market range for one role family in one market."""

    role_family: str
    role_label: str
    seniority: str
    location_key: str
    location_label: str
    currency: str
    minimum: int
    median: int
    maximum: int
    source: str = "modelled"
    sample_size: int = 0

    @property
    def is_estimate(self) -> bool:
        return self.source == "modelled"

    def as_dict(self) -> dict:
        return {
            "role_family": self.role_family,
            "role_label": self.role_label,
            "seniority": self.seniority,
            "location_key": self.location_key,
            "location_label": self.location_label,
            "currency": self.currency,
            "min": self.minimum,
            "median": self.median,
            "max": self.maximum,
            "source": self.source,
            "sample_size": self.sample_size,
        }


def _round_to(value: float, step: int = 1000) -> int:
    return int(round(value / step) * step)


def estimate_band(
    title: str | None,
    location: str | None = None,
    *,
    remote: bool | None = None,
    description: str | None = None,
    seniority: str | None = None,
) -> SalaryBand:
    """Model the market range for a role, deterministically.

    ``base median × seniority × market``, then a seniority-dependent spread
    around it. No network, no keys, same answer every time.
    """
    family, role_label, base = classify_role(title, description)
    level = (
        seniority.lower()
        if seniority and seniority.lower() in _SENIORITY_MULTIPLIER
        else classify_seniority(title)
    )
    market_key, market_label, multiplier = classify_market(location, remote=remote)

    median = base * _SENIORITY_MULTIPLIER[level] * multiplier
    low, high = _SPREAD[level]
    return SalaryBand(
        role_family=family,
        role_label=role_label,
        seniority=level,
        location_key=market_key,
        location_label=market_label,
        currency="USD",
        minimum=_round_to(median * low),
        median=_round_to(median),
        maximum=_round_to(median * high),
    )


# --------------------------------------------------------------------------- #
# Comparison                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class SalaryComparison:
    """What a posting's own band says next to the market band."""

    offered_min: int | None
    offered_max: int | None
    offered_mid: int | None
    # -1..+n as a fraction of the market median: 0.12 means 12% above market.
    delta: float | None
    # above | at | below | unknown
    verdict: str
    label: str

    def as_dict(self) -> dict:
        return {
            "offered_min": self.offered_min,
            "offered_max": self.offered_max,
            "offered_mid": self.offered_mid,
            "delta": None if self.delta is None else round(self.delta, 4),
            "verdict": self.verdict,
            "label": self.label,
        }


def parse_offered(salary_text: str | None) -> tuple[int | None, int | None]:
    """Pull ``(annual min, annual max)`` out of a posting's salary string.

    Reuses the JD parser's extractor so a band reads the same here as it does on
    the tailoring page — including the pay period, which that extractor now
    annualises (see :func:`~app.services.jd_parser.extract_salary`).

    This function used to refuse any string containing "hour", "month" or
    "week", on the reasoning that annualising needs an assumption about hours we
    would be inventing. The assumption was worth making after all: 2080 hours is
    the figure the recruiter quoting the rate is using too, and refusing was not
    free. A rejected band returns ``(None, None)``, which :func:`meets_floor`
    reads as "the employer published nothing" and waves through — so a $40/hr
    role and a $200/hr role were equally invisible to a candidate's salary
    floor, and :func:`compare_to_market` had nothing to compare and said so on
    every contract posting in the feed.
    """
    if not salary_text:
        return None, None
    _text, low, high = extract_salary(salary_text)
    return as_band(low, high)


def as_band(low: int | None, high: int | None) -> tuple[int | None, int | None]:
    """A parsed ``(low, high)`` as the pair every reader downstream expects.

    Split out of :func:`parse_offered` so a caller that has *already* parsed a
    posting can reach this without re-reading a display string — see
    :func:`app.services.job_search_service._store_matches`, where re-reading one
    was throwing a published band away.

    A single figure ("$150,000") is the whole band we know about, and it fills
    both ends. That is not cosmetic: :func:`meets_floor` is handed the *top* of
    the band, and a top of ``None`` reads as "the employer published nothing"
    and waves the posting through whatever floor the candidate set.

    Tested with ``is None`` rather than ``high or low``: those differ only when
    ``high`` is 0, and reading a zero as "no upper figure" is how a band becomes
    (0, 0).
    """
    if low is None and high is None:
        return None, None
    return low, high if high is not None else low


def meets_floor(salary_max: int | None, floor: int | None) -> bool:
    """Whether an advertised band clears a candidate's salary floor.

    Two decisions are baked in, and both are deliberately generous to the
    posting:

    * **An unpublished band always passes.** Most employers advertise no salary
      at all, so screening those out would empty the feed for anyone who set a
      floor. A floor is there to drop the roles that are provably too junior,
      not everything that declined to mention money.
    * **The top of the band is what's compared.** "$120k–$160k" clears a $150k
      floor, because $150k is inside what they said they would pay. Comparing
      the bottom would reject every range that straddles the number the
      candidate asked for, which is exactly the set worth negotiating over.
    """
    if not floor:
        return True
    if salary_max is None:
        return True
    return salary_max >= floor


def compare_to_market(
    band: SalaryBand,
    salary_text: str | None,
    *,
    offered: tuple[int | None, int | None] | None = None,
) -> SalaryComparison:
    """Position a posting's advertised pay against the modelled median.

    ``offered`` is that posting's band **as it was already parsed and stored**,
    and a caller holding one should pass it. Re-reading the display string is
    the fallback, for a row that has no figures on it.

    The two are not interchangeable, and this function re-deriving the band was
    the last place in the product that still assumed they were — the lesson
    :func:`app.services.job_search_service._store_matches` learned about
    carrying figures rather than re-reading text, arriving on the read path.
    A display string is the band alone: the words around it that licensed and
    qualified it stayed behind in the description, and two kinds of posting
    were mispriced by exactly that.

    A **cued bare figure** loses the cue. "Salary: 155000" parses here and
    stores 155,000, and the string that gets stored is "155000" — which
    :func:`app.services.jd_parser.extract_salary` will not read as money a
    second time, because a lone unmarked number is a headcount as often as it
    is pay. So a row carrying a band was reported as "This posting doesn't
    publish a salary", on the same card that prints the band.

    A **ceiling** used to lose its qualifier, and that one fabricates rather
    than drops: "Up to $150,000" is stored with ``salary_min`` NULL and was
    read back as a floor of $150,000 — the employer's ceiling reported to the
    candidate as the number they would not go below.
    :func:`app.services.jd_parser._with_ceiling` now keeps those words in the
    display string, so the round-trip is faithful for rows stored from here on;
    passing the stored figures is what makes it faithful for the rows already
    in the table.
    """
    low, high = offered if offered is not None else parse_offered(salary_text)
    if low is None and high is None:
        return SalaryComparison(
            offered_min=None,
            offered_max=None,
            offered_mid=None,
            delta=None,
            verdict="unknown",
            label="This posting doesn't publish a salary.",
        )

    # `is not None`, not truthiness. `parse_offered` no longer yields a zero
    # figure, so this can no longer empty the list — but the averaging is wrong
    # either way if it ever did: dropping a 0 from ("$0 - $150,000") reports the
    # midpoint as 150,000 rather than 75,000, which is the top of the band sold
    # as its middle. Guarded on emptiness as well, because the cost of being
    # wrong here is a 500 on a page rendering someone else's scraped text.
    values = [v for v in (low, high) if v is not None]
    if not values:
        return SalaryComparison(
            offered_min=None,
            offered_max=None,
            offered_mid=None,
            delta=None,
            verdict="unknown",
            label="This posting doesn't publish a salary.",
        )
    mid = int(sum(values) / len(values))
    delta = (mid - band.median) / band.median if band.median else 0.0

    if delta >= _MATERIAL_DELTA:
        verdict = "above"
        label = f"About {abs(delta) * 100:.0f}% above the market median for this role."
    elif delta <= -_MATERIAL_DELTA:
        verdict = "below"
        label = f"About {abs(delta) * 100:.0f}% below the market median for this role."
    else:
        verdict = "at"
        label = "In line with the market median for this role."

    return SalaryComparison(
        offered_min=low,
        offered_max=high,
        offered_mid=mid,
        delta=delta,
        verdict=verdict,
        label=label,
    )


# --------------------------------------------------------------------------- #
# Persistence                                                                  #
# --------------------------------------------------------------------------- #


def _to_band(row: SalaryBenchmark) -> SalaryBand:
    return SalaryBand(
        role_family=row.role_family,
        role_label=row.role_label or row.role_family,
        seniority=row.seniority,
        location_key=row.location_key,
        location_label=row.location_label or row.location_key,
        currency=row.currency,
        minimum=row.salary_min,
        median=row.salary_median,
        maximum=row.salary_max,
        source=row.source,
        sample_size=row.sample_size,
    )


def get_benchmark(
    db: Session,
    title: str | None,
    location: str | None = None,
    *,
    remote: bool | None = None,
    description: str | None = None,
    seniority: str | None = None,
) -> SalaryBand:
    """The stored band for this role/level/market, computing it if missing.

    Rows are global and shared across users — the market doesn't vary by who is
    looking — and refreshed once the TTL lapses so a change to the model's tables
    reaches existing buckets after a deploy.

    A row written by a real data source (``source != "modelled"``) is returned
    untouched even when stale: better a survey from last quarter than a model
    overwriting it.
    """
    band = estimate_band(
        title, location, remote=remote, description=description, seniority=seniority
    )
    row = db.scalar(
        select(SalaryBenchmark).where(
            SalaryBenchmark.role_family == band.role_family,
            SalaryBenchmark.seniority == band.seniority,
            SalaryBenchmark.location_key == band.location_key,
        )
    )

    if row is not None:
        if row.source != "modelled" or row.is_fresh(settings.salary_benchmark_ttl_days):
            return _to_band(row)
        row.salary_min = band.minimum
        row.salary_median = band.median
        row.salary_max = band.maximum
        row.role_label = band.role_label
        row.location_label = band.location_label
        row.computed_at = datetime.now(UTC)
        db.commit()
        return band

    row = SalaryBenchmark(
        role_family=band.role_family,
        seniority=band.seniority,
        location_key=band.location_key,
        role_label=band.role_label,
        location_label=band.location_label,
        currency=band.currency,
        salary_min=band.minimum,
        salary_median=band.median,
        salary_max=band.maximum,
        source=band.source,
        sample_size=band.sample_size,
        computed_at=datetime.now(UTC),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Two workers scanning at once can race on the same bucket. The other
        # one's row is just as good — the model is deterministic.
        db.rollback()
        logger.debug("salary benchmark %s already written by a concurrent scan", band.role_family)
    return band


@dataclass
class SalaryInsight:
    """Everything the salary card renders for one posting."""

    band: SalaryBand
    comparison: SalaryComparison

    def as_dict(self) -> dict:
        return {
            "band": self.band.as_dict(),
            "comparison": self.comparison.as_dict(),
            "is_estimate": self.band.is_estimate,
        }


def insight_for_posting(db: Session, posting) -> SalaryInsight:
    """The band and the comparison for one :class:`JobPosting`-shaped row."""
    band = get_benchmark(
        db,
        posting.title,
        posting.location,
        remote=posting.remote,
        description=posting.description,
    )
    # The row's own figures, not a re-reading of its display string. See
    # `compare_to_market`. Falls back to the string when the row has neither —
    # rows predating the band columns, and rows whose text arrived from a
    # provider field that was never parsed.
    stored = (getattr(posting, "salary_min", None), getattr(posting, "salary_max", None))
    return SalaryInsight(
        band=band,
        comparison=compare_to_market(
            band,
            posting.salary_text,
            offered=stored if stored != (None, None) else None,
        ),
    )


__all__ = [
    "SalaryBand",
    "SalaryComparison",
    "SalaryInsight",
    "classify_market",
    "classify_role",
    "classify_seniority",
    "compare_to_market",
    "estimate_band",
    "get_benchmark",
    "insight_for_posting",
    "as_band",
    "parse_offered",
]
