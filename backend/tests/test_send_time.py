"""Send-time optimization: timezone resolution and the business-hours slot.

Mostly pure unit tests over the service. The resolution-order tests need a DB
because the company-research cache and the posting live there.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.company_profile import CompanyProfile
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.job import JobPosting, job_fingerprint
from app.models.recruiter import Recruiter
from app.services import send_time

LA = ZoneInfo("America/Los_Angeles")
LONDON = ZoneInfo("Europe/London")


def _recruiter(db, user, **overrides) -> Recruiter:
    defaults = dict(
        user_id=user.id,
        email="talent@northwind.example",
        name="Sam Recruiter",
        company="Northwind Labs",
    )
    defaults.update(overrides)
    row = Recruiter(**defaults)
    db.add(row)
    db.commit()
    return row


class TestZoneForLocation:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            ("San Francisco, CA", "America/Los_Angeles"),
            ("London, UK", "Europe/London"),
            ("Bangalore, India", "Asia/Kolkata"),
            ("Berlin", "Europe/Berlin"),
            ("Sydney, Australia", "Australia/Sydney"),
            ("New York, NY", "America/New_York"),
            ("Austin, TX", "America/Chicago"),
            ("Tel Aviv", "Asia/Jerusalem"),
        ],
    )
    def test_known_locations_resolve(self, location, expected):
        assert str(send_time.zone_for_location(location)) == expected

    def test_an_unknown_location_resolves_to_nothing(self):
        assert send_time.zone_for_location("Atlantis, Somewhere") is None
        assert send_time.zone_for_location("") is None
        assert send_time.zone_for_location(None) is None

    def test_longest_match_wins(self):
        """'Portland, Oregon' must not be caught by a bare 'or'."""
        assert str(send_time.zone_for_location("Portland, Oregon")) == (
            "America/Los_Angeles"
        )
        assert str(send_time.zone_for_location("New York State")) == "America/New_York"

    def test_matching_is_on_word_boundaries(self):
        """A city name inside a longer word is not a match."""
        assert send_time.zone_for_location("Bangorville") is None
        assert send_time.zone_for_location("Parisian Holdings Ltd") is None

    def test_remote_alone_resolves_to_nothing(self):
        """A remote role isn't in a timezone; the recruiter reading it is."""
        assert send_time.zone_for_location("Remote") is None

    def test_a_bare_country_abbreviation_still_resolves(self):
        """'Remote (US)' and 'Remote, UK' are extremely common posting strings."""
        assert str(send_time.zone_for_location("Remote (US)")) == "America/New_York"
        assert str(send_time.zone_for_location("Remote, UK")) == "Europe/London"


class TestZoneForEmail:
    def test_an_unambiguous_country_tld_resolves(self):
        assert str(send_time.zone_for_email("talent@acme.co.uk")) == "Europe/London"
        assert str(send_time.zone_for_email("jobs@firma.de")) == "Europe/Berlin"

    def test_generic_tlds_say_nothing(self):
        for address in ("a@acme.com", "a@acme.io", "a@acme.ai", "a@acme.dev"):
            assert send_time.zone_for_email(address) is None

    def test_a_malformed_address_is_safe(self):
        assert send_time.zone_for_email("not-an-address") is None
        assert send_time.zone_for_email(None) is None


class TestResolutionOrder:
    def test_the_cached_column_wins(self, db_session, current_user):
        rec = _recruiter(db_session, current_user, timezone="Asia/Tokyo")
        db_session.add(
            CompanyProfile(
                name="Northwind Labs",
                normalized_name="northwind labs",
                headquarters="London, UK",
            )
        )
        db_session.commit()
        assert str(send_time.resolve_timezone(db_session, rec)) == "Asia/Tokyo"

    def test_the_company_headquarters_is_used_next(self, db_session, current_user):
        rec = _recruiter(db_session, current_user, email="talent@northwind.co.uk")
        db_session.add(
            CompanyProfile(
                name="Northwind Labs",
                normalized_name="northwind labs",
                headquarters="San Francisco, CA",
            )
        )
        db_session.commit()
        # The HQ beats the .co.uk TLD, which would say London.
        assert str(send_time.resolve_timezone(db_session, rec)) == "America/Los_Angeles"

    def test_the_posting_location_is_used_when_there_is_no_profile(
        self, db_session, current_user, resume
    ):
        rec = _recruiter(db_session, current_user)
        campaign = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
        posting = JobPosting(
            user_id=current_user.id,
            title="Backend Engineer",
            company="Northwind Labs",
            location="Berlin, Germany",
            fingerprint=job_fingerprint("Backend Engineer", "Northwind Labs", None),
        )
        db_session.add_all([campaign, posting])
        db_session.flush()
        application = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=rec.id,
            job_posting_id=posting.id,
            status=ApplicationStatus.QUEUED,
        )
        db_session.add(application)
        db_session.commit()

        resolved = send_time.resolve_timezone(db_session, rec, application=application)
        assert str(resolved) == "Europe/Berlin"

    def test_the_tld_is_the_last_resort(self, db_session, current_user):
        rec = _recruiter(db_session, current_user, email="talent@acme.co.uk")
        assert str(send_time.resolve_timezone(db_session, rec)) == "Europe/London"

    def test_nothing_known_falls_back_to_the_default(self, db_session, current_user):
        rec = _recruiter(db_session, current_user, company=None, email="a@acme.com")
        assert str(send_time.resolve_timezone(db_session, rec)) == "UTC"

    def test_a_resolved_zone_is_cached_on_the_recruiter(self, db_session, current_user):
        rec = _recruiter(db_session, current_user, email="talent@acme.de")
        send_time.resolve_timezone(db_session, rec)
        assert rec.timezone == "Europe/Berlin"

    def test_a_defaulted_zone_is_not_cached(self, db_session, current_user):
        """A CompanyProfile written later should still be able to improve it."""
        rec = _recruiter(db_session, current_user, company=None, email="a@acme.com")
        send_time.resolve_timezone(db_session, rec)
        assert rec.timezone is None

    def test_a_missing_recruiter_is_safe(self, db_session):
        assert str(send_time.resolve_timezone(db_session, None)) == "UTC"

    def test_an_invalid_default_setting_degrades_to_utc(self, monkeypatch):
        monkeypatch.setattr(settings, "default_send_timezone", "Mars/Olympus_Mons")
        assert str(send_time.default_timezone()) == "UTC"


class TestNextSlot:
    def test_a_saturday_lands_on_monday_morning(self):
        saturday = datetime(2026, 7, 25, 15, 0, tzinfo=UTC)
        slot = send_time.next_slot(saturday, ZoneInfo("UTC"), jitter=False)
        assert slot.weekday() == 0  # Monday
        assert slot.hour == 9

    def test_a_moment_inside_the_window_is_not_deferred(self):
        tuesday = datetime(2026, 7, 21, 9, 40, tzinfo=UTC)
        assert send_time.next_slot(tuesday, ZoneInfo("UTC"), jitter=False) == tuesday

    def test_an_afternoon_moves_to_the_next_morning(self):
        tuesday_pm = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)
        slot = send_time.next_slot(tuesday_pm, ZoneInfo("UTC"), jitter=False)
        assert slot.weekday() == 2  # Wednesday
        assert slot.hour == 9

    def test_the_window_is_the_recipients_local_morning_not_ours(self):
        """The whole point: 09:00 UTC is 02:00 in California."""
        monday_late = datetime(2026, 7, 20, 23, 0, tzinfo=UTC)
        slot = send_time.next_slot(monday_late, LA, jitter=False)
        local = slot.astimezone(LA)
        assert local.weekday() in (0, 1, 2, 3, 4)
        assert 9 <= local.hour < 11
        # ...which is *not* 09:00 UTC.
        assert slot.hour != 9

    def test_always_returns_a_weekday_morning_in_utc(self):
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        for hours in range(0, 24 * 14, 7):
            for tz in (ZoneInfo("UTC"), LA, LONDON, ZoneInfo("Asia/Kolkata")):
                slot = send_time.next_slot(start + timedelta(hours=hours), tz)
                assert slot.tzinfo is UTC
                local = slot.astimezone(tz)
                assert local.weekday() in send_time.WEEKDAYS
                assert 9 <= local.hour < 11

    def test_never_returns_a_time_in_the_past(self):
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        for hours in range(0, 24 * 10, 5):
            after = start + timedelta(hours=hours)
            assert send_time.next_slot(after, LA) >= after

    def test_jitter_stays_inside_the_window(self):
        rng = random.Random(1234)
        saturday = datetime(2026, 7, 25, 15, 0, tzinfo=UTC)
        for _ in range(200):
            slot = send_time.next_slot(saturday, LA, rng=rng)
            local = slot.astimezone(LA)
            assert 9 <= local.hour < 11

    def test_jitter_actually_spreads_the_sends(self):
        rng = random.Random(7)
        saturday = datetime(2026, 7, 25, 15, 0, tzinfo=UTC)
        slots = {send_time.next_slot(saturday, LA, rng=rng) for _ in range(50)}
        assert len(slots) > 10  # not all piled on 09:00:00

    def test_a_naive_datetime_is_treated_as_utc(self):
        naive = datetime(2026, 7, 25, 15, 0)
        assert send_time.next_slot(naive, ZoneInfo("UTC"), jitter=False).tzinfo is UTC

    def test_an_invalid_window_setting_falls_back(self, monkeypatch):
        monkeypatch.setattr(settings, "send_window_start_hour", 20)
        monkeypatch.setattr(settings, "send_window_end_hour", 4)
        slot = send_time.next_slot(
            datetime(2026, 7, 25, 15, 0, tzinfo=UTC), ZoneInfo("UTC"), jitter=False
        )
        assert slot.hour == 9


class TestDelaySeconds:
    def test_a_future_slot_is_the_difference(self):
        now = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
        assert send_time.delay_seconds(now + timedelta(hours=2), now=now) == 7200

    def test_a_past_slot_is_zero_not_negative(self):
        now = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
        assert send_time.delay_seconds(now - timedelta(hours=2), now=now) == 0

    def test_the_delay_is_clamped(self):
        """A bad timezone must not park a task in the broker for a month."""
        now = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
        far = now + timedelta(days=90)
        assert send_time.delay_seconds(far, now=now) == send_time.MAX_SEND_DELAY_SECONDS


class TestCampaignDispatchIntegration:
    """The countdown the throttled sender actually receives."""

    def _queued_email(self, db, user, resume, *, location: str | None) -> Email:
        campaign = Campaign(user_id=user.id, name="c", resume_id=resume.id)
        recruiter = Recruiter(
            user_id=user.id, email="talent@acme.com", company="Acme", timezone=location
        )
        db.add_all([campaign, recruiter])
        db.flush()
        application = Application(
            user_id=user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=ApplicationStatus.QUEUED,
        )
        db.add(application)
        db.flush()
        thread = EmailThread(application_id=application.id, subject="s")
        db.add(thread)
        db.flush()
        email = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.QUEUED,
            to_address=recruiter.email,
            subject="s",
            body_text="b",
        )
        db.add(email)
        db.commit()
        return email

    def test_the_countdown_lands_in_the_recipients_morning(
        self, db_session, current_user, resume
    ):
        from app.tasks import email_tasks

        email = self._queued_email(
            db_session, current_user, resume, location="America/Los_Angeles"
        )
        now = datetime(2026, 7, 20, 23, 0, tzinfo=UTC)  # Monday night UTC
        countdown = email_tasks._send_countdown(db_session, email, 120, now=now)

        landing = (now + timedelta(seconds=countdown)).astimezone(LA)
        assert landing.weekday() in send_time.WEEKDAYS
        assert 9 <= landing.hour < 11

    def test_disabling_the_feature_restores_plain_spacing(
        self, db_session, current_user, resume, monkeypatch
    ):
        from app.tasks import email_tasks

        monkeypatch.setattr(settings, "send_time_optimization_enabled", False)
        email = self._queued_email(db_session, current_user, resume, location=None)
        assert email_tasks._send_countdown(db_session, email, 347) == 347
