"""Tests for the sender domain blocklist."""
from __future__ import annotations

from app.services.sender_blocklist import is_blocked


class TestSenderBlocklist:
    """Blocklist correctly identifies spam senders while passing recruiters."""

    def test_blocks_matrimonial_sites(self):
        assert is_blocked("matchmaking@shaadi.com") is True
        assert is_blocked("alerts@jeevansathi.com") is True
        assert is_blocked("updates@bharatmatrimony.com") is True

    def test_blocks_status_pages(self):
        assert is_blocked("noreply@statuspage.io") is True

    def test_blocks_event_platforms(self):
        assert is_blocked("events@user.luma-mail.com") is True
        assert is_blocked("rsvp@calendar.luma-mail.com") is True

    def test_blocks_travel_shopping(self):
        assert is_blocked("offers@tripjack.com") is True
        assert is_blocked("deals@eg.hotels.com") is True

    def test_blocks_banking_suffix(self):
        """Any .bank.in subdomain is blocked via suffix rule."""
        assert is_blocked("noreply@axis.bank.in") is True
        assert is_blocked("alerts@digital.axisbankmail.bank.in") is True
        assert is_blocked("noreply@hdfc.bank.in") is True

    def test_allows_recruiter_domains(self):
        """Real recruiter emails must not be blocked."""
        assert is_blocked("jane@acme.com") is False
        assert is_blocked("talent@synchronycorp.com") is False
        assert is_blocked("recruiter@stage4solutions.com") is False
        assert is_blocked("hr@google.com") is False

    def test_allows_job_platforms(self):
        """Job platforms that may send real opportunities."""
        assert is_blocked("alert@linkedin.com") is False
        assert is_blocked("jobs@torre.ai") is False

    def test_handles_edge_cases(self):
        assert is_blocked("") is False
        assert is_blocked("no-at-sign") is False
        assert is_blocked("UPPER@SHAADI.COM") is True  # case-insensitive
