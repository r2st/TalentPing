"""Job discovery — turn saved search criteria into scored postings.

Provider strategy follows research §2.9 and §3.3: **never scrape LinkedIn or
Indeed directly.** Proxycurl was sued into shutdown in July 2025 for exactly
that, and the legal exposure is not worth the coverage.

Providers, in preference order:

* **Google Jobs via SerpApi** — the roadmap's primary aggregator, covering
  Indeed, LinkedIn, ZipRecruiter and Workday through one legal integration.
  Requires ``SERPAPI_API_KEY``; skipped silently when unset.
* **Public job-board APIs** — RemoteOK, Arbeitnow and Jobicy all publish free,
  keyless JSON feeds. These are the always-on baseline, so the feature works on
  a fresh install with no third-party account.
* **LinkedIn's public guest listings** — off unless ``LINKEDIN_JOB_SOURCE_ENABLED``
  is set. See :func:`fetch_linkedin`: it reads only what an unauthenticated job
  card shows, and the default deployment keeps the no-scrape posture above.

Every provider returns :class:`RawJob`; the service normalizes, filters on the
search criteria, deduplicates against what the user has already seen, scores the
survivors, and stores only those above the search's threshold.

A scan finishes with three enrichment passes over what it just stored, in order
of how much each costs:

* **cross-board dedup** (free, :mod:`app.services.job_dedup`) — the same role on
  three boards becomes one row with three source links;
* **Scout's re-rank** (one LLM call, :func:`app.services.fit_scorer.rerank`) — a
  nuanced second opinion on the shortlist the deterministic score produced;
* **company research** (one call per new employer, capped per scan) — the
  context the ad left out.

None of the three can fail a scan. Each is wrapped so that a dead upstream costs
the candidate an enrichment, never the jobs themselves.

Network-bound and synchronous — call it from a Celery worker, not a request
handler. (The API exposes a manual "run now" that accepts the wait.)
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.core import outbound
from app.core.config import settings
from app.models.job import JobPosting, JobSearch, JobStatus, job_fingerprint
from app.models.resume import Resume
from app.models.user import User
from app.services import (
    ats_boards,
    company_research,
    ghost_job,
    job_dedup,
    linkedin_service,
    profile_service,
    salary_service,
)
from app.services.fit_scorer import RerankCandidate, Targeting, rerank
from app.services.jd_parser import ParsedJob, parse_job
from app.services.places import fold_diacritics
from app.services.profile_service import ProfileMatch, ScoringTarget
from app.services.smart_apply_service import save_fit_score

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class JobQuery:
    """What a saved search is looking for, flattened for the providers."""

    roles: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    location: str | None = None
    remote_only: bool = False

    @property
    def terms(self) -> list[str]:
        """Lowercased match terms — roles and keywords together."""
        return [t.lower().strip() for t in [*self.roles, *self.keywords] if t.strip()]

    @property
    def phrase(self) -> str:
        """A single search string for providers that take one."""
        head = self.roles[0] if self.roles else ""
        tail = " ".join(self.keywords[:3])
        return " ".join(p for p in (head, tail) if p).strip()


@dataclass
class RawJob:
    """A posting as a provider reported it, before normalization."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    description: str | None = None
    salary_text: str | None = None
    remote: bool | None = None
    posted_at: datetime | None = None
    source: str = "unknown"
    # Filled by the dedup pass when the same role turned up on another board.
    source_urls: list[dict[str, str]] = field(default_factory=list)


def _clean(text: str | None, limit: int = 12000) -> str | None:
    """Strip HTML and collapse whitespace — feeds arrive as HTML fragments."""
    if not text:
        return None
    stripped = _HTML_TAG_RE.sub(" ", html.unescape(text))
    return _WS_RE.sub(" ", stripped).strip()[:limit] or None


def _field(value, limit: int = 255) -> str | None:
    """Normalize a short field (title, company, location).

    Boards routinely embed newlines and runs of spaces in titles — "Python\\n
    Developer" arrives that way from Jobicy — which then breaks the layout and
    poisons the dedupe fingerprint. Entities come through raw too, so a company
    called "Smith & Co" arrives as "Smith &amp; Co" and would be displayed that
    way verbatim.
    """
    if not isinstance(value, str):
        return None
    return _WS_RE.sub(" ", html.unescape(value)).strip()[:limit] or None


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": settings.scraper_user_agent,
            "Accept": "application/json",
        }
    )
    return session


def _epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Providers                                                                    #
# --------------------------------------------------------------------------- #


def fetch_serpapi(query: JobQuery, session: requests.Session, limit: int = 30) -> list[RawJob]:
    """Google Jobs via SerpApi — the legal route to Indeed/LinkedIn/Workday listings."""
    if not settings.serpapi_api_key:
        return []
    params = {
        "engine": "google_jobs",
        "q": query.phrase or "software engineer",
        "api_key": settings.serpapi_api_key,
        "hl": "en",
    }
    if query.location:
        params["location"] = query.location
    if query.remote_only:
        params["ltype"] = "1"  # SerpApi's work-from-home filter

    try:
        resp = outbound.capped_get(
            session,
            "https://serpapi.com/search.json",
            params=params,
            timeout=settings.scraper_timeout_seconds * 2,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("serpapi job search failed: %s", exc)
        return []

    out: list[RawJob] = []
    for item in (payload.get("jobs_results") or [])[:limit]:
        extensions = item.get("detected_extensions") or {}
        apply_options = item.get("apply_options") or []
        out.append(
            RawJob(
                title=_field(item.get("title"), 500),
                company=_field(item.get("company_name")),
                location=_field(item.get("location")),
                url=(apply_options[0].get("link") if apply_options else item.get("share_link")),
                description=_clean(item.get("description")),
                salary_text=extensions.get("salary"),
                remote=bool(extensions.get("work_from_home")),
                source="serpapi",
            )
        )
    return out


def fetch_remoteok(query: JobQuery, session: requests.Session, limit: int = 100) -> list[RawJob]:
    """RemoteOK's public feed. Remote-only by definition."""
    try:
        resp = session.get("https://remoteok.com/api", timeout=settings.scraper_timeout_seconds)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("remoteok job search failed: %s", exc)
        return []
    if not isinstance(payload, list):
        return []

    out: list[RawJob] = []
    # Element 0 is a legal notice, not a job.
    for item in payload[1 : limit + 1]:
        if not isinstance(item, dict) or not item.get("position"):
            continue
        salary_min, salary_max = item.get("salary_min"), item.get("salary_max")
        salary = (
            f"${salary_min:,} - ${salary_max:,}"
            if isinstance(salary_min, int) and isinstance(salary_max, int) and salary_min
            else None
        )
        out.append(
            RawJob(
                title=_field(item.get("position"), 500),
                company=_field(item.get("company")),
                location=_field(item.get("location")) or "Remote",
                url=item.get("url") or item.get("apply_url"),
                description=_clean(item.get("description")),
                salary_text=salary,
                remote=True,
                posted_at=_epoch(item.get("epoch")) or _iso(item.get("date")),
                source="remoteok",
            )
        )
    return out


def fetch_arbeitnow(query: JobQuery, session: requests.Session, limit: int = 100) -> list[RawJob]:
    """Arbeitnow's public board API — Europe-heavy, keyless."""
    try:
        resp = outbound.capped_get(
            session,
            "https://www.arbeitnow.com/api/job-board-api",
            timeout=settings.scraper_timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("arbeitnow job search failed: %s", exc)
        return []

    out: list[RawJob] = []
    for item in (payload.get("data") or [])[:limit]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        out.append(
            RawJob(
                title=_field(item.get("title"), 500),
                company=_field(item.get("company_name")),
                location=_field(item.get("location")),
                url=item.get("url"),
                description=_clean(item.get("description")),
                remote=bool(item.get("remote")),
                posted_at=_epoch(item.get("created_at")),
                source="arbeitnow",
            )
        )
    return out


def fetch_jobicy(query: JobQuery, session: requests.Session, limit: int = 50) -> list[RawJob]:
    """Jobicy's remote-jobs API — keyless, accepts a free-text tag filter."""
    params: dict[str, str | int] = {"count": min(limit, 50)}
    if query.phrase:
        params["tag"] = query.phrase[:60]
    try:
        resp = outbound.capped_get(
            session,
            "https://jobicy.com/api/v2/remote-jobs",
            params=params,
            timeout=settings.scraper_timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("jobicy job search failed: %s", exc)
        return []

    out: list[RawJob] = []
    for item in (payload.get("jobs") or [])[:limit]:
        if not isinstance(item, dict) or not item.get("jobTitle"):
            continue
        salary_min, salary_max = item.get("salaryMin"), item.get("salaryMax")
        salary = f"{salary_min} - {salary_max}" if salary_min and salary_max else None
        out.append(
            RawJob(
                title=_field(item.get("jobTitle"), 500),
                company=_field(item.get("companyName")),
                location=_field(item.get("jobGeo")) or "Remote",
                url=item.get("url"),
                description=_clean(item.get("jobDescription") or item.get("jobExcerpt")),
                salary_text=salary,
                remote=True,
                posted_at=_iso(item.get("pubDate")),
                source="jobicy",
            )
        )
    return out


def fetch_linkedin(query: JobQuery, session: requests.Session, limit: int = 25) -> list[RawJob]:
    """LinkedIn's public guest job listings — **off unless explicitly enabled**.

    LinkedIn is the largest single source of postings and the one candidates ask
    for by name, so it is available; it is also the one this codebase has always
    declined to touch, for the reason in the module docstring above. The
    compromise is deliberate:

    * It reads the *guest* endpoint — the listing an unauthenticated browser
      gets — and takes only what a job card shows: title, company, location,
      link, date. No member profiles, nothing behind the login, no session.
    * It is off unless ``LINKEDIN_JOB_SOURCE_ENABLED`` is set, so the default
      deployment keeps the original posture and turning it on is an informed
      decision. Google Jobs via SerpApi remains the covered-by-a-contract route
      to the same listings, and stays first in the provider order.
    """
    if not settings.linkedin_job_source_enabled:
        return []

    params = linkedin_service.search_params(
        keywords=query.phrase or "software engineer",
        location=query.location,
        remote_only=query.remote_only,
    )
    try:
        resp = outbound.capped_get(
            session,
            linkedin_service.GUEST_SEARCH_URL,
            params=params,
            headers={"Accept": "text/html"},
            timeout=settings.scraper_timeout_seconds,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("linkedin job search failed: %s", exc)
        return []

    out: list[RawJob] = []
    for card in linkedin_service.parse_guest_jobs(outbound.page_text(resp))[:limit]:
        out.append(
            RawJob(
                title=_field(card.get("title"), 500),
                company=_field(card.get("company")),
                location=_field(card.get("location")),
                url=card.get("url"),
                # A guest card carries no description; the fit scorer works off
                # the title and company, and the full text arrives when the
                # candidate opens the posting.
                description=None,
                remote=(
                    True if "remote" in (card.get("location") or "").lower() else None
                ),
                posted_at=_iso(card.get("posted_at")),
                source="linkedin",
            )
        )
    return out


# Ordered so the highest-quality source runs first; all of them run every scan.
# LinkedIn is last: it is the least structured of the lot (no description, no
# salary) and it is the one that can be switched off entirely.
PROVIDERS = (
    fetch_serpapi,
    fetch_remoteok,
    fetch_arbeitnow,
    fetch_jobicy,
    fetch_linkedin,
)


def discover(query: JobQuery) -> list[RawJob]:
    """Run every configured provider and return their combined, deduped results.

    Query-driven providers only. The company-board sweep is not one of these: it
    needs a database session and a user to know *which* companies to read, and it
    is not answering the query so much as filling in what the query's answers left
    out. :func:`run_search` calls it separately.
    """
    session = _session()
    seen: set[str] = set()
    out: list[RawJob] = []
    for provider in PROVIDERS:
        try:
            found = provider(query, session)
        except Exception:  # noqa: BLE001 - one bad provider must not kill the scan
            logger.exception("job provider %s failed", provider.__name__)
            continue
        for job in found:
            key = job_fingerprint(job.title, job.company, job.url)
            if key in seen:
                continue
            seen.add(key)
            out.append(job)
    return out


def _merge_unique(*groups: list[RawJob]) -> list[RawJob]:
    """Concatenate job lists, keeping the first copy of each fingerprint.

    First wins, so the caller expresses its preference by argument order rather
    than by a sort key nobody can see.

    A dropped copy still hands over its board link before it goes. The
    fingerprint deliberately ignores the URL, so four aggregators carrying the
    same title for the same company collapse here — earlier than
    `job_dedup.group_duplicates`, which is what would otherwise have collected
    those links. Discarding them outright lost the answer to "where else is
    this?", which the feed shows as "on N boards" and which ghost scoring reads
    as breadth-without-a-home-board.
    """
    seen: dict[str, RawJob] = {}
    out: list[RawJob] = []
    for group in groups:
        for job in group:
            key = job_fingerprint(job.title, job.company, job.url)
            kept = seen.get(key)
            if kept is not None:
                kept.source_urls = job_dedup.merge_source_urls(
                    kept.source_urls, kept, job
                )
                continue
            seen[key] = job
            out.append(job)
    return out


def _board_jobs(db: Session, user: User) -> list[RawJob]:
    """Postings read straight off the employers' own ATS boards.

    Best-effort in exactly the way every other enrichment here is: a company that
    has moved ATS, a board that 500s, a network that is down — all of them cost
    this scan the board coverage and nothing else. The aggregators have already
    returned by the time this runs.
    """
    if not settings.ats_board_discovery_enabled:
        return []
    try:
        scan = ats_boards.sweep(db, user)
    except Exception:  # noqa: BLE001 - board coverage is a bonus, never a blocker
        logger.exception("ats board sweep failed for user %s", user.id)
        return []

    out: list[RawJob] = []
    for job in scan.jobs:
        out.append(
            RawJob(
                title=job.title,
                company=job.company,
                location=job.location,
                url=job.url,
                description=job.description,
                salary_text=job.salary_text,
                remote=job.remote,
                posted_at=job.posted_at,
                source=ats_boards.source_name(job.platform),
            )
        )
    if scan.boards_found:
        logger.info(
            "ats boards: %s companies checked, %s boards read, %s postings",
            scan.companies_checked,
            scan.boards_found,
            len(out),
        )
    return out


# --------------------------------------------------------------------------- #
# Filtering + persistence                                                      #
# --------------------------------------------------------------------------- #


# Words that carry no signal in a job title. This is a list rather than a length
# test on purpose. The filter used to keep only tokens longer than three
# characters, which reads as "drop the noise words" and isn't: it also drops
# ``ai``, ``ml``, ``qa``, ``bi``, ``ux``, ``sre`` and ``ios`` — the tokens that
# *name the specialism*. A candidate targeting "AI Engineer" had their search
# silently reduced to "engineer", which matched every engineering post on every
# board and could not express the one thing they actually wanted.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "the", "of", "for", "with", "to", "in", "on", "at",
        "or", "our", "your", "their", "this", "that", "we", "you", "is", "are",
    }
)


def term_words(term: str) -> list[str]:
    """The meaningful words in a search term, for fuzzy overlap matching."""
    return [
        w
        for w in re.split(r"\W+", fold_diacritics((term or "").lower()))
        if w and w not in _STOPWORDS
    ]


# Above this length a term may match inside a longer word; at or below it, it
# has to be a word of its own.
#
# Every comparison in this filter was bare ``in`` — substring containment — and
# the very tokens :data:`_STOPWORDS` exists to protect are the ones that cannot
# survive it. "ai" is inside *maintenance*, *retail*, *training*, *email*,
# *Spain* and *Ukraine*; "go" is inside *Chicago*, *Google* and *goal*; "ui" is
# inside *building* and *guide*; "api" is inside *capital*; "git" is inside
# *digital*; "ci" is inside *efficient*, *special* and *decision*. So a
# candidate whose saved search said "AI Engineer" matched every maintenance,
# retail and training engineer on every board, and one who typed the keyword
# "Go" matched every job in Chicago — and matched on the *description* too,
# where a word containing "ai" or "go" is in essentially every posting written.
#
# That is the exact failure this filter exists to prevent, arriving through the
# one door nobody checked. It is worse than a bad score, because the postings it
# admits go on to the scorer and from there into a pipeline that emails people:
# the feed reported forty candidates and the candidate read forty roles they had
# not asked for.
#
# The cut is at three characters rather than at every length because longer
# words inflect and compound, and matching inside them is load-bearing:
# "engineer" has to find *engineering*, "ingenieur" has to find
# *Vertriebsingenieur*. Nothing a job-searcher types as two or three characters
# does that — "ai", "ml", "qa", "ux", "bi", "go", "ios", "sre", "sql", "aws",
# "php", "api", "git", "erp" are acronyms and language names, written exactly
# as they are and never inflected.
_SHORT_TERM_CHARS = 3


@lru_cache(maxsize=512)
def _bounded(needle: str) -> re.Pattern[str]:
    """*needle* compiled so it cannot match inside a longer word.

    The edges are ``(?<![0-9a-z])`` / ``(?![0-9a-z])`` rather than ``\\b``
    because a search term is not always made of word characters: "C++", "C#"
    and ".NET" begin or end on punctuation, and ``\\b`` after a ``+`` asserts
    that a word character follows — the opposite of what is wanted. The class
    is ASCII because both sides of the comparison are folded before they get
    here.

    Cached because a scan runs this over hundreds of postings against the same
    handful of terms.
    """
    return re.compile(rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])")


def _states(haystack: str, needle: str) -> bool:
    """Whether *haystack* says *needle*, rather than merely spelling it.

    Both arguments are already folded and lower-cased. See
    :data:`_SHORT_TERM_CHARS` for why the answer depends on the length.
    """
    if not needle:
        return False
    if len(needle) > _SHORT_TERM_CHARS:
        return needle in haystack
    return _bounded(needle).search(haystack) is not None


def matches_query(job: RawJob, query: JobQuery) -> bool:
    """Cheap pre-filter before the (much more expensive) fit scoring.

    The public boards return everything they have, so this is what keeps a
    "Senior Backend Engineer" search from scoring 300 marketing roles.

    Two haystacks, not one, and the difference matters. The *identity* of a
    posting — title, company, location — is a few dozen characters where a
    matched word means something. The description is several thousand
    characters of prose that names other teams, other tools and other roles;
    "we're a small team, you'll work daily with our backend engineers and
    designers" is a marketing ad that mentions two jobs it isn't.

    So the fuzzy word-overlap fallback runs over the identity only. The
    description can still admit a posting, but only by containing the search
    term outright, which is a deliberate statement rather than a coincidence of
    vocabulary. This filter feeds the scorer, and the scorer feeds a pipeline
    that emails people.

    "Contains" means :func:`_states`, not ``in``: a short term has to be a word
    of its own, or the specialism tokens this filter is built around match
    inside unrelated ones. See :data:`_SHORT_TERM_CHARS`.
    """
    if query.remote_only and job.remote is False:
        return False

    terms = query.terms
    if not terms:
        return True

    # Folded on both sides. Everything this compares is a string two people
    # spelt independently — the candidate typing their target role, the employer
    # writing the posting — and an accent is where those two spellings part.
    # "Développeur Backend" against a term of "Developpeur Backend" shares no
    # substring and no word, so the posting was rejected here and never reached
    # the scorer at all: not a low score the candidate could see and argue with,
    # an absence.
    identity = fold_diacritics(
        " ".join(p.lower() for p in (job.title, job.company, job.location) if p)
    )
    body = fold_diacritics((job.description or "").lower())

    for term in terms:
        term = fold_diacritics(term)
        if _states(identity, term) or _states(body, term):
            return True
        # "Senior Backend Engineer" rarely appears verbatim, so fall back to word
        # overlap. A two-thirds majority is the useful line: it lets a seniority
        # qualifier go missing (that's the fit scorer's job, not the filter's)
        # while still rejecting a posting that merely shares the word "engineer".
        words = term_words(term)
        if not words:
            continue
        hits = sum(1 for w in words if _states(identity, w))
        if hits >= max(1, (len(words) * 2 + 2) // 3):
            return True
    return False


def _location_ok(job: RawJob, query: JobQuery) -> bool:
    """Location filter that never rejects a remote role."""
    if not query.location or job.remote:
        return True
    # Same fold, and the place names are where it bites hardest: half the cities
    # this feed carries are spelt with an accent by whoever is in them.
    # "Zürich" against a posting in "Zurich, Switzerland" shared no word, and so
    # did "Zurich" against one in "Zürich, Schweiz" — the miss runs in both
    # directions, because neither side is the canonical one.
    wanted = {
        w
        for w in re.split(r"\W+", fold_diacritics(query.location.lower()))
        if len(w) > 2
    }
    if not wanted:
        return True
    have = fold_diacritics((job.location or "").lower())
    # A *shared word*, which is what the paragraph above says and what bare
    # ``in`` does not do. One word is enough here on purpose — a posting writes
    # "Zurich, Switzerland" where the candidate wrote "Zürich" — and that is
    # exactly what makes containment expensive: any one place name found inside
    # any other admits the posting on its own. "Ann Arbor" matched *Cannes*,
    # "Bern" matched *Bernburg*, "India" matched *Indianapolis*, "Rome" matched
    # *Romeoville* and "Ely" matched *Greely*.
    #
    # Bounded at every length, unlike the terms in :func:`_states`: a place name
    # does not inflect or compound the way a role word does, so there is nothing
    # for the length exemption to protect. Punctuation is still a boundary, so
    # "Frankfurt am Main" finds "Frankfurt/Main" and "Berlin" finds
    # "Berlin-Mitte".
    return any(_bounded(w).search(have) for w in wanted) or "remote" in have


def _score_raw_job(
    targets: list[ScoringTarget], job: RawJob
) -> tuple[ParsedJob, ProfileMatch | None]:
    """Parse one discovered posting and score it against every active profile.

    Heuristic parsing only: a scan evaluates hundreds of postings, and an LLM
    call per posting would burn the free tier in a single run. The candidate gets
    the full LLM treatment when they open a job to tailor against it.

    The parse happens once and is reused across profiles — it is a fact about the
    posting, not about who is reading it. Only the scoring runs per profile,
    which is deterministic string work and cheap enough to do three times.
    """
    parsed = parse_job(
        job.description or f"{job.title or ''}\n{job.company or ''}",
        page_title=job.title,
        use_llm=False,
    )
    # The provider's structured fields beat anything parsed out of the body.
    parsed.title = job.title or parsed.title
    parsed.company = job.company or parsed.company
    parsed.location = job.location or parsed.location
    if job.remote is not None:
        parsed.remote = job.remote
    if job.salary_text and not parsed.salary_text:
        parsed.salary_text = job.salary_text

    # A band is three fields — the text and the two figures — and only the text
    # was carried across. `fit_scorer.score_salary` reads the *figures*, so a
    # posting whose band arrived in the provider's structured field was scored
    # as though the employer had published nothing: NEUTRAL and `known=False` on
    # a dimension worth 8% of the total, under the note "The posting doesn't
    # publish a salary band" — printed on the same card that displays the band.
    #
    # Wrong in both directions, and neither is small. A band clearing the
    # candidate's stated floor lost the 1.0 it had earned (0.7 instead), which
    # is 2.4 points off the total and enough to drop a posting out of the
    # "strong" band it belonged in; a band falling short of the floor kept the
    # neutral 0.7 rather than the penalty, so an underpaying role scored as an
    # unknown one.
    #
    # `parse_offered` is the same reader `_store_matches` runs over the row it
    # is about to write, so the band the score is computed from is the band the
    # feed stores, filters on and shows.
    if parsed.salary_min is None and parsed.salary_max is None and parsed.salary_text:
        parsed.salary_min, parsed.salary_max = salary_service.parse_offered(
            parsed.salary_text
        )

    return parsed, profile_service.score_against_targets(targets, parsed, explain=False)


@dataclass
class ScanResult:
    scanned: int = 0
    added: int = 0
    below_threshold: int = 0
    # Postings whose *advertised* band tops out under the search's salary floor.
    # Counted apart from ``below_threshold`` so the run report can say which of
    # the two criteria did the filtering — they are tuned separately.
    below_salary: int = 0
    duplicates: int = 0
    # Cross-board copies folded into a canonical row rather than shown twice.
    merged: int = 0
    # Postings Scout re-ranked after the deterministic pass.
    reranked: int = 0
    # Postings that came from an employer's own ATS board rather than from an
    # aggregator. Reported because it is the coverage number worth watching: it
    # is the part of the feed with an exact posting date and a canonical apply URL.
    from_boards: int = 0
    # Screened out above the search's ``max_ghost_risk``. Counted separately and
    # always reported: a suppression the user cannot see is indistinguishable
    # from a scan that found nothing.
    ghosts: int = 0
    # Rows whose ``repost_count`` this scan bumped — the same role advertised
    # again with a fresher date.
    reposts: int = 0
    postings: list[JobPosting] = field(default_factory=list)
    detail: str | None = None


# How far back to look for a role the candidate has already been shown on
# another board. Bounded because it is a fuzzy comparison against every row:
# a repost lands within days, so scanning further buys nothing.
_DEDUP_LOOKBACK = 400


def run_search(db: Session, search: JobSearch, *, limit: int = 40) -> ScanResult:
    """Execute one saved search: discover, filter, score, store.

    Only postings at or above the search's ``min_fit_score`` are stored — the
    point of the feature is to surface the handful worth applying to, not to
    rebuild a job board inside the product.
    """
    user = db.get(User, search.user_id)
    if user is None:  # pragma: no cover - FK makes this unreachable
        return ScanResult(detail="orphaned search")

    targets = _scoring_targets(db, user, search)
    query = JobQuery(
        roles=list(search.roles or []),
        keywords=list(search.keywords or []),
        location=search.location,
        remote_only=search.remote_only,
    )

    result = ScanResult()
    try:
        found = discover(query)
    except Exception as exc:  # noqa: BLE001 - record it on the search row
        logger.exception("job search %s failed", search.id)
        search.last_error = str(exc)[:500]
        search.last_run_at = datetime.now(UTC)
        db.commit()
        return ScanResult(detail=str(exc)[:200])

    # The employers' own boards, read *after* the aggregators and merged in front
    # of them. Order matters twice over: the sweep picks its companies out of the
    # feed the aggregators built, and a board row carries a canonical apply URL,
    # a full description and a real posting date, so it should be the version
    # `job_dedup` promotes to canonical when the same role arrives twice.
    board_jobs = _board_jobs(db, user)
    result.from_boards = len(board_jobs)
    found = _merge_unique(board_jobs, found)

    candidates = [j for j in found if matches_query(j, query) and _location_ok(j, query)]
    result.scanned = len(candidates)

    # Collapse the same role appearing on several boards *before* anything
    # expensive touches it: one scoring pass, one row, one line in the feed.
    groups = job_dedup.group_duplicates(candidates)
    for group in groups:
        for copy in group.duplicates:
            job_dedup.absorb(group.canonical, copy)
    result.merged = sum(len(g.duplicates) for g in groups)

    # One query for every fingerprint we're about to consider, rather than a
    # lookup per job — a scan routinely evaluates a few hundred postings.
    fingerprinted = [
        (
            group,
            job_fingerprint(group.canonical.title, group.canonical.company, group.canonical.url),
        )
        for group in groups
    ]
    # Loaded as rows rather than bare fingerprints so a re-sighting can be told
    # apart from a repost: the same role advertised again with a materially
    # fresher date is the ghost signal, and it is only visible by comparing the
    # incoming date against the stored one.
    stored_by_key: dict[str, JobPosting] = {
        row.fingerprint: row
        for row in db.scalars(
            select(JobPosting)
            .where(
                JobPosting.user_id == user.id,
                JobPosting.fingerprint.in_([key for _g, key in fingerprinted]),
            )
            .options(
                load_only(
                    JobPosting.id,
                    JobPosting.fingerprint,
                    JobPosting.title,
                    JobPosting.source,
                    JobPosting.posted_at,
                    JobPosting.repost_count,
                    JobPosting.source_urls,
                )
            )
        )
    }
    existing = set(stored_by_key)
    # Canonical rows already in the feed, for the fuzzy cross-scan match that
    # `fingerprint` can't make. Only the columns dedup reads are loaded — the
    # descriptions would be megabytes.
    seen_before = list(
        db.scalars(
            select(JobPosting)
            .where(JobPosting.user_id == user.id, JobPosting.duplicate_of_id.is_(None))
            .options(
                load_only(
                    JobPosting.id,
                    JobPosting.title,
                    JobPosting.company,
                    JobPosting.location,
                    JobPosting.url,
                    JobPosting.remote,
                    JobPosting.source,
                    JobPosting.source_urls,
                    # Read by `_note_repost` when the fuzzy match is the path
                    # that recognises a repost. Deferring them here would not
                    # error — it would emit a refresh query per matched row,
                    # which is the cost this `load_only` exists to avoid.
                    JobPosting.posted_at,
                    JobPosting.repost_count,
                )
            )
            .order_by(JobPosting.id.desc())
            .limit(_DEDUP_LOOKBACK)
        )
    )

    matched_by_posting: dict[int, ProfileMatch] = {}

    # Scored here, once per group, and reused inside the loop below rather than
    # recomputed — so ranking and evaluation share one pass over `_score_raw_job`
    # (cheap heuristic parsing, no LLM call; see its docstring) instead of two.
    #
    # `fingerprinted` arrives in whatever order the providers happened to answer
    # in — board jobs first, then SerpAPI, RemoteOK, Arbeitnow, Jobicy, LinkedIn,
    # each internally in *their* order, which is not a fit judgement about
    # anything. A scan that finds more matching candidates than `limit` used to
    # slice this list before scoring a single one of them, so whichever postings
    # a provider happened to list first got evaluated and stored and everything
    # past position `limit` was silently dropped — including a posting that
    # would have scored 95 if it was unlucky enough to be provider-ordered
    # 41st. Sorting by fit score first, before the slice, means a scan that
    # finds too much keeps its *best* matches rather than its earliest ones.
    # Resume-less searches have no fit signal to sort by, so they keep
    # discovery order, same as before.
    scored: dict[int, tuple[ParsedJob | None, ProfileMatch | None]] = {}
    if targets:
        for group, _key in fingerprinted:
            scored[id(group)] = _score_raw_job(targets, group.canonical)
        fingerprinted.sort(
            key=lambda gk: (
                match.fit.overall
                if (match := scored[id(gk[0])][1]) is not None
                else -1.0
            ),
            reverse=True,
        )

    for group, key in fingerprinted[:limit]:
        job = group.canonical
        if key in existing:
            result.duplicates += 1
            if _note_repost(stored_by_key.get(key), job):
                result.reposts += 1
            continue

        # The same role, already in the feed under another board's title. The
        # stored row stays canonical — the candidate may have triaged it
        # already — and simply gains a link to where it turned up this time.
        prior = job_dedup.find_duplicate_of(job, seen_before)
        if prior is not None:
            prior.source_urls = job_dedup.merge_source_urls(prior.source_urls, prior, job)
            result.duplicates += 1
            result.merged += 1
            if _note_repost(prior, job):
                result.reposts += 1
            continue

        parsed, match = scored.get(id(group), (None, None))

        # The band the employer published, which is often only in the body text
        # rather than in the provider's salary field — so the parsed posting is
        # the better source when there is one.
        #
        # Its *figures* are taken with it, rather than re-derived by reading the
        # display string back. `parse_offered` re-reads that string through the
        # same extractor, and the extractor is deliberately stricter about a
        # bare number than about a marked one: without a currency symbol a lone
        # figure only counts when a pay word introduces it, because "250,000"
        # is a headcount as often as it is money. The display string is the band
        # alone — the pay word that licensed it stayed behind in the
        # description — so "Salary: 155000" and "Compensation: 150k" parsed
        # correctly here and then stored `NULL`.
        #
        # That is the silent direction. A stored band of NULL is not a posting
        # with an unknown salary, it is a posting whose published salary was
        # thrown away: `meets_floor` waves it through whatever floor the
        # candidate set, `compare_to_market` has nothing to compare and says so
        # on the intel card, and the fit score loses the dimension outright.
        parsed_text = parsed.salary_text if parsed is not None else None
        if parsed_text and parsed is not None and (
            parsed.salary_min is not None or parsed.salary_max is not None
        ):
            salary_text = parsed_text
            salary_min, salary_max = salary_service.as_band(
                parsed.salary_min, parsed.salary_max
            )
        else:
            # No parse to inherit: either the provider's own field, or the
            # posting's own text carried over onto `parsed` without figures.
            salary_text = parsed_text or job.salary_text
            salary_min, salary_max = salary_service.parse_offered(salary_text)

        # Checked before the fit score: the floor is a fact the candidate stated
        # about what they will accept, and no score should be able to talk them
        # past it. It also makes the two counters mean what they say.
        if not salary_service.meets_floor(salary_max, search.min_salary):
            result.below_salary += 1
            continue

        # Judged before the fit bar and before storage, for the same reason the
        # salary floor is: this is a fact about the *posting*, not about how
        # well the candidate matches it, and a role that isn't real shouldn't be
        # allowed to score its way into the feed. Cheapest ghost is the one that
        # never lands.
        assessment = ghost_job.assess(
            job,
            source_count=max(1, len(job.source_urls or [])),
            # Parsed a few lines up for the salary floor. A fetched job has no
            # parsed columns of its own, so handing the band over is the only
            # way the width signal can fire before the row exists — and the
            # screen's whole point is to run before the row exists.
            salary_band=(salary_min, salary_max),
        )
        if assessment.risk > search.max_ghost_risk:
            result.ghosts += 1
            continue

        score = match.fit.overall if match is not None else None
        if score is not None and score < search.min_fit_score:
            result.below_threshold += 1
            continue

        posting = _store_posting(
            db,
            user,
            search,
            job,
            key,
            score,
            profile_id=match.target.profile_id if match is not None else None,
            salary_text=salary_text,
            salary_band=(salary_min, salary_max),
        )
        # The score that just cleared the bar, kept on the row so the feed can
        # show the badge without re-deriving it on every read.
        ghost_job.apply_assessment(posting, assessment)
        result.postings.append(posting)
        result.added += 1
        seen_before.append(posting)

        # The copies keep rows of their own, pointed at the canonical: a
        # deleted duplicate would just be rediscovered and re-added next scan.
        # A copy whose title and company match the canonical's exactly shares
        # its fingerprint, and the per-user uniqueness constraint means there is
        # nothing to store — the canonical row already *is* that row.
        for copy in group.duplicates:
            copy_key = job_fingerprint(copy.title, copy.company, copy.url)
            if copy_key == key or copy_key in existing:
                continue
            existing.add(copy_key)
            _store_posting(
                db,
                user,
                search,
                copy,
                copy_key,
                score,
                profile_id=match.target.profile_id if match is not None else None,
                duplicate_of=posting,
            )

        if match is not None and parsed is not None:
            # Every profile's verdict is kept, not just the winner's. The runner
            # up is the answer to "would my other profile have liked this?", and
            # a candidate switching profiles shouldn't have to wait for a rescan
            # to find out.
            for target in targets:
                fit = match.all_scores.get(target.profile_id)
                if fit is None:  # pragma: no cover - every target is scored
                    continue
                save_fit_score(
                    db,
                    user,
                    target.resume,
                    parsed,
                    fit,
                    posting if target.profile_id == match.target.profile_id else None,
                    profile_id=target.profile_id,
                )
            matched_by_posting[posting.id] = match

    search.last_run_at = datetime.now(UTC)
    search.last_error = None if targets else "No resume — jobs stored unscored"
    search.jobs_found = (search.jobs_found or 0) + result.added
    db.commit()
    for posting in result.postings:
        db.refresh(posting)

    result.reranked = _apply_rerank(db, targets, result.postings, matched_by_posting)
    _research_companies(db, result.postings)
    return result


def _note_repost(stored: JobPosting | None, job: RawJob) -> bool:
    """Count a re-advertised role against the stored row. True if it counted.

    Called on the two paths that recognise a posting we already have — the exact
    fingerprint hit and the fuzzy cross-scan match — because a repost arrives on
    whichever of them the board's title happens to trip.

    Re-scoring uses the *incoming* description rather than the stored one: the
    fresher wording is what the employer is advertising today, and the stored
    row is loaded without its description precisely to keep this scan cheap.
    """
    if stored is None:  # pragma: no cover - key came out of the same dict
        return False
    if not ghost_job.is_repost(stored.posted_at, job.posted_at):
        return False

    stored.repost_count = (stored.repost_count or 0) + 1
    # The row now advertises the newer date, which is also what the candidate
    # sees on the card; without this the age signal would keep firing off a
    # date the employer has already replaced.
    stored.posted_at = job.posted_at
    ghost_job.apply_assessment(
        stored,
        ghost_job.assess(
            job,
            repost_count=stored.repost_count,
            source_count=max(
                len(stored.source_urls or []), len(job.source_urls or []), 1
            ),
        ),
    )
    return True


def _store_posting(
    db: Session,
    user: User,
    search: JobSearch,
    job: RawJob,
    fingerprint: str,
    score: float | None,
    *,
    profile_id: int | None = None,
    duplicate_of: JobPosting | None = None,
    salary_text: str | None = None,
    salary_band: tuple[int | None, int | None] | None = None,
) -> JobPosting:
    """Persist one discovered posting, canonical or duplicate.

    ``salary_text`` overrides the provider's own field with whatever the parse of
    the description turned up, and ``salary_band`` is that text already parsed —
    both passed in rather than recomputed, because the caller has just done the
    work to screen on them.
    """
    salary_text = salary_text or job.salary_text
    if salary_band is None:
        salary_band = salary_service.parse_offered(salary_text)
    posting = JobPosting(
        user_id=user.id,
        search_id=search.id,
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        description=job.description,
        # Truncated because the parsed value comes out of the body text and the
        # column is 255; the provider's own field was always short enough.
        salary_text=salary_text[:255] if salary_text else None,
        salary_min=salary_band[0],
        salary_max=salary_band[1],
        remote=job.remote,
        source=job.source,
        posted_at=job.posted_at,
        fingerprint=fingerprint,
        status=JobStatus.NEW,
        fit_score=score,
        matched_profile_id=profile_id,
        duplicate_of_id=duplicate_of.id if duplicate_of is not None else None,
        source_urls=list(job.source_urls or []),
    )
    db.add(posting)
    db.flush()
    return posting


def _apply_rerank(
    db: Session,
    targets: list[ScoringTarget],
    postings: list[JobPosting],
    matches: dict[int, ProfileMatch],
) -> int:
    """Have Scout re-rank the shortlist and write the verdicts onto the rows.

    One call per profile that actually won something, not one per profile the
    user owns: a candidate with three profiles whose scan only matched two of
    them pays for two calls. Grouping matters because the re-rank prompt carries
    the candidate's intent — asking "is this a good next step?" without saying
    which of their three careers we mean produces a confident answer to the wrong
    question.

    Best-effort by construction: :func:`rerank` returns ``{}`` rather than
    raising when no provider is configured or the response can't be trusted, and
    anything unexpected here is logged and swallowed. A scan that found jobs has
    already succeeded — losing the commentary must not undo that.
    """
    if not postings or not targets:
        return 0

    by_target: dict[int | None, list[JobPosting]] = {}
    for posting in postings:
        match = matches.get(posting.id)
        if match is None:
            continue
        by_target.setdefault(match.target.profile_id, []).append(posting)

    targets_by_id = {t.profile_id: t for t in targets}
    reranked = 0
    for profile_id, group in by_target.items():
        target = targets_by_id.get(profile_id)
        if target is None:  # pragma: no cover - ids come from these very targets
            continue
        reranked += _rerank_group(db, target, group, matches)
    return reranked


def _rerank_group(
    db: Session,
    target: ScoringTarget,
    postings: list[JobPosting],
    matches: dict[int, ProfileMatch],
) -> int:
    """Re-rank the postings that matched one profile, under that profile."""
    try:
        verdicts = rerank(
            target.resume,
            [
                RerankCandidate(
                    key=str(p.id),
                    title=p.title,
                    company=p.company,
                    location=p.location,
                    salary_text=p.salary_text,
                    remote=p.remote,
                    description=p.description,
                    fit_score=p.fit_score,
                    recommendation=(
                        matches[p.id].fit.recommendation if p.id in matches else None
                    ),
                    missing_skills=(
                        matches[p.id].fit.missing_skills if p.id in matches else []
                    ),
                )
                for p in postings
            ],
            targeting=target.targeting,
        )
    except Exception:  # noqa: BLE001 - enrichment must never fail a scan
        logger.exception("scout re-rank failed for %s", target.label)
        return 0

    if not verdicts:
        return 0

    by_id = {str(p.id): p for p in postings}
    for key, verdict in verdicts.items():
        posting = by_id.get(key)
        if posting is None:  # pragma: no cover - rerank filters unknown ids
            continue
        posting.llm_fit_score = verdict.llm_fit_score
        posting.llm_reasoning = verdict.reasoning
    db.commit()
    return len(verdicts)


def _research_companies(db: Session, postings: list[JobPosting]) -> int:
    """Warm the company-profile cache for the employers this scan surfaced.

    Capped at ``settings.company_research_per_scan`` distinct companies, highest
    fit first: a sweep that found forty roles shouldn't fan out into forty
    lookups, and the ones the candidate reads first are the ones worth having
    ready. The rest fill in lazily when their card is opened.
    """
    seen: set[str] = set()
    researched = 0
    ranked = sorted(postings, key=lambda p: (p.fit_score or 0), reverse=True)
    for posting in ranked:
        if researched >= settings.company_research_per_scan:
            break
        key = job_dedup.normalize_company(posting.company)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            company_research.research_company(
                db,
                posting.company,
                posting_text=posting.description,
                location=posting.location,
            )
            researched += 1
        except Exception:  # noqa: BLE001 - enrichment must never fail a scan
            logger.exception("company research failed for %s", posting.company)
            db.rollback()
    return researched


def _scoring_targets(
    db: Session, user: User, search: JobSearch
) -> list[ScoringTarget]:
    """Everything this search should score its finds against.

    Normally that is the candidate's active profiles: a scan is a sweep of the
    market, and the market doesn't know which of their careers it is answering.
    Scoring each posting against all of them and keeping the best is what lets
    one feed serve a backend engineer who would also take a DevOps role.

    A search pinned to a specific resume is the exception and is honoured as
    written — the user chose that document for this search, and overriding them
    with a profile they didn't mention would be the same failure the profiles
    were built to fix, pointing the other way. Its targeting still comes from the
    profile that resume belongs to, when one does, so a pinned search keeps the
    locations and salary floor that go with it.
    """
    targets = profile_service.active_targets(db, user)
    if search.resume_id is None:
        return targets

    pinned = db.get(Resume, search.resume_id)
    if pinned is None or pinned.user_id != user.id:
        return targets
    for target in targets:
        if target.resume.id == pinned.id:
            return [target]
    return [ScoringTarget(resume=pinned, targeting=Targeting())]


__all__ = [
    "JobQuery",
    "RawJob",
    "ScanResult",
    "discover",
    "matches_query",
    "run_search",
]
