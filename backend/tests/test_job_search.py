"""Job discovery: provider normalization, filtering, dedupe, scoring, and the API.

No network is touched — every provider is monkeypatched. What's under test is
the logic that decides which of a few hundred postings the candidate ever sees.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.job import JobPosting, JobSearch, JobStatus, job_fingerprint
from app.services import job_search_service as svc
from app.services.job_search_service import JobQuery, RawJob, matches_query, run_search
from app.tasks.job_tasks import _is_due


def _raw(**kwargs) -> RawJob:
    defaults = {
        "title": "Senior Backend Engineer",
        "company": "Northwind Labs",
        "location": "San Francisco, CA",
        "url": "https://example.com/jobs/1",
        "description": (
            "Requirements\n- 6+ years of experience\n- Python and FastAPI\n"
            "- PostgreSQL and AWS\nWe are a fintech payments company."
        ),
        "remote": False,
        "source": "test",
    }
    return RawJob(**{**defaults, **kwargs})


@pytest.fixture()
def search(db_session, current_user, resume) -> JobSearch:
    row = JobSearch(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend roles",
        roles=["Backend Engineer"],
        keywords=["python"],
        min_fit_score=60,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


class TestFingerprint:
    def test_is_stable_for_the_same_role(self):
        """Aggregators rewrite URLs; title+company is what actually identifies a job."""
        assert job_fingerprint("Backend Engineer", "Acme", "https://a.com?utm=1") == (
            job_fingerprint("backend  engineer", "ACME", "https://b.com")
        )

    def test_differs_across_companies(self):
        assert job_fingerprint("Backend Engineer", "Acme", None) != job_fingerprint(
            "Backend Engineer", "Globex", None
        )

    def test_falls_back_to_the_url_without_a_company(self):
        assert job_fingerprint("Engineer", None, "https://a.com/1") != job_fingerprint(
            "Engineer", None, "https://a.com/2"
        )


class TestQueryMatching:
    def test_matches_on_a_role_phrase(self):
        assert matches_query(_raw(), JobQuery(roles=["Backend Engineer"]))

    def test_matches_when_every_significant_word_appears(self):
        """"Senior Backend Engineer" rarely appears verbatim in a listing."""
        assert matches_query(
            _raw(title="Backend Engineer, Platform"),
            JobQuery(roles=["Senior Backend Engineer"]),
        )

    def test_rejects_an_unrelated_posting(self):
        assert not matches_query(
            _raw(title="Oncology Nurse", company="City Hospital", description="Patient care."),
            JobQuery(roles=["Backend Engineer"], keywords=["python"]),
        )

    def test_remote_only_excludes_onsite(self):
        assert not matches_query(_raw(remote=False), JobQuery(roles=["Backend"], remote_only=True))

    def test_remote_only_keeps_remote(self):
        assert matches_query(_raw(remote=True), JobQuery(roles=["Backend"], remote_only=True))

    def test_no_criteria_matches_everything(self):
        assert matches_query(_raw(), JobQuery())

    def test_a_mention_in_the_body_does_not_pull_in_another_role(self):
        """Ads name the teams you'd work with. That is not the job on offer.

        The fuzzy word-overlap fallback used to run over the whole description,
        so a marketing posting that mentioned "backend engineers" in passing
        matched a backend search — and once it was in the feed, a thin posting
        could score its way into the send queue.
        """
        assert not matches_query(
            _raw(
                title="Marketing Manager",
                company="Widgets Inc",
                description=(
                    "You will partner daily with our senior backend and platform "
                    "engineers to take new products to market."
                ),
            ),
            JobQuery(roles=["Senior Backend Engineer"]),
        )

    def test_the_term_written_out_in_the_body_still_matches(self):
        """A posting that names the role outright is a deliberate signal."""
        assert matches_query(
            _raw(
                title="Engineer II",
                company="Widgets Inc",
                description="This is a backend engineer role on our payments team.",
            ),
            JobQuery(roles=["Backend Engineer"]),
        )

    def test_the_fuzzy_fallback_still_works_on_the_title(self):
        assert matches_query(
            _raw(title="Backend Engineer, Platform", description="Some prose."),
            JobQuery(roles=["Senior Backend Engineer"]),
        )


class TestRunSearch:
    def _patch_discover(self, monkeypatch, jobs):
        monkeypatch.setattr(svc, "discover", lambda query: jobs)

    def test_stores_scored_postings(self, db_session, monkeypatch, search):
        self._patch_discover(monkeypatch, [_raw()])
        result = run_search(db_session, search)

        assert result.added == 1
        posting = result.postings[0]
        assert posting.company == "Northwind Labs"
        assert posting.fit_score is not None
        assert posting.status == JobStatus.NEW
        assert posting.search_id == search.id

    def test_drops_postings_below_the_threshold(self, db_session, monkeypatch, search):
        search.min_fit_score = 95
        db_session.commit()
        self._patch_discover(monkeypatch, [_raw()])

        result = run_search(db_session, search)
        assert result.added == 0
        assert result.below_threshold == 1
        assert db_session.query(JobPosting).count() == 0

    def test_filters_out_irrelevant_postings_before_scoring(
        self, db_session, monkeypatch, search
    ):
        self._patch_discover(
            monkeypatch,
            [_raw(title="Oncology Nurse", company="City Hospital", description="Patient care.")],
        )
        result = run_search(db_session, search)
        assert result.scanned == 0
        assert result.added == 0

    def test_does_not_re_add_a_known_posting(self, db_session, monkeypatch, search):
        self._patch_discover(monkeypatch, [_raw()])
        run_search(db_session, search)
        result = run_search(db_session, search)

        assert result.added == 0
        assert result.duplicates == 1
        assert db_session.query(JobPosting).count() == 1

    def test_deduplicates_within_a_single_scan(self, db_session, monkeypatch, search):
        """The same role from two boards is one job to the candidate."""
        self._patch_discover(
            monkeypatch,
            [_raw(source="remoteok"), _raw(url="https://other.example/1", source="arbeitnow")],
        )
        result = run_search(db_session, search)
        assert result.added == 1

    def test_writes_a_fit_score_row(self, db_session, monkeypatch, search):
        from app.models.fit_score import FitScore

        self._patch_discover(monkeypatch, [_raw()])
        run_search(db_session, search)
        assert db_session.query(FitScore).count() == 1

    def test_records_the_run_on_the_search(self, db_session, monkeypatch, search):
        self._patch_discover(monkeypatch, [_raw()])
        run_search(db_session, search)

        db_session.refresh(search)
        assert search.last_run_at is not None
        assert search.jobs_found == 1

    def test_a_provider_outage_is_recorded_not_raised(self, db_session, monkeypatch, search):
        def _boom(query):
            raise RuntimeError("all providers down")

        monkeypatch.setattr(svc, "discover", _boom)
        result = run_search(db_session, search)

        assert result.added == 0
        assert result.detail is not None
        db_session.refresh(search)
        assert search.last_error


class TestProviderNormalization:
    """Each provider's payload shape, without hitting the network."""

    def test_remoteok(self, monkeypatch):
        payload = [
            {"legal": "notice"},
            {
                "position": "Backend Engineer",
                "company": "Smith &amp; Co",
                "location": "Worldwide",
                "url": "https://remoteok.com/l/1",
                "description": "<p>Python &amp; <b>AWS</b></p>",
                "salary_min": 100000,
                "salary_max": 150000,
                "epoch": 1750000000,
            },
        ]
        jobs = svc.fetch_remoteok(JobQuery(), _FakeSession(payload))

        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Backend Engineer"
        assert job.remote is True
        assert job.salary_text == "$100,000 - $150,000"
        # Entities are decoded, not shown raw: a company really is called
        # "Smith & Co", and the feed would otherwise display the markup.
        assert job.company == "Smith & Co"
        # HTML must be stripped — the description goes straight into the JD parser.
        assert "<p>" not in job.description
        assert "Python & AWS" in job.description

    def test_arbeitnow(self):
        payload = {
            "data": [
                {
                    "title": "Platform Engineer",
                    "company_name": "Globex",
                    "location": "Berlin",
                    "url": "https://arbeitnow.com/1",
                    "description": "<div>Kubernetes</div>",
                    "remote": True,
                    "created_at": 1750000000,
                }
            ]
        }
        jobs = svc.fetch_arbeitnow(JobQuery(), _FakeSession(payload))
        assert jobs[0].company == "Globex"
        assert jobs[0].remote is True

    def test_jobicy(self):
        payload = {
            "jobs": [
                {
                    # Jobicy really does embed newlines in titles.
                    "jobTitle": "Data\n  Engineer",
                    "companyName": "  Initech  ",
                    "jobGeo": "Anywhere",
                    "url": "https://jobicy.com/1",
                    "jobDescription": "Spark and dbt",
                    "pubDate": "2026-07-01 10:00:00",
                }
            ]
        }
        jobs = svc.fetch_jobicy(JobQuery(), _FakeSession(payload))
        # Whitespace is collapsed: it breaks the layout and, worse, poisons the
        # dedupe fingerprint.
        assert jobs[0].title == "Data Engineer"
        assert jobs[0].company == "Initech"
        assert jobs[0].remote is True

    def test_serpapi_is_skipped_without_a_key(self):
        """The public boards must carry the feature on a fresh install."""
        assert svc.fetch_serpapi(JobQuery(), _FakeSession({})) == []

    def test_serpapi_normalizes_google_jobs_results(self, monkeypatch):
        monkeypatch.setattr(svc.settings, "serpapi_api_key", "test-key", raising=False)
        payload = {
            "jobs_results": [
                {
                    "title": "Staff Engineer",
                    "company_name": "Umbrella",
                    "location": "Remote",
                    "description": "Go and Kubernetes",
                    "detected_extensions": {"salary": "$200k", "work_from_home": True},
                    "apply_options": [{"link": "https://umbrella.example/apply"}],
                }
            ]
        }
        jobs = svc.fetch_serpapi(JobQuery(roles=["Staff Engineer"]), _FakeSession(payload))
        assert jobs[0].url == "https://umbrella.example/apply"
        assert jobs[0].remote is True
        assert jobs[0].source == "serpapi"

    def test_a_malformed_payload_yields_nothing(self):
        assert svc.fetch_remoteok(JobQuery(), _FakeSession({"not": "a list"})) == []


class _FakeSession:
    """Stands in for requests.Session — returns one canned JSON payload."""

    def __init__(self, payload):
        self._payload = payload

    def get(self, *args, **kwargs):
        return _FakeResponse(self._payload)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class TestDueCalculation:
    def test_a_never_run_search_is_due(self, search):
        assert _is_due(search, datetime.now(UTC))

    def test_a_recent_run_is_not_due(self, search):
        now = datetime.now(UTC)
        search.last_run_at = now - timedelta(hours=1)
        assert not _is_due(search, now)

    def test_an_elapsed_interval_is_due(self, search):
        now = datetime.now(UTC)
        search.last_run_at = now - timedelta(hours=7)  # interval is 6h
        assert _is_due(search, now)

    def test_naive_timestamps_from_sqlite_are_handled(self, search):
        now = datetime.now(UTC)
        search.last_run_at = (now - timedelta(hours=7)).replace(tzinfo=None)
        assert _is_due(search, now)


class TestJobsApi:
    def test_creates_a_search_and_runs_it(self, auth_client, monkeypatch, resume):
        monkeypatch.setattr(svc, "discover", lambda query: [_raw()])

        resp = auth_client.post(
            "/api/v1/jobs/searches",
            json={"roles": ["Backend Engineer"], "keywords": ["python"], "min_fit_score": 50},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["added"] == 1
        assert body["jobs"][0]["company"] == "Northwind Labs"
        assert body["jobs"][0]["fit_score"] is not None

    def test_a_search_needs_criteria(self, auth_client, resume):
        assert auth_client.post("/api/v1/jobs/searches", json={}).status_code == 422

    def test_lists_searches(self, auth_client, monkeypatch, resume):
        monkeypatch.setattr(svc, "discover", lambda query: [])
        auth_client.post("/api/v1/jobs/searches", json={"roles": ["Backend Engineer"]})

        rows = auth_client.get("/api/v1/jobs/searches").json()
        assert len(rows) == 1
        assert rows[0]["roles"] == ["Backend Engineer"]

    def test_patches_a_search(self, auth_client, monkeypatch, resume):
        monkeypatch.setattr(svc, "discover", lambda query: [])
        auth_client.post("/api/v1/jobs/searches", json={"roles": ["Backend Engineer"]})
        search_id = auth_client.get("/api/v1/jobs/searches").json()[0]["id"]

        updated = auth_client.patch(
            f"/api/v1/jobs/searches/{search_id}", json={"is_active": False, "min_fit_score": 80}
        ).json()
        assert updated["is_active"] is False
        assert updated["min_fit_score"] == 80

    def test_deletes_a_search(self, auth_client, monkeypatch, resume):
        monkeypatch.setattr(svc, "discover", lambda query: [])
        auth_client.post("/api/v1/jobs/searches", json={"roles": ["Backend Engineer"]})
        search_id = auth_client.get("/api/v1/jobs/searches").json()[0]["id"]

        assert auth_client.delete(f"/api/v1/jobs/searches/{search_id}").status_code == 204
        assert auth_client.get("/api/v1/jobs/searches").json() == []

    def test_feed_is_sorted_by_fit(self, auth_client, db_session, current_user, resume):
        for index, score in enumerate([42.0, 91.0, 67.0]):
            db_session.add(
                JobPosting(
                    user_id=current_user.id,
                    title=f"Role {index}",
                    company=f"Company {index}",
                    fingerprint=f"fp-{index}",
                    fit_score=score,
                )
            )
        db_session.commit()

        feed = auth_client.get("/api/v1/jobs").json()
        assert [row["fit_score"] for row in feed] == [91.0, 67.0, 42.0]

    def test_feed_hides_dismissed_jobs(self, auth_client, db_session, current_user, resume):
        db_session.add_all(
            [
                JobPosting(
                    user_id=current_user.id, title="Kept", fingerprint="a", fit_score=70
                ),
                JobPosting(
                    user_id=current_user.id,
                    title="Dropped",
                    fingerprint="b",
                    fit_score=80,
                    status=JobStatus.DISMISSED,
                ),
            ]
        )
        db_session.commit()

        titles = [row["title"] for row in auth_client.get("/api/v1/jobs").json()]
        assert titles == ["Kept"]

    def test_feed_filters(self, auth_client, db_session, current_user, resume):
        db_session.add_all(
            [
                JobPosting(
                    user_id=current_user.id,
                    title="A",
                    company="Northwind Labs",
                    fingerprint="a",
                    fit_score=90,
                ),
                JobPosting(
                    user_id=current_user.id,
                    title="B",
                    company="Globex",
                    fingerprint="b",
                    fit_score=50,
                ),
            ]
        )
        db_session.commit()

        by_company = auth_client.get("/api/v1/jobs", params={"company": "northwind"}).json()
        assert [row["title"] for row in by_company] == ["A"]

        by_fit = auth_client.get("/api/v1/jobs", params={"min_fit": 60}).json()
        assert [row["title"] for row in by_fit] == ["A"]

    def test_triage_updates_status(self, auth_client, db_session, current_user, resume):
        posting = JobPosting(user_id=current_user.id, title="A", fingerprint="a")
        db_session.add(posting)
        db_session.commit()

        updated = auth_client.patch(
            f"/api/v1/jobs/{posting.id}", json={"status": "SAVED"}
        ).json()
        assert updated["status"] == "SAVED"

    def test_detail_includes_the_description(self, auth_client, db_session, current_user, resume):
        posting = JobPosting(
            user_id=current_user.id, title="A", fingerprint="a", description="Full text here."
        )
        db_session.add(posting)
        db_session.commit()

        assert (
            auth_client.get(f"/api/v1/jobs/{posting.id}").json()["description"]
            == "Full text here."
        )

    def test_another_users_job_is_invisible(self, auth_client, db_session, resume):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        posting = JobPosting(user_id=other.id, title="Secret", fingerprint="x")
        db_session.add(posting)
        db_session.commit()

        assert auth_client.get(f"/api/v1/jobs/{posting.id}").status_code == 404
        assert auth_client.get("/api/v1/jobs").json() == []

    def test_provider_status_reports_configuration(self, auth_client):
        body = auth_client.get("/api/v1/jobs/providers/status").json()
        assert body["google_jobs_serpapi"] is False
        assert "remoteok" in body["public_boards"]

    def test_requires_authentication(self, client):
        assert client.get("/api/v1/jobs").status_code == 401


class TestScanScoresAgainstEveryProfile:
    """One sweep, several intents — the scan records which one won each job."""

    def _profiles(self, db_session, current_user, resume):
        from app.models.profile import Profile

        backend = Profile(
            user_id=current_user.id,
            resume_id=resume.id,
            name="Backend Engineer",
            target_roles=["Backend Engineer"],
            is_active=True,
            is_default=True,
        )
        devops = Profile(
            user_id=current_user.id,
            resume_id=resume.id,
            name="DevOps Engineer",
            target_roles=["DevOps Engineer", "SRE"],
            is_active=True,
        )
        db_session.add_all([backend, devops])
        db_session.commit()
        return backend, devops

    def _unpinned(self, db_session, search):
        """Autopilot's own search is unpinned, so it scores against all profiles."""
        search.resume_id = None
        search.roles = ["Backend Engineer", "DevOps Engineer"]
        db_session.commit()

    def test_each_posting_records_the_profile_it_matched(
        self, db_session, monkeypatch, search, current_user, resume
    ):
        backend, devops = self._profiles(db_session, current_user, resume)
        self._unpinned(db_session, search)
        monkeypatch.setattr(
            svc,
            "discover",
            lambda query: [
                _raw(title="Senior Backend Engineer", company="Northwind Labs"),
                _raw(
                    title="DevOps Engineer",
                    company="Kube Corp",
                    url="https://example.com/jobs/2",
                    description="Kubernetes and Terraform. Python tooling.",
                ),
            ],
        )

        result = run_search(db_session, search)
        matched = {p.title: p.matched_profile_id for p in result.postings}
        assert matched["Senior Backend Engineer"] == backend.id
        assert matched["DevOps Engineer"] == devops.id

    def test_every_profiles_verdict_is_stored_not_only_the_winners(
        self, db_session, monkeypatch, search, current_user, resume
    ):
        """The runner-up answers "would my other profile have liked this?"."""
        from app.models.fit_score import FitScore

        backend, devops = self._profiles(db_session, current_user, resume)
        self._unpinned(db_session, search)
        monkeypatch.setattr(svc, "discover", lambda query: [_raw()])

        run_search(db_session, search)
        stored = {row.profile_id for row in db_session.query(FitScore).all()}
        assert stored == {backend.id, devops.id}

    def test_a_search_the_user_pinned_to_a_resume_is_honoured_as_written(
        self, db_session, monkeypatch, search, current_user, resume
    ):
        """Their search, their resume — overriding it would be the same bug back."""
        self._profiles(db_session, current_user, resume)
        # `search` is already pinned to `resume` by the fixture.
        monkeypatch.setattr(svc, "discover", lambda query: [_raw()])

        result = run_search(db_session, search)
        # Scored under the profile that owns the pinned resume, and only that one.
        from app.models.fit_score import FitScore

        assert len({row.profile_id for row in db_session.query(FitScore).all()}) == 1
        assert result.added == 1
