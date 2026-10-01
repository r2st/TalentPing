"""The tracking dashboard: funnel shape, stats, activity feed, and filters."""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.follow_up import FollowUp, FollowUpStatus, FollowUpTemplate
from app.models.recruiter import Recruiter
from app.routers.dashboard import CSV_COLUMNS, STAGES


@pytest.fixture()
def pipeline(db_session, current_user, resume):
    """Four applications spread across the funnel, with a reply on one."""
    campaign = Campaign(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend outreach",
        target_companies=["Northwind", "Globex", "Initech", "Umbrella"],
        target_roles=["Senior Backend Engineer"],
    )
    db_session.add(campaign)
    db_session.flush()

    spread = [
        ("Northwind Labs", ApplicationStatus.OUTREACH_SENT),
        ("Globex", ApplicationStatus.FOLLOW_UP),
        ("Initech", ApplicationStatus.INTERESTED),
        ("Umbrella", ApplicationStatus.INTERVIEW_SCHEDULED),
    ]
    applications = []
    now = datetime.now(UTC)

    for index, (company, status) in enumerate(spread):
        recruiter = Recruiter(
            user_id=current_user.id,
            email=f"talent@{company.split()[0].lower()}.example",
            name=f"Recruiter {index}",
            company=company,
        )
        db_session.add(recruiter)
        db_session.flush()

        application = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=status,
        )
        db_session.add(application)
        db_session.flush()

        thread = EmailThread(application_id=application.id, subject=f"Hello {company}")
        db_session.add(thread)
        db_session.flush()
        db_session.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.SENT,
                status=EmailStatus.SENT,
                to_address=recruiter.email,
                subject=thread.subject,
                body_text="Initial outreach.",
                sent_at=now - timedelta(days=6),
            )
        )
        # The Initech thread got a reply two days after the outreach.
        if status == ApplicationStatus.INTERESTED:
            db_session.add(
                Email(
                    thread_id=thread.id,
                    direction=EmailDirection.RECEIVED,
                    status=EmailStatus.RECEIVED,
                    from_address=recruiter.email,
                    subject="Re: Hello",
                    body_text="Sounds interesting, let's talk.",
                    sent_at=now - timedelta(days=4),
                )
            )
        applications.append(application)

    # One follow-up already sent, one still pending.
    db_session.add_all(
        [
            FollowUp(
                application_id=applications[1].id,
                step=1,
                scheduled_at=now - timedelta(days=2),
                sent_at=now - timedelta(days=2),
                status=FollowUpStatus.SENT,
                template=FollowUpTemplate.NO_RESPONSE,
            ),
            FollowUp(
                application_id=applications[0].id,
                step=1,
                scheduled_at=now + timedelta(days=3),
                status=FollowUpStatus.SCHEDULED,
            ),
        ]
    )
    db_session.commit()
    return {"campaign": campaign, "applications": applications}


class TestDashboardStats:
    def test_counts_the_pipeline(self, auth_client, pipeline):
        stats = auth_client.get("/api/v1/dashboard").json()["stats"]

        assert stats["total_applications"] == 4
        assert stats["responded"] == 2      # INTERESTED + INTERVIEW_SCHEDULED
        assert stats["interviews"] == 1
        assert stats["response_rate"] == 0.5
        assert stats["interview_rate"] == 0.25

    def test_counts_follow_up_automation(self, auth_client, pipeline):
        stats = auth_client.get("/api/v1/dashboard").json()["stats"]
        assert stats["follow_ups_sent"] == 1
        assert stats["follow_ups_scheduled"] == 1

    def test_reports_median_days_to_reply(self, auth_client, pipeline):
        stats = auth_client.get("/api/v1/dashboard").json()["stats"]
        assert stats["median_days_to_reply"] == pytest.approx(2.0, abs=0.2)

    def test_empty_account_reports_zeroes_not_errors(self, auth_client, resume):
        body = auth_client.get("/api/v1/dashboard").json()

        assert body["stats"]["total_applications"] == 0
        assert body["stats"]["response_rate"] == 0.0
        assert body["stats"]["median_days_to_reply"] is None
        assert body["applications"] == []
        assert body["activity"] == []

    def test_includes_smart_apply_counters(self, auth_client, resume, job_description):
        auth_client.post("/api/v1/tailor", json={"job_description": job_description})

        stats = auth_client.get("/api/v1/dashboard").json()["stats"]
        assert stats["tailored_resumes"] == 1
        assert stats["average_fit_score"] is not None


class TestPipelineFunnel:
    def test_has_every_stage_in_order(self, auth_client, pipeline):
        stages = auth_client.get("/api/v1/dashboard").json()["pipeline"]
        assert [s["key"] for s in stages] == [key for key, _, _ in STAGES]

    def test_is_cumulative_and_narrows(self, auth_client, pipeline):
        """A thread at 'interview' also passed 'applied' — the funnel must narrow."""
        counts = [s["count"] for s in auth_client.get("/api/v1/dashboard").json()["pipeline"]]

        assert counts[0] == 4  # everything entered
        assert counts == sorted(counts, reverse=True)
        assert counts[3] == 1  # only the interview thread reached that far

    def test_rates_are_shares_of_the_total(self, auth_client, pipeline):
        stages = auth_client.get("/api/v1/dashboard").json()["pipeline"]
        assert stages[0]["rate"] == 1.0
        assert stages[3]["rate"] == 0.25


class TestApplicationRows:
    def test_each_row_carries_its_context(self, auth_client, pipeline):
        rows = auth_client.get("/api/v1/dashboard").json()["applications"]
        assert len(rows) == 4

        row = next(r for r in rows if r["company"] == "Initech")
        assert row["role"] == "Senior Backend Engineer"
        assert row["status"] == "INTERESTED"
        assert row["stage"] == "responded"
        assert row["replied_at"] is not None
        assert row["message_count"] == 2

    def test_surfaces_the_next_scheduled_follow_up(self, auth_client, pipeline):
        rows = auth_client.get("/api/v1/dashboard").json()["applications"]
        row = next(r for r in rows if r["company"] == "Northwind Labs")

        assert row["follow_ups_scheduled"] == 1
        assert row["next_follow_up_at"] is not None


class TestPagination:
    def test_unset_limit_returns_every_row_and_no_more_flag(self, auth_client, pipeline):
        body = auth_client.get("/api/v1/dashboard").json()
        assert len(body["applications"]) == 4
        assert body["has_more"] is False

    def test_limit_pages_the_applications_only(self, auth_client, pipeline):
        body = auth_client.get("/api/v1/dashboard", params={"limit": 2}).json()

        assert len(body["applications"]) == 2
        assert body["has_more"] is True
        # Stats and the funnel still describe the whole filtered set.
        assert body["stats"]["total_applications"] == 4
        assert body["pipeline"][0]["count"] == 4

    def test_offset_advances_the_page(self, auth_client, pipeline):
        first_page = auth_client.get(
            "/api/v1/dashboard", params={"limit": 2, "offset": 0}
        ).json()["applications"]
        second_page = auth_client.get(
            "/api/v1/dashboard", params={"limit": 2, "offset": 2}
        ).json()["applications"]

        first_ids = {r["application_id"] for r in first_page}
        second_ids = {r["application_id"] for r in second_page}
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == {a["application_id"] for a in first_page + second_page}

    def test_last_page_reports_no_more(self, auth_client, pipeline):
        body = auth_client.get(
            "/api/v1/dashboard", params={"limit": 2, "offset": 2}
        ).json()
        assert len(body["applications"]) == 2
        assert body["has_more"] is False

    def test_offset_past_the_end_is_an_empty_page_not_an_error(self, auth_client, pipeline):
        body = auth_client.get(
            "/api/v1/dashboard", params={"limit": 2, "offset": 20}
        ).json()
        assert body["applications"] == []
        assert body["has_more"] is False


class TestActivityFeed:
    def test_is_newest_first(self, auth_client, pipeline):
        activity = auth_client.get("/api/v1/dashboard").json()["activity"]
        timestamps = [event["at"] for event in activity]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_covers_every_event_type(self, auth_client, pipeline):
        kinds = {e["kind"] for e in auth_client.get("/api/v1/dashboard").json()["activity"]}
        assert {"outreach_sent", "reply_received", "follow_up_sent"} <= kinds

    def test_replies_carry_a_snippet(self, auth_client, pipeline):
        activity = auth_client.get("/api/v1/dashboard").json()["activity"]
        reply = next(e for e in activity if e["kind"] == "reply_received")
        assert "Sounds interesting" in reply["summary"]

    def test_respects_the_limit(self, auth_client, pipeline):
        activity = auth_client.get(
            "/api/v1/dashboard", params={"activity_limit": 2}
        ).json()["activity"]
        assert len(activity) == 2


class TestFilters:
    def test_by_company(self, auth_client, pipeline):
        body = auth_client.get("/api/v1/dashboard", params={"company": "initech"}).json()

        assert len(body["applications"]) == 1
        assert body["applications"][0]["company"] == "Initech"
        # The funnel must describe the same slice as the table.
        assert body["stats"]["total_applications"] == 1

    def test_by_status(self, auth_client, pipeline):
        body = auth_client.get("/api/v1/dashboard", params={"status": "INTERESTED"}).json()
        assert len(body["applications"]) == 1
        assert body["applications"][0]["status"] == "INTERESTED"

    def test_by_campaign(self, auth_client, pipeline):
        body = auth_client.get(
            "/api/v1/dashboard", params={"campaign_id": pipeline["campaign"].id}
        ).json()
        assert len(body["applications"]) == 4

    def test_by_date_window(self, auth_client, pipeline):
        """Everything in the fixture was created just now, so a 1-day window keeps it."""
        body = auth_client.get("/api/v1/dashboard", params={"days": 1}).json()
        assert len(body["applications"]) == 4

    def test_an_unmatched_filter_returns_an_empty_slice(self, auth_client, pipeline):
        body = auth_client.get("/api/v1/dashboard", params={"company": "nonexistent"}).json()
        assert body["applications"] == []
        assert body["stats"]["total_applications"] == 0


class TestCsvExport:
    """The export is routed through the dashboard read rather than re-querying:
    a file that disagreed with the table it came from is worse than no file."""

    def test_has_a_header_and_a_row_per_application(self, auth_client, pipeline):
        text = auth_client.get("/api/v1/dashboard/export.csv").text
        rows = list(csv.reader(io.StringIO(text)))

        assert rows[0] == CSV_COLUMNS
        assert len(rows) == 5  # header + four applications

    def test_carries_each_rows_context(self, auth_client, pipeline):
        rows = list(csv.DictReader(io.StringIO(auth_client.get("/api/v1/dashboard/export.csv").text)))
        row = next(r for r in rows if r["company"] == "Initech")

        assert row["role"] == "Senior Backend Engineer"
        assert row["status"] == "INTERESTED"
        assert row["stage"] == "responded"
        assert row["campaign"] == "Backend outreach"
        assert row["messages"] == "2"
        assert row["replied_at"]

    def test_downloads_as_a_dated_file(self, auth_client, pipeline):
        response = auth_client.get("/api/v1/dashboard/export.csv")

        assert response.headers["content-type"].startswith("text/csv")
        disposition = response.headers["content-disposition"]
        assert "attachment" in disposition
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        assert f"talentping-pipeline-{stamp}.csv" in disposition

    def test_honours_the_same_filters_as_the_page(self, auth_client, pipeline):
        text = auth_client.get(
            "/api/v1/dashboard/export.csv", params={"company": "initech"}
        ).text
        rows = list(csv.DictReader(io.StringIO(text)))

        assert [r["company"] for r in rows] == ["Initech"]

    def test_matches_the_table_it_was_downloaded_from(self, auth_client, pipeline):
        params = {"status": "INTERESTED"}
        table = auth_client.get("/api/v1/dashboard", params=params).json()["applications"]
        exported = list(
            csv.DictReader(
                io.StringIO(auth_client.get("/api/v1/dashboard/export.csv", params=params).text)
            )
        )

        assert [str(r["application_id"]) for r in table] == [
            r["application_id"] for r in exported
        ]

    def test_blanks_render_as_empty_cells_not_none(
        self, auth_client, db_session, current_user, resume
    ):
        """A literal "None" in a spreadsheet cell is a bug the user has to fix."""
        campaign = Campaign(user_id=current_user.id, resume_id=resume.id, name="Sparse")
        recruiter = Recruiter(user_id=current_user.id, email="bare@example.com")
        db_session.add_all([campaign, recruiter])
        db_session.flush()
        db_session.add(
            Application(
                user_id=current_user.id,
                campaign_id=campaign.id,
                recruiter_id=recruiter.id,
            )
        )
        db_session.commit()

        rows = list(
            csv.DictReader(io.StringIO(auth_client.get("/api/v1/dashboard/export.csv").text))
        )
        assert rows[0]["next_follow_up_at"] == ""
        assert "None" not in ",".join(rows[0].values())

    def test_an_empty_pipeline_still_exports_a_header(self, auth_client, resume):
        rows = list(
            csv.reader(io.StringIO(auth_client.get("/api/v1/dashboard/export.csv").text))
        )
        assert rows == [CSV_COLUMNS]

    def test_requires_authentication(self, client):
        assert client.get("/api/v1/dashboard/export.csv").status_code == 401

    def test_filter_options_list_what_is_present(self, auth_client, pipeline):
        filters = auth_client.get("/api/v1/dashboard").json()["filters"]

        assert "Initech" in filters["companies"]
        assert "Senior Backend Engineer" in filters["roles"]
        assert filters["campaigns"][0]["name"] == "Backend outreach"
        assert "INTERESTED" in filters["statuses"]


class TestQueue:
    def test_reports_upcoming_follow_ups(self, auth_client, pipeline):
        body = auth_client.get("/api/v1/dashboard/queue").json()

        assert len(body["follow_ups"]) == 1
        assert body["follow_ups"][0]["step"] == 1
        assert body["follow_ups"][0]["scheduled_at"]

    def test_counts_pending_sends(self, auth_client, db_session, pipeline):
        thread_id = db_session.query(EmailThread).first().id
        db_session.add(
            Email(
                thread_id=thread_id,
                direction=EmailDirection.SENT,
                status=EmailStatus.QUEUED,
                to_address="x@example.com",
                subject="Queued",
            )
        )
        db_session.commit()

        assert auth_client.get("/api/v1/dashboard/queue").json()["pending_sends"] == 1


class TestIsolation:
    def test_another_users_pipeline_is_invisible(self, auth_client, db_session, pipeline):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()

        campaign = Campaign(user_id=other.id, name="Their campaign")
        recruiter = Recruiter(user_id=other.id, email="them@example.com", company="Secret Corp")
        db_session.add_all([campaign, recruiter])
        db_session.flush()
        db_session.add(
            Application(
                user_id=other.id, campaign_id=campaign.id, recruiter_id=recruiter.id
            )
        )
        db_session.commit()

        body = auth_client.get("/api/v1/dashboard").json()
        assert body["stats"]["total_applications"] == 4
        assert "Secret Corp" not in body["filters"]["companies"]

    def test_requires_authentication(self, client):
        assert client.get("/api/v1/dashboard").status_code == 401
