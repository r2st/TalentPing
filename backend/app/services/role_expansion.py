"""The other names for the job the candidate wrote down.

A candidate writes "AI Engineer". Boards advertise that job as "Machine Learning
Engineer", "ML Engineer", "Applied Scientist", "LLM Engineer". Searching for the
literal string finds a fraction of the market, and the fraction it misses is not
random — it is whichever vocabulary that particular board happens to prefer.

**Deterministic first, model second.** The static table below covers the common
specialisms at no cost and with no dependency. The model is asked only about
roles the table doesn't recognise, and the answer is cached by the caller so it
is one call per profile rather than one per scan.

That ordering is load-bearing rather than merely tidy. In production every LLM
provider rate-limited simultaneously for days; any design whose *discovery* step
depends on a live model call inherits that outage as a silent no-op, and a job
search that quietly stops searching is worse than one that never expanded at all.
With the table first, an outage costs breadth on unusual titles and nothing else.

**Expansion widens the feed, never the gate.** Nothing here reaches
:func:`app.services.auto_apply_service.relevance_gate`, which keeps judging
postings against the roles the candidate actually stated. A wide feed scored
selectively is the shape ``_search_criteria`` already documents; this makes the
feed as wide as that comment always assumed it was.
"""
from __future__ import annotations

import logging
import re

from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion_detailed,
    extract_json_object,
)

logger = logging.getLogger(__name__)

# How many names one stated role may turn into. Enough to cover the real
# synonyms, bounded so a single role can't crowd the whole query.
MAX_PER_ROLE = 6
# Ceiling across the whole expansion, matching the `_union` limit the search
# criteria already apply.
MAX_TOTAL = 24

# The common specialisms, keyed by the role's own standard spelling. Values are
# ordered most- to least-standard, because the caller truncates.
#
# The key is a title rather than a lookup string because it is also an *answer*:
# a candidate who writes "Backend Developer" should be searched for as "Backend
# Engineer" too, and that name lives nowhere else. `_SYNONYMS` below is built
# from this by putting each key at the head of its own list; the "never returns
# the role itself" rule in :func:`synonyms_for` takes it back out again for the
# candidate who wrote the standard spelling in the first place.
_TABLE: dict[str, tuple[str, ...]] = {
    "AI Engineer": (
        "Machine Learning Engineer", "ML Engineer", "AI/ML Engineer",
        "LLM Engineer", "Applied Scientist",
    ),
    "Machine Learning Engineer": (
        "ML Engineer", "AI Engineer", "Applied Scientist",
        "Deep Learning Engineer",
    ),
    "Data Scientist": (
        "Machine Learning Engineer", "Applied Scientist", "Research Scientist",
        "Data Science Engineer",
    ),
    "Data Engineer": (
        "Analytics Engineer", "Big Data Engineer", "ETL Developer",
        "Data Platform Engineer",
    ),
    "Backend Engineer": (
        "Back End Developer", "Backend Developer", "Server Engineer",
        "Software Engineer, Backend", "API Engineer",
    ),
    "Frontend Engineer": (
        "Front End Developer", "Frontend Developer", "UI Engineer",
        "React Developer", "Web Developer",
    ),
    "Full Stack Engineer": (
        "Fullstack Developer", "Full Stack Developer", "Software Engineer",
        "Web Developer",
    ),
    "Software Engineer": (
        "Software Developer", "Backend Engineer", "Full Stack Engineer",
        "Programmer",
    ),
    "DevOps Engineer": (
        "Site Reliability Engineer", "SRE", "Platform Engineer",
        "Infrastructure Engineer", "Cloud Engineer",
    ),
    "Site Reliability Engineer": (
        "SRE", "DevOps Engineer", "Platform Engineer",
        "Infrastructure Engineer",
    ),
    "Platform Engineer": (
        "Infrastructure Engineer", "DevOps Engineer",
        "Site Reliability Engineer", "Cloud Engineer",
    ),
    "Security Engineer": (
        "Application Security Engineer", "Cybersecurity Engineer",
        "InfoSec Engineer", "Security Analyst",
    ),
    "Mobile Engineer": (
        "iOS Engineer", "Android Engineer", "Mobile Developer",
        "React Native Developer",
    ),
    "iOS Engineer": ("iOS Developer", "Mobile Engineer", "Swift Developer"),
    "Android Engineer": ("Android Developer", "Mobile Engineer", "Kotlin Developer"),
    "QA Engineer": (
        "Test Engineer", "QA Automation Engineer", "SDET",
        "Quality Assurance Engineer",
    ),
    # No "Senior Product Manager": it is the same name with a seniority word on
    # it, which `normalise` strips — so it was never a distinct search, and
    # `expand_roles` deduped it against the candidate's own role every time.
    # Offering it to someone who wrote "Product Owner" would have been worse
    # than useless: a claim about their level that they did not make.
    "Product Manager": ("Technical Product Manager", "Product Owner"),
    "Engineering Manager": (
        "Software Engineering Manager", "Development Manager", "Tech Lead",
    ),
    "Designer": (
        "Product Designer", "UX Designer", "UI Designer", "UX/UI Designer",
    ),
    "Product Designer": ("UX Designer", "UI Designer", "Designer"),
    "Data Analyst": (
        "Business Analyst", "Analytics Engineer", "BI Analyst",
        "Business Intelligence Analyst",
    ),
}

# Seniority words stripped before looking a role up, so "Senior AI Engineer" and
# "AI Engineer" reach the same table entry. Seniority is the fit scorer's
# business; it has no bearing on what a job is *called*.
_SENIORITY_RE = re.compile(
    r"\b(?:senior|sr\.?|junior|jr\.?|lead|principal|staff|entry[- ]level|"
    r"mid[- ]level|associate|head\s+of|chief|vp\s+of)\b",
    re.I,
)


def normalise(role: str) -> str:
    """A role stripped to the form two spellings of one title are compared on."""
    text = _SENIORITY_RE.sub(" ", role or "")
    text = re.sub(r"[^\w\s/+-]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


# The vocabulary fold, applied when *finding* a table row and nowhere else.
#
# Half the market advertises the same job with the other craft noun. The fit
# scorer has always known this — :data:`app.services.fit_scorer._TITLE_SYNONYMS`
# maps developer, dev, programmer and coder onto "engineer", and
# :data:`~app.services.fit_scorer._TITLE_PHRASES` folds "back end" onto
# "backend" — so ``title_relevance("Backend Developer", "Backend Engineer")`` is
# 1.0: the same role, respelt.
#
# This table did not. It was keyed on the Engineer spelling alone, so a
# candidate who wrote "Backend Developer" — or "Software Developer", or
# "Front-End Engineer", or "Full-Stack Developer" — matched no row, and with the
# model tier off or rate-limited (which the docstring above explains is an
# ordinary state, not an outage) their search went to the boards as the one
# literal string they typed. The product told them the market was thin; what was
# thin was the query. It is the plainest form of the bug this module exists to
# fix, sitting in the module itself.
#
# Deliberately *not* folded into :func:`normalise`. That function is what
# :func:`expand_roles` dedupes on, and the table ships both "Front End Developer"
# and "Frontend Developer" on purpose — boards tokenise them differently, and
# folding them together in `normalise` would silently drop one of the two
# spellings this module went to the trouble of listing.
_COMPOUNDS = (
    (re.compile(r"(?<![a-z])front[ -]?end(?![a-z])"), "frontend"),
    (re.compile(r"(?<![a-z])back[ -]?end(?![a-z])"), "backend"),
    (re.compile(r"(?<![a-z])full[ -]?stack(?![a-z])"), "fullstack"),
)
# Guarded on both sides so "Backendless Engineer" is not read as "back end", and
# so "Development Manager" does not lose "development" to the "dev" alternative.
_CRAFT_RE = re.compile(r"(?<![a-z])(?:developers?|devs?|programmers?|coders?)(?![a-z])")


def _lookup_key(role: str) -> str:
    """The table row *role* names, whichever of its spellings was typed."""
    key = normalise(role)
    for pattern, replacement in _COMPOUNDS:
        key = pattern.sub(replacement, key)
    return _CRAFT_RE.sub("engineer", key)


# A title this table recommends is a title a candidate may equally well have
# typed. "Swift Developer", "Applied Scientist" and "SRE" are all names the
# product puts in front of people, and every one of them used to expand to
# nothing when it came back the other way — the table could not answer for its
# own advice.
#
# Derived rather than hand-written, so it cannot drift from the rows above. Each
# recommendation resolves to the row that makes it, first mention winning; a row
# key is never overwritten, so a title that is a heading in its own right keeps
# its own list.
#
# "Tech Lead" is the one exclusion. `_SENIORITY_RE` strips "lead", which leaves
# "tech" — a word, not a role — and a row reachable by it would answer a
# question nobody asked.
_NO_ALIAS = frozenset({"Tech Lead"})

_SYNONYMS: dict[str, tuple[str, ...]] = {
    _lookup_key(title): (title, *values) for title, values in _TABLE.items()
}
for _title, _values in _TABLE.items():
    for _value in _values:
        if _value in _NO_ALIAS:
            continue
        _SYNONYMS.setdefault(_lookup_key(_value), (_title, *_values))


_SYSTEM_PROMPT = (
    "You map a job title to the other titles employers use for the same job.\n\n"
    "Rules:\n"
    "1. Same job, different vocabulary. Not adjacent jobs, not more senior "
    "versions, not specialisations.\n"
    "2. No seniority words (senior, junior, lead, staff, principal).\n"
    "3. At most 5. Fewer is fine. An unusual title may have none.\n"
    "4. Do not repeat the input.\n\n"
    'Return ONLY a JSON object: {"titles": ["...", "..."]}'
)


def _from_model(role: str) -> list[str]:
    """Ask a model for synonyms. Returns ``[]`` on any failure, always."""
    try:
        completion = chat_completion_detailed(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Job title: {role}"},
            ],
            temperature=0.0,
            max_tokens=300,
        )
    except OpenRouterError as exc:
        logger.info("role expansion for %r fell back to the table: %s", role, exc)
        return []

    data = extract_json_object(completion.text)
    titles = (data or {}).get("titles")
    if not isinstance(titles, list):
        return []

    out: list[str] = []
    for title in titles:
        if not isinstance(title, str):
            continue
        cleaned = re.sub(r"\s+", " ", title).strip()
        # A model that echoes the input, answers with a sentence, or invents a
        # paragraph gets dropped rather than searched for.
        if not cleaned or len(cleaned) > 60 or normalise(cleaned) == normalise(role):
            continue
        out.append(cleaned)
    return out[:MAX_PER_ROLE]


def synonyms_for(role: str, *, use_llm: bool = True) -> list[str]:
    """The other names for one role. Never includes *role* itself.

    "Itself" is judged on :func:`normalise`, not on the lookup key: a candidate
    who wrote "Front-End Developer" is still shown "Front End Developer", which
    is a different string to search a board for. Only the exact title they
    already typed is dropped.
    """
    key = normalise(role)
    if not key:
        return []
    known = _SYNONYMS.get(_lookup_key(role))
    if known:
        return [title for title in known if normalise(title) != key][:MAX_PER_ROLE]
    if not use_llm:
        return []
    return _from_model(role)


def expand_roles(roles: list[str], *, use_llm: bool = True) -> list[str]:
    """Every stated role, followed by the other names employers use for them.

    The candidate's own words come first and in their original order: they are
    what the board's relevance ranking sees first, and they are what a human
    reading the search would expect at the top.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> bool:
        key = normalise(value)
        if not key or key in seen:
            return True
        seen.add(key)
        out.append(value.strip())
        return len(out) < MAX_TOTAL

    for role in roles or []:
        if not isinstance(role, str) or not role.strip():
            continue
        if not _add(role):
            return out

    for role in roles or []:
        if not isinstance(role, str) or not role.strip():
            continue
        for synonym in synonyms_for(role, use_llm=use_llm):
            if not _add(synonym):
                return out

    return out


__all__ = ["MAX_PER_ROLE", "MAX_TOTAL", "expand_roles", "normalise", "synonyms_for"]
