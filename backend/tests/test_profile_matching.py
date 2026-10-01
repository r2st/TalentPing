"""Applying under the profile that won the job, with that profile's resume.

The point of holding two profiles is that a DevOps posting gets the DevOps
resume and a backend posting gets the backend one, without the candidate running
the product twice. These are the tests for that promise end to end: the scan
records which profile won, and the auto-apply pipeline honours it.
"""
from __future__ import annotations

import pytest

from app.models.application import Application
from app.models.job import JobPosting, JobStatus, job_fingerprint
from app.models.profile import Profile
from app.models.resume import Resume
from app.models.tailored_resume import TailoredResume


@pytest.fixture()
def devops_resume(db_session, current_user) -> Resume:
    row = Resume(
        user_id=current_user.id,
        filename="devops.pdf",
        raw_text="Jordan Candidate — DevOps Engineer. Kubernetes, Terraform, AWS.",
        full_name="Jordan Candidate",
        location="San Francisco, CA",
        headline="DevOps Engineer",
        years_experience=8,
        seniority="senior",
        skills=["kubernetes", "terraform", "aws"],
        target_roles=["DevOps Engineer"],
        experience=[],
        education=[],
        links=[],
        parsed_with="heuristic",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def two_profiles(db_session, current_user, resume, devops_resume):
    """The backend resume under one profile, the DevOps resume under another."""
    backend = Profile(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend Engineer",
        target_roles=["Backend Engineer"],
        skills=["python", "fastapi"],
        is_active=True,
        is_default=True,
    )
    devops = Profile(
        user_id=current_user.id,
        resume_id=devops_resume.id,
        name="DevOps Engineer",
        target_roles=["DevOps Engineer", "SRE"],
        skills=["kubernetes", "terraform"],
        is_active=True,
    )
    db_session.add_all([backend, devops])
    db_session.commit()
    return backend, devops


def _seed(db, user, *, title, company, profile=None, fit=90):
    posting = JobPosting(
        user_id=user.id,
        title=title,
        company=company,
        location="Remote",
        remote=True,
        url=f"https://example.com/{company}".lower(),
        description=f"{title} at {company}. Kubernetes, Terraform, Python, FastAPI.",
        source="test",
        fingerprint=job_fingerprint(title, company, None),
        status=JobStatus.NEW,
        fit_score=fit,
        matched_profile_id=profile.id if profile is not None else None,
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    return posting


def _activate(client):
    return client.put(
        "/api/v1/autopilot",
        json={"is_active": True, "min_fit_score": 70, "daily_application_limit": 10},
    )


class TestTheWinningProfileDoesTheWork:
    def test_the_matched_profiles_resume_is_the_one_tailored_and_sent(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles, devops_resume,
    ):
        _backend, devops = two_profiles
        _seed(db_session, current_user, title="DevOps Engineer",
              company="Kube Corp", profile=devops)
        _activate(auth_client)

        assert auth_client.post("/api/v1/autopilot/run").json()["applied"] == 1

        tailored = db_session.query(TailoredResume).one()
        assert tailored.resume_id == devops_resume.id

    def test_the_application_records_which_profile_sent_it(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles,
    ):
        """"Why was I pitched as a tech lead here?" needs an answer months later."""
        _backend, devops = two_profiles
        _seed(db_session, current_user, title="Site Reliability Engineer",
              company="Kube Corp", profile=devops)
        _activate(auth_client)
        auth_client.post("/api/v1/autopilot/run")

        assert db_session.query(Application).one().profile_id == devops.id

    def test_the_run_reports_what_each_profile_did(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles,
    ):
        backend, devops = two_profiles
        _seed(db_session, current_user, title="Backend Engineer",
              company="Northwind", profile=backend)
        _seed(db_session, current_user, title="DevOps Engineer",
              company="Kube Corp", profile=devops)
        _activate(auth_client)

        result = auth_client.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 2, result
        assert result["applied_by_profile"] == {
            "Backend Engineer": 1,
            "DevOps Engineer": 1,
        }

    def test_a_role_only_the_second_profile_wants_still_gets_applied_to(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles,
    ):
        """The whole feature: one profile's roles must not veto another's.

        Before profiles, the relevance gate ran against a single set of target
        roles, so a DevOps posting was "not one of your target roles" for a
        candidate whose default resume said backend — and was dropped.
        """
        _backend, devops = two_profiles
        _seed(db_session, current_user, title="DevOps Engineer",
              company="Kube Corp", profile=devops)
        _activate(auth_client)

        result = auth_client.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 1, result
        assert result["skipped_irrelevant"] == 0

    def test_a_role_no_profile_wants_is_still_refused(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles,
    ):
        """Several profiles widen the net; they don't remove it."""
        backend, _devops = two_profiles
        _seed(db_session, current_user, title="Registered Nurse",
              company="Mercy Health", profile=backend)
        _activate(auth_client)

        result = auth_client.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 0
        assert result["skipped_irrelevant"] == 1

    def test_a_posting_whose_profile_was_deleted_still_gets_applied_to(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles,
    ):
        """A deleted intent shouldn't strand jobs that matched it."""
        backend, devops = two_profiles
        _seed(db_session, current_user, title="Backend Engineer",
              company="Northwind", profile=devops)
        db_session.delete(devops)
        db_session.commit()
        _activate(auth_client)

        result = auth_client.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 1, result
        assert db_session.query(Application).one().profile_id == backend.id

    def test_an_inactive_profiles_jobs_fall_back_to_a_live_profile(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles,
    ):
        backend, devops = two_profiles
        _seed(db_session, current_user, title="Backend Engineer",
              company="Northwind", profile=devops)
        devops.is_active = False
        db_session.commit()
        _activate(auth_client)

        auth_client.post("/api/v1/autopilot/run")
        assert db_session.query(Application).one().profile_id == backend.id


class TestTheSearchCoversEveryProfile:
    def test_the_autopilot_search_looks_for_all_of_their_roles(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles,
    ):
        """One sweep feeds every profile, so it has to be wide enough for all."""
        from app.models.job import JobSearch

        _activate(auth_client)
        auth_client.post("/api/v1/autopilot/run")

        search = db_session.query(JobSearch).filter_by(name="Autopilot search").one()
        # Every profile's stated roles lead the search, before the synonyms the
        # boards use for them — the feed is wide, the gates stay selective.
        assert search.roles[:3] == ["Backend Engineer", "DevOps Engineer", "SRE"]

    def test_one_remote_only_profile_does_not_narrow_the_whole_feed(
        self, auth_client, connected_gmail, stub_scraper, no_scan, db_session,
        current_user, two_profiles,
    ):
        """A profile that will take on-site work is a reason to keep it in view."""
        from app.models.job import JobSearch

        backend, devops = two_profiles
        devops.remote_only = True
        db_session.commit()
        _activate(auth_client)
        auth_client.post("/api/v1/autopilot/run")

        search = db_session.query(JobSearch).filter_by(name="Autopilot search").one()
        assert search.remote_only is False
