"""Cross-board deduplication — one role, however many boards carry it.

:func:`app.models.job.job_fingerprint` already collapses the *exact* repeat: same
title, same company, same hash. What it cannot collapse is the same job reposted
by a second aggregator under a slightly different name — and that is the common
case, because every board rewrites titles:

    SerpApi     "Sr. Software Engineer, Backend"
    RemoteOK    "Senior Software Engineer (Backend) — Remote"
    Arbeitnow   "Senior Software Engineer - Berlin (m/w/d)"

Three rows, one job, and a feed that reads as three separate opportunities.

The match is deliberately conservative, because the failure modes are not
symmetric: a missed duplicate is a mild annoyance, while a wrong merge *hides a
real job the candidate never sees*. So a pair only merges when all three hold:

* the **companies** match exactly once normalized (legal suffixes dropped),
* the **titles** clear ``settings.dedup_title_threshold`` similarity once
  abbreviations are expanded ("Sr." → "senior", "Eng" → "engineer"),
* the **locations** are compatible — same place, or one of them says remote, or
  one of them doesn't say.

Company equality carries most of the weight. Two unrelated employers advertising
"Senior Software Engineer" is the norm, so title similarity alone would merge
half the feed; requiring the company first makes the fuzzy title test safe.

When a group is found the **richest** row wins — most description, salary, real
location — and the rest are marked ``duplicate_of_id`` rather than deleted. Two
reasons to keep them: the next scan would otherwise re-add them as new, and the
canonical row inherits their ``source_urls`` so the candidate can still open the
listing on whichever board they trust.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Protocol, TypeVar

from app.core.config import settings
from app.services.places import fold_diacritics, fold_initials, places_overlap

_NON_WORD = re.compile(r"[^a-z0-9+#]+")
# "(Remote)", "[Contract]", "{m/w/d}" — board furniture, never part of the role.
_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# German/Austrian gender markers, with or without brackets: m/w/d, w/m/x, m/f/d.
_GENDER_TAG = re.compile(r"\b[mwfd](?:\s*/\s*[mwfdx]){1,2}\b")

# Legal suffixes. "Acme, Inc." and "Acme" are one employer; "Acme Labs" is not,
# so only true legal forms are stripped — never a descriptive word.
#
# Written undotted, and every one of them is *also* written with dots by the
# companies that carry it: "Airbus S.A.S.", "Ferrari S.p.A.", "Heineken N.V.",
# "Novo Nordisk A/S". See :func:`normalize_company` for why that spelling could
# never match this set, and what it cost.
_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "plc", "corp",
    "corporation", "co", "company", "gmbh", "mbh", "ag", "kg", "bv", "nv",
    "sa", "sas", "srl", "spa", "ab", "as", "oy", "pty", "pte", "sl", "sro",
    "ug", "kft", "doo", "zoo", "group", "holdings", "holding",
    # Forms the set had no undotted spelling of either. "zoo" was already here
    # for the tail of the Polish "Sp. z o.o." and "sp" — the head of the same
    # name — was not, so the one company form the set went out of its way to
    # cover was still only half covered.
    "sarl", "aps", "pvt", "sp",
}

# Title vocabulary every board spells differently.
_TITLE_SYNONYMS = {
    "sr": "senior", "snr": "senior", "sen": "senior",
    "jr": "junior", "jnr": "junior",
    "eng": "engineer", "engr": "engineer", "engineering": "engineer",
    "dev": "developer", "developper": "developer",
    "swe": "software engineer", "sde": "software engineer", "sw": "software",
    "mgr": "manager", "mngr": "manager",
    "ops": "operations", "admin": "administrator",
    "arch": "architect", "spec": "specialist", "analyst": "analyst",
    "pm": "product manager", "tpm": "technical program manager",
    "sre": "site reliability engineer",
    "ml": "machine learning", "ai": "artificial intelligence",
    "qa": "quality assurance", "ux": "user experience", "ui": "user interface",
    "fe": "frontend", "be": "backend", "fs": "fullstack",
    "front": "frontend", "back": "backend", "full": "fullstack",
    # Level numerals, so "Engineer II" and "Engineer 2" are one role.
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "one": "1", "two": "2", "three": "3",
}

# Words that say how the job is worked, not what the job is.
_TITLE_NOISE = {
    "remote", "hybrid", "onsite", "on", "site", "wfh", "anywhere", "worldwide",
    "fulltime", "full", "time", "parttime", "part", "contract", "contractor",
    "permanent", "perm", "freelance", "temp", "temporary", "intern",
    "internship", "w2", "c2c", "urgent", "hiring", "now", "new", "job",
    "position", "opening", "role", "opportunity", "the", "a", "an", "of",
    "and", "for", "with", "at", "in", "to", "our", "we", "are", "is",
    "f", "m", "d", "x", "w",
}

# Two locations count as the same place when they share one of these.
_REMOTE_WORDS = ("remote", "anywhere", "worldwide", "distributed", "global")

# --------------------------------------------------------------------------- #
# Seniority                                                                    #
# --------------------------------------------------------------------------- #
#
# A level is part of a role's identity, and the title test could not see it.
#
# Two of the three measures below are blind to seniority by construction.
# `_TITLE_NOISE` drops the words that describe how a job is *worked*, and the
# character ratio scores by spelling — so "Junior Developer" and "Senior
# Developer" came out 0.875 similar, above the 0.86 threshold, and one of them
# was marked `duplicate_of_id` and disappeared from the feed. The numeral forms
# were worse: "Software Engineer II" against "Software Engineer III" scored
# 0.947, because after normalization they differ by a single character.
#
# That is the failure this module's docstring names as the one worth being
# conservative about — a wrong merge hides a real job the candidate never sees —
# and it was hiding the openings a candidate is most likely to be choosing
# between, at one employer, on one day.
#
# So the level is read out of the title separately and compared on its own
# terms, before similarity is consulted at all.

# Rungs, canonicalised. Boards spell the same rung several ways and the
# comparison is on identity, not on order: this deliberately does not rank them,
# because "is a staff role the same posting as a principal one?" only ever has
# one safe answer and it does not depend on which is higher.
_LEVEL_WORDS = {
    "intern": "intern",
    "internship": "intern",
    "trainee": "intern",
    "apprentice": "intern",
    "graduate": "junior",
    "grad": "junior",
    "entry": "junior",
    "junior": "junior",
    "associate": "associate",
    "mid": "mid",
    "senior": "senior",
    "staff": "staff",
    "lead": "lead",
    "principal": "principal",
    "distinguished": "distinguished",
    "fellow": "fellow",
}

# A level written as a code: "L3", "E5", "IC4", "P2", "T4". Reduced to its
# numeral so a company that writes "L3" on one board and "III" on another is
# still one job — the letter names the ladder, and a single employer has one.
_LEVEL_CODE = re.compile(r"^(?:l|e|p|t|g|ic|se|sde)([1-9])$")

# The rung that is never the same posting as any other, including one that says
# nothing at all. Everywhere else silence is treated as compatible (the rule
# `locations_compatible` follows), and it has to be: plenty of boards drop the
# level from the title. An internship is the exception worth carving out —
# "Software Engineer Intern" and "Software Engineer" normalize to the same
# string once `_TITLE_NOISE` has dropped the word, so treating the silence as
# compatible there merged a three-month placement with a permanent job.
_INTERN = "intern"


class _JobLike(Protocol):
    """The shape both a ``RawJob`` and a ``JobPosting`` row already satisfy.

    Declared read-only (properties rather than attributes) so a class whose
    ``source`` is a plain ``str`` still matches one typed ``str | None`` — a
    mutable protocol attribute is invariant, and both shapes are real here.
    """

    @property
    def title(self) -> str | None: ...
    @property
    def company(self) -> str | None: ...
    @property
    def location(self) -> str | None: ...
    @property
    def url(self) -> str | None: ...
    @property
    def description(self) -> str | None: ...
    @property
    def salary_text(self) -> str | None: ...
    @property
    def remote(self) -> bool | None: ...
    @property
    def source(self) -> str | None: ...


T = TypeVar("T", bound=_JobLike)


# --------------------------------------------------------------------------- #
# Normalization                                                                #
# --------------------------------------------------------------------------- #


def _words(value: str | None) -> list[str]:
    """The comparable words of a title, company or location.

    Accents are folded first, and this module is the one that most needs it: it
    exists because "every board rewrites titles", and an ASCII-only aggregator
    rewriting an accented one is the plainest form of that. `_NON_WORD` keeps
    ``a-z0-9+#`` and turns everything else into a space, so "Société Générale"
    came out as the four fragments "soci t g n rale" against the other board's
    "societe generale" — no shared word, so `normalize_company` never matched
    and the pair never reached the title test. Company equality is what carries
    the merge, so the candidate's feed showed one job twice, permanently.

    Shared with `places` rather than copied, which is the same reason that
    module exists: a second table of what a letter is would drift from the
    first, and this module already reads `places_overlap` from it.
    """
    return [w for w in _NON_WORD.sub(" ", fold_diacritics((value or "").lower())).split() if w]


def normalize_company(name: str | None) -> str:
    """Collapse an employer name to its identity.

    ``"Acme, Inc."``, ``"ACME Inc"`` and ``"Acme"`` are one company. Suffixes are
    only dropped from the *end* — "Group Nine Media" keeps its "group", because
    the word is doing work there rather than naming a legal form.

    **The dotted spelling of a legal form is joined back up first**, and it is
    the same fix, for the same reason, that :func:`places.fold_initials` exists
    for: ``_NON_WORD`` turns every separator into a space, and in "S.A.S." the
    dots are *inside* the word. So the suffix arrived here as the three
    fragments "s a s", no one of which is in the set, and nothing was stripped:

        "Airbus SAS"   -> "airbus"
        "Airbus S.A.S." -> "airbus s a s"

    Every form in the set failed this way and only in its dotted spelling —
    "S.A.", "B.V.", "S.p.A.", "S.r.l.", "L.L.C.", "P.L.C.", "A/S" — which is how
    a large part of Europe and Latin America writes its own name.

    Two things read this function and both were wrong in the same direction.
    The deduper merges on company equality, so the same job posted as "Ferrari
    S.p.A." on one board and "Ferrari" on another stayed two jobs in the feed
    forever. And :func:`app.services.search_filters.excluded_company_gate`
    compares the candidate's excluded list against this key, so a candidate who
    wrote down "Airbus" to avoid it kept getting outreach about "Airbus S.A.S."
    — which is the one list in the product where being ignored is the whole
    failure.

    Joining is confined to this function rather than done in :func:`_words`,
    because a title is not a company name: the suffix set is what makes a joined
    acronym safe here, and there is no equivalent guard on the title side.
    """
    words = fold_initials(" ".join(_words(name))).split()
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def _title_tokens(title: str | None, location: str | None = None) -> list[str]:
    text = (title or "").lower()
    text = _BRACKETED.sub(" ", text)
    text = _GENDER_TAG.sub(" ", text)

    # Boards routinely staple the location onto the title ("Backend Engineer -
    # Berlin"). Strip it only when the posting's own location field confirms it,
    # which keeps a genuine title word like "Berlin Operations Lead" intact.
    location_words = {w for w in _words(location) if len(w) > 2}
    tokens: list[str] = []
    for word in _words(text):
        if word in _TITLE_NOISE or word in location_words:
            continue
        expanded = _TITLE_SYNONYMS.get(word, word)
        tokens.extend(expanded.split())
    return tokens


def normalize_title(title: str | None, location: str | None = None) -> str:
    """Reduce a posted title to comparable words.

    Drops board furniture (brackets, "m/w/d", "Full-Time", the location), then
    expands the abbreviations boards disagree on. ``"Sr. Software Engineer"`` and
    ``"Senior Software Engineer"`` both come out as ``"senior software
    engineer"``, which is the whole point.
    """
    return " ".join(_title_tokens(title, location))


def title_level(title: str | None, location: str | None = None) -> frozenset[str]:
    """The rungs *title* names, canonicalised — empty when it names none.

    Read from the same word stream :func:`normalize_title` works on, and
    deliberately read *before* ``_TITLE_NOISE`` is applied: "intern" is in that
    set, so anything reading the normalized title has already lost it.

    Both spellings of a numeric level come back as the bare numeral, so a
    company writing "L3" on its own board and "III" on an aggregator is one job
    rather than two.
    """
    text = _BRACKETED.sub(" ", (title or "").lower())
    text = _GENDER_TAG.sub(" ", text)
    location_words = {w for w in _words(location) if len(w) > 2}

    found: set[str] = set()
    for word in _words(text):
        if word in location_words:
            continue
        code = _LEVEL_CODE.match(word)
        if code:
            found.add(code.group(1))
            continue
        # Expanded first, and that ordering is the whole of it: the table is
        # where "Sr." becomes "senior" and where "II" becomes "2", so a level
        # test run before it sees neither. One key can expand to several words
        # ("swe"), hence the split.
        for token in _TITLE_SYNONYMS.get(word, word).split():
            if token in _LEVEL_WORDS:
                found.add(_LEVEL_WORDS[token])
            elif len(token) == 1 and token.isdigit() and token != "0":
                # A bare single numeral only. A "10" in a title is a street
                # number or a team size, never a rung.
                found.add(token)
    return frozenset(found)


def levels_compatible(left: _JobLike, right: _JobLike) -> bool:
    """True when two postings' seniority doesn't rule out their being one job.

    Silence is compatible with anything, exactly as it is for location: boards
    routinely drop the level from a title, and refusing to merge on that would
    give up most of what this module is for. Two titles that *both* state a
    level and state different ones are two jobs — that is the case worth being
    sure about, and it is the case that was being merged.

    The internship is the one asymmetry: see :data:`_INTERN`.
    """
    left_level = title_level(left.title, left.location)
    right_level = title_level(right.title, right.location)

    if (_INTERN in left_level) != (_INTERN in right_level):
        return False
    if not left_level or not right_level:
        return True
    return left_level == right_level


def normalize_location(location: str | None) -> str:
    """Normalize a location, collapsing every flavour of remote onto one word."""
    words = _words(location)
    if any(w in _REMOTE_WORDS for w in words):
        return "remote"
    return " ".join(words)


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #


def title_similarity(left: str, right: str) -> float:
    """0..1 similarity between two already-normalized titles.

    Two measures, better of the two. Character similarity handles typos and
    spelling drift; token overlap handles reordering, since ``"engineer,
    backend"`` and ``"backend engineer"`` are the same job written by two
    different boards and score badly on characters alone.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    ratio = SequenceMatcher(None, left, right).ratio()

    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
    return max(ratio, overlap)


def locations_compatible(left: _JobLike, right: _JobLike) -> bool:
    """True when two postings' locations don't rule out their being one job.

    Silence is compatible with anything — plenty of boards drop the field — and
    so is remote, since a role listed "Remote" on one board and "Berlin" on
    another is usually the same remote-friendly req rather than two openings.

    The last resort is a shared *place-naming* word rather than any shared word
    at all. "San Francisco, CA" and "San Antonio, TX" share only "san", and
    reading that as one place merged a company's two open reqs into one — which
    doesn't merely duplicate a row, it marks the San Antonio job as a duplicate
    and takes it out of the candidate's feed.
    """
    left_key = normalize_location(left.location)
    right_key = normalize_location(right.location)
    if not left_key or not right_key:
        return True
    if left_key == right_key:
        return True
    if left_key == "remote" or right_key == "remote":
        return True
    if getattr(left, "remote", None) or getattr(right, "remote", None):
        return True

    # The *original* strings, not the normalized keys. `normalize_location`
    # strips punctuation, and the comma in "Cambridge, MA" is the evidence
    # `places.locate` reads the state code off — without it the two Cambridges
    # place themselves nowhere, contradict nothing, and merge on the shared
    # word. Everything above this line has already been decided from the keys,
    # and neither is remote or empty by the time we get here.
    return places_overlap(left.location, right.location)


def is_duplicate(left: _JobLike, right: _JobLike, *, threshold: float | None = None) -> bool:
    """True when two postings are the same role seen on two boards."""
    limit = settings.dedup_title_threshold if threshold is None else threshold

    left_company = normalize_company(left.company)
    right_company = normalize_company(right.company)
    # No company on either side means no anchor, and title similarity alone is
    # not evidence — half the internet is hiring a "Senior Software Engineer".
    if not left_company or left_company != right_company:
        return False

    if not locations_compatible(left, right):
        return False

    # Before the similarity test, not after: two rungs of one ladder are spelled
    # almost identically, so this is precisely the distinction a character ratio
    # cannot make. See `levels_compatible`.
    if not levels_compatible(left, right):
        return False

    left_title = normalize_title(left.title, left.location)
    right_title = normalize_title(right.title, right.location)
    return title_similarity(left_title, right_title) >= limit


# --------------------------------------------------------------------------- #
# Merging                                                                      #
# --------------------------------------------------------------------------- #


def metadata_richness(job: _JobLike) -> int:
    """How much this row actually tells the candidate.

    Decides which copy of a duplicated role survives. The description dominates
    because it is what tailoring and scoring read; everything else is a tiebreak.
    """
    score = min(len(job.description or ""), 6000) // 500  # 0..12
    if job.salary_text:
        score += 4
    if job.url:
        score += 2
    if job.location and normalize_location(job.location) != "remote":
        score += 1
    if getattr(job, "remote", None) is not None:
        score += 1
    if getattr(job, "posted_at", None) is not None:
        score += 1
    if job.title:
        score += 1
    return score


def source_link(job: _JobLike) -> dict[str, str] | None:
    """The ``{source, url}`` entry that records where a copy was seen."""
    if not job.url:
        return None
    return {"source": job.source or "unknown", "url": job.url}


def merge_source_urls(
    existing: Sequence[dict[str, str]] | None, *jobs: _JobLike
) -> list[dict[str, str]]:
    """Union of every board a role was seen on, first sighting kept first."""
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in list(existing or []):
        url = (entry or {}).get("url")
        if url and url not in seen:
            seen.add(url)
            merged.append({"source": entry.get("source") or "unknown", "url": url})
    for job in jobs:
        link = source_link(job)
        if link and link["url"] not in seen:
            seen.add(link["url"])
            merged.append(link)
    return merged


def absorb(canonical: Any, duplicate: _JobLike) -> None:
    """Fill the canonical row's gaps from a duplicate, then record its URL.

    Only *empty* fields are filled. A board that carries the salary and one that
    carries the long description between them make a better row than either, but
    a duplicate never overwrites a value the canonical row already has — the
    canonical row won on richness for a reason.
    """
    for attr in ("description", "salary_text", "location", "posted_at", "url"):
        if getattr(canonical, attr, None) in (None, "") and getattr(duplicate, attr, None):
            setattr(canonical, attr, getattr(duplicate, attr))
    duplicate_remote = getattr(duplicate, "remote", None)
    if getattr(canonical, "remote", None) is None and duplicate_remote is not None:
        canonical.remote = duplicate_remote

    canonical.source_urls = merge_source_urls(
        getattr(canonical, "source_urls", None), canonical, duplicate
    )


# --------------------------------------------------------------------------- #
# Batch grouping                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class DuplicateGroup:
    """One real role: the row worth keeping, plus the copies of it."""

    canonical: Any
    duplicates: list[Any]

    @property
    def sources(self) -> list[str]:
        return [j.source or "unknown" for j in [self.canonical, *self.duplicates]]


def group_duplicates(
    jobs: Sequence[T], *, threshold: float | None = None
) -> list[DuplicateGroup]:
    """Cluster a batch of postings into one group per real role.

    Buckets by normalized company first, so the fuzzy title comparison only ever
    runs within a single employer — a scan evaluating several hundred postings
    would otherwise pay for every pair.

    Ordering is stable: groups come back in the order their first member
    appeared, so a scan's output doesn't shuffle between runs.
    """
    buckets: dict[str, list[list[T]]] = {}
    order: list[list[T]] = []

    for job in jobs:
        company = normalize_company(job.company)
        cluster = None
        # Postings with no company can't be matched to anything (see
        # is_duplicate), so each one stands alone.
        if company:
            for existing in buckets.setdefault(company, []):
                if is_duplicate(existing[0], job, threshold=threshold):
                    cluster = existing
                    break
        if cluster is None:
            cluster = [job]
            if company:
                buckets[company].append(cluster)
            order.append(cluster)
        else:
            cluster.append(job)

    groups: list[DuplicateGroup] = []
    for members in order:
        # Richest wins; ties go to the earlier sighting, which is the
        # higher-priority provider because that is the order they are fetched in.
        best = max(range(len(members)), key=lambda i: (metadata_richness(members[i]), -i))
        groups.append(
            DuplicateGroup(
                canonical=members[best],
                duplicates=[j for i, j in enumerate(members) if i != best],
            )
        )
    return groups


def find_duplicate_of(
    job: _JobLike, existing: Sequence[T], *, threshold: float | None = None
) -> T | None:
    """The already-stored posting *job* duplicates, if any.

    Used when a scan meets a role the candidate has seen before on another
    board. Returns the canonical row so the caller can point the new row at it.
    """
    company = normalize_company(job.company)
    if not company:
        return None
    for row in existing:
        if normalize_company(row.company) != company:
            continue
        if is_duplicate(row, job, threshold=threshold):
            return row
    return None


__all__ = [
    "DuplicateGroup",
    "absorb",
    "find_duplicate_of",
    "group_duplicates",
    "is_duplicate",
    "levels_compatible",
    "locations_compatible",
    "merge_source_urls",
    "metadata_richness",
    "normalize_company",
    "normalize_location",
    "normalize_title",
    "source_link",
    "title_level",
    "title_similarity",
]
