"""Reading the employer's own board instead of an aggregator's copy of it.

No network: every board here is a stubbed session that returns canned JSON, which
is the only honest way to test five third-party APIs. What is worth pinning is not
that ``requests`` works — it is the discovery logic around it:

* the token is read out of a URL we already hold, for free;
* probing is bounded, and its *negative* answers are cached so the cost is paid
  once per company rather than once per scan;
* a board's exact ``posted_at`` and canonical apply URL survive into the feed,
  because that is the whole reason to prefer a board over an aggregator.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.ats_board import AtsBoard
from app.models.job import JobPosting, JobStatus
from app.models.recruiter_cache import RecruiterCache
from app.services import ats_boards


# --------------------------------------------------------------------------- #
# A fake session                                                               #
# --------------------------------------------------------------------------- #


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Serves canned JSON by URL prefix and records every request made."""

    def __init__(self, routes: dict[str, object] | None = None):
        self.routes = routes or {}
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        for prefix, payload in self.routes.items():
            if url.startswith(prefix):
                return _Response(200, payload)
        return _Response(404, None)


GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 4567,
            "title": "Senior Backend Engineer",
            "location": {"name": "San Francisco, CA"},
            "absolute_url": "https://boards.greenhouse.io/northwindlabs/jobs/4567",
            "content": "<p>Python, FastAPI and PostgreSQL at scale.</p>",
            "updated_at": "2026-07-28T09:15:00-04:00",
        },
        {
            "id": 4568,
            "title": "Staff Platform Engineer",
            "location": {"name": "Remote - US"},
            "absolute_url": "https://boards.greenhouse.io/northwindlabs/jobs/4568",
            "content": "Kubernetes and Terraform.",
            "updated_at": "2026-07-29T12:00:00+00:00",
        },
    ]
}

LEVER_PAYLOAD = [
    {
        "id": "abc-123",
        "text": "Backend Engineer",
        "categories": {"location": "Berlin"},
        "workplaceType": "remote",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "descriptionPlain": "Go and Postgres.",
        "createdAt": 1785000000000,
        "salaryRange": {"min": 90000, "max": 120000, "currency": "EUR"},
    }
]


# --------------------------------------------------------------------------- #
# Token extraction — the free path                                             #
# --------------------------------------------------------------------------- #


class TestBoardIdentity:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://boards.greenhouse.io/northwindlabs/jobs/4567",
                (ats_boards.GREENHOUSE, "northwindlabs"),
            ),
            (
                "https://job-boards.greenhouse.io/acme/jobs/1",
                (ats_boards.GREENHOUSE, "acme"),
            ),
            (
                "https://boards.eu.greenhouse.io/acme/jobs/1",
                (ats_boards.GREENHOUSE, "acme"),
            ),
            (
                "https://boards.greenhouse.io/embed/job_board?for=acme&token=9",
                (ats_boards.GREENHOUSE, "acme"),
            ),
            ("https://jobs.lever.co/acme/uuid-here", (ats_boards.LEVER, "acme")),
            ("https://jobs.eu.lever.co/acme/uuid", (ats_boards.LEVER, "acme")),
            ("https://jobs.ashbyhq.com/acme/uuid", (ats_boards.ASHBY, "acme")),
            (
                "https://apply.workable.com/acme/j/ABC123/",
                (ats_boards.WORKABLE, "acme"),
            ),
            ("https://acme.workable.com/jobs/123", (ats_boards.WORKABLE, "acme")),
            (
                "https://jobs.smartrecruiters.com/Acme/744000",
                (ats_boards.SMARTRECRUITERS, "Acme"),
            ),
            # A bare host, as pasted links often arrive.
            ("jobs.lever.co/acme/uuid", (ats_boards.LEVER, "acme")),
        ],
    )
    def test_a_board_url_yields_its_platform_and_token(self, url, expected):
        assert ats_boards.board_identity(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "not a url",
            "https://example.com/careers/backend-engineer",
            "https://www.linkedin.com/jobs/view/123456789",
            # The host is right but there is no company segment to read.
            "https://boards.greenhouse.io/",
            "https://apply.workable.com/j/ABC123",
            "mailto:careers@acme.com",
        ],
    )
    def test_anything_else_yields_nothing(self, url):
        assert ats_boards.board_identity(url) is None

    def test_a_structural_segment_is_never_read_as_a_company(self):
        """``apply.workable.com/j/…`` names a job, not an employer called "j"."""
        assert ats_boards.board_identity("https://apply.workable.com/j/X1") is None
        assert (
            ats_boards.board_identity("https://apply.workable.com/jobs/X1") is None
        )


# --------------------------------------------------------------------------- #
# The fetchers                                                                 #
# --------------------------------------------------------------------------- #


class TestFetchers:
    def test_greenhouse_gives_an_exact_date_and_a_canonical_url(self):
        """The two things an aggregator cannot give, and the reason for all this."""
        session = FakeSession(
            {"https://boards-api.greenhouse.io/v1/boards/northwindlabs/jobs": GREENHOUSE_PAYLOAD}
        )
        jobs = ats_boards.fetch_greenhouse(
            session, "northwindlabs", "Northwind Labs", 10
        )

        assert len(jobs) == 2
        first = jobs[0]
        assert first.title == "Senior Backend Engineer"
        assert first.company == "Northwind Labs"
        assert first.location == "San Francisco, CA"
        assert first.url == "https://boards.greenhouse.io/northwindlabs/jobs/4567"
        assert first.posted_at == datetime(2026, 7, 28, 13, 15, tzinfo=UTC)
        assert first.external_id == "4567"
        # HTML is stripped — the scorer reads plain text.
        assert "<p>" not in first.description
        assert "FastAPI" in first.description
        # "Remote - US" in the board's own location field counts as remote.
        assert jobs[1].remote is True

    def test_lever_reads_its_structured_workplace_and_salary(self):
        session = FakeSession({"https://api.lever.co/v0/postings/acme": LEVER_PAYLOAD})
        jobs = ats_boards.fetch_lever(session, "acme", "Acme", 10)

        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Backend Engineer"
        assert job.remote is True
        assert job.salary_text == "EUR 90,000 - 120,000"
        assert job.posted_at is not None and job.posted_at.year == 2026

    def test_lever_onsite_is_a_definite_no_not_a_shrug(self):
        """`False` and `None` mean different things to the location gate."""
        session = FakeSession(
            {
                "https://api.lever.co/v0/postings/acme": [
                    {
                        "id": "x",
                        "text": "Onsite Engineer",
                        "categories": {"location": "Berlin"},
                        "workplaceType": "onsite",
                        "hostedUrl": "https://jobs.lever.co/acme/x",
                    }
                ]
            }
        )
        assert ats_boards.fetch_lever(session, "acme", "Acme", 10)[0].remote is False

    def test_ashby_reads_its_remote_flag_and_compensation_summary(self):
        session = FakeSession(
            {
                "https://api.ashbyhq.com/posting-api/job-board/acme": {
                    "jobs": [
                        {
                            "id": "a1",
                            "title": "Data Engineer",
                            "location": "Remote",
                            "isRemote": True,
                            "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
                            "descriptionPlain": "dbt and Snowflake.",
                            "compensation": {"compensationTierSummary": "$150K - $190K"},
                            "publishedAt": "2026-07-20T00:00:00Z",
                        }
                    ]
                }
            }
        )
        job = ats_boards.fetch_ashby(session, "acme", "Acme", 10)[0]
        assert job.remote is True
        assert job.salary_text == "$150K - $190K"
        assert job.posted_at == datetime(2026, 7, 20, tzinfo=UTC)

    def test_workable_assembles_a_location_from_its_parts(self):
        session = FakeSession(
            {
                "https://apply.workable.com/api/v1/widget/accounts/acme": {
                    "jobs": [
                        {
                            "shortcode": "ABC123",
                            "title": "SRE",
                            "city": "Lisbon",
                            "country": "Portugal",
                            "telecommuting": False,
                            "shortlink": "https://apply.workable.com/acme/j/ABC123",
                            "description": "<b>On call</b> rotation.",
                            "published_on": "2026-07-25",
                        }
                    ]
                }
            }
        )
        job = ats_boards.fetch_workable(session, "acme", "Acme", 10)[0]
        assert job.location == "Lisbon, Portugal"
        assert job.remote is None  # telecommuting False, location silent
        assert job.description == "On call rotation."

    def test_smartrecruiters_builds_an_apply_url_when_none_is_given(self):
        session = FakeSession(
            {
                "https://api.smartrecruiters.com/v1/companies/Acme/postings": {
                    "content": [
                        {
                            "id": "744000",
                            "name": "Product Manager",
                            "location": {"city": "London", "country": "uk"},
                            "releasedDate": "2026-07-27T10:00:00.000Z",
                        }
                    ]
                }
            }
        )
        job = ats_boards.fetch_smartrecruiters(session, "Acme", "Acme", 10)[0]
        assert job.url == "https://jobs.smartrecruiters.com/Acme/744000"
        assert job.location == "London, uk"
        assert job.posted_at == datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    def test_a_404_is_a_normal_answer_not_an_error(self):
        """"Does this company use Greenhouse?" is usually answered no."""
        session = FakeSession()
        assert ats_boards.fetch_greenhouse(session, "nope", "Nope", 10) == []
        assert ats_boards.fetch_lever(session, "nope", "Nope", 10) == []
        assert ats_boards.fetch_ashby(session, "nope", "Nope", 10) == []
        assert ats_boards.fetch_workable(session, "nope", "Nope", 10) == []
        assert ats_boards.fetch_smartrecruiters(session, "nope", "Nope", 10) == []

    def test_a_broken_board_never_raises_into_a_scan(self):
        class Exploding:
            headers: dict[str, str] = {}

            def get(self, *args, **kwargs):
                raise RuntimeError("board is down")

        assert (
            ats_boards.fetch_board(
                Exploding(), ats_boards.GREENHOUSE, "acme", "Acme", limit=5
            )
            == []
        )

    def test_the_limit_is_honoured(self):
        session = FakeSession(
            {"https://boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE_PAYLOAD}
        )
        assert len(ats_boards.fetch_greenhouse(session, "acme", "Acme", 1)) == 1


# --------------------------------------------------------------------------- #
# Discovery: cheapest route first, and remember everything                     #
# --------------------------------------------------------------------------- #


def _posting(db_session, current_user, company, url, **kwargs):
    row = JobPosting(
        user_id=current_user.id,
        title=kwargs.pop("title", "Backend Engineer"),
        company=company,
        url=url,
        fingerprint=kwargs.pop("fingerprint", f"fp-{company}-{url}"[:60]),
        status=JobStatus.NEW,
        **kwargs,
    )
    db_session.add(row)
    db_session.commit()
    return row


class TestResolveBoard:
    def test_a_token_in_a_url_we_already_have_costs_no_requests(
        self, db_session, current_user
    ):
        """The wiring that makes this feature nearly free."""
        _posting(
            db_session,
            current_user,
            "Northwind Labs",
            "https://boards.greenhouse.io/northwindlabs/jobs/4567",
        )
        session = FakeSession()

        lookup = ats_boards.resolve_board(
            db_session, current_user, "Northwind Labs", session=session
        )

        assert lookup.found is True
        assert lookup.platform == ats_boards.GREENHOUSE
        assert lookup.token == "northwindlabs"
        assert lookup.probed is False
        assert session.calls == [], "no request should have been made"

    def test_a_cached_careers_url_is_read_too(self, db_session, current_user):
        """`career_scraper` already found and cached this page for someone."""
        db_session.add(
            RecruiterCache(
                company="Acme",
                domain="acme.com",
                careers_url="https://jobs.lever.co/acme",
                status="ok",
                scraped_at=datetime.now(UTC),
            )
        )
        db_session.commit()
        session = FakeSession()

        lookup = ats_boards.resolve_board(
            db_session, current_user, "Acme", session=session
        )
        assert lookup.platform == ats_boards.LEVER
        assert lookup.token == "acme"
        assert session.calls == []

    def test_a_source_url_from_a_deduped_copy_is_read(self, db_session, current_user):
        """The canonical row keeps every board it was seen on; each is a candidate."""
        _posting(
            db_session,
            current_user,
            "Acme",
            "https://remoteok.com/l/123",
            source_urls=[
                {"source": "lever", "url": "https://jobs.lever.co/acme/uuid"}
            ],
        )
        session = FakeSession()

        lookup = ats_boards.resolve_board(
            db_session, current_user, "Acme", session=session
        )
        assert (lookup.platform, lookup.token) == (ats_boards.LEVER, "acme")

    def test_a_similar_company_name_is_not_mistaken_for_this_one(
        self, db_session, current_user
    ):
        """"Acme" and "Acme Health" are different employers with different boards."""
        _posting(
            db_session,
            current_user,
            "Acme Health",
            "https://jobs.lever.co/acmehealth/uuid",
        )
        lookup = ats_boards.resolve_board(
            db_session, current_user, "Acme", session=FakeSession(), allow_probe=False
        )
        assert lookup.found is False

    def test_with_nothing_on_file_the_slug_is_probed(self, db_session, current_user):
        session = FakeSession(
            {"https://boards-api.greenhouse.io/v1/boards/northwindlabs/jobs": GREENHOUSE_PAYLOAD}
        )
        lookup = ats_boards.resolve_board(
            db_session, current_user, "Northwind Labs", session=session
        )

        assert lookup.found is True
        assert lookup.probed is True
        assert session.calls, "a probe should have made requests"

    def test_a_probe_that_finds_nothing_is_cached_as_nothing(
        self, db_session, current_user
    ):
        """Otherwise every scan re-probes five platforms for every self-hoster."""
        session = FakeSession()
        first = ats_boards.resolve_board(
            db_session, current_user, "Selfhosted Inc", session=session
        )
        assert first.found is False
        assert first.probed is True
        spent = len(session.calls)
        assert spent > 0

        second = ats_boards.resolve_board(
            db_session, current_user, "Selfhosted Inc", session=session
        )
        assert second.found is False
        assert second.cached is True
        assert second.probed is False
        assert len(session.calls) == spent, "the second look must be free"

    def test_allow_probe_false_never_touches_the_network(
        self, db_session, current_user
    ):
        session = FakeSession()
        lookup = ats_boards.resolve_board(
            db_session, current_user, "Unknown Co", session=session, allow_probe=False
        )
        assert lookup.found is False
        assert session.calls == []

    def test_not_checking_is_not_cached_as_not_found(self, db_session, current_user):
        """"We had no budget" must not become "this company has no board"."""
        ats_boards.resolve_board(
            db_session,
            current_user,
            "Unknown Co",
            session=FakeSession(),
            allow_probe=False,
        )
        assert (
            db_session.query(AtsBoard).filter_by(normalized_name="unknown").count() == 0
        )

    def test_a_found_board_is_cached_with_a_readable_url(
        self, db_session, current_user
    ):
        _posting(
            db_session, current_user, "Acme", "https://jobs.lever.co/acme/uuid"
        )
        ats_boards.resolve_board(
            db_session, current_user, "Acme", session=FakeSession()
        )

        row = db_session.query(AtsBoard).filter_by(normalized_name="acme").one()
        assert row.platform == ats_boards.LEVER
        assert row.board_token == "acme"
        assert row.board_url == "https://jobs.lever.co/acme"
        assert row.status == ats_boards.STATUS_OK
        assert row.checked_at is not None

    def test_the_cache_is_shared_across_users(self, db_session, current_user):
        """A board token is a fact about the company, not about who is looking."""
        from app.models.user import User
        from app.core.security import hash_password

        other = User(
            email="second@example.com", hashed_password=hash_password("pw12345678")
        )
        db_session.add(other)
        _posting(
            db_session, current_user, "Acme", "https://jobs.lever.co/acme/uuid"
        )
        ats_boards.resolve_board(
            db_session, current_user, "Acme", session=FakeSession()
        )

        session = FakeSession()
        lookup = ats_boards.resolve_board(db_session, other, "Acme", session=session)
        assert lookup.token == "acme"
        assert lookup.cached is True
        assert session.calls == []

    def test_a_stale_row_is_re_derived(self, db_session, current_user):
        """Companies do migrate ATS vendors; a dead token must be able to heal."""
        from datetime import timedelta

        db_session.add(
            AtsBoard(
                company="Acme",
                normalized_name="acme",
                platform=ats_boards.LEVER,
                board_token="oldtoken",
                status=ats_boards.STATUS_OK,
                checked_at=datetime.now(UTC) - timedelta(days=365),
            )
        )
        _posting(
            db_session, current_user, "Acme", "https://jobs.ashbyhq.com/acme/uuid"
        )
        db_session.commit()

        lookup = ats_boards.resolve_board(
            db_session, current_user, "Acme", session=FakeSession()
        )
        assert lookup.platform == ats_boards.ASHBY
        assert lookup.token == "acme"


# --------------------------------------------------------------------------- #
# The sweep                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def boards_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ats_board_discovery_enabled", True)
    return settings


class TestSweep:
    def test_the_sweep_reads_boards_for_companies_in_the_feed(
        self, db_session, current_user, boards_on
    ):
        _posting(
            db_session,
            current_user,
            "Northwind Labs",
            "https://boards.greenhouse.io/northwindlabs/jobs/1",
            fit_score=88.0,
        )
        session = FakeSession(
            {"https://boards-api.greenhouse.io/v1/boards/northwindlabs/jobs": GREENHOUSE_PAYLOAD}
        )

        scan = ats_boards.sweep(db_session, current_user, session=session)

        assert scan.boards_found == 1
        assert len(scan.jobs) == 2
        assert {j.title for j in scan.jobs} == {
            "Senior Backend Engineer",
            "Staff Platform Engineer",
        }

    def test_the_switch_off_means_nothing_happens(
        self, db_session, current_user, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "ats_board_discovery_enabled", False)
        _posting(
            db_session, current_user, "Acme", "https://jobs.lever.co/acme/uuid"
        )
        session = FakeSession()

        scan = ats_boards.sweep(db_session, current_user, session=session)
        assert scan.jobs == []
        assert session.calls == []

    def test_probing_is_metered_separately_from_reading(
        self, db_session, current_user, boards_on, monkeypatch
    ):
        """A known token is cheap; guessing one is not, so only guessing is capped."""
        monkeypatch.setattr(boards_on, "ats_board_probe_limit", 1)
        # Two companies with a known token, two with nothing on file.
        for n in (1, 2):
            _posting(
                db_session,
                current_user,
                f"Known {n}",
                f"https://jobs.lever.co/known{n}/uuid",
                fit_score=90.0,
                fingerprint=f"fp-known-{n}",
            )
        for n in (1, 2):
            _posting(
                db_session,
                current_user,
                f"Mystery {n}",
                "https://example.com/job",
                fit_score=50.0,
                fingerprint=f"fp-mystery-{n}",
            )
        session = FakeSession(
            {
                "https://api.lever.co/v0/postings/known1": LEVER_PAYLOAD,
                "https://api.lever.co/v0/postings/known2": LEVER_PAYLOAD,
            }
        )

        scan = ats_boards.sweep(db_session, current_user, session=session)

        assert scan.companies_checked == 4
        assert scan.boards_found == 2, "both known boards should still be read"
        assert scan.probes_spent == 1, "only one company may be guessed at"

    def test_the_best_prospects_are_swept_first(
        self, db_session, current_user, boards_on, monkeypatch
    ):
        """A bounded sweep should spend itself where the candidate is looking."""
        monkeypatch.setattr(boards_on, "ats_board_companies_per_scan", 2)
        for n, score in ((1, 20.0), (2, 95.0), (3, 60.0)):
            _posting(
                db_session,
                current_user,
                f"Company {n}",
                f"https://jobs.lever.co/company{n}/uuid",
                fit_score=score,
                fingerprint=f"fp-{n}",
            )

        companies = ats_boards.companies_to_sweep(db_session, current_user, 2)
        assert companies == ["Company 2", "Company 3"]

    def test_a_reachable_board_that_returns_nothing_is_noted(
        self, db_session, current_user, boards_on
    ):
        _posting(
            db_session, current_user, "Acme", "https://jobs.lever.co/acme/uuid"
        )
        session = FakeSession({"https://api.lever.co/v0/postings/acme": []})

        scan = ats_boards.sweep(db_session, current_user, session=session)
        assert scan.jobs == []
        assert any("returned nothing" in n for n in scan.notes)


# --------------------------------------------------------------------------- #
# Into the feed                                                                #
# --------------------------------------------------------------------------- #


class TestIntoTheScan:
    def test_a_board_posting_reaches_the_feed_with_its_exact_date(
        self, db_session, current_user, resume, boards_on, monkeypatch
    ):
        """End to end: the board's date and canonical URL survive the scan."""
        from app.models.job import JobSearch
        from app.services import job_search_service

        _posting(
            db_session,
            current_user,
            "Northwind Labs",
            "https://boards.greenhouse.io/northwindlabs/jobs/999",
            title="Something Else Entirely",
            fingerprint="fp-seed",
            fit_score=80.0,
        )
        search = JobSearch(
            user_id=current_user.id,
            name="Backend",
            roles=["Senior Backend Engineer"],
            min_fit_score=0,
        )
        db_session.add(search)
        db_session.commit()

        # No aggregators, and the board sweep on a stubbed session.
        monkeypatch.setattr(job_search_service, "discover", lambda query: [])
        monkeypatch.setattr(
            ats_boards,
            "_session",
            lambda: FakeSession(
                {
                    "https://boards-api.greenhouse.io/v1/boards/northwindlabs/jobs": GREENHOUSE_PAYLOAD
                }
            ),
        )

        result = job_search_service.run_search(db_session, search)

        assert result.from_boards == 2
        stored = (
            db_session.query(JobPosting)
            .filter_by(title="Senior Backend Engineer")
            .one()
        )
        assert stored.source == "board:greenhouse"
        assert stored.posted_at is not None
        assert stored.url == "https://boards.greenhouse.io/northwindlabs/jobs/4567"

    def test_a_dead_sweep_costs_the_boards_and_nothing_else(
        self, db_session, current_user, resume, boards_on, monkeypatch
    ):
        """The aggregators have already returned by the time this runs."""
        from app.models.job import JobSearch
        from app.services import job_search_service

        def _explode(*args, **kwargs):
            raise RuntimeError("boards are down")

        monkeypatch.setattr(ats_boards, "sweep", _explode)
        monkeypatch.setattr(
            job_search_service,
            "discover",
            lambda query: [
                job_search_service.RawJob(
                    title="Senior Backend Engineer",
                    company="Acme",
                    location="Remote",
                    url="https://example.com/1",
                    source="remoteok",
                )
            ],
        )
        search = JobSearch(
            user_id=current_user.id,
            name="Backend",
            roles=["Senior Backend Engineer"],
            min_fit_score=0,
        )
        db_session.add(search)
        db_session.commit()

        result = job_search_service.run_search(db_session, search)
        assert result.from_boards == 0
        assert result.added == 1

    def test_a_board_copy_wins_over_an_aggregators_copy(
        self, db_session, current_user, resume, boards_on, monkeypatch
    ):
        """Same role, two sources: keep the one with the real date and apply URL."""
        from app.models.job import JobSearch
        from app.services import job_search_service

        monkeypatch.setattr(
            job_search_service,
            "discover",
            lambda query: [
                job_search_service.RawJob(
                    title="Senior Backend Engineer",
                    company="Northwind Labs",
                    location="San Francisco, CA",
                    url="https://aggregator.example/redirect?id=9",
                    source="remoteok",
                )
            ],
        )
        monkeypatch.setattr(
            ats_boards,
            "sweep",
            lambda db, user, companies=None, session=None: ats_boards.BoardScan(
                jobs=[
                    ats_boards.BoardJob(
                        title="Senior Backend Engineer",
                        company="Northwind Labs",
                        location="San Francisco, CA",
                        url="https://boards.greenhouse.io/northwindlabs/jobs/4567",
                        description="Python, FastAPI and PostgreSQL at scale.",
                        posted_at=datetime(2026, 7, 28, tzinfo=UTC),
                        platform=ats_boards.GREENHOUSE,
                    )
                ],
                boards_found=1,
            ),
        )
        search = JobSearch(
            user_id=current_user.id,
            name="Backend",
            roles=["Senior Backend Engineer"],
            min_fit_score=0,
        )
        db_session.add(search)
        db_session.commit()

        job_search_service.run_search(db_session, search)

        canonical = (
            db_session.query(JobPosting)
            .filter(JobPosting.duplicate_of_id.is_(None))
            .filter_by(title="Senior Backend Engineer")
            .one()
        )
        assert canonical.source == "board:greenhouse"
        # SQLite round-trips datetimes naive; the date is the point.
        assert canonical.posted_at.replace(tzinfo=UTC) == datetime(
            2026, 7, 28, tzinfo=UTC
        )
