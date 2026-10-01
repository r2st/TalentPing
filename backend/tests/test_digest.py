"""The weekly digest — the one email the product sends to its own user.

The risk here is not the query work, it is the mail: sending twice, sending to
someone who unsubscribed, sending from a mailbox that is in trouble, or sending
seven zeroes often enough that the user filters the whole thing. Every one of
those has a test.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.digest import DigestPreference
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.job import JobPosting, job_fingerprint
from app.models.recruiter import Recruiter
from app.services import digest_service, gmail_service

DIGEST = "/api/v1/digest"
_seq = itertools.count(1)


@pytest.fixture()
def campaign(db_session, current_user, resume) -> Campaign:
    row = Campaign(user_id=current_user.id, resume_id=resume.id, name="Outreach")
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture()
def outbox(monkeypatch) -> list[dict]:
    """Capture what would have gone to Gmail instead of reaching the network."""
    sent: list[dict] = []

    def _send(**kwargs):
        sent.append(kwargs)
        return gmail_service.SentMessage(
            gmail_message_id=f"msg-{len(sent)}", gmail_thread_id=f"thr-{len(sent)}"
        )

    monkeypatch.setattr(digest_service.gmail_service, "send_email", _send)
    return sent


def _application(db, user, campaign, *, status=ApplicationStatus.OUTREACH_SENT, company=None):
    n = next(_seq)
    recruiter = Recruiter(
        user_id=user.id,
        email=f"talent{n}@corp{n}.example",
        name="Alex Recruiter",
        company=company or f"Corp {n}",
    )
    db.add(recruiter)
    db.flush()
    application = Application(
        user_id=user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        status=status,
    )
    db.add(application)
    db.flush()
    return application


def _thread_with(db, application, *, outbound=(), inbound=()):
    """A thread carrying the given (status, sent_at) outbound and inbound mail."""
    thread = EmailThread(application_id=application.id, subject="Hello")
    db.add(thread)
    db.flush()
    for status, sent_at in outbound:
        db.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.SENT,
                status=status,
                to_address="talent@example.com",
                subject="Quick note about a backend role",
                body_text="Outreach.",
                sent_at=sent_at,
            )
        )
        db.flush()
    for sent_at in inbound:
        db.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.RECEIVED,
                status=EmailStatus.RECEIVED,
                from_address="talent@example.com",
                subject="Re: Hello",
                body_text="Interested — can you send times?",
                sent_at=sent_at,
            )
        )
        db.flush()
    return thread


class TestContent:
    def test_counts_the_weeks_sends_replies_and_finds(
        self, db_session, current_user, campaign
    ):
        now = datetime.now(UTC)
        for _ in range(3):
            app_row = _application(db_session, current_user, campaign)
            _thread_with(
                db_session, app_row,
                outbound=[(EmailStatus.SENT, now - timedelta(days=2))],
            )
        replied = _application(
            db_session, current_user, campaign, status=ApplicationStatus.REPLIED
        )
        _thread_with(
            db_session, replied,
            outbound=[(EmailStatus.SENT, now - timedelta(days=3))],
            inbound=[now - timedelta(days=1)],
        )
        db_session.add(
            JobPosting(
                user_id=current_user.id,
                title="Backend Engineer",
                company="Northwind",
                fingerprint=job_fingerprint("Backend Engineer", "Northwind", None),
            )
        )
        db_session.commit()

        digest = digest_service.build(db_session, current_user)
        assert digest.sent == 4
        assert digest.replies == 1
        assert digest.jobs_found == 1
        assert digest.is_quiet is False

    def test_ignores_activity_older_than_the_window(
        self, db_session, current_user, campaign
    ):
        old = datetime.now(UTC) - timedelta(days=30)
        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.SENT, old)])
        db_session.commit()

        assert digest_service.build(db_session, current_user).sent == 0

    def test_names_the_drafts_that_are_waiting(
        self, db_session, current_user, campaign
    ):
        app_row = _application(db_session, current_user, campaign, company="Northwind")
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        digest = digest_service.build(db_session, current_user)
        assert digest.drafts_waiting == 1
        assert digest.drafts[0].company == "Northwind"
        assert digest.drafts[0].kind == "outreach"

    def test_labels_a_draft_on_an_answered_thread_as_a_reply(
        self, db_session, current_user, campaign
    ):
        now = datetime.now(UTC)
        app_row = _application(db_session, current_user, campaign)
        _thread_with(
            db_session, app_row,
            outbound=[
                (EmailStatus.SENT, now - timedelta(days=3)),
                (EmailStatus.DRAFT, None),
            ],
            inbound=[now - timedelta(days=1)],
        )
        db_session.commit()

        digest = digest_service.build(db_session, current_user)
        assert digest.drafts_waiting == 1
        # A draft on a thread that already holds inbound mail is an answer, and
        # the user reads those with different urgency from a cold pitch.
        assert digest.drafts[0].kind == "reply"

    def test_lists_at_most_five_drafts_but_counts_them_all(
        self, db_session, current_user, campaign
    ):
        for _ in range(8):
            app_row = _application(db_session, current_user, campaign)
            _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        digest = digest_service.build(db_session, current_user)
        assert digest.drafts_waiting == 8
        assert len(digest.drafts) == digest_service.LIST_LIMIT

    def test_counts_a_thread_whose_newest_message_is_inbound_as_unanswered(
        self, db_session, current_user, campaign
    ):
        now = datetime.now(UTC)
        waiting = _application(
            db_session, current_user, campaign, status=ApplicationStatus.REPLIED
        )
        _thread_with(
            db_session, waiting,
            outbound=[(EmailStatus.SENT, now - timedelta(days=4))],
            inbound=[now - timedelta(days=2)],
        )
        answered = _application(
            db_session, current_user, campaign, status=ApplicationStatus.REPLIED
        )
        thread = _thread_with(
            db_session, answered,
            outbound=[(EmailStatus.SENT, now - timedelta(days=4))],
            inbound=[now - timedelta(days=2)],
        )
        db_session.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.SENT,
                status=EmailStatus.SENT,
                to_address="talent@example.com",
                subject="Re: Re: Hello",
                body_text="Here are three times.",
                sent_at=now - timedelta(days=1),
            )
        )
        db_session.commit()

        assert digest_service.build(db_session, current_user).unanswered_replies == 1

    def test_a_draft_still_counts_as_unanswered(
        self, db_session, current_user, campaign
    ):
        """An unapproved draft has not reached the recruiter, so they are still
        waiting — counting it as answered is exactly the blind spot the digest
        exists to close."""
        now = datetime.now(UTC)
        app_row = _application(
            db_session, current_user, campaign, status=ApplicationStatus.REPLIED
        )
        thread = _thread_with(
            db_session, app_row,
            outbound=[(EmailStatus.SENT, now - timedelta(days=4))],
            inbound=[now - timedelta(days=2)],
        )
        db_session.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.SENT,
                status=EmailStatus.DRAFT,
                to_address="talent@example.com",
                subject="Re: Re: Hello",
                body_text="Draft answer.",
            )
        )
        db_session.commit()

        assert digest_service.build(db_session, current_user).unanswered_replies == 1


class TestHeadline:
    def test_leads_with_the_recruiter_who_is_waiting(
        self, db_session, current_user, campaign
    ):
        now = datetime.now(UTC)
        app_row = _application(
            db_session, current_user, campaign, status=ApplicationStatus.REPLIED
        )
        _thread_with(
            db_session, app_row,
            outbound=[(EmailStatus.SENT, now - timedelta(days=4))],
            inbound=[now - timedelta(days=2)],
        )
        db_session.commit()

        digest = digest_service.build(db_session, current_user)
        assert "unanswered" in digest.headline
        assert "waiting on you" in digest_service.subject_line(digest)

    def test_falls_back_to_drafts_then_to_volume(
        self, db_session, current_user, campaign
    ):
        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        digest = digest_service.build(db_session, current_user)
        assert "draft waiting for your approval" in digest.headline
        assert "1 draft to approve" in digest_service.subject_line(digest)

    def test_reports_a_moved_reply_rate_when_nothing_needs_doing(
        self, db_session, current_user, campaign
    ):
        now = datetime.now(UTC)
        # Last week: 1 of 4 replied. The week before: 0 of 4.
        for index in range(4):
            row = _application(
                db_session, current_user, campaign,
                status=(
                    ApplicationStatus.REPLIED if index == 0 else ApplicationStatus.NO_RESPONSE
                ),
            )
            row.created_at = now - timedelta(days=2)
            _thread_with(
                db_session, row,
                outbound=[(EmailStatus.SENT, now - timedelta(days=2))],
            )
        for _ in range(4):
            row = _application(
                db_session, current_user, campaign, status=ApplicationStatus.NO_RESPONSE
            )
            row.created_at = now - timedelta(days=10)
        db_session.commit()

        digest = digest_service.build(db_session, current_user)
        assert "Reply rate is up to 25% from 0%" in digest.headline

    def test_says_so_plainly_when_the_week_was_empty(self, db_session, current_user):
        digest = digest_service.build(db_session, current_user)
        assert digest.is_quiet is True
        assert "quiet week" in digest.headline


class TestRendering:
    def test_the_body_carries_the_numbers_and_the_opt_out(
        self, db_session, current_user, campaign
    ):
        app_row = _application(db_session, current_user, campaign, company="Northwind")
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        pref = digest_service.get_or_create(db_session, current_user)
        body = digest_service.render_text(
            digest_service.build(db_session, current_user),
            app_url="https://app.example",
            opt_out_url=digest_service.unsubscribe_url(pref),
        )
        assert "Northwind" in body
        assert "Emails sent" in body
        assert pref.unsubscribe_token in body

    def test_the_html_part_escapes_what_a_recruiter_controls(
        self, db_session, current_user, campaign
    ):
        """Company names come off scraped careers pages, so they are untrusted."""
        app_row = _application(
            db_session, current_user, campaign, company="<script>x</script>"
        )
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        html = digest_service.render_html(
            digest_service.build(db_session, current_user),
            app_url="https://app.example",
            opt_out_url="https://example/unsub",
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestScheduling:
    def _pref(self, db, user, **kwargs) -> DigestPreference:
        pref = digest_service.get_or_create(db, user)
        for name, value in kwargs.items():
            setattr(pref, name, value)
        db.commit()
        return pref

    def test_a_brand_new_row_is_due_immediately(self, db_session, current_user):
        """The first digest is the one that teaches the user the feature exists."""
        pref = self._pref(db_session, current_user)
        assert digest_service.is_due(pref) is True

    def test_is_not_due_again_the_same_week(self, db_session, current_user):
        now = datetime.now(UTC)
        pref = self._pref(db_session, current_user, last_sent_at=now - timedelta(hours=2))
        assert digest_service.is_due(pref, now=now) is False

    def test_is_due_on_the_scheduled_hour(self, db_session, current_user):
        # A Monday at 09:00 UTC, with the last digest eight days back.
        monday = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
        pref = self._pref(
            db_session, current_user,
            weekday=0, hour=8, last_sent_at=monday - timedelta(days=8),
        )
        assert digest_service.is_due(pref, now=monday) is True

    def test_a_missed_window_still_goes_out_later_that_week(
        self, db_session, current_user
    ):
        """Late is a much smaller failure than silent."""
        wednesday = datetime(2026, 7, 29, 15, 0, tzinfo=UTC)
        pref = self._pref(
            db_session, current_user,
            weekday=0, hour=8, last_sent_at=wednesday - timedelta(days=9),
        )
        assert digest_service.is_due(pref, now=wednesday) is True

    def test_an_unsubscribed_user_is_never_due(self, db_session, current_user):
        pref = self._pref(db_session, current_user, enabled=False)
        assert digest_service.is_due(pref) is False


class TestSending:
    def test_sends_to_the_user_from_their_own_mailbox(
        self, db_session, current_user, connected_gmail, campaign, outbox
    ):
        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        result = digest_service.send(db_session, current_user)
        assert result["status"] == "sent"
        assert outbox[0]["to"] == current_user.email
        assert outbox[0]["account"].email == connected_gmail.email

    def test_carries_the_unsubscribe_header_without_the_canspam_footer(
        self, db_session, current_user, connected_gmail, campaign, outbox
    ):
        """That footer explains why a *stranger* is being contacted. Here it
        would be a lie; the List-Unsubscribe header is the honest opt-out."""
        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        digest_service.send(db_session, current_user)
        assert outbox[0]["footer"] == ""
        assert "/digest?token=" in outbox[0]["unsubscribe_url"]
        assert settings_address() not in outbox[0]["body_text"]

    def test_books_the_send_against_the_mailboxs_own_allowance(
        self, db_session, current_user, connected_gmail, campaign, outbox
    ):
        """The digest costs the user a send; under-counting would let the
        warm-up ramp believe it has more headroom than Gmail does."""
        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()
        before = connected_gmail.sent_total or 0

        digest_service.send(db_session, current_user)
        db_session.refresh(connected_gmail)
        assert connected_gmail.sent_total == before + 1

    def test_will_not_send_twice_in_a_week(
        self, db_session, current_user, connected_gmail, campaign, outbox
    ):
        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        assert digest_service.send(db_session, current_user)["status"] == "sent"
        assert digest_service.send(db_session, current_user)["status"] == "not_due"
        assert len(outbox) == 1

    def test_refuses_after_an_unsubscribe_even_when_forced(
        self, db_session, current_user, connected_gmail, outbox
    ):
        """A button in the app is not consent that overrides an opt-out."""
        pref = digest_service.get_or_create(db_session, current_user)
        pref.enabled = False
        db_session.commit()

        result = digest_service.send(db_session, current_user, force=True)
        assert result["status"] == "unsubscribed"
        assert outbox == []

    def test_refuses_while_the_mailbox_is_paused(
        self, db_session, current_user, connected_gmail, campaign, outbox
    ):
        connected_gmail.paused_until = datetime.now(UTC) + timedelta(hours=6)
        connected_gmail.pause_reason = "Bounce rate too high"
        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        assert (
            digest_service.send(db_session, current_user, force=True)["status"]
            == "mailbox_paused"
        )
        assert outbox == []

    def test_skips_a_week_where_nothing_happened(
        self, db_session, current_user, connected_gmail, outbox
    ):
        """Seven zeroes trains the user to delete the digest unread."""
        result = digest_service.send(db_session, current_user)
        assert result["status"] == "skipped_quiet"
        assert outbox == []
        # Marked as done so the hourly sweep doesn't retry it all day.
        assert digest_service.get_or_create(db_session, current_user).last_sent_at

    def test_a_user_with_no_mailbox_is_a_status_not_a_crash(
        self, db_session, current_user, outbox
    ):
        assert digest_service.send(db_session, current_user)["status"] == "no_mailbox"

    def test_a_failing_mailbox_records_the_error_and_moves_on(
        self, db_session, current_user, connected_gmail, campaign, monkeypatch
    ):
        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        db_session.commit()

        def _boom(**kwargs):
            raise gmail_service.GmailNotConfigured("token revoked")

        monkeypatch.setattr(digest_service.gmail_service, "send_email", _boom)

        result = digest_service.send(db_session, current_user)
        assert result["status"] == "failed"
        pref = digest_service.get_or_create(db_session, current_user)
        assert "token revoked" in pref.last_error
        # Not marked as sent — next sweep tries again.
        assert pref.last_sent_at is None


class TestApi:
    def test_preview_renders_without_sending(self, auth_client, outbox):
        body = auth_client.get(f"{DIGEST}/preview").json()
        assert "subject" in body
        assert body["is_quiet"] is True
        assert outbox == []

    def test_preferences_round_trip(self, auth_client):
        assert auth_client.get(DIGEST).json()["enabled"] is True
        body = auth_client.put(DIGEST, json={"weekday": 3, "hour": 17}).json()
        assert (body["weekday"], body["hour"]) == (3, 17)

    def test_rejects_an_impossible_hour(self, auth_client):
        assert auth_client.put(DIGEST, json={"hour": 25}).status_code == 422

    def test_reports_that_there_is_no_mailbox_to_send_from(self, auth_client):
        assert auth_client.get(DIGEST).json()["can_send"] is False

    def test_send_now_ignores_the_schedule_but_not_the_opt_out(
        self, auth_client, db_session, current_user, connected_gmail, outbox
    ):
        assert auth_client.post(f"{DIGEST}/send").json()["status"] == "sent"
        auth_client.put(DIGEST, json={"enabled": False})
        assert auth_client.post(f"{DIGEST}/send").json()["status"] == "unsubscribed"

    def test_the_unsubscribe_link_switches_the_digest_off(
        self, client, auth_client, db_session, current_user
    ):
        pref = digest_service.get_or_create(db_session, current_user)
        token = pref.unsubscribe_token

        resp = client.get(f"{DIGEST}/unsubscribe", params={"token": token})
        assert resp.status_code == 200
        db_session.refresh(pref)
        assert pref.enabled is False

    def test_a_bad_token_answers_identically(self, client, db_session, current_user):
        """A different answer for a live token makes this an oracle for them."""
        pref = digest_service.get_or_create(db_session, current_user)
        good = client.get(
            f"{DIGEST}/unsubscribe", params={"token": pref.unsubscribe_token}
        )
        bad = client.get(f"{DIGEST}/unsubscribe", params={"token": "not-a-token"})
        assert good.status_code == bad.status_code == 200
        assert good.text == bad.text

    def test_preferences_require_authentication(self, client):
        assert client.get(DIGEST).status_code == 401


class TestBeatSweep:
    def test_only_considers_users_who_have_a_row(
        self, db_session, current_user, connected_gmail, campaign, outbox, monkeypatch
    ):
        from app.tasks import digest_tasks

        monkeypatch.setattr(digest_tasks, "SessionLocal", lambda: db_session)
        monkeypatch.setattr(db_session, "close", lambda: None)

        # No preference row yet — the sweep must not invent one and mail them.
        assert digest_tasks.send_weekly_digests()["considered"] == 0

        app_row = _application(db_session, current_user, campaign)
        _thread_with(db_session, app_row, outbound=[(EmailStatus.DRAFT, None)])
        digest_service.get_or_create(db_session, current_user)
        db_session.commit()

        result = digest_tasks.send_weekly_digests()
        assert result["considered"] == 1
        assert result["outcomes"] == {"sent": 1}


def settings_address() -> str:
    from app.core.config import settings

    return settings.compliance_physical_address
