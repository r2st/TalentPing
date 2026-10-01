"""Career-page scraping: extraction rules, domain guessing, and the cache.

Nothing here touches the network — HTML is passed in directly, and the one test
that exercises the full crawl monkeypatches the fetch layer.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.recruiter_cache import RecruiterCache
from app.services import career_scraper
from app.services.career_scraper import (
    Contact,
    ScrapeResult,
    extract_contacts,
    find_careers_links,
    normalize_domain,
    pattern_contacts,
    slugify_company,
)

CAREERS_HTML = """
<html><body>
  <h1>Join Acme</h1>
  <p>Send your CV to <a href="mailto:careers@acme.com">careers@acme.com</a></p>
  <p>Questions? Reach Dana at <a href="mailto:dana.lee@acme.com">Dana Lee</a></p>
  <p>Press enquiries: press@acme.com</p>
  <p>Do not reply: noreply@acme.com</p>
  <p>Our vendor: support@wixpress.com</p>
  <a href="https://linkedin.com/in/dana-lee">Dana on LinkedIn</a>
</body></html>
"""


class TestDomainHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://www.acme.com/careers", "acme.com"),
            ("ACME.COM", "acme.com"),
            ("someone@acme.co.uk", "acme.co.uk"),
            ("acme.com:443/jobs", "acme.com"),
            ("not a domain", None),
            ("", None),
        ],
    )
    def test_normalize_domain(self, raw, expected):
        assert normalize_domain(raw) == expected

    @pytest.mark.parametrize(
        "company,expected",
        [
            ("Acme Technologies, Inc.", "acme"),
            ("Northwind Ltd", "northwind"),
            ("Stripe", "stripe"),
            ("Data Systems Group", "data"),
        ],
    )
    def test_slugify_company(self, company, expected):
        assert slugify_company(company) == expected


class TestSiteVerification:
    """A domain that merely responds is not proof it belongs to the company."""

    def test_matching_title_is_accepted(self):
        html = "<html><head><title>Linear – product development</title></head></html>"
        assert career_scraper._looks_like_company_site(html, "Linear") is True

    def test_meta_site_name_is_accepted(self):
        html = '<head><meta property="og:site_name" content="Acme Technologies"></head>'
        assert career_scraper._looks_like_company_site(html, "Acme") is True

    def test_parked_page_without_the_name_is_rejected(self):
        html = "<html><head><title>Domain for sale</title></head><body>Buy this</body></html>"
        assert career_scraper._looks_like_company_site(html, "Linear") is False

    def test_body_text_alone_is_not_enough(self):
        """A 'linear.co is for sale' listing mentions the word but isn't the company."""
        html = (
            "<html><head><title>Domain marketplace</title></head>"
            "<body>The domain linear.co is available to purchase.</body></html>"
        )
        assert career_scraper._looks_like_company_site(html, "Linear") is False

    def test_redirect_into_a_domain_broker_is_rejected(self):
        html = "<html><head><title>Linear for sale</title></head></html>"
        assert (
            career_scraper._looks_like_company_site(
                html, "Linear", "https://www.namepros.com/threads/linear-co"
            )
            is False
        )

    def test_search_results_are_verified_before_being_trusted(self, monkeypatch):
        """An unverified search hit would email a resume to a stranger."""
        results = (
            '<a href="https://namepros.com/linear">Linear domain</a>'
            '<a href="https://linear.app">Linear</a>'
        )

        class _Resp:
            status_code = 200
            text = results

        monkeypatch.setattr(
            career_scraper.requests.Session, "get", lambda self, *a, **k: _Resp()
        )
        pages = {
            "https://linear.app": (
                "<html><head><title>Linear – product development</title></head></html>",
                "https://linear.app",
            )
        }
        monkeypatch.setattr(
            career_scraper, "_fetch", lambda s, url: pages.get(url, (None, None))
        )

        session = career_scraper._session()
        assert career_scraper._search_domain(session, "Linear") == "linear.app"

    def test_search_returns_none_when_nothing_verifies(self, monkeypatch):
        class _Resp:
            status_code = 200
            text = '<a href="https://unrelated.example">Something</a>'

        monkeypatch.setattr(
            career_scraper.requests.Session, "get", lambda self, *a, **k: _Resp()
        )
        monkeypatch.setattr(career_scraper, "_fetch", lambda s, url: (None, None))
        session = career_scraper._session()
        assert career_scraper._search_domain(session, "Ghost Co") is None


class TestContactExtraction:
    def test_mailto_links_rank_highest(self):
        contacts = extract_contacts(CAREERS_HTML, "https://acme.com/careers", "acme.com")
        assert contacts[0].email == "careers@acme.com"
        assert contacts[0].confidence >= 0.9

    def test_link_text_becomes_a_name(self):
        contacts = extract_contacts(CAREERS_HTML, "https://acme.com/careers", "acme.com")
        dana = next(c for c in contacts if c.email == "dana.lee@acme.com")
        assert dana.name == "Dana Lee"

    def test_noreply_and_press_addresses_are_dropped(self):
        emails = {
            c.email
            for c in extract_contacts(CAREERS_HTML, "https://acme.com/careers", "acme.com")
        }
        assert "noreply@acme.com" not in emails
        assert "press@acme.com" not in emails

    def test_third_party_vendor_addresses_are_dropped(self):
        emails = {
            c.email
            for c in extract_contacts(CAREERS_HTML, "https://acme.com/careers", "acme.com")
        }
        assert "support@wixpress.com" not in emails

    def test_freemail_addresses_are_not_treated_as_company_contacts(self):
        html = '<a href="mailto:recruiter@gmail.com">Recruiter</a>'
        assert extract_contacts(html, "https://acme.com/careers", "acme.com") == []

    def test_linkedin_profile_is_attached_to_the_top_contact(self):
        contacts = extract_contacts(CAREERS_HTML, "https://acme.com/careers", "acme.com")
        assert contacts[0].linkedin_url == "https://linkedin.com/in/dana-lee"

    def test_bare_text_addresses_are_found(self):
        html = "<p>Apply via talent@acme.com today</p>"
        contacts = extract_contacts(html, "https://acme.com/careers", "acme.com")
        assert [c.email for c in contacts] == ["talent@acme.com"]

    def test_image_filenames_are_not_mistaken_for_addresses(self):
        html = '<img src="logo@2x.png"><p>hi</p>'
        assert extract_contacts(html, "https://acme.com/careers", "acme.com") == []

    def test_page_with_no_contacts_returns_empty(self):
        assert extract_contacts("<p>No emails here.</p>", "https://acme.com", "acme.com") == []


class TestCareersLinkDiscovery:
    def test_finds_careers_links_on_the_homepage(self):
        html = """
        <a href="/about">About</a>
        <a href="/careers">Careers</a>
        <a href="/blog">Blog</a>
        <a href="https://boards.greenhouse.io/acme">Open roles</a>
        """
        links = find_careers_links(html, "https://acme.com")
        assert "https://acme.com/careers" in links
        assert "https://boards.greenhouse.io/acme" in links
        assert not any("blog" in link for link in links)

    def test_offsite_non_ats_links_are_ignored(self):
        html = '<a href="https://randomjobsite.example/jobs">Jobs</a>'
        assert find_careers_links(html, "https://acme.com") == []

    def test_mailto_and_anchor_hrefs_are_skipped(self):
        html = '<a href="mailto:jobs@acme.com">Jobs</a><a href="#careers">Careers</a>'
        assert find_careers_links(html, "https://acme.com") == []


class TestPatternFallback:
    def test_generates_role_mailboxes_at_low_confidence(self):
        contacts = pattern_contacts("acme.com")
        assert [c.email for c in contacts] == [
            "careers@acme.com",
            "jobs@acme.com",
            "recruiting@acme.com",
        ]
        assert all(c.kind == "pattern" and c.confidence < 0.5 for c in contacts)


class TestScrapeCompany:
    def test_falls_back_to_patterns_when_nothing_is_published(self, monkeypatch):
        monkeypatch.setattr(career_scraper, "resolve_domain", lambda *a, **k: "acme.com")
        monkeypatch.setattr(career_scraper, "find_careers_page", lambda *a: (None, None))
        monkeypatch.setattr(career_scraper, "_fetch", lambda *a: (None, None))

        result = career_scraper.scrape_company("Acme")
        assert result.status == "ok"
        assert result.contacts[0].kind == "pattern"
        assert "role-based patterns" in (result.note or "")

    def test_unresolvable_company_is_empty_not_an_error(self, monkeypatch):
        monkeypatch.setattr(career_scraper, "resolve_domain", lambda *a, **k: None)
        result = career_scraper.scrape_company("Nonexistent Co")
        assert result.status == "empty"
        assert result.contacts == []

    def test_scraped_contacts_win_over_patterns(self, monkeypatch):
        monkeypatch.setattr(career_scraper, "resolve_domain", lambda *a, **k: "acme.com")
        monkeypatch.setattr(
            career_scraper,
            "find_careers_page",
            lambda *a: ("https://acme.com/careers", CAREERS_HTML),
        )
        result = career_scraper.scrape_company("Acme")
        assert result.careers_url == "https://acme.com/careers"
        assert all(c.kind != "pattern" for c in result.contacts)

    def test_an_exception_mid_crawl_is_reported_not_raised(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(career_scraper, "resolve_domain", _boom)
        result = career_scraper.scrape_company("Acme")
        assert result.status == "error"
        assert "connection reset" in (result.note or "")


class TestCache:
    def _stub_scrape(self, monkeypatch, calls: list):
        def _fake(company, domain=None, **kwargs):
            calls.append(company)
            return ScrapeResult(
                company=company,
                domain="acme.com",
                contacts=[Contact(email="careers@acme.com", confidence=0.95)],
                careers_url="https://acme.com/careers",
                source_url="https://acme.com/careers",
            )

        monkeypatch.setattr(career_scraper, "scrape_company", _fake)

    def test_first_lookup_crawls_and_persists(self, db_session, monkeypatch):
        calls: list = []
        self._stub_scrape(monkeypatch, calls)

        result = career_scraper.get_or_scrape(db_session, "Acme")
        assert calls == ["Acme"]
        assert result.emails == ["careers@acme.com"]

        row = db_session.query(RecruiterCache).one()
        assert row.domain == "acme.com"
        assert row.scraped_at is not None

    def test_second_lookup_is_served_from_cache(self, db_session, monkeypatch):
        calls: list = []
        self._stub_scrape(monkeypatch, calls)

        career_scraper.get_or_scrape(db_session, "Acme")
        cached = career_scraper.get_or_scrape(db_session, "Acme")

        assert calls == ["Acme"], "the second lookup must not re-crawl"
        assert cached.from_cache is True
        assert cached.emails == ["careers@acme.com"]
        assert db_session.query(RecruiterCache).one().hit_count == 1

    def test_the_cache_is_global_across_users(self, db_session, monkeypatch):
        """A different candidate targeting the same company reuses the crawl."""
        calls: list = []
        self._stub_scrape(monkeypatch, calls)

        career_scraper.get_or_scrape(db_session, "Acme")
        # No user is involved in the lookup at all — that is the point.
        career_scraper.get_or_scrape(db_session, "acme")
        assert calls == ["Acme"]

    def test_stale_rows_are_recrawled(self, db_session, monkeypatch):
        calls: list = []
        self._stub_scrape(monkeypatch, calls)

        career_scraper.get_or_scrape(db_session, "Acme")
        row = db_session.query(RecruiterCache).one()
        row.scraped_at = datetime.now(UTC) - timedelta(days=90)
        db_session.commit()

        career_scraper.get_or_scrape(db_session, "Acme")
        assert len(calls) == 2

    def test_force_bypasses_a_fresh_row(self, db_session, monkeypatch):
        calls: list = []
        self._stub_scrape(monkeypatch, calls)

        career_scraper.get_or_scrape(db_session, "Acme")
        career_scraper.get_or_scrape(db_session, "Acme", force=True)
        assert len(calls) == 2

    def test_freshness_handles_naive_timestamps(self, db_session):
        """SQLite hands back naive datetimes; they must not blow up the check."""
        row = RecruiterCache(
            company="Acme",
            domain="acme.com",
            scraped_at=datetime.now(UTC).replace(tzinfo=None),
        )
        assert row.is_fresh(30) is True
        assert RecruiterCache(company="A", domain="a.com").is_fresh(30) is False
