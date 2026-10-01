"""LinkedIn: encrypted credentials, and a rate limit that actually holds.

The account at risk here is the candidate's own, so the rate limiting is the
feature rather than an implementation detail. Two properties get pinned hardest:
the window is rolling (25 at 23:50 plus 25 at 00:05 is exactly the burst a
calendar-day counter waves through), and the credentials never exist in
plaintext on the row.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.models.linkedin_account import LinkedInAccount
from app.services import linkedin_service as svc

# Shaped like a real guest job card: the title lives in an h3, the link carries
# a slug and tracking params, and the date is a <time datetime=...>.
GUEST_HTML = """
<ul class="jobs-search__results-list">
  <li>
    <a class="base-card__full-link"
       href="https://www.linkedin.com/jobs/view/senior-backend-engineer-at-northwind-1234567890/?trk=guest">
    </a>
    <h3 class="base-search-card__title">Senior Backend Engineer</h3>
    <h4 class="base-search-card__subtitle">Northwind Labs</h4>
    <span class="job-search-card__location">San Francisco, CA</span>
    <time datetime="2026-07-01"></time>
  </li>
</ul>
"""


@pytest.fixture()
def account(db_session, current_user) -> LinkedInAccount:
    return svc.store_credentials(
        db_session, current_user, email="candidate@example.com", password="hunter2"
    )


class TestCredentials:
    def test_the_password_is_never_stored_in_plaintext(self, account):
        assert "hunter2" not in (account.password_encrypted or "")
        assert svc.password_for(account) == "hunter2"

    def test_storing_again_replaces_the_same_row(self, db_session, current_user, account):
        again = svc.store_credentials(
            db_session, current_user, email="new@example.com", password="different"
        )
        assert again.id == account.id
        assert svc.password_for(again) == "different"
        assert db_session.query(LinkedInAccount).count() == 1

    def test_clearing_removes_the_row(self, db_session, current_user, account):
        assert svc.clear_credentials(db_session, current_user) is True
        assert svc.get_account(db_session, current_user) is None

    def test_clearing_nothing_is_not_an_error(self, db_session, current_user):
        assert svc.clear_credentials(db_session, current_user) is False

    def test_session_state_round_trips_encrypted(self, db_session, account):
        svc.save_session(db_session, account, {"cookies": [{"name": "li_at"}]})
        assert "li_at" not in (account.session_state_encrypted or "")
        assert svc.session_state(account) == {"cookies": [{"name": "li_at"}]}

    def test_clearing_the_session_is_allowed(self, db_session, account):
        svc.save_session(db_session, account, {"cookies": []})
        svc.save_session(db_session, account, None)
        assert svc.session_state(account) is None


class TestApplyBudget:
    def test_no_account_is_not_allowed(self):
        decision = svc.apply_budget(None)
        assert decision.allowed is False
        assert "No LinkedIn account" in decision.reason

    def test_a_fresh_account_may_apply(self, account):
        assert svc.apply_budget(account).allowed is True

    def test_the_daily_cap_stops_further_applies(self, db_session, account):
        now = datetime.now(UTC)
        account.daily_apply_count = settings.linkedin_easy_apply_daily_limit
        account.daily_count_reset_at = now
        account.last_apply_at = now - timedelta(hours=1)
        db_session.commit()

        decision = svc.apply_budget(account, now=now)
        assert decision.allowed is False
        assert decision.remaining == 0
        assert decision.retry_after_seconds > 0

    def test_the_window_is_rolling_not_calendar_day(self, db_session, account):
        """25 at 23:50 then 25 at 00:05 is the burst the cap exists to stop."""
        start = datetime.now(UTC)
        account.daily_apply_count = settings.linkedin_easy_apply_daily_limit
        account.daily_count_reset_at = start
        account.last_apply_at = start
        db_session.commit()

        # Fifteen minutes later — a new calendar day would reset here. It doesn't.
        assert svc.apply_budget(account, now=start + timedelta(minutes=15)).allowed is False
        # A full 24 hours later, the window has genuinely rolled over.
        assert svc.apply_budget(account, now=start + timedelta(hours=25)).allowed is True

    def test_applying_again_too_soon_is_paced(self, db_session, account):
        now = datetime.now(UTC)
        account.daily_apply_count = 1
        account.daily_count_reset_at = now
        account.last_apply_at = now - timedelta(seconds=5)
        db_session.commit()

        decision = svc.apply_budget(account, now=now)
        assert decision.allowed is False
        assert "Pacing" in decision.reason
        assert 0 < decision.retry_after_seconds <= settings.linkedin_min_seconds_between_applies

    def test_a_paused_account_may_not_apply(self, db_session, account):
        svc.set_status(db_session, account, "challenged", error="checkpoint")
        decision = svc.apply_budget(account)
        assert decision.allowed is False

    def test_recording_an_apply_counts_against_the_window(self, db_session, account):
        now = datetime.now(UTC)
        svc.record_apply(db_session, account, now=now)

        assert account.daily_apply_count == 1
        assert account.easy_apply_total == 1
        assert account.last_apply_at is not None

    def test_recording_after_the_window_rolls_restarts_the_count(self, db_session, account):
        start = datetime.now(UTC)
        svc.record_apply(db_session, account, now=start)
        svc.record_apply(db_session, account, now=start + timedelta(hours=25))

        assert account.daily_apply_count == 1  # the window reset
        assert account.easy_apply_total == 2  # the lifetime total did not


class TestGuestJobParsing:
    def test_reads_a_listing(self):
        jobs = svc.parse_guest_jobs(GUEST_HTML)
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Backend Engineer"
        assert jobs[0]["company"] == "Northwind Labs"
        assert jobs[0]["location"] == "San Francisco, CA"

    def test_empty_html_yields_nothing(self):
        assert svc.parse_guest_jobs("") == []
        assert svc.parse_guest_jobs("<html><body>nothing here</body></html>") == []

    def test_strips_tracking_params_from_the_url(self):
        jobs = svc.parse_guest_jobs(GUEST_HTML)
        assert "trk=" not in jobs[0]["url"]

    def test_reads_the_job_id(self):
        assert (
            svc.job_id_from_url("https://www.linkedin.com/jobs/view/1234567890/?trk=x")
            == "1234567890"
        )

    def test_a_non_job_url_has_no_id(self):
        assert svc.job_id_from_url("https://example.com/careers") is None
        assert svc.job_id_from_url(None) is None


class TestStatusSummary:
    def test_no_account_reads_as_disconnected(self):
        summary = svc.status_summary(None)
        assert summary["connected"] is False

    def test_a_connected_account_reports_its_budget(self, account):
        summary = svc.status_summary(account)
        assert summary["connected"] is True
        assert summary["budget"]["limit"] == settings.linkedin_easy_apply_daily_limit

    def test_the_summary_never_leaks_the_password(self, account):
        assert "hunter2" not in str(svc.status_summary(account))
