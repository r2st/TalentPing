"""Career-page scraping — turn a company name into recruiter contact details.

The user names companies; this module finds who to email. It works in four
stages, each falling through to the next:

1. **Resolve a domain** for the company name (direct guess, then a search engine).
2. **Locate the careers page** — follow links off the homepage, then probe the
   conventional paths (``/careers``, ``/jobs``, …).
3. **Extract contacts** — ``mailto:`` links first (highest confidence), then bare
   addresses in the page text, then LinkedIn recruiter profiles.
4. **Fall back to role patterns** — ``careers@``, ``hr@``, ``talent@`` on the
   company domain, marked low-confidence so the caller can prefer real finds.

Results are cached globally by domain (see
:class:`~app.models.recruiter_cache.RecruiterCache`): one crawl of a company
serves every candidate targeting it.

Everything here is synchronous and network-bound — call it from a Celery worker,
not a request handler.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import outbound
from app.core.config import settings
from app.core.sql_text import ci_equals
from app.models.recruiter_cache import RecruiterCache
from app.services.places import fold_diacritics

logger = logging.getLogger(__name__)

# Role-based mailboxes worth trying on any company domain, best first.
ROLE_PREFIXES = (
    "careers",
    "jobs",
    "recruiting",
    "recruitment",
    "talent",
    "hr",
    "hiring",
    "people",
    "work",
)

# Prefixes that mean "a human who hires", used to rank scraped addresses.
_RECRUITING_HINTS = ROLE_PREFIXES + ("apply", "resume", "cv", "join")

# Addresses that are never a recruiter, however they were found.
_BLOCKED_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster", "abuse",
    "privacy", "legal", "security", "dmca", "unsubscribe", "bounce", "mailer-daemon",
    "support", "sales", "billing", "press", "media", "investors", "webmaster",
)
# Placeholder/vendor addresses that show up in templates and tracking pixels.
_BLOCKED_DOMAINS = (
    "example.com", "example.org", "sentry.io", "wixpress.com", "godaddy.com",
    "squarespace.com", "domain.com", "yourcompany.com", "email.com",
)

# Search results that are never an employer's own site. A trailing dot means
# "this name under any TLD" — google.com, google.co.uk, google.de.
_NON_EMPLOYER_HOSTS = (
    "duckduckgo.", "google.", "bing.", "wikipedia.", "linkedin.", "facebook.",
    "twitter.", "x.com", "instagram.", "youtube.", "reddit.", "glassdoor.",
    "indeed.", "crunchbase.", "bloomberg.", "pitchbook.", "zoominfo.",
    "namepros.", "godaddy.", "sedo.", "afternic.",
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
_LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w\-%]+", re.I)

# Link text / hrefs that signal a careers page.
_CAREER_LINK_HINTS = (
    "career", "careers", "jobs", "job-openings", "openings", "vacancies",
    "work-with-us", "work-for-us", "join-us", "join-our-team", "we-are-hiring",
    "hiring", "opportunities", "employment", "life-at", "open role",
    "open position", "current roles", "view roles",
)

# Applicant-tracking hosts. A link to one of these *is* the careers page, whatever
# the link text says ("Open roles", "We're hiring", or just a logo).
#
# ``workday`` used to be here as a bare word, which only worked because these
# were matched as substrings. It is spelled out as the hosts Workday actually
# serves from, so it survives the label matching below.
_ATS_HOSTS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "myworkdaysite.com",
    "workday.com", "ashbyhq.com", "smartrecruiters.com", "bamboohr.com",
    "jobvite.com", "recruitee.com", "workable.com", "teamtailor.com",
    "personio.com", "breezy.hr", "rippling.com",
)


# Conventional paths to probe when no homepage link points at careers.
_CAREER_PATHS = (
    "/careers", "/career", "/jobs", "/join-us", "/join", "/work-with-us",
    "/about/careers", "/company/careers", "/en/careers", "/careers/",
    "/about-us/careers", "/opportunities",
)

# Pages that usually list the humans (and their emails) behind the hiring.
_CONTACT_PATHS = ("/contact", "/contact-us", "/about/contact", "/about")

# Common personal-mail hosts — an address here is not a company recruiter.
_FREEMAIL = frozenset(
    {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
        "icloud.com", "protonmail.com", "proton.me", "mail.com", "gmx.com",
        "yandex.com", "zoho.com", "live.com", "msn.com",
    }
)

# Legal-entity suffixes stripped before guessing a domain from a company name.
_COMPANY_SUFFIXES = (
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "gmbh", "plc", "sa", "ag", "bv", "pty", "group", "holdings", "technologies",
    "technology", "labs", "software", "solutions", "systems",
)


def host_matches(host: str | None, patterns: tuple[str, ...]) -> bool:
    """Whether *host* is one of *patterns*, or a subdomain of one.

    Both lists above were matched with bare ``in``, and a hostname is the one
    kind of string where substring containment is never the question being
    asked — the boundaries are the dots, and every one of these names appears
    inside unrelated domains:

    * ``"x.com"`` is inside **linux.com**, **flex.com** and **onex.com**, and
      ``"google."`` is inside **notgoogle.com**. All four are employers, and all
      four were struck out of :func:`_search_domain`'s candidate list — so the
      company's own site was never opened, no careers page was found, and the
      employer contributed nothing to the feed.
    * ``"lever.co"`` is inside **clever.com**. A link to one there read as an
      applicant-tracking host, which is the one verdict in
      :func:`find_careers_links` that waives the same-site rule: the scraper
      followed it off-site and scraped a stranger's pages for jobs.

    A pattern ending in a dot names a second-level name under any TLD, which is
    what :data:`_NON_EMPLOYER_HOSTS` has always meant by "google." — it has to
    cover google.com, google.co.uk and google.de without listing them.
    Everything else is a full domain, matched exactly or as a parent.
    """
    labels = (host or "").strip().lower().strip(".").split(".")
    if labels == [""]:
        return False
    for pattern in patterns:
        name = pattern.lower().strip(".")
        if pattern.endswith("."):
            if name in labels:
                return True
        else:
            wanted = name.split(".")
            if labels[-len(wanted):] == wanted:
                return True
    return False


@dataclass
class Contact:
    """One discovered way to reach a company's hiring side."""

    email: str
    name: str | None = None
    title: str | None = None
    # careers_page | contact_page | pattern
    kind: str = "careers_page"
    confidence: float = 0.5
    source_url: str | None = None
    linkedin_url: str | None = None

    def as_dict(self) -> dict:
        return {
            "email": self.email,
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "confidence": round(self.confidence, 2),
            "source_url": self.source_url,
            "linkedin_url": self.linkedin_url,
        }


@dataclass
class ScrapeResult:
    company: str
    domain: str | None = None
    contacts: list[Contact] = field(default_factory=list)
    careers_url: str | None = None
    source_url: str | None = None
    status: str = "ok"  # ok | empty | error
    note: str | None = None
    from_cache: bool = False

    @property
    def emails(self) -> list[str]:
        return [c.email for c in self.contacts]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": settings.scraper_user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _href(anchor) -> str:
    """Read an anchor's href as a string (bs4 types multi-valued attrs as lists)."""
    value = anchor.get("href") or ""
    return (value[0] if isinstance(value, list) else value).strip()


def _fetch(session: requests.Session, url: str) -> tuple[str | None, str | None]:
    """GET a URL → (html, final_url). Returns (None, None) on any failure.

    Scraping third-party sites fails constantly and unremarkably (timeouts, 403s,
    bot walls). Callers treat a miss as "try the next candidate", so nothing here
    raises.

    A URL refused by :mod:`app.core.outbound` is one more unremarkable miss, and
    the guard matters here for a reason it does not on a pasted link: most of
    what this fetches is an ``href`` lifted off somebody else's page, so a
    careers page that links to ``http://169.254.169.254/`` picks the target of
    our next request. It is the classic second hop, and it is why the check
    lives around the fetch rather than around the endpoint.
    """
    try:
        resp = outbound.safe_get(session, url, timeout=settings.scraper_timeout_seconds)
    except outbound.BlockedURLError as exc:
        logger.debug("fetch refused %s: %s", url, exc)
        return None, None
    except requests.RequestException as exc:
        logger.debug("fetch failed %s: %s", url, exc)
        return None, None
    if resp.status_code != 200:
        logger.debug("fetch %s returned %s", url, resp.status_code)
        return None, None
    if "html" not in resp.headers.get("Content-Type", "").lower():
        return None, None
    return outbound.page_text(resp), resp.url


# --------------------------------------------------------------------------- #
# Domain resolution
# --------------------------------------------------------------------------- #


def slugify_company(company: str) -> str:
    r"""'Acme Technologies, Inc.' -> 'acme'.

    Folded onto ASCII first, because the two things this slug is measured
    against are both ASCII and it was neither. `\w` is Unicode-aware, so
    "Société Générale" came out of here as ``sociétégénérale`` — a hostname
    guess no resolver will answer, and a needle that
    :func:`_looks_like_company_site` searches for in a haystack it has just
    stripped every non-ASCII character out of. That comparison could not
    succeed, so *every* company with an accent in its name failed
    verification: the direct ``<slug>.<tld>`` guess was always rejected, and
    so was every result the search fallback then turned up.
    """
    cleaned = re.sub(r"[^\w\s-]", " ", fold_diacritics(company.lower()))
    words = [w for w in cleaned.split() if w and w not in _COMPANY_SUFFIXES]
    return "".join(words) or re.sub(r"[^a-z0-9]", "", fold_diacritics(company.lower()))


def normalize_domain(value: str) -> str | None:
    """Extract a bare registrable domain from a URL, email, or hostname."""
    if not value:
        return None
    text = value.strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if "//" in text:
        text = urlparse(text).netloc or text.split("//", 1)[1]
    text = text.split("/", 1)[0].split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", text) else None


def _search_domain(session: requests.Session, company: str) -> str | None:
    """Find a company's domain via DuckDuckGo's no-JS HTML endpoint."""
    try:
        resp = outbound.capped_get(
            session,
            "https://duckduckgo.com/html/",
            params={"q": f"{company} official website careers"},
            timeout=settings.scraper_timeout_seconds,
        )
    except requests.RequestException as exc:
        logger.debug("domain search failed for %s: %s", company, exc)
        return None
    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(outbound.page_text(resp), "html.parser")
    slug = slugify_company(company)

    candidates: list[str] = []
    for anchor in soup.select("a.result__a, a.result__url, a[href]"):
        domain = normalize_domain(_href(anchor))
        if not domain or domain in candidates:
            continue
        if host_matches(domain, _NON_EMPLOYER_HOSTS):
            continue
        # A domain containing the company slug goes to the front of the queue.
        if slug and slug[:6] in domain.replace("-", ""):
            candidates.insert(0, domain)
        else:
            candidates.append(domain)

    # Verify before trusting: search results for a generic company name ("Linear",
    # "Ramp") are full of unrelated sites, and an unvalidated guess would send
    # someone's resume to a stranger.
    for domain in candidates[:5]:
        html, final = _fetch(session, f"https://{domain}")
        if html and _looks_like_company_site(html, company, final):
            return normalize_domain(final or "") or domain
    return None


def _looks_like_company_site(html: str, company: str, final_url: str | None = None) -> bool:
    """Does this page actually belong to *company*, or did we hit a squatter?

    A guessed domain that merely responds proves nothing: ``linear.io`` serves a
    parked page and ``linear.co`` redirects to a domain-sale listing, while the
    real company is at ``linear.app``.

    The company name must appear in the ``<title>`` or a site-identifying meta
    tag. Body text deliberately does *not* count — a "linear.co is for sale"
    listing mentions the word too, and accepting it would mean emailing a
    stranger's resume to a domain broker.
    """
    slug = slugify_company(company)
    if not slug:
        return False
    # A redirect into a known non-employer host settles it regardless of content.
    landed = normalize_domain(final_url or "") or ""
    if landed and host_matches(landed, _NON_EMPLOYER_HOSTS):
        return False

    soup = BeautifulSoup(html, "html.parser")
    signals = [soup.title.get_text(" ", strip=True) if soup.title else ""]
    for name in ("og:site_name", "og:title", "application-name", "description"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find(
            "meta", attrs={"property": name}
        )
        content = tag.get("content") if hasattr(tag, "get") else None
        if content:
            signals.append(str(content))

    # Folded on this side too. The strip below keeps `a-z0-9` and nothing else,
    # so a title reading "Société Générale" became "socitgnrale" — which is not
    # what the slug says however the slug is spelled. Both sides fold, so this
    # can only make two spellings of one name agree.
    haystack = re.sub(r"[^a-z0-9]", "", fold_diacritics(" ".join(signals).lower()))
    return slug in haystack


def resolve_domain(company: str, session: requests.Session | None = None) -> str | None:
    """Best-effort company name -> domain.

    Tries ``<slug>.<tld>`` first (right far more often than not, and free) but
    only accepts a hit that actually looks like the company's own site. Falls
    back to a web search when no guess is convincing.
    """
    direct = normalize_domain(company)
    if direct:  # caller already passed a domain or URL
        return direct

    owns_session = session is None
    session = session or _session()
    try:
        slug = slugify_company(company)
        for tld in (".com", ".io", ".ai", ".co", ".app", ".dev"):
            candidate = f"{slug}{tld}"
            html, final = _fetch(session, f"https://{candidate}")
            if html is None:
                continue
            if _looks_like_company_site(html, company, final):
                # A redirect is authoritative: prefer where we landed.
                return normalize_domain(final or "") or candidate
            logger.debug("%s responded but does not look like %s", candidate, company)
        return _search_domain(session, company)
    finally:
        if owns_session:
            session.close()


# --------------------------------------------------------------------------- #
# Careers page discovery
# --------------------------------------------------------------------------- #


def find_careers_links(html: str, base_url: str) -> list[str]:
    """Return same-site links off a page that look like careers pages."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = normalize_domain(base_url)
    found: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href = _href(anchor)
        if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        host = normalize_domain(absolute) or ""
        on_ats = host_matches(host, _ATS_HOSTS)

        label = f"{href.lower()} {anchor.get_text(' ', strip=True).lower()}"
        # An ATS host is self-evidently the careers page; anything else has to
        # say so in its href or link text.
        if not on_ats and not any(hint in label for hint in _CAREER_LINK_HINTS):
            continue
        # Off-site links that aren't an ATS belong to someone else's job board.
        if host and host != base_host and not on_ats:
            continue
        if absolute not in found:
            found.append(absolute)
        if len(found) >= 6:
            break
    return found


def find_careers_page(domain: str, session: requests.Session) -> tuple[str | None, str | None]:
    """Locate a company's careers page → (url, html), or (None, None)."""
    home_html, home_url = _fetch(session, f"https://{domain}")
    if home_html:
        for link in find_careers_links(home_html, home_url or f"https://{domain}"):
            html, final = _fetch(session, link)
            if html:
                return final or link, html

    for path in _CAREER_PATHS:
        url = f"https://{domain}{path}"
        html, final = _fetch(session, url)
        if html:
            return final or url, html
    return None, None


# --------------------------------------------------------------------------- #
# Contact extraction
# --------------------------------------------------------------------------- #


def _is_usable_email(email: str, domain: str | None) -> bool:
    """Reject noreply/support/vendor addresses and obvious junk."""
    email = email.lower().strip(".,;:)")
    if email.count("@") != 1 or len(email) > 254:
        return False
    local, host = email.split("@")
    if not local or host in _BLOCKED_DOMAINS or host in _FREEMAIL:
        return False
    if any(local.startswith(prefix) for prefix in _BLOCKED_PREFIXES):
        return False
    # Image/asset filenames sometimes survive the regex ("logo@2x.png").
    if re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", email):
        return False
    # Keep the company's own domain and its subdomains only. Third-party
    # addresses on a careers page belong to vendors, not the employer.
    return not domain or (
        host == domain or host.endswith(f".{domain}") or domain.endswith(f".{host}")
    )


def _names_a_role(local: str) -> bool:
    """Whether an address's local part *is* a hiring mailbox, not merely starts like one.

    The test was a bare ``startswith``, and two of the hints are two letters
    long: "hr" opens **hristov**, **hrabowski** and **hruska**, and "cv" opens
    **cvetkovic** and **cvijanovic**. Every one of those is a person, and the
    0.95 this function hands out is not decoration —
    :func:`app.services.recruiter_discovery.contact_priority` ranks on it, and
    auto-apply mails the single highest-ranked contact. So an engineer whose
    address happened to appear on a careers page beat that company's own
    ``careers@`` and received the candidate's application: the wrong recipient,
    and a stranger who never published themselves as a hiring contact deciding
    what to do with an unsolicited email sent from the candidate's own Gmail.

    So a hint has to end where the local part does, or at something that is not
    a letter — ``hr@``, ``hr-team@``, ``careers.eu@``, ``jobs2026@``.

    The asymmetry is what makes the strict rule safe, and it runs the opposite
    way to :data:`_BLOCKED_PREFIXES` (which stays a plain ``startswith``, and
    should: over-blocking loses one contact, under-blocking mails a job
    application to a support desk). Here over-matching is the expensive error
    and under-matching is nearly free — ``hrteam@`` no longer earns the boost,
    but it is still a careers-page find at 0.75, which outranks every guessed
    address.
    """
    return any(
        local.startswith(hint) and not local[len(hint) : len(hint) + 1].isalpha()
        for hint in _RECRUITING_HINTS
    )


def _score_email(email: str, kind: str) -> float:
    """Rank a contact: a scraped hiring mailbox beats a guessed one."""
    local = email.split("@")[0].lower()
    if kind == "pattern":
        # Guessed addresses are plausible but unverified.
        return 0.4 if local in ROLE_PREFIXES[:4] else 0.3
    if _names_a_role(local):
        return 0.95
    if kind == "careers_page":
        return 0.75
    return 0.55


def extract_contacts(html: str, source_url: str, domain: str | None) -> list[Contact]:
    """Pull recruiter contacts out of a page's HTML.

    ``mailto:`` links are trusted most — someone deliberately published them.
    Bare addresses in the text come next, then LinkedIn recruiter profiles (which
    carry no email but are still a usable lead).
    """
    soup = BeautifulSoup(html, "html.parser")
    kind = "contact_page" if "/contact" in source_url.lower() else "careers_page"
    by_email: dict[str, Contact] = {}

    def _add(email: str, *, name: str | None = None, bump: float = 0.0) -> None:
        email = email.lower().strip(".,;:)")
        if not _is_usable_email(email, domain) or email in by_email:
            return
        by_email[email] = Contact(
            email=email,
            name=name,
            kind=kind,
            confidence=min(1.0, _score_email(email, kind) + bump),
            source_url=source_url,
        )

    # 1. mailto: links — the author meant these to be contacted.
    for anchor in soup.select('a[href^="mailto:"]'):
        address = _href(anchor)[7:].split("?", 1)[0].strip()
        label = anchor.get_text(" ", strip=True)
        # Use the link text as a name only when it isn't just the address again.
        name = label if label and "@" not in label and len(label) <= 60 else None
        _add(address, name=name, bump=0.05)

    # 2. Bare addresses in the visible text.
    for match in _EMAIL_RE.finditer(soup.get_text(" ", strip=True)):
        _add(match.group(0))

    contacts = sorted(by_email.values(), key=lambda c: c.confidence, reverse=True)

    # 3. LinkedIn recruiter profiles — attach to a contact, or stand alone.
    linkedin = [m.group(0) for m in _LINKEDIN_RE.finditer(html)][:3]
    # Pairs off as far as the shorter list goes — either may be empty.
    for url, contact in zip(linkedin, contacts, strict=False):
        contact.linkedin_url = url

    return contacts


def pattern_contacts(domain: str, limit: int = 3) -> list[Contact]:
    """Guess role-based mailboxes for a domain when scraping found nothing.

    These are unverified — ``careers@`` exists at most companies but not all — so
    they carry low confidence and are only used as a last resort.
    """
    return [
        Contact(
            email=f"{prefix}@{domain}",
            kind="pattern",
            confidence=_score_email(f"{prefix}@{domain}", "pattern"),
            source_url=f"https://{domain}",
        )
        for prefix in ROLE_PREFIXES[:limit]
    ]


# --------------------------------------------------------------------------- #
# Orchestration + cache
# --------------------------------------------------------------------------- #


def scrape_company(
    company: str, domain: str | None = None, *, allow_patterns: bool = True
) -> ScrapeResult:
    """Crawl one company for recruiter contacts. Never raises."""
    result = ScrapeResult(company=company)
    session = _session()
    try:
        resolved = normalize_domain(domain or "") or resolve_domain(company, session)
        if not resolved:
            result.status = "empty"
            result.note = "Could not resolve a website for this company"
            return result
        result.domain = resolved

        careers_url, careers_html = find_careers_page(resolved, session)
        contacts: list[Contact] = []
        if careers_html:
            result.careers_url = careers_url
            result.source_url = careers_url
            contacts = extract_contacts(careers_html, careers_url or "", resolved)

        # Careers pages are often pure ATS embeds with no address on them; the
        # contact page is the usual place the humans are listed.
        if not contacts:
            for path in _CONTACT_PATHS:
                html, final = _fetch(session, f"https://{resolved}{path}")
                if not html:
                    continue
                contacts = extract_contacts(html, final or f"https://{resolved}{path}", resolved)
                if contacts:
                    result.source_url = final or f"https://{resolved}{path}"
                    break

        if not contacts and allow_patterns:
            contacts = pattern_contacts(resolved)
            result.source_url = result.source_url or f"https://{resolved}"
            result.note = "No published address found — using role-based patterns"

        result.contacts = contacts[: settings.max_contacts_per_company]
        result.status = "ok" if result.contacts else "empty"
        return result
    except Exception as exc:  # noqa: BLE001 - a bad page must not kill a campaign
        logger.warning(
            "scrape_company(%s) failed: %s", company, exc, exc_info=True
        )
        result.status = "error"
        result.note = str(exc)[:300]
        return result
    finally:
        session.close()


def _cache_ttl_days(status: str | None) -> float:
    """How long a cached crawl of this kind may be reused.

    ``ok`` and ``empty`` are findings about the employer: these are the
    recruiting addresses published on their site, or there are none to find.
    Those hold for the month ``recruiter_cache_ttl_days`` allows.

    ``error`` is not a finding. :func:`scrape_company` sets it when the crawl
    raised — a DNS failure, a refused connection, an expired certificate, a WAF
    serving a challenge page — and it is written to the same deployment-wide row
    every user reads. Trusted for a month, one unreachable afternoon meant every
    candidate who ever targets that employer got "no contacts found" until
    September, with no crawl in between to notice the site had come back.

    Hours instead, so the next campaign to name the company simply tries again.
    """
    if status == "error":
        return settings.recruiter_cache_error_ttl_hours / 24
    return float(settings.recruiter_cache_ttl_days)


def _result_from_cache(row: RecruiterCache) -> ScrapeResult:
    return ScrapeResult(
        company=row.company,
        domain=row.domain,
        contacts=[
            Contact(
                email=c["email"],
                name=c.get("name"),
                title=c.get("title"),
                kind=c.get("kind", "careers_page"),
                confidence=float(c.get("confidence", 0.5)),
                source_url=c.get("source_url"),
                linkedin_url=c.get("linkedin_url"),
            )
            for c in (row.contacts or [])
            if c.get("email")
        ],
        careers_url=row.careers_url,
        source_url=row.source_url,
        status=row.status,
        note=row.note,
        from_cache=True,
    )


def get_or_scrape(
    db: Session, company: str, domain: str | None = None, *, force: bool = False
) -> ScrapeResult:
    """Return cached contacts for a company, crawling only when stale or absent.

    The cache is global on purpose: the recruiting contacts for a company are the
    same for every candidate, so the first user to target it pays the crawl and
    everyone after reads the row.

    That sharing is also why the name fallback below matches with
    :func:`~app.core.sql_text.ci_equals` rather than ``ilike``. ``ilike`` reads
    as "equals, ignoring case" and is not: the company name arrives from a
    user's ``target_companies``, so ``%`` and ``_`` in it are wildcards against
    a table holding *every* company anyone has ever crawled. A candidate
    targeting the real brand ``100% PURE`` matched whichever row the database
    returned first, and the damage ran both ways — the crawl came back with
    somebody else's recruiters, and then the write below stamped this user's
    company name onto that shared row, so the next candidate to target the
    company it really belonged to read it under the wrong name.
    """
    lookup = normalize_domain(domain or "") or None
    row: RecruiterCache | None = None

    if lookup:
        row = db.scalar(select(RecruiterCache).where(RecruiterCache.domain == lookup))
    if row is None:
        row = db.scalar(
            select(RecruiterCache).where(ci_equals(RecruiterCache.company, company))
        )

    if row is not None and not force and row.is_fresh(_cache_ttl_days(row.status)):
        row.hit_count += 1
        db.commit()
        return _result_from_cache(row)

    result = scrape_company(company, domain or (row.domain if row else None))
    if not result.domain:
        # Nothing to key a cache row on; report without persisting.
        return result

    existing = row if (row and row.domain == result.domain) else db.scalar(
        select(RecruiterCache).where(RecruiterCache.domain == result.domain)
    )
    if existing is None:
        existing = RecruiterCache(company=company.strip(), domain=result.domain)
        db.add(existing)
        _apply_scrape(existing, company, result)
        try:
            db.commit()
        except IntegrityError:
            # `uq_recruiter_cache_domain` is deployment-wide, and this cache is
            # shared on purpose — so read-then-insert races any other campaign
            # targeting the same employer, including one belonging to a
            # different user. That is the ordinary case rather than an exotic
            # one: two candidates in the same field target the same twenty
            # companies. Losing the race raised out of the commit, and the
            # caller is `discover_for_companies` inside `run_autopilot`, so one
            # unlucky company marked the whole campaign FAILED and discarded the
            # contacts already found for every other.
            db.rollback()
            logger.debug(
                "recruiter cache for %s was written by a concurrent crawl",
                result.domain,
            )
            existing = db.scalar(
                select(RecruiterCache).where(RecruiterCache.domain == result.domain)
            )
            if existing is None:
                # The constraint fired for a reason we did not predict. Re-raise
                # rather than reporting a crawl that was never persisted.
                raise
            # The winner answered the same question about the same employer;
            # this answer is merely the fresher one, so it goes on top.
            _apply_scrape(existing, company, result)
            db.commit()
        return _reported(existing, result)

    _apply_scrape(existing, company, result)
    db.commit()
    return _reported(existing, result)


def _reported(row: RecruiterCache, result: ScrapeResult) -> ScrapeResult:
    """What this caller is told, given what the row ended up holding.

    A crawl that failed over a row with findings on it leaves those findings in
    place (see :func:`_apply_scrape`), and this hands them to the caller that
    triggered the failed crawl as well — rather than an empty result, which is
    what ``recruiter_discovery`` reads as "this employer publishes no contacts"
    and acts on.

    Both write paths go through here, including the one that lost the insert
    race: the winner's contacts are on the row either way, so the loser must not
    report an emptier answer than the row it just wrote to.
    """
    if result.status == "error" and (row.contacts or row.emails):
        return _result_from_cache(row)
    return result


def _apply_scrape(row: RecruiterCache, company: str, result: ScrapeResult) -> None:
    """Write one crawl's findings onto a cache row.

    A crawl that *errored* found nothing, and "found nothing" is not the same
    claim as "there is nothing". So it records that the attempt failed and when,
    and leaves the findings alone — where an earlier crawl found four recruiting
    addresses, those four are still the best answer anyone has.

    They used to be overwritten. Every field here was assigned unconditionally,
    so a timeout on a site that had been crawled successfully replaced its
    contacts with an empty list, on the row *every* user reads. The campaign
    that triggered the failed crawl lost the addresses, and so did everyone
    after it — the contacts were not re-derivable, because the next lookup read
    the same emptied row back out of the cache rather than crawling again.

    The status is still stamped, because that is what shortens the row's
    lifetime (see :func:`_cache_ttl_days`) and gets the site re-crawled in hours.
    """
    row.company = company.strip() or row.company
    row.status = result.status
    row.note = result.note
    row.scraped_at = datetime.now(UTC)
    if result.status == "error" and (row.contacts or row.emails):
        logger.info(
            "crawl of %s failed; keeping the %s contact(s) already on file",
            row.domain,
            len(row.contacts or []),
        )
        return
    row.emails = result.emails
    row.contacts = [c.as_dict() for c in result.contacts]
    row.careers_url = result.careers_url
    row.source_url = result.source_url
