"""Reading jobs from the company's own board, not from an aggregator's copy.

Every provider in :mod:`app.services.job_search_service` is somebody's index of
somebody else's postings. Aggregators are late, lossy and partial: the apply link
is a redirect chain, the description is truncated, the posted date is inferred
from when the crawler noticed, and coverage of any single employer is whatever
that crawler happened to catch.

Meanwhile most target companies publish their whole board as JSON, for free, with
no key:

* Greenhouse  — ``boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``
* Lever       — ``api.lever.co/v0/postings/{token}?mode=json``
* Ashby       — ``api.ashbyhq.com/posting-api/job-board/{token}``
* Workable    — ``apply.workable.com/api/v1/widget/accounts/{token}?details=true``
* SmartRecruiters — ``api.smartrecruiters.com/v1/companies/{token}/postings``

Those feeds are complete for that employer, updated the minute a role opens, and
carry a canonical apply URL and a real ``posted_at``. That last one is why this
module matters beyond coverage: posting age stops being a guess, which is what
every freshness and ghost-job signal downstream needs.

**The hard part is not fetching, it is knowing the token.** So discovery runs
cheapest-first and remembers everything:

1. **Free.** The token is sitting in a URL we already hold. A posting an
   aggregator gave us often links straight at ``jobs.lever.co/acme/…``, and
   :func:`~app.services.career_scraper.get_or_scrape` has already cached a
   ``careers_url`` per company. :func:`board_identity` reads the token out of
   either. No request, no guess.
2. **Bounded probe.** With nothing on file, the company's slug is tried against
   each platform — a handful of GETs that either return the board or 404. Capped
   per scan by ``ats_board_probe_limit`` so a sweep cannot turn into a hundred
   requests.
3. **Cached, including the failures — but not as the same kind of answer.** The
   result lands in :class:`~app.models.ats_board.AtsBoard`, keyed by normalized
   company name and shared across all users the way ``recruiter_cache`` is — one
   company has one board whoever is looking. A company with no public board is
   recorded as ``none`` so it is never probed twice. A company whose board could
   not be *read* — a throttle, a gateway error, a refused connection — is
   recorded as ``error`` instead and re-probed within hours, because that is a
   fact about a vendor's afternoon rather than about the employer, and the two
   were indistinguishable for as long as every failure returned an empty list.

The platform names here are **not** the same vocabulary as
:class:`~app.models.form_apply.ATSPlatform`. That enum means "we have a browser
adapter that can fill this form"; these constants mean "this vendor publishes a
readable JSON board". The two overlap (Greenhouse, Lever and Ashby are in both)
but neither implies the other — Workable and SmartRecruiters are readable with
no adapter, iCIMS has an adapter and no public board — so they stay separate
rather than one enum that half-lies in both directions.

Network-bound: call it from a Celery worker, never inline in a request.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

import requests
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import outbound
from app.core.config import settings
from app.core.sql_text import escape_like
from app.models.ats_board import AtsBoard
from app.models.job import JobPosting
from app.models.recruiter_cache import RecruiterCache
from app.models.user import User
from app.services.job_dedup import normalize_company
from app.services.places import fold_diacritics, names_arrangement

logger = logging.getLogger(__name__)

# Board platform identifiers. Local to this module rather than added to
# ``ATSPlatform`` — see the module docstring.
GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
WORKABLE = "workable"
SMARTRECRUITERS = "smartrecruiters"

PLATFORMS: tuple[str, ...] = (GREENHOUSE, LEVER, ASHBY, WORKABLE, SMARTRECRUITERS)

_LABELS = {
    GREENHOUSE: "Greenhouse",
    LEVER: "Lever",
    ASHBY: "Ashby",
    WORKABLE: "Workable",
    SMARTRECRUITERS: "SmartRecruiters",
}

# Cache row states, mirroring RecruiterCache's vocabulary.
STATUS_OK = "ok"
STATUS_NONE = "none"    # probed, no public board — do not probe again
STATUS_ERROR = "error"  # the board could not be read; retry sooner than `none`

#: HTTP statuses that mean "ask again later" rather than "there is no board".
#:
#: The split matters more here than the small list suggests, because the two
#: answers are cached for very different lengths of time — see
#: :func:`_cache_ttl_days`. Everything *not* in this set is read as a definite
#: absence, which is the right default: all five vendors answer an unknown
#: token with ``404``, so a probe that must treat every non-200 as inconclusive
#: would never conclude anything and would re-probe every company forever.
#:
#: ``403`` is in the set rather than out of it. No vendor returns it for a
#: public board token that does not exist; what returns it is a WAF deciding
#: this deployment's IP looks like a crawler, which is exactly the
#: environmental failure that must not be recorded as a fact about the employer.
#:
#: Deliberately **not** the same set as ``llm_router._TRANSIENT_STATUSES``, and
#: not to be unified with it. That one answers "is it worth retrying this
#: provider inside the current request?", where a wrong yes costs a round trip,
#: so it is narrow. This one answers "may I write a conclusion down?", where a
#: wrong yes costs a shared cache row for a fortnight — so it is broad, and
#: errs toward admitting it does not know.
_TRANSIENT_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504, 507, 529})


class BoardUnavailable(Exception):
    """The board could not be read — which is not the same as it not being there.

    Every failure mode of a third-party API used to arrive at the caller as the
    same empty list: a ``404`` meaning "this company does not use Greenhouse", a
    ``429`` meaning "you are asking too fast", a DNS failure meaning "this
    worker has no network", and a half-delivered body meaning "the response was
    cut off" were one value. :func:`resolve_board` then wrote that single value
    down as *"no public board found on any supported platform"* and cached it,
    so a rate limit lasting ninety seconds became a fact about the employer that
    outlived it by two weeks.

    Raised only on the probe path, where the distinction changes what is
    written. The read path for a board whose token is already known keeps
    swallowing everything, because there the answer is the same either way:
    return no jobs this scan and try again the next one.
    """

    def __init__(self, reason: str, *, rate_limited: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        #: Whether the vendor said *slow down* specifically. A sweep stops
        #: probing a platform that says this, rather than asking it the same
        #: question once per remaining company.
        self.rate_limited = rate_limited


def label(platform: str) -> str:
    """Human-readable platform name, as the feed shows it."""
    return _LABELS.get(platform, platform)


def source_name(platform: str) -> str:
    """The value written to ``JobPosting.source`` for a board fetch.

    Prefixed so the feed can tell "Greenhouse, from the company's own board" from
    "an aggregator that happened to be indexing Greenhouse", which are different
    claims about freshness and completeness.
    """
    return f"board:{platform}"


# --------------------------------------------------------------------------- #
# Token extraction — the free path                                             #
# --------------------------------------------------------------------------- #

# Path segments that are structural rather than a company token, so a URL like
# ``apply.workable.com/j/ABC123`` is not read as the company "j".
_RESERVED = frozenset(
    {
        "j", "jobs", "job", "embed", "careers", "career", "postings", "posting",
        "apply", "search", "api", "v1", "v0", "boards", "board", "companies",
        "company", "en", "en-us", "widget", "accounts", "list", "opportunities",
    }
)

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,98}$")

# host substring -> (platform, which path segment holds the token)
_HOST_PATTERNS: tuple[tuple[str, str, int], ...] = (
    ("boards.greenhouse.io", GREENHOUSE, 0),
    ("job-boards.greenhouse.io", GREENHOUSE, 0),
    ("boards.eu.greenhouse.io", GREENHOUSE, 0),
    ("job-boards.eu.greenhouse.io", GREENHOUSE, 0),
    ("jobs.lever.co", LEVER, 0),
    ("jobs.eu.lever.co", LEVER, 0),
    ("hire.lever.co", LEVER, 0),
    ("jobs.ashbyhq.com", ASHBY, 0),
    ("apply.workable.com", WORKABLE, 0),
    ("jobs.smartrecruiters.com", SMARTRECRUITERS, 0),
    ("careers.smartrecruiters.com", SMARTRECRUITERS, 0),
)

# ``…/embed/job_board?for=acme`` — Greenhouse's iframe form, where the token is a
# query parameter rather than a path segment.
_GREENHOUSE_EMBED = re.compile(r"[?&]for=([A-Za-z0-9._-]+)", re.I)
# ``acme.workable.com`` — Workable's per-company subdomain form.
_WORKABLE_SUBDOMAIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)\.workable\.com$", re.I)


def _valid_token(value: str | None) -> str | None:
    """A path segment that could be a company's board token, else None."""
    if not value:
        return None
    token = value.strip().strip("/")
    if not token or token.lower() in _RESERVED or not _TOKEN.match(token):
        return None
    return token


def board_identity(url: str | None) -> tuple[str, str] | None:
    """The ``(platform, token)`` a board URL names, or ``None``.

    Pure string work on a URL we already have — the cheapest possible way to
    learn a company's board, and the reason most companies never need a probe.
    """
    if not url or not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower().split(":")[0]
    if not host:
        return None
    segments = [s for s in (parsed.path or "").split("/") if s]

    if "greenhouse.io" in host and "/embed" in (parsed.path or "").lower():
        match = _GREENHOUSE_EMBED.search(candidate)
        token = _valid_token(match.group(1)) if match else None
        if token:
            return GREENHOUSE, token

    subdomain = _WORKABLE_SUBDOMAIN.match(host)
    if subdomain and subdomain.group(1).lower() not in ("apply", "www", "api"):
        token = _valid_token(subdomain.group(1))
        if token:
            return WORKABLE, token

    for needle, platform, index in _HOST_PATTERNS:
        if host != needle and not host.endswith(f".{needle}"):
            continue
        if len(segments) <= index:
            return None
        token = _valid_token(segments[index])
        if token:
            return platform, token
    return None


# --------------------------------------------------------------------------- #
# Fetchers — one per board                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class BoardJob:
    """One posting as its own employer published it."""

    title: str | None
    company: str | None
    location: str | None
    url: str | None
    description: str | None = None
    salary_text: str | None = None
    remote: bool | None = None
    posted_at: datetime | None = None
    platform: str = ""
    # The board's own id, kept so a re-fetch can be matched to the same role.
    external_id: str | None = None


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": settings.scraper_user_agent, "Accept": "application/json"}
    )
    return session


def _get_json(session: requests.Session, url: str, **params):
    """GET *url* expecting JSON. Returns ``None`` when there is nothing there.

    Every board is treated as optional infrastructure: a 404 is the normal answer
    to "does this company use Greenhouse?", and a 500 is the vendor's problem, not
    the candidate's. Neither may raise into a scan.

    But they are not the same answer, so they no longer return the same value.
    A definite absence returns ``None``; anything that leaves the question open —
    a throttle, a gateway error, a refused connection, a name that would not
    resolve, an expired certificate, a body that was cut off mid-object —
    raises :class:`BoardUnavailable`. Callers that only want jobs catch it and
    carry on; the probe, which is about to write a conclusion down, does not.

    The connection and TLS errors are worth spelling out because they are the
    ones that arrive in bulk: when this worker's network is down, *every* board
    fails at once, and the old code read that as five platforms each
    independently confirming the employer has no board.
    """
    try:
        resp = outbound.capped_get(
            session, url, params=params or None,
            timeout=settings.scraper_timeout_seconds,
        )
    except requests.RequestException as exc:
        # Covers Timeout, ConnectionError (refused, reset, DNS failure) and
        # SSLError alike — none of them is evidence about the employer.
        logger.debug("ats board request failed for %s: %s", url, exc)
        raise BoardUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code in _TRANSIENT_STATUSES:
        raise BoardUnavailable(
            f"HTTP {resp.status_code}", rate_limited=resp.status_code == 429
        )
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        # A 200 that is not JSON is an HTML error page, a captcha interstitial
        # or a body `outbound` truncated at its ceiling. All three mean the feed
        # was not read, and none of them means the board is absent.
        raise BoardUnavailable(f"unparseable body: {exc}") from exc


def _iso(value) -> datetime | None:
    """Parse a board's timestamp — ISO-8601, or epoch milliseconds."""
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _looks_remote(location: str | None) -> bool | None:
    """Whether a board's own location string says remote. ``None`` when silent.

    ``None`` rather than ``False`` throughout: this reads a field that is
    usually a city, and a city is not a statement that the role is on-site.
    Every caller that has a structured flag from the board reads that first and
    only falls back to here; Greenhouse, which publishes none, gets this alone.

    The word list is :func:`app.services.places.names_arrangement`, shared with
    the fit scorer. It was a bare ``"remote" in location.lower()`` here — the
    one English spelling, matched by containment — so an ATS board serving a
    French or German employer set ``remote=None`` on every posting it carries,
    and everything downstream had to re-derive the answer from the same string
    with the same list.
    """
    if not location:
        return None
    return True if names_arrangement(location) else None


_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(value: str | None, limit: int = 12000) -> str | None:
    """Board descriptions are HTML fragments; the scorer wants plain text."""
    if not isinstance(value, str) or not value.strip():
        return None
    import html as html_module

    stripped = _HTML_TAG.sub(" ", html_module.unescape(value))
    return _WS.sub(" ", stripped).strip()[:limit] or None


def _short(value, limit: int = 255) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    import html as html_module

    return _WS.sub(" ", html_module.unescape(value)).strip()[:limit] or None


def _mapping(value) -> dict:
    """*value* if it is a JSON object, else an empty one.

    ``(item.get("location") or {}).get("name")`` reads as defensive and is not:
    it guards the field being **absent**, and raises ``AttributeError`` the
    moment the field is *present with the wrong shape*. Which these feeds do —
    every one of them has a field that is an object on some postings and a bare
    string on others, because that is what happens when a vendor widens a schema
    and leaves the old rows alone. Greenhouse's ``location``, SmartRecruiters'
    ``company`` and ``location``, Ashby's ``compensation``: a single posting with
    ``"location": "Remote"`` was enough to raise out of the fetcher and discard
    the employer's entire board.
    """
    return value if isinstance(value, dict) else {}


def _collect(
    items, limit: int, build, *, platform: str, token: str
) -> list[BoardJob]:
    """Build a :class:`BoardJob` per item, skipping the ones that will not build.

    The isolation is the point. ``fetch_board`` wraps each fetcher in a blanket
    ``except`` so that a board can never fail a scan, and that is right, but it
    made the *unit of failure* the whole board: one unexpected shape in one
    posting and the candidate loses all forty roles at that employer — silently,
    and looking exactly like a company that is not hiring.

    Here the unit of failure is one posting, which is the honest size of the
    problem. A malformed row costs that row. The count is logged rather than
    swallowed, because "this board parses two-thirds of its postings" is a fact
    about a vendor's feed that nobody would otherwise ever learn.

    *items* being the wrong shape entirely — a bare string, ``null``, an object
    where a list belongs — yields nothing rather than raising, since there is no
    posting to salvage.
    """
    if not isinstance(items, list):
        return []
    out: list[BoardJob] = []
    skipped = 0
    for item in items[:limit]:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            job = build(item)
        except Exception:  # noqa: BLE001 - one bad posting, not one bad board
            logger.debug(
                "ats board %s/%s: skipping an unparseable posting",
                platform, token, exc_info=True,
            )
            skipped += 1
            continue
        if job is not None:
            out.append(job)
    if skipped:
        logger.info(
            "ats board %s/%s: %s of %s postings could not be read",
            platform, token, skipped, min(len(items), limit),
        )
    return out


def fetch_greenhouse(
    session: requests.Session, token: str, company: str, limit: int
) -> list[BoardJob]:
    """Greenhouse's public board API. ``content=true`` includes the full JD."""
    payload = _get_json(
        session,
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        content="true",
    )
    if not isinstance(payload, dict):
        return []

    def build(item: dict) -> BoardJob | None:
        if not item.get("title"):
            return None
        location = _mapping(item.get("location")).get("name")
        return BoardJob(
            title=_short(item.get("title"), 500),
            company=company,
            location=_short(location),
            url=item.get("absolute_url"),
            description=_text(item.get("content")),
            remote=_looks_remote(location),
            posted_at=_iso(item.get("updated_at") or item.get("first_published")),
            platform=GREENHOUSE,
            external_id=str(item.get("id")) if item.get("id") else None,
        )

    return _collect(
        payload.get("jobs"), limit, build, platform=GREENHOUSE, token=token
    )


def fetch_lever(
    session: requests.Session, token: str, company: str, limit: int
) -> list[BoardJob]:
    """Lever's public postings API. Returns a bare JSON array."""
    payload = _get_json(
        session, f"https://api.lever.co/v0/postings/{token}", mode="json"
    )
    if not isinstance(payload, list):
        return []

    def build(item: dict) -> BoardJob | None:
        if not item.get("text"):
            return None
        location = _mapping(item.get("categories")).get("location")
        workplace = item.get("workplaceType")
        workplace = workplace.lower() if isinstance(workplace, str) else ""
        return BoardJob(
            title=_short(item.get("text"), 500),
            company=company,
            location=_short(location),
            url=item.get("hostedUrl") or item.get("applyUrl"),
            description=_text(
                item.get("descriptionPlain") or item.get("description")
            ),
            salary_text=_lever_salary(item.get("salaryRange")),
            remote=(
                True
                if workplace == "remote"
                else (False if workplace == "onsite" else _looks_remote(location))
            ),
            posted_at=_iso(item.get("createdAt")),
            platform=LEVER,
            external_id=_short(item.get("id"), 100),
        )

    return _collect(payload, limit, build, platform=LEVER, token=token)


def _lever_salary(payload) -> str | None:
    """Lever's structured salary range as the one-line string the scorer reads."""
    if not isinstance(payload, dict):
        return None
    low, high = payload.get("min"), payload.get("max")
    currency = payload.get("currency") or ""
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return f"{currency} {int(low):,} - {int(high):,}".strip()


def fetch_ashby(
    session: requests.Session, token: str, company: str, limit: int
) -> list[BoardJob]:
    """Ashby's public job-board API."""
    payload = _get_json(
        session,
        f"https://api.ashbyhq.com/posting-api/job-board/{token}",
        includeCompensation="true",
    )
    if not isinstance(payload, dict):
        return []

    def build(item: dict) -> BoardJob | None:
        if not item.get("title"):
            return None
        location = item.get("location")
        return BoardJob(
            title=_short(item.get("title"), 500),
            company=company,
            location=_short(location),
            url=item.get("jobUrl") or item.get("applyUrl"),
            description=_text(
                item.get("descriptionPlain") or item.get("descriptionHtml")
            ),
            salary_text=_short(
                _mapping(item.get("compensation")).get("compensationTierSummary")
            ),
            remote=(
                True
                if item.get("isRemote") is True
                else _looks_remote(location)
            ),
            posted_at=_iso(item.get("publishedAt") or item.get("updatedAt")),
            platform=ASHBY,
            external_id=_short(item.get("id"), 100),
        )

    return _collect(payload.get("jobs"), limit, build, platform=ASHBY, token=token)


def fetch_workable(
    session: requests.Session, token: str, company: str, limit: int
) -> list[BoardJob]:
    """Workable's public widget API — the feed behind ``apply.workable.com``."""
    payload = _get_json(
        session,
        f"https://apply.workable.com/api/v1/widget/accounts/{token}",
        details="true",
    )
    if not isinstance(payload, dict):
        return []

    def build(item: dict) -> BoardJob | None:
        if not item.get("title"):
            return None
        location = ", ".join(
            part
            for part in (item.get("city"), item.get("state"), item.get("country"))
            if isinstance(part, str) and part.strip()
        )
        return BoardJob(
            title=_short(item.get("title"), 500),
            company=_short(item.get("company")) or company,
            location=_short(location) or _short(item.get("location")),
            url=item.get("shortlink") or item.get("url") or item.get("application_url"),
            description=_text(item.get("description")),
            remote=(
                True if item.get("telecommuting") is True else _looks_remote(location)
            ),
            posted_at=_iso(item.get("published_on") or item.get("created_at")),
            platform=WORKABLE,
            external_id=_short(item.get("shortcode") or item.get("id"), 100),
        )

    return _collect(payload.get("jobs"), limit, build, platform=WORKABLE, token=token)


def fetch_smartrecruiters(
    session: requests.Session, token: str, company: str, limit: int
) -> list[BoardJob]:
    """SmartRecruiters' public postings API.

    The list response carries no description — SmartRecruiters puts it behind a
    per-posting call, and a scan will not spend one request per role to get it.
    Title, company, location and an exact ``releasedDate`` are enough for the
    deterministic scorer; the full text arrives when the candidate opens the job.
    """
    payload = _get_json(
        session,
        f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
        limit=min(limit, 100),
    )
    if not isinstance(payload, dict):
        return []

    def build(item: dict) -> BoardJob | None:
        if not item.get("name"):
            return None
        location_payload = _mapping(item.get("location"))
        location = ", ".join(
            part
            for part in (
                location_payload.get("city"),
                location_payload.get("region"),
                location_payload.get("country"),
            )
            if isinstance(part, str) and part.strip()
        )
        posting_id = item.get("id")
        return BoardJob(
            title=_short(item.get("name"), 500),
            company=_short(_mapping(item.get("company")).get("name")) or company,
            location=_short(location),
            url=(
                item.get("applyUrl")
                or (
                    f"https://jobs.smartrecruiters.com/{token}/{posting_id}"
                    if posting_id
                    else None
                )
            ),
            remote=(
                True
                if location_payload.get("remote") is True
                else _looks_remote(location)
            ),
            posted_at=_iso(item.get("releasedDate") or item.get("createdOn")),
            platform=SMARTRECRUITERS,
            external_id=_short(posting_id, 100),
        )

    return _collect(
        payload.get("content"), limit, build, platform=SMARTRECRUITERS, token=token
    )


FETCHERS = {
    GREENHOUSE: fetch_greenhouse,
    LEVER: fetch_lever,
    ASHBY: fetch_ashby,
    WORKABLE: fetch_workable,
    SMARTRECRUITERS: fetch_smartrecruiters,
}

# The human-facing board page per platform, so the feed can link somewhere real.
_BOARD_URLS = {
    GREENHOUSE: "https://boards.greenhouse.io/{token}",
    LEVER: "https://jobs.lever.co/{token}",
    ASHBY: "https://jobs.ashbyhq.com/{token}",
    WORKABLE: "https://apply.workable.com/{token}/",
    SMARTRECRUITERS: "https://jobs.smartrecruiters.com/{token}",
}


def board_url(platform: str, token: str) -> str | None:
    template = _BOARD_URLS.get(platform)
    return template.format(token=token) if template else None


def fetch_board(
    session: requests.Session,
    platform: str,
    token: str,
    company: str,
    *,
    limit: int,
    strict: bool = False,
) -> list[BoardJob]:
    """Fetch one company's board. Returns ``[]`` for anything that isn't there.

    *strict* is for the caller that is about to write a conclusion down.
    :class:`BoardUnavailable` — the vendor throttled us, the gateway is down,
    the connection was refused — propagates instead of flattening into an empty
    list, so :func:`resolve_board` can tell "this employer has no board" from
    "we could not find out". Everything else is still swallowed under either
    setting: a board must never fail a scan.
    """
    fetcher = FETCHERS.get(platform)
    if fetcher is None:
        return []
    try:
        return fetcher(session, token, company, limit)
    except BoardUnavailable as exc:
        if strict:
            raise
        logger.debug("ats board %s/%s unavailable: %s", platform, token, exc.reason)
        return []
    except Exception:  # noqa: BLE001 - a board must never fail a scan
        logger.exception("ats board fetch failed for %s/%s", platform, token)
        return []


# --------------------------------------------------------------------------- #
# Finding the token                                                            #
# --------------------------------------------------------------------------- #


def _known_urls(db: Session, user: User, company: str) -> list[str]:
    """Every URL already on file for *company* that might name its board.

    The user's own feed first — an aggregator's posting frequently links straight
    at the ATS — then the globally-cached careers page, which
    :mod:`app.services.career_scraper` wrote the last time anyone targeted this
    employer. Both are already paid for.
    """
    urls: list[str] = []
    normalized = normalize_company(company)

    # `escape_like`, because the needle is a company name the user typed. Both
    # queries here are a cheap prefilter for the `normalize_company` comparison
    # below, so a stray wildcard does not change the *answer* — but `%` matches
    # every row, and the second table is the deployment-wide crawl cache, so it
    # turns a two-row lookup into loading that whole table into this process.
    like = f"%{escape_like(company.strip())}%"

    for posting in db.scalars(
        select(JobPosting)
        .where(
            JobPosting.user_id == user.id,
            JobPosting.company.ilike(like, escape="\\"),
        )
        .order_by(JobPosting.id.desc())
        .limit(25)
    ):
        if normalize_company(posting.company) != normalized:
            continue
        if posting.url:
            urls.append(posting.url)
        for entry in posting.source_urls or []:
            if isinstance(entry, dict) and entry.get("url"):
                urls.append(entry["url"])

    for row in db.scalars(
        select(RecruiterCache).where(
            RecruiterCache.company.ilike(like, escape="\\")
        )
    ):
        if normalize_company(row.company) != normalized:
            continue
        for value in (row.careers_url, row.source_url):
            if value:
                urls.append(value)
    return urls


def _slug_candidates(company: str) -> list[str]:
    """Board tokens worth probing for *company*, most likely first.

    Boards are overwhelmingly named after the company with the punctuation taken
    out ("Northwind Labs" -> ``northwindlabs``), with a hyphenated form a distant
    second. Deliberately only two: each candidate costs one request per platform,
    and a third spelling has a worse hit rate than the negative cache has value.

    Notably *not* :func:`~app.services.career_scraper.slugify_company`, which
    drops trailing words like "Labs", "Group" and "Technologies" because it is
    guessing a *domain* — ``northwindlabs.com`` is usually reachable as
    ``northwind.com``. A board token is an account name, not a hostname, and
    Northwind Labs is ``northwindlabs`` on Greenhouse. Same input, different
    question.
    """
    # Folded first: `[a-z0-9]+` does not contain an accented letter, so without
    # this it is a separator. "Société Générale" probed as ``socitgnrale`` and
    # ``soci-t-g-n-rale``, "Telefónica" as ``telefnica``, "Ørsted" as ``rsted``
    # — none of which is anyone's board token, while the real one is the ASCII
    # spelling boards force everybody onto. Two guesses is the whole budget, so
    # spending both on garbage means the board is simply never found, and the
    # miss is then cached as an answer.
    words = re.findall(r"[a-z0-9]+", fold_diacritics(company.strip().lower()))
    if not words:
        return []
    out = ["".join(words)]
    hyphenated = "-".join(words)
    if hyphenated != out[0]:
        out.append(hyphenated)
    return out


@dataclass
class BoardLookup:
    """What we know about one company's board, and where that came from."""

    company: str
    platform: str | None = None
    token: str | None = None
    status: str = STATUS_NONE
    note: str | None = None
    # True when the answer came from the cache rather than from fresh work.
    cached: bool = False
    # True when this lookup spent requests guessing. The caller's probe budget is
    # metered on this flag rather than on parsing the note — a budget that
    # depended on prose would silently stop metering the day the wording changed.
    probed: bool = False

    @property
    def found(self) -> bool:
        return bool(self.platform and self.token)


def _cache_ttl_days(status: str | None) -> float:
    """How long a cached answer of this kind may be reused.

    A found board and a genuine absence are both facts about the employer, and
    change on the timescale an employer changes ATS vendor — the fortnight
    ``ats_board_cache_ttl_days`` allows.

    An error is not a fact about the employer at all. It is a fact about a
    vendor's afternoon: a throttle, a bad gateway, a certificate that expired
    over the weekend. Reusing it for a fortnight would let a ninety-second
    outage decide that eight employers have no board until the end of the
    month, which is what happened while every failure was cached as ``none``.
    So it expires in hours, and the next sweep asks again.
    """
    if status == STATUS_ERROR:
        return settings.ats_board_error_ttl_hours / 24
    return float(settings.ats_board_cache_ttl_days)


def _cached_board(db: Session, company: str) -> AtsBoard | None:
    normalized = normalize_company(company)
    if not normalized:
        return None
    return db.scalar(select(AtsBoard).where(AtsBoard.normalized_name == normalized))


def _apply_lookup(row: AtsBoard, lookup: BoardLookup, jobs_seen: int) -> None:
    """Copy one discovery's result onto a cache row."""
    row.platform = lookup.platform
    row.board_token = lookup.token
    row.board_url = (
        board_url(lookup.platform, lookup.token)
        if lookup.platform and lookup.token
        else None
    )
    row.status = lookup.status
    row.note = lookup.note
    row.checked_at = datetime.now(UTC)
    if jobs_seen:
        row.jobs_seen = jobs_seen


def _upsert_board(
    db: Session, lookup: BoardLookup, *, jobs_seen: int = 0
) -> AtsBoard | None:
    """Write what we learned, so nobody pays for this discovery twice.

    ``normalized_name`` is unique across the whole deployment — the cache is
    global on purpose, because an employer's board is the same board whoever
    asks — so read-then-insert is a race between any two scans that hit the same
    company at once, and that is the *common* case rather than an exotic one: a
    nightly scan fans out over a job feed where one employer has twenty reqs,
    and every user's scan reads the same table. Losing the race raised
    ``IntegrityError`` out of the commit and killed the whole scan task,
    discarding the work it had already done for every other company.

    The loser now takes the winner's row and applies its own findings on top,
    which is safe because both scans were answering the same question about the
    same employer and the later answer is the fresher one.

    None is returned when the name does not normalise to anything — a company
    called ``"!!!"`` has no cache key, and writing it under the empty one would
    make every such employer collide with every other.
    """
    normalized = normalize_company(lookup.company)
    if not normalized:
        return None

    row = _cached_board(db, lookup.company)
    if row is None:
        row = AtsBoard(company=lookup.company[:255], normalized_name=normalized[:255])
        _apply_lookup(row, lookup, jobs_seen)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.debug(
                "ats board for %s was written by a concurrent scan", lookup.company
            )
            row = _cached_board(db, lookup.company)
            if row is None:
                # The constraint fired for a reason we did not predict; re-raise
                # rather than silently returning a row that is not this lookup.
                raise
            _apply_lookup(row, lookup, jobs_seen)
            db.commit()
        return row

    _apply_lookup(row, lookup, jobs_seen)
    db.commit()
    return row


def resolve_board(
    db: Session,
    user: User,
    company: str,
    *,
    session: requests.Session | None = None,
    allow_probe: bool = True,
    throttled: set[str] | None = None,
) -> BoardLookup:
    """Find *company*'s public board, cheapest route first.

    Returns a :class:`BoardLookup`; ``found`` is False when the company has no
    public board we can read, which is a perfectly normal answer and is cached as
    such so it costs one probe ever rather than one per scan.

    *allow_probe* is the scan's budget knob: with it False, only the free paths
    (cache, then a URL already on file) are used and nothing touches the network.

    *throttled* is the set of platforms that have already answered ``429``
    during this sweep, and is both read and written. A sweep passes one set
    through every company so that a vendor throttling us costs one refusal
    rather than one per remaining employer; passing ``None`` gives this call its
    own empty set, which is right for a one-off lookup.
    """
    company = (company or "").strip()
    if not company:
        return BoardLookup(company="", status=STATUS_NONE, note="no company name")

    cached = _cached_board(db, company)
    if cached is not None and cached.is_fresh(_cache_ttl_days(cached.status)):
        cached.hit_count = (cached.hit_count or 0) + 1
        db.commit()
        return BoardLookup(
            company=company,
            platform=cached.platform,
            token=cached.board_token,
            status=cached.status,
            note=cached.note,
            cached=True,
        )

    # 1. Free: read the token out of a URL we already hold.
    for url in _known_urls(db, user, company):
        identity = board_identity(url)
        if identity is not None:
            platform, token = identity
            lookup = BoardLookup(
                company=company,
                platform=platform,
                token=token,
                status=STATUS_OK,
                note=f"token read from a {label(platform)} link already on file",
            )
            _upsert_board(db, lookup)
            return lookup

    if not allow_probe:
        # Nothing learned and no budget to look. Deliberately *not* cached: this
        # is "we didn't check", and writing it as `none` would mean the company
        # never gets probed once the budget allows.
        return BoardLookup(
            company=company, status=STATUS_NONE, note="not checked this scan"
        )

    # 2. Bounded probe: the company's own slug, against each platform.
    session = session or _session()
    throttled = throttled if throttled is not None else set()
    unreadable: list[str] = []
    # Whether this lookup actually spent a request. A company whose every
    # platform was already throttled costs nothing, and the sweep's probe budget
    # exists to meter spend — charging it for a lookup that made no request
    # would end the sweep's discovery early over work that never happened.
    spent = False
    for token in _slug_candidates(company):
        for platform in PLATFORMS:
            if platform in throttled:
                # This platform asked us to slow down earlier in the sweep.
                # Asking it again, once per remaining company, is the retry
                # storm that got us throttled — and every answer it gives while
                # throttled is unusable anyway.
                unreadable.append(f"{label(platform)} (throttled)")
                continue
            spent = True
            try:
                jobs = fetch_board(
                    session, platform, token, company, limit=1, strict=True
                )
            except BoardUnavailable as exc:
                if exc.rate_limited:
                    throttled.add(platform)
                unreadable.append(f"{label(platform)} ({exc.reason})")
                continue
            if not jobs:
                continue
            lookup = BoardLookup(
                company=company,
                platform=platform,
                token=token,
                status=STATUS_OK,
                note=f"found by probing {label(platform)} for '{token}'",
                probed=spent,
            )
            _upsert_board(db, lookup)
            return lookup

    if unreadable:
        # At least one platform never answered the question, so "no board" is
        # not something this probe established — it is something it failed to
        # rule out. Recorded as an error, which is re-probed in hours rather
        # than in a fortnight.
        lookup = BoardLookup(
            company=company,
            status=STATUS_ERROR,
            note="could not read " + ", ".join(dict.fromkeys(unreadable))[:400],
            probed=spent,
        )
    else:
        lookup = BoardLookup(
            company=company,
            status=STATUS_NONE,
            note="no public board found on any supported platform",
            probed=spent,
        )
    _upsert_board(db, lookup)
    return lookup


# --------------------------------------------------------------------------- #
# The scan-facing entry point                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class BoardScan:
    """What one board sweep produced, for the scan report."""

    jobs: list[BoardJob] = field(default_factory=list)
    companies_checked: int = 0
    boards_found: int = 0
    probes_spent: int = 0
    notes: list[str] = field(default_factory=list)


def companies_to_sweep(db: Session, user: User, limit: int) -> list[str]:
    """Which employers are worth reading a board for, best prospects first.

    The candidate's own feed is the source: an aggregator told us this company is
    hiring for something we scored well, and the board is how we see the *rest*
    of what they have open — including the roles no aggregator caught. Ordered by
    the best fit score seen for that employer, so a bounded sweep spends itself on
    the companies the candidate actually cares about.

    Companies with a board already cached cost nothing to re-read, so they are not
    excluded; the cap is about probes, which :func:`resolve_board` meters
    separately.
    """
    ranked: dict[str, tuple[float, str]] = {}
    for posting in db.scalars(
        select(JobPosting)
        .where(
            JobPosting.user_id == user.id,
            JobPosting.company.is_not(None),
            JobPosting.duplicate_of_id.is_(None),
        )
        .order_by(JobPosting.id.desc())
        .limit(400)
    ):
        key = normalize_company(posting.company)
        if not key:
            continue
        score = posting.fit_score or 0.0
        current = ranked.get(key)
        if current is None or score > current[0]:
            ranked[key] = (score, posting.company)

    best = sorted(ranked.values(), key=lambda row: row[0], reverse=True)
    return [company for _score, company in best[:limit]]


def sweep(
    db: Session,
    user: User,
    companies: list[str] | None = None,
    *,
    session: requests.Session | None = None,
) -> BoardScan:
    """Read every reachable company board and return the postings found.

    Two budgets, because the costs are different in kind. Reading a board whose
    token is already known is one request for an employer's entire pipeline —
    cheap, so ``ats_board_companies_per_scan`` is generous. *Discovering* an
    unknown token costs up to one request per platform per spelling, so
    ``ats_board_probe_limit`` meters that separately and the rest of the companies
    are simply looked up for free and skipped if unknown. Over a few scans every
    employer in the feed gets its one probe.
    """
    scan = BoardScan()
    if not settings.ats_board_discovery_enabled:
        return scan

    session = session or _session()
    targets = (
        companies
        if companies is not None
        else companies_to_sweep(db, user, settings.ats_board_companies_per_scan)
    )
    probe_budget = settings.ats_board_probe_limit
    # One set for the whole sweep: a vendor that throttles us on the first
    # company has throttled us for the rest of them too, and probing it again
    # per employer is the behaviour that earned the 429 in the first place.
    throttled: set[str] = set()

    for company in targets:
        scan.companies_checked += 1
        lookup = resolve_board(
            db,
            user,
            company,
            session=session,
            allow_probe=probe_budget > 0,
            throttled=throttled,
        )
        if lookup.probed:
            # Charged whether or not it found anything — a probe that came back
            # empty spent exactly the same requests as one that succeeded.
            probe_budget -= 1
            scan.probes_spent += 1

        platform, token = lookup.platform, lookup.token
        if not platform or not token:
            continue

        jobs = fetch_board(
            session,
            platform,
            token,
            company,
            limit=settings.ats_board_jobs_per_company,
        )
        if not jobs:
            scan.notes.append(f"{company}: {label(platform)} board returned nothing")
            continue
        scan.boards_found += 1
        scan.jobs.extend(jobs)
        row = _cached_board(db, company)
        if row is not None:
            row.jobs_seen = len(jobs)
            db.commit()

    if throttled:
        scan.notes.append(
            "stopped probing "
            + ", ".join(sorted(label(p) for p in throttled))
            + " for this sweep after being rate limited"
        )
    return scan


__all__ = [
    "ASHBY",
    "FETCHERS",
    "GREENHOUSE",
    "LEVER",
    "PLATFORMS",
    "SMARTRECRUITERS",
    "STATUS_ERROR",
    "STATUS_NONE",
    "STATUS_OK",
    "WORKABLE",
    "BoardJob",
    "BoardUnavailable",
    "BoardLookup",
    "BoardScan",
    "board_identity",
    "board_url",
    "companies_to_sweep",
    "fetch_board",
    "label",
    "resolve_board",
    "source_name",
    "sweep",
]
