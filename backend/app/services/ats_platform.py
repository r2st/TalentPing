"""Which ATS is behind an application URL.

Every form-apply run starts here, because the platform decides everything after
it: Workday is a multi-step wizard behind a sign-in wall, Greenhouse is one page
(often inside an iframe on the company's own careers site), Lever and Ashby are
one page each at a predictable ``/apply`` / ``/application`` URL, iCIMS is one
page inside an iframe that most tenants put behind a candidate account, and
LinkedIn is a modal that only exists once you are signed in.

Detection is deliberately **pure string work** on the URL, with an HTML fallback
for the embedded case — a company careers page at ``careers.acme.com/jobs/123``
gives nothing away in its URL, but the Greenhouse iframe it renders does. No
network calls, so the whole module is unit-testable and cheap enough to run on
every posting in a scan.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from app.models.form_apply import ATSPlatform

# Ordered most-specific first. LinkedIn leads because a LinkedIn job page can
# link out to any of the others, and the marker we match on is the page we are
# actually going to open.
_URL_MARKERS: tuple[tuple[ATSPlatform, tuple[str, ...]], ...] = (
    (
        ATSPlatform.LINKEDIN,
        ("linkedin.com/jobs", "linkedin.com/comm/jobs", "lnkd.in/jobs"),
    ),
    (
        ATSPlatform.GREENHOUSE,
        (
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "my.greenhouse.io",
            "greenhouse.io/embed",
            "grnh.se",
            "gh_jid=",
        ),
    ),
    (
        ATSPlatform.LEVER,
        ("jobs.lever.co", "jobs.eu.lever.co", "hire.lever.co", "lever.co/postings"),
    ),
    (
        ATSPlatform.ASHBY,
        ("jobs.ashbyhq.com", "ashbyhq.com/embed", "app.ashbyhq.com", "ashbyhq.com/posting"),
    ),
    (
        # Every iCIMS tenant is a subdomain — ``careers-acme.icims.com``,
        # ``acme.icims.com`` — so the registrable domain is the marker.
        ATSPlatform.ICIMS,
        (".icims.com", "icims.com/jobs"),
    ),
    (
        ATSPlatform.WORKDAY,
        (
            ".myworkdayjobs.com",
            ".myworkdaysite.com",
            "myworkdayjobs.com",
            "/wday/",
            "workday.com/en-us/",
        ),
    ),
)

# The same platforms as they appear in embedded markup, for the careers-page
# case where the URL is the company's own domain.
_HTML_MARKERS: tuple[tuple[ATSPlatform, tuple[str, ...]], ...] = (
    (
        ATSPlatform.GREENHOUSE,
        ("grnhse_iframe", "boards.greenhouse.io", "greenhouse.io/embed", "gh_jid"),
    ),
    (ATSPlatform.LEVER, ("jobs.lever.co", "lever-jobs", "data-lever")),
    (ATSPlatform.ASHBY, ("jobs.ashbyhq.com", "ashby_embed", "ashby-job-board")),
    # iCIMS embeds itself in the company's own page as a same-named iframe, and
    # prefixes its markup so heavily that the vendor name is unambiguous.
    (ATSPlatform.ICIMS, ("icims_content_iframe", ".icims.com", "icims_")),
    (ATSPlatform.WORKDAY, ("myworkdayjobs.com", "data-automation-id")),
)

_LABELS: dict[ATSPlatform, str] = {
    ATSPlatform.LINKEDIN: "LinkedIn Easy Apply",
    ATSPlatform.WORKDAY: "Workday",
    ATSPlatform.GREENHOUSE: "Greenhouse",
    ATSPlatform.LEVER: "Lever",
    ATSPlatform.ASHBY: "Ashby",
    ATSPlatform.ICIMS: "iCIMS",
    ATSPlatform.GENERIC: "Career page",
    ATSPlatform.UNKNOWN: "Unknown",
}

# Platforms with a purpose-built adapter. GENERIC is *attemptable* — the
# best-effort filler — but it is not "supported" in the sense the UI means.
DEDICATED_PLATFORMS: frozenset[ATSPlatform] = frozenset(
    {
        ATSPlatform.LINKEDIN,
        ATSPlatform.WORKDAY,
        ATSPlatform.GREENHOUSE,
        ATSPlatform.LEVER,
        ATSPlatform.ASHBY,
        ATSPlatform.ICIMS,
    }
)

_LEVER_ID = re.compile(r"lever\.co/[^/]+/([0-9a-f-]{8,})", re.I)
_LINKEDIN_ID = re.compile(r"(?:/jobs/view/|currentJobId=)(\d{6,})", re.I)
_WORKDAY_JOB = re.compile(r"/job/[^/]+/([^/?#]+)", re.I)
# Ashby posting ids are UUIDs, one path segment after the company token.
_ASHBY_ID = re.compile(r"ashbyhq\.com/[^/]+/([0-9a-f]{8}-[0-9a-f-]{8,})", re.I)
_ICIMS_ID = re.compile(r"/jobs/(\d+)", re.I)

# Platforms whose form lives at a predictable suffix of the posting URL.
_APPLY_SUFFIX: dict[ATSPlatform, str] = {
    ATSPlatform.LEVER: "/apply",
    ATSPlatform.ASHBY: "/application",
}


def detect_platform(url: str | None) -> ATSPlatform:
    """The ATS behind *url*, from the URL alone.

    Returns :attr:`ATSPlatform.UNKNOWN` when there is nothing openable, and
    :attr:`ATSPlatform.GENERIC` for a real http(s) URL we don't recognise — the
    general-purpose filler still gets a shot at those.
    """
    if not url or not isinstance(url, str):
        return ATSPlatform.UNKNOWN

    candidate = url.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return ATSPlatform.UNKNOWN

    parsed = urlparse(candidate)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        # mailto:, tel:, javascript: — not something we can open and fill.
        return ATSPlatform.UNKNOWN
    if not parsed.scheme:
        # A bare host is common in pasted links, but "just some text" is not a
        # URL: require something domain-shaped before assuming https.
        if "." not in candidate.split("/", 1)[0]:
            return ATSPlatform.UNKNOWN
        parsed = urlparse(f"https://{candidate}")
    if not parsed.netloc:
        return ATSPlatform.UNKNOWN

    haystack = candidate.lower()
    for platform, markers in _URL_MARKERS:
        if any(marker in haystack for marker in markers):
            return platform
    return ATSPlatform.GENERIC


def detect_from_html(html: str | None) -> ATSPlatform:
    """The ATS embedded in a page's markup, for company-hosted job boards.

    Greenhouse and Lever are usually iframed into ``careers.<company>.com``,
    where the URL says nothing. Returns ``UNKNOWN`` when the markup gives
    nothing away — the caller keeps whatever the URL said.
    """
    if not html:
        return ATSPlatform.UNKNOWN
    haystack = html.lower()
    for platform, markers in _HTML_MARKERS:
        if any(marker in haystack for marker in markers):
            return platform
    return ATSPlatform.UNKNOWN


def resolve_platform(url: str | None, html: str | None = None) -> ATSPlatform:
    """Best available answer: the URL's verdict, refined by the markup.

    Markup only gets to speak when the URL was non-committal — a page served
    from ``jobs.lever.co`` is Lever even if it happens to mention Greenhouse.
    """
    from_url = detect_platform(url)
    if from_url is not ATSPlatform.GENERIC:
        return from_url
    from_html = detect_from_html(html)
    return from_html if from_html is not ATSPlatform.UNKNOWN else from_url


def label(platform: ATSPlatform) -> str:
    """Human-readable platform name, as the UI shows it."""
    return _LABELS.get(platform, platform.value)


def has_adapter(platform: ATSPlatform) -> bool:
    """True when a purpose-built adapter exists for *platform*."""
    return platform in DEDICATED_PLATFORMS


def is_attemptable(platform: ATSPlatform) -> bool:
    """True when there is anything worth opening a browser for."""
    return platform is not ATSPlatform.UNKNOWN


def external_job_id(url: str | None, platform: ATSPlatform | None = None) -> str | None:
    """The posting's id on its own platform, when the URL carries one.

    Used to build canonical apply URLs (Lever's ``/apply`` page, LinkedIn's job
    view) rather than driving whatever redirect chain the feed handed us.
    """
    if not url:
        return None
    platform = platform or detect_platform(url)

    if platform is ATSPlatform.LEVER:
        match = _LEVER_ID.search(url)
        return match.group(1) if match else None
    if platform is ATSPlatform.LINKEDIN:
        match = _LINKEDIN_ID.search(url)
        return match.group(1) if match else None
    if platform is ATSPlatform.GREENHOUSE:
        params = parse_qs(urlparse(url).query)
        for key in ("gh_jid", "token"):
            if params.get(key):
                return params[key][0]
        tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        return tail if tail.isdigit() else None
    if platform is ATSPlatform.WORKDAY:
        match = _WORKDAY_JOB.search(url)
        return match.group(1) if match else None
    if platform is ATSPlatform.ASHBY:
        match = _ASHBY_ID.search(url)
        return match.group(1) if match else None
    if platform is ATSPlatform.ICIMS:
        match = _ICIMS_ID.search(url)
        return match.group(1) if match else None
    return None


def apply_url(url: str, platform: ATSPlatform | None = None) -> str:
    """The URL that actually shows the application form.

    Lever and Ashby both split the posting and its form across two pages, and
    opening the posting means an extra click that sometimes lands on a cookie
    banner instead. Both suffixes are predictable, so we go straight there.
    Everyone else applies in place — including iCIMS, where the form is behind a
    tenant-specific redirect that only the page itself knows.

    **The suffix is a path segment, so it is added to the path.** This used to
    be string concatenation onto the end of the whole URL, which is the same
    thing right up until the URL carries a query — and a posting URL that
    reaches this product almost always does. Lever writes its own attribution
    parameter (``?lever-source=LinkedIn``), aggregators staple ``utm_source``
    on, and the feeds :mod:`app.services.job_search_service` reads store the
    URL exactly as they hand it over. So the form URL came out as
    ``.../123?lever-source=LinkedIn/apply``: the *posting* page, with a
    nonsense parameter value. :class:`~app.services.ats_adapters.LeverAdapter`
    opened it, found the posting rather than the form, and returned
    ``no_form`` — "No form controls on the page" for a job whose form was one
    path segment away, and the candidate is told the application could not be
    filled.

    The "is it already there?" test moves for the same reason and was wrong in
    the same cases, the other way round: a URL that was *already* the form page
    did not end with the suffix once a parameter trailed it, so it collected a
    second one.

    Query and fragment are carried through untouched rather than dropped —
    ``lever-source`` is how the employer's own dashboard attributes the
    application, and removing it would silently rewrite that.
    """
    platform = platform or detect_platform(url)
    suffix = _APPLY_SUFFIX.get(platform)
    if not suffix:
        return url
    # `urlsplit` on a schemeless URL puts the whole thing in `path`, which is
    # exactly where the suffix belongs anyway — nothing here invents a scheme.
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if path.endswith(suffix):
        return url
    return urlunsplit(parsed._replace(path=f"{path}{suffix}"))


__all__ = [
    "DEDICATED_PLATFORMS",
    "apply_url",
    "detect_from_html",
    "detect_platform",
    "external_job_id",
    "has_adapter",
    "is_attemptable",
    "label",
    "resolve_platform",
]
