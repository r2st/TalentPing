"""Turn a campaign's targets into recruiter rows the sender can use.

Sits between the raw scraper and the outreach pipeline:

* expands industries into concrete company names (LLM, with a static fallback),
* runs each company through the globally-cached scraper,
* upserts the results as :class:`~app.models.recruiter.Recruiter` rows for the
  campaign's owner, skipping anything they already have or have been asked not
  to contact.
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.recruiter import Recruiter
from app.models.user import User
from app.services import bounce_service
from app.services.career_scraper import Contact, get_or_scrape
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    llm_is_configured,
)

logger = logging.getLogger(__name__)

# Used when no LLM is configured (or it fails) so industry targeting still works.
_INDUSTRY_FALLBACK: dict[str, list[str]] = {
    "tech": ["Stripe", "Shopify", "Datadog", "Cloudflare", "Atlassian", "Twilio",
             "HashiCorp", "GitLab", "Snowflake", "MongoDB"],
    "fintech": ["Stripe", "Plaid", "Wise", "Revolut", "Adyen", "Chime", "Brex",
                "Ramp", "Marqeta", "Robinhood"],
    "healthcare": ["Oscar Health", "Ro", "Cedar", "Komodo Health", "Tempus",
                   "Included Health", "Hinge Health", "Omada Health"],
    "ecommerce": ["Shopify", "Etsy", "Instacart", "Wayfair", "Faire", "Klarna",
                  "BigCommerce", "Shipbob"],
    "ai": ["Anthropic", "Hugging Face", "Scale AI", "Weights & Biases",
           "Cohere", "Runway", "Perplexity", "Together AI"],
    "gaming": ["Riot Games", "Epic Games", "Unity", "Roblox", "Discord",
               "Bungie", "Supercell"],
    "security": ["Cloudflare", "Okta", "1Password", "Snyk", "Wiz", "Tailscale",
                 "CrowdStrike", "Datadog"],
    "data": ["Snowflake", "Databricks", "dbt Labs", "Fivetran", "Airbyte",
             "Confluent", "Starburst"],
}

_COMPANIES_PER_INDUSTRY = 8

_INDUSTRY_PROMPT = (
    "List real companies that actively hire for the given role and industry. "
    'Return ONLY a JSON array of company names, e.g. ["Stripe", "Shopify"]. '
    "No commentary, no markdown fence. Prefer well-known employers with public "
    "careers pages."
)


def _parse_company_list(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    return [
        item.strip()[:120]
        for item in data
        if isinstance(item, str) and 1 < len(item.strip()) <= 120
    ]


def companies_for_industry(industry: str, roles: list[str] | None = None) -> list[str]:
    """Suggest companies hiring in *industry*, LLM-first with a static fallback."""
    fallback = _INDUSTRY_FALLBACK.get(industry.strip().lower(), [])

    if llm_is_configured():
        role_hint = f" hiring for: {', '.join(roles[:3])}" if roles else ""
        try:
            raw = chat_completion(
                [
                    {"role": "system", "content": _INDUSTRY_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Industry: {industry}{role_hint}. "
                            f"Return {_COMPANIES_PER_INDUSTRY} companies."
                        ),
                    },
                ],
                model=settings.openrouter_model,
                temperature=0.4,
                max_tokens=300,
            )
            suggested = _parse_company_list(raw)
            if suggested:
                return suggested[:_COMPANIES_PER_INDUSTRY]
        except OpenRouterError as exc:
            logger.info("industry expansion fell back to static list: %s", exc)

    return fallback[:_COMPANIES_PER_INDUSTRY]


def expand_targets(
    companies: list[str], industries: list[str], roles: list[str] | None = None
) -> list[str]:
    """Merge explicit companies with industry-derived ones, de-duplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for name in [*companies, *(c for i in industries for c in companies_for_industry(i, roles))]:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())
    return out


def _upsert_recruiter(
    db: Session, user: User, company: str, contact: Contact
) -> Recruiter | None:
    """Create (or return) a recruiter row for a discovered contact.

    Returns ``None`` when the address already exists for this user — the caller
    treats that as "nothing new", not an error.
    """
    email = contact.email.lower()
    existing = db.scalar(
        select(Recruiter).where(Recruiter.user_id == user.id, Recruiter.email == email)
    )
    if existing is not None:
        # Enrich a thin manual entry with what the crawl learned.
        if not existing.company:
            existing.company = company
        if not existing.source_url:
            existing.source_url = contact.source_url
        return None

    recruiter = Recruiter(
        user_id=user.id,
        email=email,
        name=contact.name,
        title=contact.title,
        company=company,
        linkedin_url=contact.linkedin_url,
        source="pattern" if contact.kind == "pattern" else "careers_page",
        source_url=contact.source_url,
        confidence=contact.confidence,
    )
    db.add(recruiter)
    return recruiter


def _upsert_and_commit(
    db: Session, user: User, company: str, contacts: list[Contact]
) -> int:
    """Upsert every contact for one company and commit. Returns how many are new.

    The commit is retried once, because ``_upsert_recruiter`` is a read followed
    by an insert and ``uq_recruiter_user_email`` is what sits between them. Two
    runs for the same user reaching the same employer is the ordinary case rather
    than an exotic one — the beat autopilot and a campaign the candidate started
    by hand target the same list, and a company that appears under two industries
    is scraped twice inside a single run. The loser used to raise ``IntegrityError``
    out of this commit, and the caller is ``discover_for_companies`` inside
    ``run_autopilot``, so one racing insert marked the whole campaign FAILED and
    threw away the contacts already found for every other company.

    Retrying is safe precisely because the two writers are writing the same
    thing: the address the race was lost to is this address, for this user. The
    second pass therefore finds it committed, takes the enrichment branch instead
    of the insert, and reports it as "already in your list" — which is what it is.
    """
    def _pass() -> int:
        return sum(
            1
            for contact in contacts
            if _upsert_recruiter(db, user, company, contact) is not None
        )

    added = _pass()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.debug(
            "recruiter rows for %s were written by a concurrent run", company
        )
        added = _pass()
        # Not caught: a second failure is not the race — the row this pass read
        # back is committed — so it is a constraint we have not accounted for,
        # and reporting contacts that were never stored would be worse.
        db.commit()
    return added


# Titles that mark a contact as someone who actually hires — worth a boost over a
# generic mailbox when we have to pick one contact for an auto-applied outreach.
_RECRUITER_TITLE_HINTS = (
    "recruit", "talent", "hiring", "people", "sourcer", "staffing", "hr ", "head of",
)


def contact_priority(contact: Contact) -> float:
    """Rank a contact for *auto-apply*: higher goes first. Quality over quantity.

    Starts from the scraper's confidence (a published ``mailto:`` beats a guessed
    ``careers@``), then nudges up for a recruiter-shaped title and down for a
    pattern-guessed address. Smart targeting, roadmap improvement 5.
    """
    score = contact.confidence
    title = (contact.title or "").lower()
    if any(hint in title for hint in _RECRUITER_TITLE_HINTS):
        score += 0.15
    if contact.kind == "pattern":
        score -= 0.10
    return score


def prioritize_contacts(
    contacts: list[Contact], *, limit: int | None = None
) -> list[Contact]:
    """Re-rank discovered contacts best-first, optionally capping the count."""
    ranked = sorted(contacts, key=contact_priority, reverse=True)
    return ranked[:limit] if limit is not None else ranked


def best_recruiter_for_company(
    db: Session, user: User, company: str, *, min_confidence: float = 0.0
) -> tuple[Recruiter | None, str | None]:
    """Pick the single best contactable recruiter at *company*, creating the row.

    Used by the auto-apply pipeline, which emails *one* well-chosen contact per
    role rather than blasting every address — the deliberate opposite of
    spray-and-pray. Returns ``(recruiter, skip_reason)``.

    **Two reasons a row is passed over, not one.** ``opted_out`` is consent and
    was always checked here. A hard bounce is deliverability, and was not: this
    function happily returned an address that "no such user" had already been
    said about, because the only place that reads ``DeliveryState`` before
    composing is ``outreach_service.generate_drafts_for_campaign`` — the pipeline
    a user launches by hand. Auto-apply, the one that runs while they are asleep,
    came through here.

    The cost was not one wasted send. Each posting at that company spent an LLM
    resume tailoring, a cover letter and an outreach composition; wrote an
    ``Application``, a thread and an email; and marked the ``JobPosting``
    ``APPLIED`` — so the pipeline board and the funnel counted an application
    that the send-time backstop then wrote off as undeliverable. The same shape
    of gap ``spam_risk.screen`` was written to close, on the same two callers.

    Checked inside the loop rather than in front of it, which is the better half
    of the fix: a company whose best contact is dead now falls through to the
    next one instead of failing the company outright.
    """
    result = get_or_scrape(db, company)
    if not result.contacts:
        return None, result.note or "no contacts found"

    undeliverable = 0
    for contact in prioritize_contacts(result.contacts):
        if contact.confidence < min_confidence:
            continue
        _upsert_and_commit(db, user, company, [contact])
        row = db.scalar(
            select(Recruiter).where(
                Recruiter.user_id == user.id,
                Recruiter.email == contact.email.lower(),
            )
        )
        if row is None or row.opted_out or row.is_excluded:
            continue
        if bounce_service.is_suppressed(row):
            undeliverable += 1
            continue
        return row, None

    if undeliverable:
        # Named separately so the run's notes distinguish "we couldn't find
        # anyone good" from "the people we know about are undeliverable", which
        # are different problems with different remedies.
        return None, f"every known contact at {company} has hard-bounced"
    return None, "no contact met the quality bar"


def discover_for_companies(
    db: Session, user: User, companies: list[str], *, per_company: int | None = None
) -> tuple[list[int], list[str]]:
    """Scrape each company and add its contacts to *user*'s recruiter list.

    Returns ``(new_recruiter_ids, notes)``. Notes are human-readable per-company
    outcomes ("Acme: no contacts found") — surfaced in the tracker rather than
    raised, because a partial result is still a usable campaign.

    A hard-bounced address is left out of the returned ids for the same reason
    an opted-out one is: it is not a contact this campaign can use.
    ``generate_drafts_for_campaign`` refuses it a second time, so nothing was
    ever *sent* to one — but it was counted. ``campaign.contacts_found`` is
    written straight off the length of this list, so the tracker told the user a
    campaign had found twelve contacts when three of them were undeliverable,
    and every one of those three produced a "undeliverable (hard bounce)" line
    in the notes for a contact that should never have been offered.
    """
    limit = per_company or settings.max_contacts_per_company
    created: list[int] = []
    notes: list[str] = []

    for company in companies:
        result = get_or_scrape(db, company)
        if not result.contacts:
            notes.append(f"{company}: {result.note or 'no contacts found'}")
            continue

        wanted = result.contacts[:limit]
        added = _upsert_and_commit(db, user, company, wanted)

        # Ids are only assigned on flush, so collect them after the commit — in
        # one read per company rather than one per contact. A crawl runs this
        # over every target company with `MAX_CONTACTS_PER_COMPANY` addresses
        # each, and the round trips were the bulk of what discovery spent on the
        # database.
        addresses = [c.email.lower() for c in wanted]
        rows = {
            row.email: row
            for row in db.scalars(
                select(Recruiter).where(
                    Recruiter.user_id == user.id,
                    Recruiter.email.in_(addresses),
                )
            )
        }
        # Iterated over the contacts rather than over `rows` so the ids come back
        # in the order the scraper ranked them, which is the order the campaign
        # then works through. `seen` does what the `not in created` test did, for
        # a list that grows to thousands across a crawl.
        seen = set(created)
        for address in addresses:
            row = rows.get(address)
            if (
                row is not None
                and row.id not in seen
                and not row.opted_out
                and not row.is_excluded
                and not bounce_service.is_suppressed(row)
            ):
                created.append(row.id)
                seen.add(row.id)

        if added == 0:
            notes.append(f"{company}: contacts already in your list")

    return created, notes
