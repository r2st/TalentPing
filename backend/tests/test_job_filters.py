"""Salary, location and remote filters on the job feed and on saved searches.

The rule every case here circles is the one that decides whether the feature is
usable at all: **a posting that publishes no salary is never filtered out by a
salary filter.** Most employers publish nothing, so the other reading of "min
salary" empties the feed and looks like a bug in discovery rather than a filter
doing what it was told.
"""
from __future__ import annotations

import pytest

from app.models.job import JobPosting, JobSearch, JobStatus
from app.services import job_search_service as svc
from app.services.job_search_service import RawJob, run_search
from app.services.salary_service import meets_floor, parse_offered

JOBS = "/api/v1/jobs"


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
        min_fit_score=0,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


class TestMeetsFloor:
    def test_an_unpublished_band_always_passes(self):
        """The rule the whole feature rests on."""
        assert meets_floor(None, 150_000)

    def test_no_floor_passes_everything(self):
        assert meets_floor(40_000, None)
        assert meets_floor(None, None)

    def test_the_top_of_the_band_is_what_counts(self):
        """"$120k–$160k" clears a $150k ask — that band is worth a conversation."""
        assert meets_floor(160_000, 150_000)

    def test_a_band_that_tops_out_below_the_floor_is_rejected(self):
        assert not meets_floor(110_000, 150_000)

    def test_the_floor_itself_passes(self):
        assert meets_floor(150_000, 150_000)


class TestBandParsedAtIngest:
    def _patch_discover(self, monkeypatch, jobs):
        monkeypatch.setattr(svc, "discover", lambda query: jobs)

    def test_the_providers_salary_field_is_parsed_onto_the_row(
        self, db_session, monkeypatch, search
    ):
        self._patch_discover(monkeypatch, [_raw(salary_text="$150,000 - $190,000")])
        posting = run_search(db_session, search).postings[0]

        assert posting.salary_min == 150_000
        assert posting.salary_max == 190_000

    def test_a_band_only_in_the_description_is_not_lost(
        self, db_session, monkeypatch, search
    ):
        """Most boards publish no salary field and write the number in the body."""
        self._patch_discover(
            monkeypatch,
            [
                _raw(
                    salary_text=None,
                    description=(
                        "Requirements\n- 6+ years of experience\n- Python and FastAPI\n"
                        "The salary range for this role is $170,000 - $210,000."
                    ),
                )
            ],
        )
        posting = run_search(db_session, search).postings[0]

        assert posting.salary_min == 170_000
        assert posting.salary_max == 210_000
        assert posting.salary_text

    def test_an_unpublished_band_stays_null_rather_than_zero(
        self, db_session, monkeypatch, search
    ):
        self._patch_discover(monkeypatch, [_raw(salary_text=None)])
        posting = run_search(db_session, search).postings[0]

        assert posting.salary_min is None
        assert posting.salary_max is None

    def test_an_hourly_rate_is_not_guessed_at(self, db_session, monkeypatch, search):
        """Annualising "$80/hr" needs an assumption we would be inventing."""
        self._patch_discover(monkeypatch, [_raw(salary_text="$80/hr")])
        posting = run_search(db_session, search).postings[0]

        assert posting.salary_min is None
        assert parse_offered("$80/hr") == (None, None)


class TestSearchFloor:
    def _patch_discover(self, monkeypatch, jobs):
        monkeypatch.setattr(svc, "discover", lambda query: jobs)

    def test_a_posting_advertised_below_the_floor_is_screened_out(
        self, db_session, monkeypatch, search
    ):
        search.min_salary = 150_000
        db_session.commit()
        self._patch_discover(monkeypatch, [_raw(salary_text="$90,000 - $110,000")])

        result = run_search(db_session, search)

        assert result.added == 0
        assert result.below_salary == 1
        # And not miscounted as a fit-score rejection — the two are tuned apart.
        assert result.below_threshold == 0
        assert db_session.query(JobPosting).count() == 0

    def test_a_posting_that_publishes_nothing_still_reaches_the_feed(
        self, db_session, monkeypatch, search
    ):
        search.min_salary = 150_000
        db_session.commit()
        self._patch_discover(monkeypatch, [_raw(salary_text=None)])

        result = run_search(db_session, search)
        assert result.added == 1
        assert result.below_salary == 0

    def test_a_posting_at_or_above_the_floor_is_kept(
        self, db_session, monkeypatch, search
    ):
        search.min_salary = 150_000
        db_session.commit()
        self._patch_discover(monkeypatch, [_raw(salary_text="$150,000 - $180,000")])

        assert run_search(db_session, search).added == 1

    def test_no_floor_changes_nothing(self, db_session, monkeypatch, search):
        self._patch_discover(monkeypatch, [_raw(salary_text="$40,000")])
        assert run_search(db_session, search).added == 1


class TestSavedSearchAPI:
    def test_the_floor_round_trips(self, auth_client, monkeypatch):
        monkeypatch.setattr(svc, "discover", lambda query: [])

        created = auth_client.post(
            "/api/v1/jobs/searches",
            json={"roles": ["Backend Engineer"], "min_salary": 165_000},
        )
        assert created.status_code == 201

        listed = auth_client.get("/api/v1/jobs/searches").json()
        assert listed[0]["min_salary"] == 165_000

    def test_the_floor_can_be_changed_later(self, auth_client, monkeypatch):
        monkeypatch.setattr(svc, "discover", lambda query: [])
        created = auth_client.post(
            "/api/v1/jobs/searches", json={"roles": ["Backend Engineer"]}
        ).json()
        search_id = created["search_id"]

        patched = auth_client.patch(
            f"/api/v1/jobs/searches/{search_id}", json={"min_salary": 200_000}
        ).json()
        assert patched["min_salary"] == 200_000

    def test_a_negative_floor_is_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/v1/jobs/searches",
            json={"roles": ["Backend Engineer"], "min_salary": -1},
        )
        assert resp.status_code == 422


@pytest.fixture()
def feed(db_session, current_user) -> dict[str, JobPosting]:
    """One posting per case the feed filters on."""
    rows = {
        "rich": JobPosting(
            user_id=current_user.id,
            title="Staff Engineer",
            company="Northwind",
            location="New York, NY",
            fingerprint="fp-rich",
            salary_text="$200,000 - $240,000",
            salary_min=200_000,
            salary_max=240_000,
            remote=False,
            fit_score=90,
        ),
        "modest": JobPosting(
            user_id=current_user.id,
            title="Junior Engineer",
            company="Globex",
            location="Austin, TX",
            fingerprint="fp-modest",
            salary_text="$70,000 - $90,000",
            salary_min=70_000,
            salary_max=90_000,
            remote=False,
            fit_score=80,
        ),
        "silent": JobPosting(
            user_id=current_user.id,
            title="Backend Engineer",
            company="Initech",
            location="Remote (US)",
            fingerprint="fp-silent",
            remote=None,
            fit_score=70,
        ),
    }
    db_session.add_all(rows.values())
    db_session.commit()
    for row in rows.values():
        db_session.refresh(row)
    return rows


class TestFeedFilters:
    def _titles(self, response) -> set[str]:
        return {row["title"] for row in response.json()}

    def test_unfiltered_feed_has_everything(self, auth_client, feed):
        assert len(self._titles(auth_client.get(JOBS))) == 3

    def test_min_salary_drops_the_band_below_it(self, auth_client, feed):
        titles = self._titles(auth_client.get(JOBS, params={"min_salary": 150_000}))
        assert "Junior Engineer" not in titles
        assert "Staff Engineer" in titles

    def test_min_salary_keeps_postings_that_publish_nothing(self, auth_client, feed):
        """Same rule as the saved search's floor, for the same reason."""
        titles = self._titles(auth_client.get(JOBS, params={"min_salary": 150_000}))
        assert "Backend Engineer" in titles

    def test_has_salary_is_the_separate_switch(self, auth_client, feed):
        titles = self._titles(auth_client.get(JOBS, params={"has_salary": True}))
        assert titles == {"Staff Engineer", "Junior Engineer"}

    def test_has_salary_false_finds_the_ones_that_said_nothing(self, auth_client, feed):
        assert self._titles(auth_client.get(JOBS, params={"has_salary": False})) == (
            {"Backend Engineer"}
        )

    def test_the_band_is_returned_to_the_client(self, auth_client, feed):
        row = next(
            r for r in auth_client.get(JOBS).json() if r["title"] == "Staff Engineer"
        )
        assert row["salary_min"] == 200_000
        assert row["salary_max"] == 240_000

    def test_location_matches_on_the_posting_text(self, auth_client, feed):
        assert self._titles(auth_client.get(JOBS, params={"location": "austin"})) == (
            {"Junior Engineer"}
        )

    def test_remote_admits_a_location_that_says_remote(self, auth_client, feed):
        """The board's ``remote`` flag is often null on a plainly remote role."""
        assert self._titles(auth_client.get(JOBS, params={"remote": True})) == (
            {"Backend Engineer"}
        )

    def test_remote_false_keeps_the_onsite_roles(self, auth_client, feed):
        titles = self._titles(auth_client.get(JOBS, params={"remote": False}))
        assert titles == {"Staff Engineer", "Junior Engineer"}

    def test_filters_compose(self, auth_client, feed):
        titles = self._titles(
            auth_client.get(
                JOBS, params={"min_salary": 150_000, "has_salary": True, "remote": False}
            )
        )
        assert titles == {"Staff Engineer"}

    def test_a_dismissed_posting_stays_hidden_under_a_filter(
        self, auth_client, db_session, feed
    ):
        feed["rich"].status = JobStatus.DISMISSED
        db_session.commit()

        titles = self._titles(auth_client.get(JOBS, params={"min_salary": 150_000}))
        assert "Staff Engineer" not in titles
