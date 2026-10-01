"""The stats view: what was scanned, what came of it, and what moved.

The gap this closes is a denominator. ``ScanResult`` counted everything a
dashboard needs — listed, examined, and one counter per reason a message was
passed over — and ``inbound_scanner`` logged the dict once and dropped it. "How
many emails did you scan?" was not answerable from the database at all, and
neither was "why was that one skipped?".

Two counting rules get their own tests here because they are the ones a reader
will second-guess:

* Scan counters and outcome counters are **different denominators over different
  time axes** and are never merged into one funnel.
* ``auto_sent`` counts only the AUTO band. A draft the user approved is a reply
  *they* sent, and counting it here would overstate exactly the number they most
  want to be honest.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailKind,
    RecruiterEmailStatus,
    ReplyRoute,
)
from app.models.recruiter_scan_run import (
    TRIGGER_BEAT,
    TRIGGER_MANUAL,
    TRIGGER_PUSH,
    RecruiterScanRun,
)
from app.services import recruiter_reply_service
from tests.test_recruiter_reply import gmail_message

STATS = "/api/v1/recruiter-inbox/stats"


def add_run(db, user, account, *, trigger=TRIGGER_BEAT, when=None, **counters):
    run = RecruiterScanRun(
        user_id=user.id,
        gmail_account_id=account.id if account else None,
        trigger=trigger,
        **counters,
    )
    db.add(run)
    db.flush()
    if when is not None:
        run.created_at = when
    db.commit()
    return run


def add_email(db, user, *, status, route=None, kind=None, when=None, message_id=None):
    row = RecruiterEmail(
        user_id=user.id,
        gmail_message_id=message_id or f"m-{datetime.now(UTC).timestamp()}",
        from_address="alex@northwind.com",
        kind=kind or RecruiterEmailKind.RECRUITER_OUTREACH,
        classification_confidence=0.9,
        route=route,
        status=status,
        received_at=when or datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# Writing the scan-run rows                                                    #
# --------------------------------------------------------------------------- #


class TestScanRunsArePersisted:
    def test_a_scan_writes_its_counters_down(
        self, db_session, current_user, connected_gmail, stub_gmail
    ):
        stub_gmail["m1"] = gmail_message("m1")

        recruiter_reply_service.record_scan(db_session, current_user, connected_gmail)
        db_session.commit()

        run = db_session.query(RecruiterScanRun).one()
        assert run.listed == 1
        assert run.examined == 1
        assert run.detected == 1
        assert run.query  # the search is recorded, so it can be questioned

    def test_the_skip_reasons_survive_the_scan(
        self, db_session, current_user, connected_gmail, stub_gmail
    ):
        """The numbers that were only ever in a log line."""
        stub_gmail["m1"] = gmail_message("m1")
        recruiter_reply_service.record_scan(db_session, current_user, connected_gmail)
        db_session.commit()

        recruiter_reply_service.record_scan(db_session, current_user, connected_gmail)
        db_session.commit()

        second = db_session.query(RecruiterScanRun).order_by(
            RecruiterScanRun.id.desc()
        ).first()
        assert second.skipped_known == 1
        assert second.detected == 0

    @pytest.mark.parametrize(
        "trigger", [TRIGGER_BEAT, TRIGGER_PUSH, TRIGGER_MANUAL]
    )
    def test_the_trigger_is_recorded(
        self, db_session, current_user, connected_gmail, stub_gmail, trigger
    ):
        """How "is push actually doing the work?" gets answered."""
        recruiter_reply_service.record_scan(
            db_session, current_user, connected_gmail, trigger=trigger
        )
        db_session.commit()

        assert db_session.query(RecruiterScanRun).one().trigger == trigger

    def test_the_manual_button_tags_its_scan(
        self, auth_client, db_session, current_user, connected_gmail, stub_gmail, monkeypatch
    ):
        monkeypatch.setattr(settings, "recruiter_reply_enabled", True)

        auth_client.post("/api/v1/recruiter-inbox/scan")

        assert db_session.query(RecruiterScanRun).one().trigger == TRIGGER_MANUAL

    def test_a_failed_scan_is_still_recorded(
        self, db_session, current_user, connected_gmail, monkeypatch
    ):
        """A scan that found nothing because Gmail was unreachable is a fact too."""
        from app.services import gmail_service, inbound_scanner

        def _boom(account, query, *, max_results=50):
            raise gmail_service.GmailNotConfigured("no credentials")

        monkeypatch.setattr(inbound_scanner.gmail_service, "list_messages", _boom)

        recruiter_reply_service.record_scan(db_session, current_user, connected_gmail)
        db_session.commit()

        run = db_session.query(RecruiterScanRun).one()
        assert run.error == "no credentials"
        assert run.detected == 0


# --------------------------------------------------------------------------- #
# The endpoint                                                                 #
# --------------------------------------------------------------------------- #


class TestTotals:
    def test_an_account_with_no_history_returns_zeros(self, auth_client):
        """Not a 404 — "nothing yet" is a real state with a real shape."""
        body = auth_client.get(STATS).json()

        assert body["totals"]["scans"] == 0
        assert body["totals"]["detected"] == 0
        assert body["skipped"] == []
        assert body["trend"] == []

    def test_scan_totals_add_up(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        add_run(db_session, current_user, connected_gmail, listed=100, examined=8)
        add_run(db_session, current_user, connected_gmail, listed=40, examined=2)

        totals = auth_client.get(STATS).json()["totals"]

        assert totals["scans"] == 2
        assert totals["listed"] == 140
        assert totals["examined"] == 10

    def test_outcome_totals_come_from_the_messages(
        self, auth_client, db_session, current_user
    ):
        add_email(
            db_session,
            current_user,
            status=RecruiterEmailStatus.REPLIED,
            route=ReplyRoute.AUTO,
            message_id="m1",
        )
        add_email(
            db_session,
            current_user,
            status=RecruiterEmailStatus.DRAFTED,
            route=ReplyRoute.DRAFT,
            message_id="m2",
        )
        add_email(
            db_session,
            current_user,
            status=RecruiterEmailStatus.FLAGGED,
            route=ReplyRoute.FLAG,
            message_id="m3",
        )

        totals = auth_client.get(STATS).json()["totals"]

        assert totals["detected"] == 3
        assert totals["classified_recruiter"] == 3
        assert totals["replied"] == 1
        assert totals["drafts_pending"] == 1
        assert totals["flagged"] == 1

    def test_auto_sent_counts_only_the_unread_band(
        self, auth_client, db_session, current_user
    ):
        """An approved draft is a reply the *user* sent. Counting it here lies."""
        add_email(
            db_session,
            current_user,
            status=RecruiterEmailStatus.REPLIED,
            route=ReplyRoute.AUTO,
            message_id="m1",
        )
        add_email(
            db_session,
            current_user,
            status=RecruiterEmailStatus.REPLIED,
            route=ReplyRoute.DRAFT,
            message_id="m2",
        )

        totals = auth_client.get(STATS).json()["totals"]

        assert totals["replied"] == 2
        assert totals["auto_sent"] == 1

    def test_non_recruiter_mail_is_detected_but_not_counted_as_recruiter(
        self, auth_client, db_session, current_user
    ):
        add_email(
            db_session,
            current_user,
            status=RecruiterEmailStatus.CLASSIFIED,
            kind=RecruiterEmailKind.JOB_ALERT,
            message_id="m1",
        )

        totals = auth_client.get(STATS).json()["totals"]

        assert totals["detected"] == 1
        assert totals["classified_recruiter"] == 0

    def test_escalations_are_counted(self, auth_client, db_session, current_user):
        row = add_email(
            db_session, current_user, status=RecruiterEmailStatus.REPLIED, message_id="m1"
        )
        row.escalated = True
        db_session.commit()

        assert auth_client.get(STATS).json()["totals"]["escalated"] == 1


class TestSkipReasons:
    def test_reasons_are_ranked_and_labelled(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        add_run(
            db_session,
            current_user,
            connected_gmail,
            skipped_known=90,
            skipped_own_thread=5,
            skipped_opt_out=1,
        )

        skipped = auth_client.get(STATS).json()["skipped"]

        assert [s["reason"] for s in skipped] == [
            "skipped_known",
            "skipped_own_thread",
            "skipped_opt_out",
        ]
        # Plain English — the raw counter names are for the log, not for a person.
        assert skipped[0]["label"] == "Already checked"
        assert skipped[0]["count"] == 90

    def test_reasons_that_never_fired_are_left_out(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        add_run(db_session, current_user, connected_gmail, skipped_known=3)

        skipped = auth_client.get(STATS).json()["skipped"]

        assert len(skipped) == 1


class TestTrend:
    def test_one_point_per_day(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        now = datetime.now(UTC)
        add_run(
            db_session,
            current_user,
            connected_gmail,
            listed=10,
            when=now - timedelta(days=2),
        )
        add_run(db_session, current_user, connected_gmail, listed=20, when=now)

        trend = auth_client.get(STATS, params={"bucket": "day"}).json()["trend"]

        assert len(trend) == 2
        # Oldest first.
        assert trend[0]["scanned"] == 10
        assert trend[1]["scanned"] == 20

    def test_weekly_buckets_collapse_the_days(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        now = datetime.now(UTC)
        for offset in (0, 1, 2):
            add_run(
                db_session,
                current_user,
                connected_gmail,
                listed=10,
                when=now - timedelta(days=offset),
            )

        trend = auth_client.get(STATS, params={"bucket": "week"}).json()["trend"]

        assert len(trend) <= 2
        assert sum(p["scanned"] for p in trend) == 30

    def test_detections_are_bucketed_on_their_own_timestamp(
        self, auth_client, db_session, current_user
    ):
        """A week-old message noticed today sorts as a week old.

        The rule `received_at` follows everywhere else in this feature.
        """
        add_email(
            db_session,
            current_user,
            status=RecruiterEmailStatus.DRAFTED,
            when=datetime.now(UTC) - timedelta(days=3),
            message_id="m1",
        )

        trend = auth_client.get(STATS).json()["trend"]

        assert len(trend) == 1
        assert trend[0]["detected"] == 1

    def test_a_day_of_scans_with_no_detections_still_shows(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        """400 scans and nothing found is a real and useful shape."""
        add_run(db_session, current_user, connected_gmail, listed=400)

        trend = auth_client.get(STATS).json()["trend"]

        assert trend[0]["scanned"] == 400
        assert trend[0]["detected"] == 0

    def test_every_point_carries_a_label(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        add_run(db_session, current_user, connected_gmail, listed=1)

        trend = auth_client.get(STATS).json()["trend"]

        assert trend[0]["label"]


class TestWindowAndScoping:
    def test_the_window_excludes_older_rows(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        add_run(
            db_session,
            current_user,
            connected_gmail,
            listed=99,
            when=datetime.now(UTC) - timedelta(days=60),
        )

        assert auth_client.get(STATS, params={"days": 7}).json()["totals"]["scans"] == 0
        assert auth_client.get(STATS, params={"days": 90}).json()["totals"]["scans"] == 1

    @pytest.mark.parametrize("days", [0, -1, 900])
    def test_out_of_range_windows_are_rejected(self, auth_client, days):
        assert auth_client.get(STATS, params={"days": days}).status_code == 422

    def test_an_unknown_bucket_is_rejected(self, auth_client):
        assert auth_client.get(STATS, params={"bucket": "fortnight"}).status_code == 422

    def test_another_users_scans_are_invisible(
        self, auth_client, db_session, current_user
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x", full_name="O")
        db_session.add(other)
        db_session.commit()
        add_run(db_session, other, None, listed=500)
        add_email(
            db_session, other, status=RecruiterEmailStatus.REPLIED, message_id="m-theirs"
        )

        totals = auth_client.get(STATS).json()["totals"]

        assert totals["scans"] == 0
        assert totals["detected"] == 0

    def test_stats_require_authentication(self, client):
        assert client.get(STATS).status_code == 401


class TestPushState:
    def test_it_reports_how_scans_were_triggered(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        add_run(db_session, current_user, connected_gmail, trigger=TRIGGER_PUSH)
        add_run(db_session, current_user, connected_gmail, trigger=TRIGGER_PUSH)
        add_run(db_session, current_user, connected_gmail, trigger=TRIGGER_BEAT)
        add_run(db_session, current_user, connected_gmail, trigger=TRIGGER_MANUAL)

        push = auth_client.get(STATS).json()["push"]

        assert push["scans_from_push"] == 2
        assert push["scans_from_beat"] == 1
        assert push["scans_manual"] == 1

    def test_registered_and_delivering_are_separate_claims(
        self, auth_client, db_session, connected_gmail
    ):
        """A watch can be healthy and still not be covering the mailbox."""
        from app.models.gmail_watch import GmailWatch

        db_session.add(
            GmailWatch(
                gmail_account_id=connected_gmail.id,
                status="active",
                expires_at=datetime.now(UTC) + timedelta(days=5),
                last_notified_at=datetime.now(UTC) - timedelta(hours=3),
                last_renewed_at=datetime.now(UTC) - timedelta(hours=3),
            )
        )
        db_session.commit()

        push = auth_client.get(STATS).json()["push"]

        assert push["healthy"] is True
        assert push["covering"] is False

    def test_no_mailbox_at_all_reports_quietly(self, auth_client):
        push = auth_client.get(STATS).json()["push"]

        assert push["healthy"] is False
        assert push["notifications"] == 0


# --------------------------------------------------------------------------- #
# The migration and the models must not drift                                  #
# --------------------------------------------------------------------------- #


class TestTheMigrationMatchesTheModels:
    """Cheap insurance against a column that exists in one place only.

    The test suite builds its schema with ``create_all``, so a column present on
    the model and missing from the migration passes every other test in this
    repo and then fails on a real deployment. This reads the migration source and
    checks the two agree.
    """

    MIGRATION = "alembic/versions/f8d1c4b62a05_recruiter_email_improvements_v2.py"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.MIGRATION).read_text()

    @pytest.mark.parametrize(
        "column",
        [
            "rfc_message_id",
            "rfc_references",
            "confidence_adjustment",
            "escalated",
            "escalation_reason",
            "follow_up_count",
            "last_follow_up_at",
            "previous_recruiter_email_id",
            "selected_resume_id",
            "resume_choice_reason",
        ],
    )
    def test_every_new_recruiter_email_column_is_in_the_migration(self, column):
        assert hasattr(RecruiterEmail, column)
        assert f'"{column}"' in self._source()

    @pytest.mark.parametrize("column", ["in_reply_to", "email_references"])
    def test_every_new_email_column_is_in_the_migration(self, column):
        from app.models.email import Email

        assert hasattr(Email, column)
        assert f'"{column}"' in self._source()

    @pytest.mark.parametrize(
        "table", ["reply_feedback", "classifier_priors", "recruiter_scan_runs"]
    )
    def test_every_new_table_is_created(self, table):
        assert f'create_table(\n        "{table}"' in self._source()

    def test_the_migration_hangs_off_the_previous_head(self):
        source = self._source()

        assert 'revision: str = "f8d1c4b62a05"' in source
        assert 'down_revision: str | None = "d2b6e8f04a71"' in source

    def test_references_is_not_used_as_a_column_name(self):
        """A reserved word in both SQLite and Postgres — quoted forever, or renamed."""
        assert '"references"' not in self._source()
