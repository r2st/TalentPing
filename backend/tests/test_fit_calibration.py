"""Fit-score calibration: does a higher score actually reply more often?

The report grades the product's own scorer, so the tests are mostly about it
refusing to flatter itself — staying silent on a thin sample, saying "flat" out
loud when the bands don't separate, and never recommending a threshold it
cannot back with sends.
"""
from __future__ import annotations

import itertools

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.autopilot import AutopilotPreference
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.fit_score import FitScore
from app.models.job import JobPosting, job_fingerprint
from app.models.recruiter import Recruiter
from app.services import fit_calibration

CALIBRATION = "/api/v1/analytics/calibration"

_seq = itertools.count(1)


@pytest.fixture()
def campaign(db_session, current_user, resume) -> Campaign:
    row = Campaign(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend outreach",
    )
    db_session.add(row)
    db_session.commit()
    return row


def _send(
    db,
    user,
    campaign,
    resume,
    *,
    score: float | None,
    replied: bool = False,
    interviewed: bool = False,
    sent: bool = True,
    cached_score_only: bool = False,
) -> Application:
    """One application that went out at *score*, and how it ended up.

    ``score=None`` models company-targeted outreach with no posting behind it —
    the case the report has to count as unscored rather than quietly drop.
    """
    n = next(_seq)
    recruiter = Recruiter(
        user_id=user.id,
        email=f"talent{n}@corp{n}.example",
        name="Alex Recruiter",
        company=f"Corp {n}",
    )
    db.add(recruiter)
    db.flush()

    posting = None
    if score is not None:
        posting = JobPosting(
            user_id=user.id,
            title=f"Backend Engineer {n}",
            company=f"Corp {n}",
            fingerprint=job_fingerprint(f"Backend Engineer {n}", f"Corp {n}", None),
            fit_score=score,
        )
        db.add(posting)
        db.flush()
        if not cached_score_only:
            db.add(
                FitScore(
                    user_id=user.id,
                    resume_id=resume.id,
                    job_posting_id=posting.id,
                    jd_hash=f"hash-{n}",
                    overall=score,
                )
            )

    if interviewed:
        status = ApplicationStatus.INTERVIEW_SCHEDULED
    elif replied:
        status = ApplicationStatus.REPLIED
    elif sent:
        status = ApplicationStatus.NO_RESPONSE
    else:
        status = ApplicationStatus.QUEUED

    application = Application(
        user_id=user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        job_posting_id=posting.id if posting else None,
        status=status,
    )
    db.add(application)
    db.flush()

    thread = EmailThread(application_id=application.id, subject="Hello")
    db.add(thread)
    db.flush()
    db.add(
        Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.SENT if sent else EmailStatus.DRAFT,
            to_address=recruiter.email,
            subject="Hello",
            body_text="Initial outreach.",
        )
    )
    return application


def _separating(db, user, campaign, resume) -> None:
    """Thirty sends where the score plainly works: 90s reply, 50s don't."""
    for index in range(15):
        _send(db, user, campaign, resume, score=92, replied=index < 9)
    for index in range(15):
        _send(db, user, campaign, resume, score=54, replied=index < 1)
    db.commit()


def _flat(db, user, campaign, resume) -> None:
    """Thirty sends where the score does nothing: both bands reply 1 in 3."""
    for index in range(15):
        _send(db, user, campaign, resume, score=92, replied=index < 5)
    for index in range(15):
        _send(db, user, campaign, resume, score=54, replied=index < 5)
    db.commit()


class TestBands:
    def test_buckets_each_send_into_its_score_band(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        _send(db_session, current_user, campaign, resume, score=95, replied=True)
        _send(db_session, current_user, campaign, resume, score=72)
        _send(db_session, current_user, campaign, resume, score=41)
        db_session.commit()

        bands = {b["label"]: b for b in auth_client.get(CALIBRATION).json()["bands"]}
        assert bands["90–100"]["sends"] == 1
        assert bands["90–100"]["responses"] == 1
        assert bands["70–79"]["sends"] == 1
        assert bands["Below 50"]["sends"] == 1
        assert bands["50–59"]["sends"] == 0

    def test_every_band_is_returned_even_when_empty(self, auth_client, resume):
        labels = [b["label"] for b in auth_client.get(CALIBRATION).json()["bands"]]
        assert labels == [label for _, _, label in fit_calibration.BANDS]

    def test_flags_thin_bands_as_unreliable_without_hiding_them(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        _send(db_session, current_user, campaign, resume, score=95, replied=True)
        db_session.commit()

        band = next(
            b for b in auth_client.get(CALIBRATION).json()["bands"] if b["sends"]
        )
        assert band["response_rate"] == 1.0
        assert band["reliable"] is False

    def test_counts_interviews_separately_from_replies(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        _send(db_session, current_user, campaign, resume, score=95, interviewed=True)
        _send(db_session, current_user, campaign, resume, score=95, replied=True)
        db_session.commit()

        band = next(
            b for b in auth_client.get(CALIBRATION).json()["bands"] if b["sends"]
        )
        assert band["responses"] == 2
        assert band["interviews"] == 1


class TestWhatCounts:
    def test_a_queued_application_is_not_a_send(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        """Nothing left the outbox, so there is no outcome to attribute."""
        _send(db_session, current_user, campaign, resume, score=95, sent=False)
        db_session.commit()

        body = auth_client.get(CALIBRATION).json()
        assert body["scored_sends"] == 0
        assert body["unscored_sends"] == 0

    def test_outreach_with_no_posting_is_reported_not_dropped(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        _send(db_session, current_user, campaign, resume, score=None)
        _send(db_session, current_user, campaign, resume, score=88)
        db_session.commit()

        body = auth_client.get(CALIBRATION).json()
        assert body["scored_sends"] == 1
        assert body["unscored_sends"] == 1

    def test_falls_back_to_the_cached_posting_score(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        """Postings scored before the breakdown was persisted still count."""
        _send(
            db_session,
            current_user,
            campaign,
            resume,
            score=88,
            cached_score_only=True,
        )
        db_session.commit()

        body = auth_client.get(CALIBRATION).json()
        assert body["scored_sends"] == 1
        assert body["unscored_sends"] == 0

    def test_prefers_the_highest_score_when_two_profiles_judged_a_posting(
        self, db_session, current_user, campaign, resume
    ):
        """The score that cleared the gate is the one the send happened on."""
        application = _send(db_session, current_user, campaign, resume, score=55)
        db_session.add(
            FitScore(
                user_id=current_user.id,
                resume_id=resume.id,
                job_posting_id=application.job_posting_id,
                jd_hash="second-profile",
                overall=91.0,
            )
        )
        db_session.commit()

        result = fit_calibration.report(db_session, current_user.id)
        assert next(b for b in result.bands if b.sends).label == "90–100"


class TestVerdict:
    def test_stays_quiet_below_the_sample_floor(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        for _ in range(5):
            _send(db_session, current_user, campaign, resume, score=95, replied=True)
        db_session.commit()

        body = auth_client.get(CALIBRATION).json()
        assert body["verdict"] == "insufficient_data"
        assert body["suggested_min_fit_score"] is None
        assert str(fit_calibration.MIN_TOTAL_SENDS) in body["summary"]

    def test_says_separating_when_the_score_predicts_replies(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        _separating(db_session, current_user, campaign, resume)

        body = auth_client.get(CALIBRATION).json()
        assert body["verdict"] == "separating"
        assert body["correlation"] > fit_calibration.FLAT_CORRELATION

    def test_says_flat_out_loud_when_the_bands_do_not_separate(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        """The report's most valuable output is bad news about the scorer."""
        _flat(db_session, current_user, campaign, resume)

        body = auth_client.get(CALIBRATION).json()
        assert body["verdict"] == "flat"
        assert body["suggested_min_fit_score"] is None
        assert "does not predict" in body["summary"]

    def test_says_inverted_when_low_scores_reply_more(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        for index in range(15):
            _send(db_session, current_user, campaign, resume, score=92, replied=index < 1)
        for index in range(15):
            _send(db_session, current_user, campaign, resume, score=54, replied=index < 9)
        db_session.commit()

        body = auth_client.get(CALIBRATION).json()
        assert body["verdict"] == "inverted"
        assert body["correlation"] < 0

    def test_no_correlation_when_every_send_scored_the_same(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        for index in range(25):
            _send(db_session, current_user, campaign, resume, score=80, replied=index < 8)
        db_session.commit()

        body = auth_client.get(CALIBRATION).json()
        assert body["correlation"] is None
        assert body["verdict"] == "insufficient_data"
        assert "no variation" in body["summary"]


class TestSuggestedThreshold:
    def test_recommends_the_lowest_threshold_that_beats_the_baseline(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        """Every point above the lowest working line is volume given up free."""
        _separating(db_session, current_user, campaign, resume)

        body = auth_client.get(CALIBRATION).json()
        # The 90s reply at 60% against a 33% average; 50+ sweeps in the whole
        # account and cannot beat it. 60 is the first floor that excludes the
        # weak band entirely, so it is the least restrictive line that works.
        assert body["suggested_min_fit_score"] == 60
        assert "earns its keep" in body["summary"]

    def test_never_recommends_a_threshold_it_cannot_back_with_sends(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        """Twenty low sends plus a handful of perfect ones is not evidence."""
        for index in range(22):
            _send(db_session, current_user, campaign, resume, score=55, replied=index < 4)
        for _ in range(2):
            _send(db_session, current_user, campaign, resume, score=97, replied=True)
        db_session.commit()

        body = auth_client.get(CALIBRATION).json()
        assert body["suggested_min_fit_score"] is None

    def test_reports_the_gate_the_user_is_actually_using(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        db_session.add(
            AutopilotPreference(user_id=current_user.id, min_fit_score=65)
        )
        db_session.commit()

        assert auth_client.get(CALIBRATION).json()["current_min_fit_score"] == 65


class TestFiltersAndIsolation:
    def test_days_narrows_the_window(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        from datetime import UTC, datetime, timedelta

        old = _send(db_session, current_user, campaign, resume, score=95, replied=True)
        old.created_at = datetime.now(UTC) - timedelta(days=200)
        _send(db_session, current_user, campaign, resume, score=95)
        db_session.commit()

        assert auth_client.get(CALIBRATION, params={"days": 30}).json()["scored_sends"] == 1

    def test_another_users_sends_are_invisible(
        self, auth_client, db_session, current_user, campaign, resume
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        their_campaign = Campaign(user_id=other.id, name="Theirs")
        db_session.add(their_campaign)
        db_session.flush()
        _send(db_session, other, their_campaign, resume, score=99, replied=True)
        db_session.commit()

        assert auth_client.get(CALIBRATION).json()["scored_sends"] == 0

    def test_an_empty_account_is_a_report_not_an_error(self, auth_client, resume):
        body = auth_client.get(CALIBRATION).json()
        assert body["scored_sends"] == 0
        assert body["verdict"] == "insufficient_data"
        assert body["response_rate"] == 0.0

    def test_requires_authentication(self, client):
        assert client.get(CALIBRATION).status_code == 401
