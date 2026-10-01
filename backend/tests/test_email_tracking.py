"""Open/click tracking: HTML building, the public endpoints, and the stats.

The endpoints are reachable without auth (a recruiter's mail client has no
session), so a good half of these tests are about what happens when the caller
is hostile or wrong: an unknown token, a `javascript:` redirect target, a
guessed URL.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_event import EmailEvent, EmailEventType
from app.models.email_thread import EmailThread
from app.models.recruiter import Recruiter
from app.services import email_tracking, gmail_service


@pytest.fixture()
def sent_email(db_session, current_user, resume) -> Email:
    """One sent, tracked outreach email with its thread and application."""
    campaign = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
    recruiter = Recruiter(
        user_id=current_user.id, email="talent@acme.com", company="Acme"
    )
    db_session.add_all([campaign, recruiter])
    db_session.flush()
    application = Application(
        user_id=current_user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        status=ApplicationStatus.OUTREACH_SENT,
    )
    db_session.add(application)
    db_session.flush()
    thread = EmailThread(application_id=application.id, subject="Hello")
    db_session.add(thread)
    db_session.flush()
    email = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.SENT,
        to_address=recruiter.email,
        subject="Hello",
        body_text="Hi there.",
        tracking_token="tok-abc-123",
        # Old enough that an open isn't classified as a proxy prefetch.
        sent_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add(email)
    db_session.commit()
    return email


class TestTokens:
    def test_minting_is_idempotent(self):
        email = Email(thread_id=1, direction=EmailDirection.SENT)
        first = email_tracking.ensure_token(email)
        assert email_tracking.ensure_token(email) == first
        assert len(first) > 20

    def test_tokens_are_unique(self):
        tokens = {
            email_tracking.ensure_token(Email(thread_id=1, direction=EmailDirection.SENT))
            for _ in range(50)
        }
        assert len(tokens) == 50


class TestTargetEncoding:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "https://example.com/a?b=1&c=2#frag",
            "https://example.com/søk?q=ø&r=%20",
            "https://example.com/path/with/a/very/long/segment?" + "x=1&" * 40,
        ],
    )
    def test_round_trip_is_exact(self, url):
        assert email_tracking.decode_target(email_tracking.encode_target(url)) == url

    def test_garbage_decodes_to_nothing(self):
        assert email_tracking.decode_target("!!!not-base64!!!") is None
        assert email_tracking.decode_target("") is None

    @pytest.mark.parametrize(
        ("url", "safe"),
        [
            ("https://example.com", True),
            ("http://example.com", True),
            ("javascript:alert(1)", False),
            ("data:text/html;base64,PHNjcmlwdD4=", False),
            ("file:///etc/passwd", False),
            ("https://", False),
            ("", False),
            (None, False),
        ],
    )
    def test_only_http_targets_are_safe(self, url, safe):
        assert email_tracking.is_safe_target(url) is safe


class TestBuildTrackedHtml:
    def test_links_are_wrapped_and_the_pixel_is_last(self):
        html = email_tracking.build_tracked_html(
            "See https://portfolio.example.com for my work.", "tok"
        )
        assert "/t/c/tok?u=" in html
        assert html.rstrip().endswith("</div>")
        assert "/t/o/tok.gif" in html
        # The visible text stays the real URL; only the href is rewritten.
        assert ">https://portfolio.example.com<" in html

    def test_a_url_with_query_params_is_not_mangled(self):
        """Escaping must happen around URLs, not before: `&` becomes `&amp;`."""
        url = "https://example.com/a?b=1&c=2"
        html = email_tracking.build_tracked_html(f"Link: {url}", "tok")
        encoded = email_tracking.encode_target(url)
        assert encoded in html
        assert email_tracking.decode_target(encoded) == url

    def test_the_unsubscribe_link_is_never_wrapped(self):
        """Breaking one-click opt-out to measure a click would be illegal."""
        unsub = "http://localhost:8000/api/v1/unsubscribe?email=a%40b.com"
        html = email_tracking.build_tracked_html(
            "Body", "tok", footer=f"Unsubscribe: {unsub}", unsubscribe_url=unsub
        )
        assert unsub.split("?")[0] in html
        assert "/t/c/tok" not in html

    def test_html_in_the_body_is_escaped(self):
        html = email_tracking.build_tracked_html(
            "Hi <script>alert(1)</script> & co", "tok"
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp; co" in html

    def test_paragraphs_and_line_breaks_survive(self):
        html = email_tracking.build_tracked_html("One\nTwo\n\nThree", "tok")
        assert html.count("<p>") == 2
        assert "<br>" in html


class TestMimeCompatibility:
    def test_tracking_off_produces_the_original_single_part_message(self):
        """The deliverability work assumed text/plain; that must not change."""
        raw = gmail_service._build_mime(
            "me@gmail.com", "you@acme.com", "Subject", "Body", "Footer"
        )
        import base64

        decoded = base64.urlsafe_b64decode(raw).decode()
        assert "Content-Type: text/plain" in decoded
        assert "multipart" not in decoded

    def test_tracking_on_adds_an_alternative_part_keeping_the_text(self):
        import base64

        raw = gmail_service._build_mime(
            "me@gmail.com",
            "you@acme.com",
            "Subject",
            "Body",
            "Footer",
            body_html="<p>Body</p>",
        )
        decoded = base64.urlsafe_b64decode(raw).decode()
        assert "multipart/alternative" in decoded
        assert "text/plain" in decoded
        assert "text/html" in decoded


class TestOpenEndpoint:
    def test_returns_a_gif_and_records_the_open(self, client, db_session, sent_email):
        resp = client.get("/api/v1/t/o/tok-abc-123.gif")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/gif"
        assert resp.content == email_tracking.TRANSPARENT_GIF
        assert "no-store" in resp.headers["cache-control"]

        db_session.expire_all()
        email = db_session.get(Email, sent_email.id)
        assert email.open_count == 1
        assert email.first_opened_at is not None
        events = db_session.query(EmailEvent).all()
        assert len(events) == 1
        assert events[0].event_type == EmailEventType.OPEN
        assert events[0].user_id == email.thread.application.user_id

    def test_a_repeat_open_inside_the_window_does_not_double_count(
        self, client, db_session, sent_email
    ):
        client.get("/api/v1/t/o/tok-abc-123.gif")
        client.get("/api/v1/t/o/tok-abc-123.gif")

        db_session.expire_all()
        assert db_session.get(Email, sent_email.id).open_count == 1

    def test_an_open_outside_the_window_counts_again(self, db_session, sent_email):
        now = datetime.now(UTC)
        email_tracking.record_open(db_session, sent_email, now=now)
        email_tracking.record_open(
            db_session, sent_email, now=now + email_tracking.DEDUPE_WINDOW + timedelta(seconds=1)
        )
        db_session.commit()
        assert sent_email.open_count == 2

    def test_an_open_moments_after_the_send_is_flagged_as_a_prefetch(
        self, db_session, sent_email
    ):
        """Gmail's image proxy fetches the pixel at delivery, not at read time."""
        sent_email.sent_at = datetime.now(UTC)
        db_session.commit()

        event = email_tracking.record_open(db_session, sent_email)
        db_session.commit()
        assert event.is_prefetch is True

    def test_an_unknown_token_still_returns_a_pixel_and_writes_nothing(
        self, client, db_session
    ):
        """A 404 would make this an oracle for guessing valid tokens."""
        resp = client.get("/api/v1/t/o/not-a-real-token.gif")
        assert resp.status_code == 200
        assert resp.content == email_tracking.TRANSPARENT_GIF
        assert db_session.query(EmailEvent).count() == 0

    def test_the_ip_is_hashed_never_stored(self, db_session, sent_email):
        event = email_tracking.record_open(db_session, sent_email, ip="203.0.113.9")
        db_session.commit()
        assert event.ip_hash and "203.0.113.9" not in event.ip_hash

    def test_tracking_disabled_records_nothing(
        self, client, db_session, sent_email, monkeypatch
    ):
        monkeypatch.setattr(settings, "email_tracking_enabled", False)
        resp = client.get("/api/v1/t/o/tok-abc-123.gif")
        assert resp.status_code == 200
        assert db_session.query(EmailEvent).count() == 0


class TestClickEndpoint:
    def _url(self, target: str, token: str = "tok-abc-123") -> str:
        return f"/api/v1/t/c/{token}?u={email_tracking.encode_target(target)}"

    def test_redirects_and_records_the_click(self, client, db_session, sent_email):
        target = "https://example.com/a?b=1&c=2"
        resp = client.get(self._url(target), follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == target

        db_session.expire_all()
        email = db_session.get(Email, sent_email.id)
        assert email.click_count == 1
        event = db_session.query(EmailEvent).one()
        assert event.event_type == EmailEventType.CLICK
        assert event.url == target

    def test_a_click_counts_as_an_open_when_images_were_blocked(
        self, db_session, sent_email
    ):
        """Outlook blocks the pixel; a click still proves they read it."""
        email_tracking.record_click(db_session, sent_email, "https://example.com")
        db_session.commit()
        assert sent_email.open_count == 1
        assert sent_email.first_opened_at is not None

    def test_an_unsafe_target_is_refused_not_followed(self, client, db_session):
        """This header is attacker-reachable; it must never emit javascript:."""
        resp = client.get(
            self._url("javascript:alert(document.cookie)"), follow_redirects=False
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == settings.frontend_url
        assert db_session.query(EmailEvent).count() == 0

    def test_a_missing_target_bounces_to_the_frontend(self, client):
        resp = client.get("/api/v1/t/c/tok-abc-123", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == settings.frontend_url

    def test_an_unknown_token_still_redirects_and_writes_nothing(
        self, client, db_session
    ):
        target = "https://example.com"
        resp = client.get(self._url(target, token="nope"), follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == target
        assert db_session.query(EmailEvent).count() == 0


class TestAnalyticsEngagement:
    def test_rates_are_computed_off_tracked_sends_only(
        self, auth_client, db_session, sent_email
    ):
        """Mail sent before tracking existed can never open; excluding it is the
        difference between an honest rate and a permanently depressed one."""
        untracked = Email(
            thread_id=sent_email.thread_id,
            direction=EmailDirection.SENT,
            status=EmailStatus.SENT,
            to_address="talent@acme.com",
            subject="Old",
            body_text="b",
            sent_at=datetime.now(UTC) - timedelta(days=30),
        )
        db_session.add(untracked)
        email_tracking.record_open(db_session, sent_email)
        db_session.commit()

        body = auth_client.get("/api/v1/analytics/overview").json()
        engagement = body["engagement"]
        assert engagement["tracked"] == 1  # not 2
        assert engagement["opened"] == 1
        assert engagement["open_rate"] == 1.0

    def test_prefetch_opens_are_excluded_from_the_rate(
        self, auth_client, db_session, sent_email
    ):
        sent_email.sent_at = datetime.now(UTC)
        db_session.commit()
        email_tracking.record_open(db_session, sent_email)
        db_session.commit()

        engagement = auth_client.get("/api/v1/analytics/overview").json()["engagement"]
        assert engagement["tracked"] == 1
        assert engagement["opened"] == 0

    def test_a_thin_sample_is_reported_as_unreliable(
        self, auth_client, db_session, sent_email
    ):
        engagement = auth_client.get("/api/v1/analytics/overview").json()["engagement"]
        assert engagement["reliable"] is False

    def test_click_to_open_rate(self, auth_client, db_session, sent_email):
        email_tracking.record_open(db_session, sent_email)
        email_tracking.record_click(db_session, sent_email, "https://example.com")
        db_session.commit()

        engagement = auth_client.get("/api/v1/analytics/overview").json()["engagement"]
        assert engagement["clicked"] == 1
        assert engagement["click_to_open_rate"] == 1.0
