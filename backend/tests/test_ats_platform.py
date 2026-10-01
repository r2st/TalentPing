"""ATS detection — the first decision every form-apply run makes.

Pure string work, so it is tested exhaustively against the URL shapes these
platforms actually produce. Getting this wrong sends a Workday wizard to the
single-page Greenhouse adapter, which fills the first step and reports success.
"""
from __future__ import annotations

import pytest

from app.models.form_apply import ATSPlatform
from app.services import ats_platform


class TestDetectPlatform:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://boards.greenhouse.io/acme/jobs/4567", ATSPlatform.GREENHOUSE),
            ("https://job-boards.greenhouse.io/acme/jobs/4567", ATSPlatform.GREENHOUSE),
            ("https://acme.com/careers?gh_jid=4567", ATSPlatform.GREENHOUSE),
            ("https://grnh.se/abc123", ATSPlatform.GREENHOUSE),
            ("https://jobs.lever.co/acme/1a2b3c4d-5e6f", ATSPlatform.LEVER),
            ("https://jobs.eu.lever.co/acme/1a2b3c4d-5e6f", ATSPlatform.LEVER),
            (
                "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Remote/Engineer_R-1",
                ATSPlatform.WORKDAY,
            ),
            ("https://www.linkedin.com/jobs/view/3912345678/", ATSPlatform.LINKEDIN),
            ("https://careers.acme.com/apply/123", ATSPlatform.GENERIC),
        ],
    )
    def test_recognises_the_platforms(self, url, expected):
        assert ats_platform.detect_platform(url) is expected

    @pytest.mark.parametrize("url", [None, "", "   ", "not a url", "mailto:a@b.com"])
    def test_unusable_urls_are_unknown(self, url):
        assert ats_platform.detect_platform(url) is ATSPlatform.UNKNOWN

    def test_a_linkedin_link_wins_over_an_embedded_ats(self):
        # A LinkedIn job page can mention any ATS; the page we open is LinkedIn.
        url = "https://www.linkedin.com/jobs/view/3912345678/?refId=greenhouse"
        assert ats_platform.detect_platform(url) is ATSPlatform.LINKEDIN


class TestDetectFromHtml:
    def test_finds_an_embedded_greenhouse_board(self):
        html = '<div id="grnhse_app"><iframe id="grnhse_iframe" src="..."></iframe></div>'
        assert ats_platform.detect_from_html(html) is ATSPlatform.GREENHOUSE

    def test_finds_an_embedded_lever_form(self):
        html = '<form action="https://jobs.lever.co/acme/123/apply">'
        assert ats_platform.detect_from_html(html) is ATSPlatform.LEVER

    def test_plain_markup_gives_nothing_away(self):
        assert ats_platform.detect_from_html("<html><body>Apply</body></html>") is (
            ATSPlatform.UNKNOWN
        )


class TestResolvePlatform:
    def test_markup_refines_an_unrecognised_url(self):
        resolved = ats_platform.resolve_platform(
            "https://careers.acme.com/jobs/123",
            '<iframe id="grnhse_iframe">',
        )
        assert resolved is ATSPlatform.GREENHOUSE

    def test_markup_never_overrides_a_recognised_url(self):
        resolved = ats_platform.resolve_platform(
            "https://jobs.lever.co/acme/123", "boards.greenhouse.io"
        )
        assert resolved is ATSPlatform.LEVER


class TestApplyUrl:
    def test_lever_postings_are_redirected_to_their_form(self):
        assert (
            ats_platform.apply_url("https://jobs.lever.co/acme/123")
            == "https://jobs.lever.co/acme/123/apply"
        )

    def test_a_lever_apply_url_is_left_alone(self):
        url = "https://jobs.lever.co/acme/123/apply"
        assert ats_platform.apply_url(url) == url

    def test_other_platforms_apply_in_place(self):
        url = "https://boards.greenhouse.io/acme/jobs/4567"
        assert ats_platform.apply_url(url) == url


class TestExternalJobId:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://jobs.lever.co/acme/1a2b3c4d-5e6f/apply", "1a2b3c4d-5e6f"),
            ("https://www.linkedin.com/jobs/view/3912345678/", "3912345678"),
            ("https://acme.com/careers?gh_jid=4567", "4567"),
            ("https://boards.greenhouse.io/acme/jobs/4567", "4567"),
        ],
    )
    def test_extracts_the_platform_id(self, url, expected):
        assert ats_platform.external_job_id(url) == expected

    def test_returns_none_when_there_is_no_id(self):
        assert ats_platform.external_job_id("https://careers.acme.com/apply") is None


class TestSupport:
    def test_the_top_three_plus_linkedin_have_adapters(self):
        for platform in (
            ATSPlatform.WORKDAY,
            ATSPlatform.GREENHOUSE,
            ATSPlatform.LEVER,
            ATSPlatform.LINKEDIN,
        ):
            assert ats_platform.has_adapter(platform)

    def test_generic_is_attemptable_but_not_dedicated(self):
        assert ats_platform.is_attemptable(ATSPlatform.GENERIC)
        assert not ats_platform.has_adapter(ATSPlatform.GENERIC)

    def test_unknown_is_not_worth_opening_a_browser_for(self):
        assert not ats_platform.is_attemptable(ATSPlatform.UNKNOWN)

    def test_every_platform_has_a_human_label(self):
        for platform in ATSPlatform:
            assert ats_platform.label(platform)
