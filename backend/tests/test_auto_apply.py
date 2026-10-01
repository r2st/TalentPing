"""End-to-end auto-apply pipeline: discover → score → tailor → email → follow up.

Discovery and the career-page scraper are stubbed — these tests assert the
autopilot's wiring, budget and guardrails, not the network-bound crawlers.
"""
from __future__ import annotations

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.email import Email, EmailStatus
from app.models.job import JobPosting, JobStatus, job_fingerprint


def _seed_job(
    db,
    user,
    *,
    title,
    company,
    fit,
    status=JobStatus.NEW,
    llm_fit=None,
    duplicate_of=None,
):
    posting = JobPosting(
        user_id=user.id,
        title=title,
        company=company,
        location="Remote",
        url=f"https://example.com/{company}".lower(),
        description=f"{title} at {company}. Python, FastAPI, AWS.",
        remote=True,
        source="test",
        fingerprint=job_fingerprint(title, company, None),
        status=status,
        fit_score=fit,
        llm_fit_score=llm_fit,
        duplicate_of_id=duplicate_of.id if duplicate_of is not None else None,
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    return posting


def _activate(auth_client, **overrides):
    payload = {"is_active": True, "min_fit_score": 70, "daily_application_limit": 10}
    payload.update(overrides)
    return auth_client.put("/api/v1/autopilot", json=payload)


@pytest.fixture()
def ready(auth_client, connected_gmail, resume, stub_scraper, no_scan):
    """A fully onboarded user with autopilot ready to switch on."""
    return auth_client


class TestPreferences:
    def test_get_creates_a_default_off_row(self, auth_client):
        body = auth_client.get("/api/v1/autopilot").json()
        assert body["is_active"] is False
        assert body["min_fit_score"] == 70
        assert body["applications_created"] == 0

    def test_cannot_activate_without_gmail(self, auth_client, resume):
        resp = _activate(auth_client)
        assert resp.status_code == 409
        assert "Gmail" in resp.json()["detail"]

    def test_update_persists_targeting(self, auth_client, connected_gmail):
        resp = auth_client.put(
            "/api/v1/autopilot",
            json={"target_roles": ["Backend Engineer"], "min_fit_score": 80},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["target_roles"] == ["Backend Engineer"]
        assert body["min_fit_score"] == 80

    def test_run_requires_active_autopilot(self, auth_client, connected_gmail):
        assert auth_client.post("/api/v1/autopilot/run").status_code == 409


class TestAutoApply:
    def test_applies_to_a_strong_match_end_to_end(self, ready, db_session, current_user):
        posting = _seed_job(db_session, current_user, title="Senior Backend Engineer",
                            company="Northwind", fit=88)
        _activate(ready)

        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 1, result

        # An application was created, linked to the posting.
        application = db_session.query(Application).one()
        assert application.job_posting_id == posting.id
        assert application.status == ApplicationStatus.QUEUED

        # Its outreach email is queued and names the role.
        email = db_session.query(Email).filter_by(direction="SENT").first()
        assert email.status == EmailStatus.QUEUED
        assert email.to_address == "talent@northwind.com"
        assert "Senior Backend Engineer" in (email.subject or "") + (email.body_text or "")

        # The posting is marked applied so it's never re-processed.
        db_session.refresh(posting)
        assert posting.status == JobStatus.APPLIED
        assert posting.applied_at is not None

    def test_low_fit_jobs_are_left_alone(self, ready, db_session, current_user):
        _seed_job(db_session, current_user, title="Marketing Lead", company="Acme", fit=40)
        _activate(ready, min_fit_score=70)
        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 0
        assert db_session.query(Application).count() == 0

    def test_warmup_budget_caps_a_first_run(self, ready, db_session, current_user):
        for i in range(8):
            _seed_job(db_session, current_user, title=f"Backend Engineer {i}",
                     company=f"Company{i}", fit=90 - i)
        _activate(ready, daily_application_limit=10)

        result = ready.post("/api/v1/autopilot/run").json()
        # Fresh mailbox → the warm-up ramp allows only 5/day, even though the
        # user's own cap is 10.
        assert result["budget"] == 5
        assert result["applied"] == 5
        assert db_session.query(Application).count() == 5

    def test_a_posting_with_no_company_is_skipped(self, ready, db_session, current_user):
        _seed_job(db_session, current_user, title="Engineer", company=None, fit=95)
        _activate(ready)
        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 0

    def test_the_same_recruiter_is_not_contacted_twice(self, ready, db_session, current_user):
        # Two roles at the same company resolve to the same recruiter address.
        _seed_job(db_session, current_user, title="Backend Engineer", company="Acme", fit=90)
        _seed_job(db_session, current_user, title="Platform Engineer", company="Acme", fit=88)
        _activate(ready)

        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 1
        assert result["skipped_duplicate"] == 1
        assert db_session.query(Application).count() == 1

    def test_review_mode_parks_outreach_as_drafts(self, ready, db_session, current_user):
        _seed_job(db_session, current_user, title="Backend Engineer", company="Acme", fit=90)
        _activate(ready, auto_send=False)

        ready.post("/api/v1/autopilot/run")
        email = db_session.query(Email).filter_by(direction="SENT").one()
        assert email.status == EmailStatus.DRAFT

    def test_follow_ups_are_scheduled_for_auto_applications(self, ready, db_session, current_user):
        _seed_job(db_session, current_user, title="Backend Engineer", company="Acme", fit=90)
        _activate(ready, follow_up_count=2)
        ready.post("/api/v1/autopilot/run")

        from app.models.follow_up import FollowUp

        assert db_session.query(FollowUp).count() == 2

    def test_second_run_does_not_re_apply(self, ready, db_session, current_user):
        _seed_job(db_session, current_user, title="Backend Engineer", company="Acme", fit=90)
        _activate(ready)
        ready.post("/api/v1/autopilot/run")
        first = db_session.query(Application).count()
        ready.post("/api/v1/autopilot/run")
        assert db_session.query(Application).count() == first


class TestReputationEndpoint:
    def test_reports_warmup_status_for_the_mailbox(self, auth_client, connected_gmail):
        body = auth_client.get("/api/v1/autopilot/reputation").json()
        assert len(body) == 1
        assert body[0]["email"] == "candidate@gmail.com"
        assert body[0]["day_limit"] == 5  # fresh mailbox, first warm-up step
        assert body[0]["warmup"]["day_limit"] == 5


class TestRelevanceGate:
    """Nothing off-target reaches a stranger's inbox.

    These are the tests for the reported bug: autopilot was emailing companies
    that had nothing to do with the candidate. The fit score is a weighted
    average, so a posting can clear the threshold on location, seniority and
    salary alone; this gate is checked separately and cannot be outvoted.
    """

    def test_an_unrelated_role_is_not_emailed_even_with_a_high_fit_score(
        self, ready, db_session, current_user
    ):
        """The exact failure: a great-looking number on the wrong kind of job."""
        _seed_job(db_session, current_user, title="Registered Nurse",
                  company="Mercy Health", fit=95)
        _activate(ready)

        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 0, result
        assert result["skipped_irrelevant"] == 1
        assert db_session.query(Application).count() == 0
        assert db_session.query(Email).count() == 0

    def test_the_skip_reason_names_the_role_and_the_targets(
        self, ready, db_session, current_user
    ):
        _seed_job(db_session, current_user, title="Class A Truck Driver",
                  company="Swift", fit=99)
        _activate(ready)
        notes = " ".join(ready.post("/api/v1/autopilot/run").json()["notes"])
        assert "Truck Driver" in notes
        assert "target roles" in notes

    def test_an_on_target_role_still_goes_out(self, ready, db_session, current_user):
        """The gate must not cost real applications."""
        _seed_job(db_session, current_user, title="Senior Backend Engineer",
                  company="Northwind", fit=88)
        _activate(ready)
        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 1, result
        assert result["skipped_irrelevant"] == 0

    def test_an_adjacent_role_goes_out(self, ready, db_session, current_user):
        """Same craft, different specialism, is a legitimate application."""
        _seed_job(db_session, current_user, title="Python Developer",
                  company="Plaid", fit=80)
        _activate(ready)
        assert ready.post("/api/v1/autopilot/run").json()["applied"] == 1

    def test_the_users_own_targets_beat_the_resume(
        self, ready, db_session, current_user
    ):
        """Stated preferences win — that is what asking for them is for."""
        _seed_job(db_session, current_user, title="Senior Backend Engineer",
                  company="Northwind", fit=95)
        _activate(ready, target_roles=["Product Designer"])
        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 0
        assert result["skipped_irrelevant"] == 1

    def test_an_untitled_posting_is_refused_rather_than_waved_through(
        self, db_session, current_user, resume
    ):
        from app.services.auto_apply_service import relevance_gate
        from app.services.fit_scorer import Targeting

        posting = JobPosting(user_id=current_user.id, title=None, company="Acme",
                             fingerprint="x", status=JobStatus.NEW)
        want = Targeting(roles=["Backend Engineer"])
        assert relevance_gate(resume, want, posting) is not None

    def test_a_candidate_with_no_targets_is_not_blocked(
        self, db_session, current_user, resume
    ):
        """With nothing to judge against, the fit score remains the only gate."""
        from app.services.auto_apply_service import relevance_gate
        from app.services.fit_scorer import Targeting

        resume.target_roles = []
        resume.experience = []
        resume.headline = None
        posting = JobPosting(user_id=current_user.id, title="Registered Nurse",
                             company="Mercy", fingerprint="x", status=JobStatus.NEW)
        assert relevance_gate(resume, Targeting(), posting) is None


class TestScoutsVeto:
    """Scout's re-rank was computed, stored, shown — and ignored when it mattered."""

    def test_a_posting_scout_marked_down_is_not_emailed(
        self, ready, db_session, current_user
    ):
        _seed_job(db_session, current_user, title="Senior Backend Engineer",
                  company="Northwind", fit=92, llm_fit=15)
        _activate(ready)
        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 0, result
        assert db_session.query(Email).count() == 0

    def test_a_posting_scout_liked_goes_out(self, ready, db_session, current_user):
        _seed_job(db_session, current_user, title="Senior Backend Engineer",
                  company="Northwind", fit=92, llm_fit=85)
        _activate(ready)
        assert ready.post("/api/v1/autopilot/run").json()["applied"] == 1

    def test_a_posting_scout_never_saw_is_unaffected(
        self, ready, db_session, current_user
    ):
        """The re-rank covers a top-N shortlist; a null is silence, not a veto."""
        _seed_job(db_session, current_user, title="Senior Backend Engineer",
                  company="Northwind", fit=92, llm_fit=None)
        _activate(ready)
        assert ready.post("/api/v1/autopilot/run").json()["applied"] == 1


class TestDuplicatesAreNotAppliedTo:
    def test_a_cross_board_duplicate_is_skipped(
        self, ready, db_session, current_user
    ):
        """The same role on a second board is the same job, not a second one."""
        canonical = _seed_job(db_session, current_user, title="Senior Backend Engineer",
                              company="Northwind", fit=90)
        _seed_job(db_session, current_user, title="Sr Backend Engineer",
                  company="Northwind Labs", fit=90, duplicate_of=canonical)
        _activate(ready)

        result = ready.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 1, result
        assert db_session.query(Application).count() == 1


class TestSearchStaysInSyncWithPreferences:
    def test_changing_target_roles_rewrites_the_autopilot_search(
        self, ready, db_session, current_user
    ):
        """A search written once and never updated kept feeding the old criteria."""
        from app.models.job import JobSearch

        _activate(ready, target_roles=["Backend Engineer"], remote_only=False)
        ready.post("/api/v1/autopilot/run")
        search = db_session.query(JobSearch).filter_by(name="Autopilot search").one()
        # The stated role leads; the rest are the other names boards use for it.
        assert search.roles[0] == "Backend Engineer"
        assert "Data Engineer" not in search.roles

        _activate(ready, target_roles=["Data Engineer"], remote_only=True,
                  min_fit_score=85)
        ready.post("/api/v1/autopilot/run")
        db_session.refresh(search)
        assert search.roles[0] == "Data Engineer"
        assert "Backend Engineer" not in search.roles
        assert search.remote_only is True
        assert search.min_fit_score == 85

    def test_a_user_authored_search_is_left_alone(
        self, ready, db_session, current_user, resume
    ):
        """Their feed is theirs; autopilot enforces its selectivity at apply time."""
        from app.models.job import JobSearch

        mine = JobSearch(user_id=current_user.id, resume_id=resume.id,
                         name="My own search", roles=["Rust Engineer"],
                         min_fit_score=30, is_active=True)
        db_session.add(mine)
        db_session.commit()

        _activate(ready, target_roles=["Data Engineer"], min_fit_score=85)
        ready.post("/api/v1/autopilot/run")
        db_session.refresh(mine)
        assert mine.roles == ["Rust Engineer"]
        assert mine.min_fit_score == 30
