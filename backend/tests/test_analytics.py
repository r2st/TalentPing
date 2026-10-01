"""Application analytics: what is actually producing replies.

The dashboard reports *state*; this reports *effect*. The two read the same
applications, so the tests here mostly guard the things that make the numbers
trustworthy rather than merely present: cohorting by send date, refusing to
crown a resume off one lucky reply, and keeping thin samples out of the top of
a ranked list.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.recruiter import Recruiter
from app.models.resume import Resume
from app.routers.analytics import MIN_SAMPLE

OVERVIEW = "/api/v1/analytics/overview"

# Recruiter email is unique per user, and several tests point a handful of
# applications at the same company — so the address is counter-derived rather
# than company-derived.
_seq = itertools.count(1)


def _make_application(
    db,
    user,
    campaign,
    *,
    company: str,
    status: ApplicationStatus,
    created_at: datetime | None = None,
    industry: str | None = None,
    sent_at: datetime | None = None,
    replied_at: datetime | None = None,
) -> Application:
    """One application, optionally with a sent/received pair on its thread."""
    recruiter = Recruiter(
        user_id=user.id,
        email=f"talent{next(_seq)}@{company.replace(' ', '').lower()}.example",
        name=f"Recruiter at {company}",
        company=company,
        industry=industry,
    )
    db.add(recruiter)
    db.flush()

    application = Application(
        user_id=user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        status=status,
    )
    if created_at is not None:
        application.created_at = created_at
    db.add(application)
    db.flush()

    if sent_at is not None:
        thread = EmailThread(application_id=application.id, subject=f"Hello {company}")
        db.add(thread)
        db.flush()
        db.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.SENT,
                status=EmailStatus.SENT,
                to_address=recruiter.email,
                subject=thread.subject,
                body_text="Initial outreach.",
                sent_at=sent_at,
            )
        )
        if replied_at is not None:
            db.add(
                Email(
                    thread_id=thread.id,
                    direction=EmailDirection.RECEIVED,
                    status=EmailStatus.RECEIVED,
                    from_address=recruiter.email,
                    subject="Re: Hello",
                    body_text="Let's talk.",
                    sent_at=replied_at,
                )
            )
    return application


@pytest.fixture()
def campaign(db_session, current_user, resume) -> Campaign:
    row = Campaign(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend outreach",
        target_roles=["Senior Backend Engineer"],
        target_industries=["fintech"],
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture()
def history(db_session, current_user, campaign):
    """Six applications: two replied, one of those reached interview."""
    now = datetime.now(UTC)
    spread = [
        ("Northwind Labs", ApplicationStatus.OUTREACH_SENT),
        ("Globex", ApplicationStatus.NO_RESPONSE),
        ("Initech", ApplicationStatus.FOLLOW_UP),
        ("Umbrella", ApplicationStatus.NO_RESPONSE),
        ("Acme", ApplicationStatus.REPLIED),
        ("Stark", ApplicationStatus.INTERVIEW_SCHEDULED),
    ]
    for company, status in spread:
        _make_application(
            db_session,
            current_user,
            campaign,
            company=company,
            status=status,
            created_at=now - timedelta(days=3),
            industry="fintech",
            sent_at=now - timedelta(days=3),
            replied_at=(
                now - timedelta(days=1)
                if status
                in {ApplicationStatus.REPLIED, ApplicationStatus.INTERVIEW_SCHEDULED}
                else None
            ),
        )
    db_session.commit()


class TestHeadlineNumbers:
    def test_counts_responses_and_interviews(self, auth_client, history):
        body = auth_client.get(OVERVIEW).json()

        assert body["total_applications"] == 6
        assert body["responses"] == 2       # REPLIED + INTERVIEW_SCHEDULED
        assert body["interviews"] == 1      # only INTERVIEW_SCHEDULED
        assert body["response_rate"] == pytest.approx(0.333, abs=0.001)
        assert body["interview_rate"] == pytest.approx(0.167, abs=0.001)

    def test_reports_median_days_to_reply(self, auth_client, history):
        body = auth_client.get(OVERVIEW).json()
        assert body["median_days_to_reply"] == pytest.approx(2.0, abs=0.2)

    def test_an_empty_account_is_zeroes_not_an_error(self, auth_client, resume):
        body = auth_client.get(OVERVIEW).json()

        assert body["total_applications"] == 0
        assert body["response_rate"] == 0.0
        assert body["median_days_to_reply"] is None
        assert body["trend"] == []
        assert body["resumes"] == []

    def test_reports_the_sample_floor_it_ranks_by(self, auth_client, history):
        assert auth_client.get(OVERVIEW).json()["min_sample"] == MIN_SAMPLE


class TestTrend:
    def test_cohorts_by_when_the_application_was_sent(
        self, auth_client, db_session, current_user, campaign
    ):
        """A reply lands in the cohort of the week it was *sent*, not received.

        Bucketing on reply date makes a quiet week look like a collapse in
        performance when nothing was sent that week at all.
        """
        now = datetime.now(UTC)
        _make_application(
            db_session,
            current_user,
            campaign,
            company="Old Corp",
            status=ApplicationStatus.REPLIED,
            created_at=now - timedelta(days=40),
            sent_at=now - timedelta(days=40),
            replied_at=now - timedelta(days=1),  # replied recently
        )
        db_session.commit()

        trend = auth_client.get(OVERVIEW, params={"bucket": "month"}).json()["trend"]
        cohort = next(p for p in trend if p["applications"])
        # The single application sits in the old cohort, carrying its response.
        assert cohort["responses"] == 1
        assert cohort["response_rate"] == 1.0

    def test_is_ordered_oldest_first(
        self, auth_client, db_session, current_user, campaign
    ):
        now = datetime.now(UTC)
        for weeks in (6, 2, 0):
            _make_application(
                db_session,
                current_user,
                campaign,
                company=f"Corp {weeks}",
                status=ApplicationStatus.OUTREACH_SENT,
                created_at=now - timedelta(weeks=weeks),
            )
        db_session.commit()

        trend = auth_client.get(OVERVIEW).json()["trend"]
        periods = [point["period"] for point in trend]
        assert periods == sorted(periods)

    def test_month_buckets_start_on_the_first(self, auth_client, history):
        trend = auth_client.get(OVERVIEW, params={"bucket": "month"}).json()["trend"]
        assert all(point["period"].endswith("-01") for point in trend)

    def test_rejects_an_unknown_bucket(self, auth_client, history):
        assert auth_client.get(OVERVIEW, params={"bucket": "day"}).status_code == 422


class TestStatusBreakdown:
    def test_only_lists_statuses_that_occur(self, auth_client, history):
        slices = auth_client.get(OVERVIEW).json()["by_status"]
        statuses = {s["status"] for s in slices}

        assert "NO_RESPONSE" in statuses
        assert "OFFER" not in statuses   # nothing reached it
        assert all(s["count"] > 0 for s in slices)

    def test_shares_sum_to_one(self, auth_client, history):
        slices = auth_client.get(OVERVIEW).json()["by_status"]
        assert sum(s["share"] for s in slices) == pytest.approx(1.0, abs=0.01)

    def test_is_in_funnel_order_not_alphabetical(self, auth_client, history):
        labels = [s["label"] for s in auth_client.get(OVERVIEW).json()["by_status"]]
        assert labels.index("Sent") < labels.index("Replied")


class TestResumePerformance:
    def test_attributes_applications_to_the_campaign_resume(
        self, auth_client, db_session, current_user, resume, campaign
    ):
        other = Resume(
            user_id=current_user.id,
            filename="v2.pdf",
            raw_text="Second version.",
            is_default=False,
            parsed_with="heuristic",
        )
        db_session.add(other)
        db_session.flush()
        second = Campaign(user_id=current_user.id, resume_id=other.id, name="V2 run")
        db_session.add(second)
        db_session.flush()

        _make_application(
            db_session,
            current_user,
            campaign,
            company="Alpha",
            status=ApplicationStatus.REPLIED,
        )
        _make_application(
            db_session,
            current_user,
            second,
            company="Beta",
            status=ApplicationStatus.NO_RESPONSE,
        )
        db_session.commit()

        rows = auth_client.get(OVERVIEW).json()["resumes"]
        by_id = {row["resume_id"]: row for row in rows}
        assert by_id[resume.id]["applications"] == 1
        assert by_id[resume.id]["responses"] == 1
        assert by_id[other.id]["responses"] == 0

    def test_crowns_a_best_resume_once_the_sample_is_real(
        self, auth_client, db_session, current_user, resume, campaign
    ):
        for index in range(MIN_SAMPLE):
            _make_application(
                db_session,
                current_user,
                campaign,
                company=f"Corp {index}",
                status=ApplicationStatus.REPLIED,
            )
        db_session.commit()

        rows = auth_client.get(OVERVIEW).json()["resumes"]
        best = [row for row in rows if row["is_best"]]
        assert len(best) == 1
        assert best[0]["resume_id"] == resume.id

    def test_will_not_crown_one_lucky_reply(
        self, auth_client, db_session, current_user, campaign
    ):
        """Below the sample floor nothing is 'best' — a 100% rate off one send
        is the most misleading number the page could show."""
        _make_application(
            db_session,
            current_user,
            campaign,
            company="Fluke Inc",
            status=ApplicationStatus.REPLIED,
        )
        db_session.commit()

        rows = auth_client.get(OVERVIEW).json()["resumes"]
        assert rows[0]["response_rate"] == 1.0
        assert not any(row["is_best"] for row in rows)

    def test_applications_with_no_resume_are_labelled_not_dropped(
        self, auth_client, db_session, current_user
    ):
        bare = Campaign(user_id=current_user.id, resume_id=None, name="No resume")
        db_session.add(bare)
        db_session.flush()
        _make_application(
            db_session,
            current_user,
            bare,
            company="Nowhere",
            status=ApplicationStatus.OUTREACH_SENT,
        )
        db_session.commit()

        rows = auth_client.get(OVERVIEW).json()["resumes"]
        assert rows[0]["resume_id"] is None
        assert rows[0]["label"] == "No resume attached"


class TestSegmentRanking:
    def test_ranks_volume_backed_rows_above_thin_ones(
        self, auth_client, db_session, current_user, campaign
    ):
        """A company with 4 applications at 50% must outrank one at 1-for-1.

        Sorting on rate alone is exactly backwards as advice: it puts the row
        the user can learn nothing from at the top of the list.
        """
        for index in range(4):
            _make_application(
                db_session,
                current_user,
                campaign,
                company="Volume Corp",
                status=(
                    ApplicationStatus.REPLIED if index < 2 else ApplicationStatus.NO_RESPONSE
                ),
            )
        _make_application(
            db_session,
            current_user,
            campaign,
            company="Fluke Inc",
            status=ApplicationStatus.REPLIED,
        )
        db_session.commit()

        companies = auth_client.get(OVERVIEW).json()["companies"]
        assert companies[0]["name"] == "Volume Corp"
        assert companies[0]["response_rate"] == 0.5
        # The thin row is still reported — flagged by the UI, not hidden.
        assert companies[1]["name"] == "Fluke Inc"

    def test_industries_fall_back_to_the_campaign_target(
        self, auth_client, db_session, current_user, campaign
    ):
        """Contacts scraped without an industry shouldn't empty the list."""
        _make_application(
            db_session,
            current_user,
            campaign,
            company="Unlabelled Corp",
            status=ApplicationStatus.REPLIED,
            industry=None,
        )
        db_session.commit()

        industries = auth_client.get(OVERVIEW).json()["industries"]
        assert [row["name"] for row in industries] == ["fintech"]

    def test_the_recruiters_own_industry_wins_over_the_campaigns(
        self, auth_client, db_session, current_user, campaign
    ):
        _make_application(
            db_session,
            current_user,
            campaign,
            company="Health Corp",
            status=ApplicationStatus.REPLIED,
            industry="healthcare",
        )
        db_session.commit()

        names = {row["name"] for row in auth_client.get(OVERVIEW).json()["industries"]}
        assert names == {"healthcare"}

    def test_honours_the_row_limit(self, auth_client, db_session, current_user, campaign):
        for index in range(12):
            _make_application(
                db_session,
                current_user,
                campaign,
                company=f"Corp {index}",
                status=ApplicationStatus.OUTREACH_SENT,
            )
        db_session.commit()

        assert len(auth_client.get(OVERVIEW, params={"limit": 5}).json()["companies"]) == 5


class TestFilters:
    def test_days_narrows_the_window(
        self, auth_client, db_session, current_user, campaign
    ):
        now = datetime.now(UTC)
        _make_application(
            db_session,
            current_user,
            campaign,
            company="Recent Corp",
            status=ApplicationStatus.OUTREACH_SENT,
            created_at=now - timedelta(days=2),
        )
        _make_application(
            db_session,
            current_user,
            campaign,
            company="Ancient Corp",
            status=ApplicationStatus.OUTREACH_SENT,
            created_at=now - timedelta(days=200),
        )
        db_session.commit()

        body = auth_client.get(OVERVIEW, params={"days": 30}).json()
        assert body["total_applications"] == 1
        assert [row["name"] for row in body["companies"]] == ["Recent Corp"]

    def test_rejects_an_absurd_window(self, auth_client, history):
        assert auth_client.get(OVERVIEW, params={"days": 5000}).status_code == 422


class TestIsolation:
    def test_another_users_applications_are_invisible(
        self, auth_client, db_session, history
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        their_campaign = Campaign(user_id=other.id, name="Theirs")
        db_session.add(their_campaign)
        db_session.flush()
        _make_application(
            db_session,
            other,
            their_campaign,
            company="Secret Corp",
            status=ApplicationStatus.OFFER,
        )
        db_session.commit()

        body = auth_client.get(OVERVIEW).json()
        assert body["total_applications"] == 6
        assert "Secret Corp" not in {row["name"] for row in body["companies"]}

    def test_requires_authentication(self, client):
        assert client.get(OVERVIEW).status_code == 401
