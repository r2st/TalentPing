"""Where the candidate said they'd work, taken seriously.

Two layers, tested separately because they answer different questions:

* the **location dimension** decides how a posting *scores*, and now reads the
  places the candidate named rather than the one their resume happens to list;
* the **location gate** decides whether an application is *sent*, and cannot be
  outvoted by a strong showing on skills or salary.
"""
from __future__ import annotations

import pytest

from app.models.profile import Profile
from app.services import auto_apply_service
from app.services.fit_scorer import Targeting, location_match, score_location
from app.services.jd_parser import ParsedJob


def _job(location=None, *, remote=None, title="Backend Engineer"):
    return ParsedJob(title=title, location=location, remote=remote)


class TestLocationMatch:
    @pytest.mark.parametrize(
        ("posted", "wanted"),
        [
            ("London", ["London"]),
            ("London, UK", ["London"]),
            ("Greater London Area", ["London"]),
            ("Berlin, Germany", ["Munich", "Berlin"]),
            ("San Francisco, CA", ["San Francisco"]),
        ],
    )
    def test_the_same_place_under_three_boards_worth_of_formatting(self, posted, wanted):
        assert location_match(posted, wanted) is not None

    @pytest.mark.parametrize(
        ("posted", "wanted"),
        [
            ("Berlin, Germany", ["London"]),
            ("Austin, TX", ["Boston"]),
            ("Tokyo", ["Toronto"]),
        ],
    )
    def test_different_places_do_not_match(self, posted, wanted):
        assert location_match(posted, wanted) is None

    def test_remote_in_the_wanted_list_is_a_preference_not_a_place(self):
        """"Remote" is an arrangement; matching it against a city means nothing."""
        assert location_match("Remoteville, IA", ["Remote"]) is None


class TestLocationDimension:
    def test_a_stated_location_beats_the_one_on_the_resume(self, resume):
        """The resume says where you live; the profile says where you'll work."""
        assert resume.location == "San Francisco, CA"
        want = Targeting(locations=["London"])

        assert score_location(resume, _job("London, UK"), want).score == 1.0
        # Where they live is no longer where the job has to be.
        assert score_location(resume, _job("San Francisco, CA"), want).score < 0.5

    def test_missing_a_stated_location_is_a_real_miss_not_a_soft_one(self, resume):
        """They told us; a posting that still doesn't qualify isn't ambiguous."""
        stated = score_location(resume, _job("Berlin"), Targeting(locations=["London"]))
        unstated = score_location(resume, _job("Berlin"), Targeting())
        assert stated.score < unstated.score

    def test_remote_fits_everyone(self, resume):
        want = Targeting(locations=["London"])
        assert score_location(resume, _job("Anywhere", remote=True), want).score == 1.0
        assert score_location(resume, _job("Remote"), want).score == 1.0

    def test_remote_only_disqualifies_an_on_site_role(self, resume):
        want = Targeting(remote_only=True)
        dimension = score_location(resume, _job("Berlin", remote=False), want)
        assert dimension.score < 0.1
        assert "remote" in dimension.note.lower()

    def test_no_stated_preference_falls_back_to_the_resume(self, resume):
        """The pre-profile behaviour, unchanged for anyone who never said."""
        assert score_location(resume, _job("San Francisco, CA"), Targeting()).score == 1.0


class TestLocationGate:
    """The last thing standing between a job you can't take and an email."""

    def _posting(self, **kwargs):
        from app.models.job import JobPosting, JobStatus

        defaults = {
            "title": "Backend Engineer",
            "company": "Acme",
            "fingerprint": "x",
            "status": JobStatus.NEW,
        }
        return JobPosting(**{**defaults, **kwargs})

    def test_a_role_outside_your_locations_is_not_emailed_about(self):
        want = Targeting(locations=["Berlin"])
        reason = auto_apply_service.location_gate(want, self._posting(location="Tokyo"))
        assert reason is not None
        assert "Tokyo" in reason and "Berlin" in reason

    def test_a_role_inside_your_locations_goes_through(self):
        want = Targeting(locations=["Berlin"])
        posting = self._posting(location="Berlin, Germany")
        assert auto_apply_service.location_gate(want, posting) is None

    def test_remote_passes_whatever_the_preferences_say(self):
        """A remote job is in everyone's preferred location."""
        want = Targeting(locations=["Berlin"])
        assert (
            auto_apply_service.location_gate(
                want, self._posting(location="Tokyo", remote=True)
            )
            is None
        )
        # Boards that only ever write it in the location field count too.
        assert (
            auto_apply_service.location_gate(want, self._posting(location="Remote"))
            is None
        )

    def test_remote_only_means_remote_only(self):
        want = Targeting(remote_only=True)
        reason = auto_apply_service.location_gate(want, self._posting(location="Berlin"))
        assert reason is not None and "remote" in reason

    def test_a_posting_that_wont_say_where_it_is_is_not_applied_to(self):
        """Unknown is not evidence of acceptable — the same posture as untitled."""
        want = Targeting(locations=["Berlin"])
        assert auto_apply_service.location_gate(want, self._posting()) is not None

    def test_a_candidate_who_named_no_locations_is_not_blocked(self):
        assert (
            auto_apply_service.location_gate(Targeting(), self._posting(location="Tokyo"))
            is None
        )

    def test_the_gate_can_be_switched_off_at_the_server(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "autopilot_enforce_locations", False)
        want = Targeting(locations=["Berlin"])
        assert (
            auto_apply_service.location_gate(want, self._posting(location="Tokyo"))
            is None
        )


class TestGateInThePipeline:
    """The gate has to hold on a real run, not just as a function."""

    def test_an_off_location_job_is_skipped_even_with_a_high_fit_score(
        self, auth_client, connected_gmail, resume, stub_scraper, no_scan, db_session,
        current_user,
    ):
        from app.models.application import Application
        from app.models.job import JobPosting, JobStatus, job_fingerprint

        db_session.add(
            Profile(
                user_id=current_user.id,
                resume_id=resume.id,
                name="Backend",
                target_roles=["Backend Engineer"],
                location_preferences=["Berlin"],
                is_active=True,
                is_default=True,
            )
        )
        db_session.add(
            JobPosting(
                user_id=current_user.id,
                title="Senior Backend Engineer",
                company="Tokyo Labs",
                location="Tokyo, Japan",
                remote=False,
                description="Python, FastAPI, AWS.",
                source="test",
                fingerprint=job_fingerprint("Senior Backend Engineer", "Tokyo Labs", None),
                status=JobStatus.NEW,
                fit_score=95,
            )
        )
        db_session.commit()

        auth_client.put(
            "/api/v1/autopilot",
            json={"is_active": True, "min_fit_score": 70, "daily_application_limit": 10},
        )
        result = auth_client.post("/api/v1/autopilot/run").json()

        assert result["applied"] == 0, result
        assert result["skipped_location"] == 1
        assert db_session.query(Application).count() == 0
        assert any("Tokyo" in note for note in result["notes"])

    def test_the_same_job_marked_remote_goes_out(
        self, auth_client, connected_gmail, resume, stub_scraper, no_scan, db_session,
        current_user,
    ):
        """The exemption has to actually work, or remote candidates get nothing."""
        from app.models.job import JobPosting, JobStatus, job_fingerprint

        db_session.add(
            Profile(
                user_id=current_user.id,
                resume_id=resume.id,
                name="Backend",
                target_roles=["Backend Engineer"],
                location_preferences=["Berlin"],
                is_active=True,
                is_default=True,
            )
        )
        db_session.add(
            JobPosting(
                user_id=current_user.id,
                title="Senior Backend Engineer",
                company="Tokyo Labs",
                location="Tokyo, Japan",
                remote=True,
                description="Python, FastAPI, AWS.",
                source="test",
                fingerprint=job_fingerprint("Senior Backend Engineer", "Tokyo Labs", None),
                status=JobStatus.NEW,
                fit_score=95,
            )
        )
        db_session.commit()

        auth_client.put(
            "/api/v1/autopilot",
            json={"is_active": True, "min_fit_score": 70, "daily_application_limit": 10},
        )
        result = auth_client.post("/api/v1/autopilot/run").json()
        assert result["applied"] == 1, result
        assert result["skipped_location"] == 0
