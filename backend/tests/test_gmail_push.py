"""Gmail push: decoding notifications, renewal, and the fallback to polling.

The property that matters most here is that push can only ever make the product
*faster*, never blinder. Every failure path — no topic configured, a rejected
watch, an aged-out cursor, a malformed webhook body — has to end with the
mailbox still being polled. The cursor discipline is the other half: it advances
only once the messages it covers have been handed off, so a crash replays a
range instead of silently skipping the replies inside it.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.models.application import Application
from app.models.campaign import Campaign
from app.models.email_thread import EmailThread
from app.models.gmail_watch import GmailWatch
from app.models.recruiter import Recruiter
from app.services import gmail_push


def envelope(email="candidate@gmail.com", history_id="12345") -> dict:
    payload = json.dumps({"emailAddress": email, "historyId": history_id})
    return {"message": {"data": base64.urlsafe_b64encode(payload.encode()).decode()}}


@pytest.fixture()
def watch(db_session, connected_gmail) -> GmailWatch:
    row = GmailWatch(
        gmail_account_id=connected_gmail.id,
        topic="projects/p/topics/t",
        history_id="1000",
        status="active",
        expires_at=datetime.now(UTC) + timedelta(days=6),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


class TestDecodeNotification:
    def test_reads_the_mailbox_and_cursor(self):
        found = gmail_push.decode_notification(envelope())
        assert found is not None
        assert found.email_address == "candidate@gmail.com"
        assert found.history_id == "12345"

    def test_lowercases_the_address(self):
        found = gmail_push.decode_notification(envelope(email="Candidate@Gmail.com"))
        assert found.email_address == "candidate@gmail.com"

    def test_tolerates_missing_base64_padding(self):
        payload = json.dumps({"emailAddress": "a@b.com", "historyId": "7"})
        stripped = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        found = gmail_push.decode_notification({"message": {"data": stripped}})
        assert found is not None

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"message": None},
            {"message": {}},
            {"message": {"data": ""}},
            {"message": {"data": "!!!not base64!!!"}},
            {"message": {"data": base64.urlsafe_b64encode(b"not json").decode()}},
            # Well-formed base64 JSON, but missing the fields that identify a mailbox.
            {"message": {"data": base64.urlsafe_b64encode(b'{"foo": 1}').decode()}},
        ],
    )
    def test_malformed_payloads_return_none_rather_than_raising(self, payload):
        assert gmail_push.decode_notification(payload) is None


class TestToken:
    def test_any_token_passes_when_none_is_configured(self, monkeypatch):
        monkeypatch.setattr("app.services.gmail_push.settings.gmail_pubsub_token", "")
        assert gmail_push.token_is_valid(None) is True

    def test_matching_token_passes(self, monkeypatch):
        monkeypatch.setattr("app.services.gmail_push.settings.gmail_pubsub_token", "s3cret")
        assert gmail_push.token_is_valid("s3cret") is True

    def test_wrong_or_absent_token_fails(self, monkeypatch):
        monkeypatch.setattr("app.services.gmail_push.settings.gmail_pubsub_token", "s3cret")
        assert gmail_push.token_is_valid("nope") is False
        assert gmail_push.token_is_valid(None) is False


class TestStartWatch:
    def test_no_topic_means_stopped_not_failed(
        self, db_session, monkeypatch, connected_gmail
    ):
        monkeypatch.setattr("app.services.gmail_push.settings.gmail_pubsub_topic", "")
        row = gmail_push.start_watch(db_session, connected_gmail)
        assert row.status == "stopped"
        assert "polling" in (row.last_error or "")

    def test_records_expiry_from_gmail(self, db_session, monkeypatch, connected_gmail):
        monkeypatch.setattr(
            "app.services.gmail_push.settings.gmail_pubsub_topic", "projects/p/topics/t"
        )
        expiry_ms = int(
            (datetime.now(UTC) + timedelta(days=7)).timestamp() * 1000
        )
        with patch(
            "app.services.gmail_push.gmail_service.start_watch",
            return_value={"historyId": "555", "expiration": str(expiry_ms)},
        ):
            row = gmail_push.start_watch(db_session, connected_gmail)

        assert row.status == "active"
        assert row.history_id == "555"
        assert row.expires_at is not None
        assert row.last_error is None

    def test_a_failed_registration_leaves_the_mailbox_on_polling(
        self, db_session, monkeypatch, connected_gmail
    ):
        monkeypatch.setattr(
            "app.services.gmail_push.settings.gmail_pubsub_topic", "projects/p/topics/t"
        )
        with patch(
            "app.services.gmail_push.gmail_service.start_watch",
            side_effect=RuntimeError("permission denied on topic"),
        ):
            row = gmail_push.start_watch(db_session, connected_gmail)

        assert row.status == "failed"
        assert "permission denied" in row.last_error
        assert gmail_push.push_is_healthy(row) is False

    def test_re_registering_updates_the_same_row(
        self, db_session, monkeypatch, connected_gmail
    ):
        monkeypatch.setattr(
            "app.services.gmail_push.settings.gmail_pubsub_topic", "projects/p/topics/t"
        )
        with patch(
            "app.services.gmail_push.gmail_service.start_watch",
            return_value={"historyId": "1", "expiration": "0"},
        ):
            first = gmail_push.start_watch(db_session, connected_gmail)
            db_session.commit()
            second = gmail_push.start_watch(db_session, connected_gmail)

        assert first.id == second.id


class TestRenewal:
    def test_a_watch_near_expiry_is_due(self, db_session, watch):
        watch.expires_at = datetime.now(UTC) + timedelta(hours=6)
        db_session.commit()
        assert watch in gmail_push.due_for_renewal(db_session)

    def test_a_fresh_watch_is_not_due(self, db_session, watch):
        watch.expires_at = datetime.now(UTC) + timedelta(days=6)
        db_session.commit()
        assert gmail_push.due_for_renewal(db_session) == []

    def test_a_failed_watch_is_retried(self, db_session, watch):
        watch.status = "failed"
        watch.expires_at = datetime.now(UTC) + timedelta(days=6)
        db_session.commit()
        assert watch in gmail_push.due_for_renewal(db_session)

    def test_a_stopped_watch_is_left_alone(self, db_session, watch):
        watch.status = "stopped"
        watch.expires_at = None
        db_session.commit()
        assert gmail_push.due_for_renewal(db_session) == []

    def test_a_watch_with_no_expiry_is_due(self, db_session, watch):
        watch.expires_at = None
        db_session.commit()
        assert watch in gmail_push.due_for_renewal(db_session)


class TestHealth:
    def test_active_and_unexpired_is_healthy(self, watch):
        assert gmail_push.push_is_healthy(watch) is True

    def test_an_expired_watch_is_not_healthy_even_if_marked_active(self, watch):
        watch.expires_at = datetime.now(UTC) - timedelta(hours=1)
        assert watch.status == "active"
        assert gmail_push.push_is_healthy(watch) is False

    def test_no_watch_is_not_healthy(self):
        assert gmail_push.push_is_healthy(None) is False


class TestCursorDiscipline:
    def test_a_notification_does_not_advance_the_cursor(self, db_session, watch):
        gmail_push.record_notification(db_session, watch, "9999")
        db_session.commit()

        # The fetch hasn't run, so the processed-up-to marker must not move.
        assert watch.history_id == "1000"
        assert watch.notifications_received == 1
        assert watch.last_notified_at is not None

    def test_the_cursor_moves_only_after_the_work(self, db_session, watch):
        gmail_push.record_notification(db_session, watch, "9999")
        gmail_push.advance_cursor(db_session, watch, "9999", 3)
        db_session.commit()

        assert watch.history_id == "9999"
        assert watch.messages_ingested == 3

    def test_a_notification_revives_a_watch_we_thought_was_broken(
        self, db_session, watch
    ):
        watch.status = "failed"
        watch.last_error = "transient 503"
        db_session.commit()

        gmail_push.record_notification(db_session, watch, "2000")
        assert watch.status == "active"
        assert watch.last_error is None


class TestWebhookEndpoint:
    def test_a_valid_notification_is_acknowledged(
        self, auth_client, db_session, watch, monkeypatch
    ):
        monkeypatch.setattr("app.services.gmail_push.settings.gmail_pubsub_token", "")
        with patch("app.tasks.inbox_tasks.ingest_push_notification.delay") as enqueue:
            resp = auth_client.post("/api/v1/gmail/webhook", json=envelope())

        assert resp.status_code == 204
        enqueue.assert_called_once()
        db_session.refresh(watch)
        assert watch.notifications_received == 1

    def test_a_bad_token_is_acknowledged_but_ignored(
        self, auth_client, db_session, watch, monkeypatch
    ):
        monkeypatch.setattr("app.services.gmail_push.settings.gmail_pubsub_token", "s3cret")
        with patch("app.tasks.inbox_tasks.ingest_push_notification.delay") as enqueue:
            resp = auth_client.post(
                "/api/v1/gmail/webhook", json=envelope(), params={"token": "wrong"}
            )

        # 204 rather than 403: a non-2xx makes Pub/Sub retry forever.
        assert resp.status_code == 204
        enqueue.assert_not_called()
        db_session.refresh(watch)
        assert watch.notifications_received == 0

    def test_a_malformed_body_is_acknowledged(self, auth_client, monkeypatch):
        monkeypatch.setattr("app.services.gmail_push.settings.gmail_pubsub_token", "")
        with patch("app.tasks.inbox_tasks.ingest_push_notification.delay") as enqueue:
            resp = auth_client.post("/api/v1/gmail/webhook", json={"nope": True})

        assert resp.status_code == 204
        enqueue.assert_not_called()

    def test_an_unknown_mailbox_is_acknowledged(self, auth_client, monkeypatch):
        monkeypatch.setattr("app.services.gmail_push.settings.gmail_pubsub_token", "")
        with patch("app.tasks.inbox_tasks.ingest_push_notification.delay") as enqueue:
            resp = auth_client.post(
                "/api/v1/gmail/webhook", json=envelope(email="stranger@gmail.com")
            )

        assert resp.status_code == 204
        enqueue.assert_not_called()

    def test_status_reports_the_watch(self, auth_client, watch, monkeypatch):
        monkeypatch.setattr(
            "app.services.gmail_push.settings.gmail_pubsub_topic", "projects/p/topics/t"
        )
        body = auth_client.get("/api/v1/gmail/status").json()
        assert body["push_configured"] is True
        assert body["watch"]["status"] == "active"


class TestPollingFallback:
    """Push never replaces polling — it only lets polling skip what it covers."""

    def _thread(self, db_session, current_user, connected_gmail):
        """The minimum Campaign → Recruiter → Application → EmailThread chain."""
        campaign = Campaign(user_id=current_user.id, name="Test")
        recruiter = Recruiter(user_id=current_user.id, email="talent@acme.com")
        db_session.add_all([campaign, recruiter])
        db_session.flush()

        application = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
        )
        db_session.add(application)
        db_session.flush()

        thread = EmailThread(application_id=application.id, gmail_thread_id="t-1")
        db_session.add(thread)
        db_session.commit()
        return thread

    def test_a_healthy_watch_lets_polling_skip_the_thread(
        self, db_session, current_user, connected_gmail, watch
    ):
        from app.tasks import inbox_tasks

        thread = self._thread(db_session, current_user, connected_gmail)
        assert inbox_tasks._push_covers(db_session, thread) is True

    def test_an_expired_watch_falls_back_to_polling(
        self, db_session, current_user, connected_gmail, watch
    ):
        from app.tasks import inbox_tasks

        thread = self._thread(db_session, current_user, connected_gmail)
        watch.expires_at = datetime.now(UTC) - timedelta(hours=1)
        db_session.commit()

        assert inbox_tasks._push_covers(db_session, thread) is False

    def test_a_mailbox_with_no_watch_at_all_is_polled(
        self, db_session, current_user, connected_gmail
    ):
        from app.tasks import inbox_tasks

        thread = self._thread(db_session, current_user, connected_gmail)
        assert inbox_tasks._push_covers(db_session, thread) is False

    def test_a_thread_with_no_application_is_polled(self, db_session):
        from app.tasks import inbox_tasks

        orphan = EmailThread(application_id=None, gmail_thread_id="t-x")
        assert inbox_tasks._push_covers(db_session, orphan) is False


# --------------------------------------------------------------------------- #
# Push as the primary route, beat as the five-minute fallback                   #
# --------------------------------------------------------------------------- #


class TestPushCovers:
    """`push_covers` is what lets the beat sweep stand down for a mailbox.

    It is deliberately stricter than `push_is_healthy`. A subscription can sit at
    status="active" with a future expires_at while Google has quietly stopped
    publishing — renewal keeps the row looking fine either way — so "active" is
    not evidence of *delivery*. If beat trusted that, a silently dead
    subscription would switch a mailbox off rather than falling back to polling,
    which is the one thing this feature must never do.
    """

    def test_a_watch_notified_recently_is_covered(self, db_session, watch):
        watch.last_notified_at = datetime.now(UTC) - timedelta(minutes=2)

        assert gmail_push.push_covers(watch) is True

    def test_a_watch_silent_past_the_trust_window_is_not_covered(
        self, db_session, watch, monkeypatch
    ):
        """Fifteen minutes of silence is enough to stop believing it."""
        monkeypatch.setattr(settings, "recruiter_push_trust_seconds", 900)
        watch.last_notified_at = datetime.now(UTC) - timedelta(minutes=20)
        watch.last_renewed_at = datetime.now(UTC) - timedelta(hours=6)

        assert gmail_push.push_covers(watch) is False
        # And it is still "healthy" — which is exactly why the two are separate.
        assert gmail_push.push_is_healthy(watch) is True

    def test_a_freshly_registered_watch_is_covered_before_its_first_notification(
        self, db_session, watch
    ):
        """A quiet mailbox has nothing to report; that is not a failure."""
        watch.last_notified_at = None
        watch.last_renewed_at = datetime.now(UTC) - timedelta(minutes=1)

        assert gmail_push.push_covers(watch) is True

    def test_a_watch_that_has_never_signalled_anything_is_not_covered(
        self, db_session, watch
    ):
        watch.last_notified_at = None
        watch.last_renewed_at = None

        assert gmail_push.push_covers(watch) is False

    def test_an_expired_watch_is_not_covered_however_recently_it_spoke(
        self, db_session, watch
    ):
        watch.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        watch.last_notified_at = datetime.now(UTC)

        assert gmail_push.push_covers(watch) is False

    @pytest.mark.parametrize("status", ["failed", "stopped"])
    def test_a_non_active_watch_is_not_covered(self, db_session, watch, status):
        watch.status = status
        watch.last_notified_at = datetime.now(UTC)

        assert gmail_push.push_covers(watch) is False

    def test_no_watch_at_all_is_not_covered(self):
        assert gmail_push.push_covers(None) is False

    def test_a_zero_trust_window_never_covers(self, db_session, watch, monkeypatch):
        """The escape hatch: a deployment that wants beat to sweep everything."""
        monkeypatch.setattr(settings, "recruiter_push_trust_seconds", 0)
        watch.last_notified_at = datetime.now(UTC)

        assert gmail_push.push_covers(watch) is False

    def test_naive_timestamps_from_sqlite_are_handled(self, db_session, watch):
        """SQLite round-trips datetimes without a tzinfo; the comparison must not blow up."""
        watch.last_notified_at = datetime.now(UTC).replace(tzinfo=None)

        assert gmail_push.push_covers(watch) is True


class TestBeatStandsDownForCoveredMailboxes:
    """The beat sweep is a fallback now. It must skip — and *say* it skipped."""

    def test_a_push_covered_mailbox_is_not_enqueued(
        self, db_session, current_user, connected_gmail, watch, monkeypatch
    ):
        from app.models.recruiter_email import RecruiterReplyPreference
        from app.tasks import recruiter_reply_tasks

        db_session.add(
            RecruiterReplyPreference(user_id=current_user.id, enabled=True)
        )
        db_session.commit()
        watch.last_notified_at = datetime.now(UTC)
        db_session.commit()

        monkeypatch.setattr(settings, "recruiter_reply_enabled", True)
        monkeypatch.setattr(recruiter_reply_tasks, "SessionLocal", lambda: db_session)
        monkeypatch.setattr(db_session, "close", lambda: None)
        enqueued: list = []
        monkeypatch.setattr(
            recruiter_reply_tasks.scan_mailbox,
            "apply_async",
            lambda *a, **kw: enqueued.append((a, kw)),
        )

        result = recruiter_reply_tasks.scan_all_recruiter_inboxes()

        assert enqueued == []
        # Named, not silent. "Why did nothing happen?" is answerable from the
        # task result alone — the contract every other skip here keeps.
        assert result["skipped_push_covered"] == 1
        assert result["mailboxes_enqueued"] == 0

    def test_a_mailbox_push_has_gone_quiet_on_is_still_swept(
        self, db_session, current_user, connected_gmail, watch, monkeypatch
    ):
        """The fallback doing its job: push looks fine, isn't delivering, beat scans."""
        from app.models.recruiter_email import RecruiterReplyPreference
        from app.tasks import recruiter_reply_tasks

        db_session.add(
            RecruiterReplyPreference(user_id=current_user.id, enabled=True)
        )
        watch.last_notified_at = datetime.now(UTC) - timedelta(hours=2)
        watch.last_renewed_at = datetime.now(UTC) - timedelta(hours=2)
        db_session.commit()

        monkeypatch.setattr(settings, "recruiter_reply_enabled", True)
        monkeypatch.setattr(recruiter_reply_tasks, "SessionLocal", lambda: db_session)
        monkeypatch.setattr(db_session, "close", lambda: None)
        enqueued: list = []
        monkeypatch.setattr(
            recruiter_reply_tasks.scan_mailbox,
            "apply_async",
            lambda *a, **kw: enqueued.append((a, kw)),
        )

        result = recruiter_reply_tasks.scan_all_recruiter_inboxes()

        assert len(enqueued) == 1
        assert result["skipped_push_covered"] == 0

    def test_a_mailbox_with_no_watch_is_swept(
        self, db_session, current_user, connected_gmail, monkeypatch
    ):
        from app.models.recruiter_email import RecruiterReplyPreference
        from app.tasks import recruiter_reply_tasks

        db_session.add(
            RecruiterReplyPreference(user_id=current_user.id, enabled=True)
        )
        db_session.commit()

        monkeypatch.setattr(settings, "recruiter_reply_enabled", True)
        monkeypatch.setattr(recruiter_reply_tasks, "SessionLocal", lambda: db_session)
        monkeypatch.setattr(db_session, "close", lambda: None)
        enqueued: list = []
        monkeypatch.setattr(
            recruiter_reply_tasks.scan_mailbox,
            "apply_async",
            lambda *a, **kw: enqueued.append((a, kw)),
        )

        result = recruiter_reply_tasks.scan_all_recruiter_inboxes()

        assert len(enqueued) == 1
        assert result["skipped_push_covered"] == 0


class TestStatusReportsPushHealth:
    def test_status_distinguishes_configured_from_working(
        self, auth_client, db_session, connected_gmail, watch
    ):
        """"Push is set up" and "push is working" are different claims.

        `watch.status` says "active" for a lapsed subscription too, so a user
        whose replies arrive slowly could not tell which one was wrong.
        """
        body = auth_client.get("/api/v1/gmail/status").json()

        assert body["push_healthy"] is True

    def test_an_expired_watch_reports_unhealthy(
        self, auth_client, db_session, connected_gmail, watch
    ):
        watch.expires_at = datetime.now(UTC) - timedelta(hours=1)
        db_session.commit()

        body = auth_client.get("/api/v1/gmail/status").json()

        assert body["push_healthy"] is False
        # Still "active" on the row — the exact discrepancy this field exists for.
        assert body["watch"]["status"] == "active"

    def test_no_connection_reports_unhealthy_rather_than_erroring(self, auth_client):
        body = auth_client.get("/api/v1/gmail/status").json()

        assert body["push_healthy"] is False
