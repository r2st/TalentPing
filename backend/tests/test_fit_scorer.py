"""Fit scoring: the weights, each dimension, and the determinism guarantee."""
from __future__ import annotations

import pytest

from app.services import fit_scorer
from app.services.fit_scorer import (
    NEUTRAL,
    WEIGHTS,
    recommendation_for,
    score_experience,
    score_fit,
    score_industry,
    score_location,
    score_role,
    score_salary,
    score_skills,
)
from app.services.jd_parser import ParsedJob, parse_heuristic


def _job(**overrides) -> ParsedJob:
    return ParsedJob(**overrides)


class TestWeights:
    def test_sum_to_one(self):
        """A weight table that doesn't sum to 1 silently caps the max score."""
        assert sum(WEIGHTS.values()) == pytest.approx(1.0)

    def test_skills_dominate(self, ):
        assert WEIGHTS["skills"] == max(WEIGHTS.values())


class TestSkillsDimension:
    def test_full_overlap_scores_top(self, resume):
        dim, matched, missing = score_skills(resume, _job(required_skills=["python", "aws"]))
        assert dim.score == 1.0
        assert missing == []
        assert set(matched) == {"python", "aws"}

    def test_no_overlap_scores_zero(self, resume):
        dim, _matched, missing = score_skills(resume, _job(required_skills=["cobol", "fortran"]))
        assert dim.score == 0.0
        assert set(missing) == {"cobol", "fortran"}

    def test_preferred_skills_count_less_than_required(self, resume):
        required_only = score_skills(resume, _job(required_skills=["python"]))[0].score
        with_a_missed_preference = score_skills(
            resume, _job(required_skills=["python"], preferred_skills=["cobol"])
        )[0].score
        # Missing a nice-to-have costs something, but far less than missing a must.
        assert with_a_missed_preference < required_only
        assert with_a_missed_preference > 0.7

    def test_a_posting_with_no_skills_is_neutral(self, resume):
        dim, _m, _mi = score_skills(resume, _job())
        assert dim.score == NEUTRAL

    def test_note_reports_the_shortfall(self, resume):
        dim, _m, _mi = score_skills(resume, _job(required_skills=["python", "cobol"]))
        assert "1 of 2" in dim.note
        assert "cobol" in dim.note


class TestExperienceDimension:
    def test_meeting_the_bar_scores_top(self, resume):
        # Fixture resume: 8 years, senior.
        assert score_experience(resume, _job(years_required=6, seniority="senior")).score == 1.0

    def test_falling_short_is_penalised_by_the_gap(self, resume):
        small_gap = score_experience(resume, _job(years_required=10)).score
        big_gap = score_experience(resume, _job(years_required=16)).score
        assert big_gap < small_gap < 1.0

    def test_overqualification_is_penalised_gently(self, resume):
        """A senior applying to a mid role is a choice, not a mismatch."""
        under = score_experience(resume, _job(seniority="exec")).score
        over = score_experience(resume, _job(seniority="junior")).score
        assert over > under

    def test_silent_posting_is_neutral(self, resume):
        assert score_experience(resume, _job()).score == NEUTRAL


class TestLocationDimension:
    def test_remote_fits_everyone(self, resume):
        assert score_location(resume, _job(remote=True, location="Berlin")).score == 1.0

    def test_same_city_scores_top(self, resume):
        assert score_location(resume, _job(location="San Francisco, CA")).score == 1.0

    def test_onsite_elsewhere_is_heavily_penalised(self, resume):
        dim = score_location(resume, _job(location="Berlin, Germany", remote=False))
        assert dim.score < 0.3

    def test_unstated_location_is_neutral(self, resume):
        assert score_location(resume, _job()).score == NEUTRAL


class TestSalaryDimension:
    def test_no_published_band_is_neutral(self, resume):
        assert score_salary(resume, _job()).score == NEUTRAL

    def test_a_band_at_the_level_scores_top(self, resume):
        dim = score_salary(
            resume, _job(salary_min=160000, salary_max=200000, salary_text="$160k-$200k")
        )
        assert dim.score == 1.0

    def test_a_low_band_is_flagged(self, resume):
        dim = score_salary(
            resume, _job(salary_min=40000, salary_max=55000, salary_text="$40k-$55k")
        )
        assert dim.score < 0.6
        assert "low" in dim.note


class TestIndustryDimension:
    def test_a_target_industry_scores_top(self, resume):
        # The fixture resume targets fintech.
        assert score_industry(resume, _job(industry="fintech")).score == 1.0

    def test_an_untargeted_industry_scores_low(self, resume):
        assert score_industry(resume, _job(industry="gaming")).score < 0.6

    def test_no_industry_signal_is_neutral(self, resume):
        assert score_industry(resume, _job()).score == NEUTRAL


class TestRecommendation:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [(95, "strong"), (80, "strong"), (70, "good"), (50, "stretch"), (20, "poor")],
    )
    def test_buckets(self, score, expected):
        assert recommendation_for(score) == expected


class TestScoreFit:
    def test_a_strong_match_scores_high(self, resume, job_description):
        result = score_fit(resume, parse_heuristic(job_description))
        assert result.overall >= 70
        assert result.recommendation in {"strong", "good"}
        assert result.summary

    def test_a_bad_match_scores_low(self, resume):
        job = _job(
            title="Senior Oncology Nurse",
            required_skills=["phlebotomy", "triage", "patient care"],
            seniority="senior",
            location="Reykjavik, Iceland",
            remote=False,
            industry="healthcare",
        )
        result = score_fit(resume, job)
        assert result.overall < 45
        assert result.recommendation == "poor"

    def test_is_deterministic(self, resume, job_description):
        """The same inputs must always produce the same number — the UI caches it."""
        job = parse_heuristic(job_description)
        scores = {score_fit(resume, job, explain=False).overall for _ in range(5)}
        assert len(scores) == 1

    def test_stays_within_bounds(self, resume, job_description):
        result = score_fit(resume, parse_heuristic(job_description))
        assert 0.0 <= result.overall <= 100.0

    def test_every_dimension_is_explained(self, resume, job_description):
        notes = score_fit(resume, parse_heuristic(job_description)).notes()
        assert set(notes) == set(WEIGHTS)
        assert all(note for note in notes.values())

    def test_summary_never_contradicts_the_score(self, resume, job_description, monkeypatch):
        """An LLM that miscounts must not change the number the user acts on."""
        monkeypatch.setattr(
            fit_scorer.settings, "openrouter_api_key", "test-key", raising=False
        )
        monkeypatch.setattr(
            fit_scorer, "chat_completion", lambda *a, **kw: '{"summary": "Great fit."}'
        )
        job = parse_heuristic(job_description)
        deterministic = score_fit(resume, job, explain=False).overall

        result = score_fit(resume, job, explain=True)
        assert result.overall == deterministic
        assert result.summary == "Great fit."

    def test_falls_back_to_a_template_summary_on_llm_failure(
        self, resume, job_description, monkeypatch
    ):
        monkeypatch.setattr(
            fit_scorer.settings, "openrouter_api_key", "test-key", raising=False
        )

        def _fail(*args, **kwargs):
            raise fit_scorer.OpenRouterError("upstream down")

        monkeypatch.setattr(fit_scorer, "chat_completion", _fail)
        result = score_fit(resume, parse_heuristic(job_description))
        assert result.summary


class TestRoleDimension:
    """The dimension that asks whether this is even the candidate's line of work.

    Its absence is what let irrelevant employers into the send queue: every other
    dimension goes neutral on a thin posting, so a posting nothing was known
    about scored 70 — exactly the default autopilot threshold.
    """

    def test_the_target_role_scores_top(self, resume):
        assert score_role(resume, _job(title="Senior Backend Engineer")).score == 1.0

    def test_seniority_noise_does_not_change_the_role(self, resume):
        """Level is score_experience's question; this dimension only asks "what"."""
        for title in (
            "Backend Engineer",
            "Staff Backend Engineer",
            "Junior Backend Engineer",
            "Backend Engineer (Remote, f/m/d)",
            "Backend Engineer - Full Time",
        ):
            assert score_role(resume, _job(title=title)).score == 1.0, title

    def test_an_adjacent_role_scores_between(self, resume):
        """Same craft, different specialism — worth surfacing, not a perfect match."""
        dim = score_role(resume, _job(title="Python Developer"))
        assert 0.3 < dim.score < 1.0

    def test_an_unrelated_role_scores_near_zero(self, resume):
        """A mismatch is evidence, not an absence of it — so it is not NEUTRAL."""
        for title in ("Registered Nurse", "Class A Truck Driver", "Marketing Manager"):
            dim = score_role(resume, _job(title=title))
            assert dim.score < 0.1, title
            assert dim.score < NEUTRAL

    def test_no_targets_on_file_is_neutral_and_flagged_unknown(self, resume):
        resume.target_roles = []
        resume.experience = []
        resume.headline = None
        dim = score_role(resume, _job(title="Registered Nurse"))
        assert dim.score == NEUTRAL
        assert dim.known is False

    def test_an_untitled_posting_is_unknown(self, resume):
        dim = score_role(resume, _job(title=None))
        assert dim.known is False

    def test_falls_back_to_work_history_when_no_target_is_stated(self, resume):
        resume.target_roles = []
        dim = score_role(resume, _job(title="Backend Engineer"))
        assert dim.score == 1.0
        assert dim.known is True


class TestTitleRelevance:
    @pytest.mark.parametrize(
        ("job_title", "target", "expected"),
        [
            ("Senior Backend Engineer", "Backend Engineer", 1.0),
            ("Backend Engineer, Payments", "Backend Engineer", 1.0),
            ("Sr. Backend Developer", "Backend Engineer", 1.0),
            ("DevOps Engineer", "Infrastructure Engineer", 1.0),
            ("Python Developer", "Backend Engineer", 0.65),
            ("Registered Nurse", "Backend Engineer", 0.0),
            ("Warehouse Associate", "Backend Engineer", 0.0),
        ],
    )
    def test_bands(self, job_title, target, expected):
        assert fit_scorer.title_relevance(job_title, target) == expected

    def test_is_symmetric_enough_to_be_predictable(self):
        assert fit_scorer.title_relevance("Backend Engineer", "Registered Nurse") == 0.0

    def test_empty_inputs_score_zero(self):
        assert fit_scorer.title_relevance(None, "Backend Engineer") == 0.0
        assert fit_scorer.title_relevance("Backend Engineer", "") == 0.0


class TestIrrelevantPostingsDoNotClearTheBar:
    """The regression this dimension exists for.

    A posting nothing can be read off used to score exactly 70.0 — the default
    ``min_fit_score`` an autopilot user applies at — because every dimension fell
    back to NEUTRAL (0.7). Tagged remote it reached 74.5 and read as "good".
    """

    def _thin(self, title, company, **kw):
        return _job(title=title, company=company, **kw)

    def test_a_thin_unrelated_remote_posting_no_longer_clears_seventy(self, resume):
        result = score_fit(
            resume,
            self._thin("Remote Registered Nurse", "Mercy Health", remote=True),
            explain=False,
        )
        assert result.overall < 70, "an unrelated posting must not reach the apply bar"
        assert result.recommendation != "good"

    def test_a_thin_posting_teaches_nothing_and_is_capped(self, resume):
        """No skills, no placeable title: an *unknown* posting, not a good one."""
        resume.target_roles = []
        resume.experience = []
        resume.headline = None
        result = score_fit(
            resume, self._thin("Registered Nurse", "Mercy", remote=True), explain=False
        )
        assert result.capped is True
        assert result.overall <= fit_scorer.UNEVALUATED_CAP
        assert result.overall < 70

    def test_the_cap_says_so_rather_than_claiming_a_verdict(self, resume):
        resume.target_roles = []
        resume.experience = []
        resume.headline = None
        result = score_fit(resume, self._thin("Whatever", "Somewhere"), explain=False)
        assert "judge" in result.summary.lower() or "enough" in result.summary.lower()

    def test_a_thin_but_on_target_posting_still_scores_well(self, resume):
        """The fix must not cost real matches — the feed still has to work."""
        result = score_fit(
            resume,
            self._thin("Senior Backend Engineer", "Stripe", remote=True),
            explain=False,
        )
        assert result.overall >= 70
        assert result.capped is False

    def test_remote_no_longer_rescues_an_unrelated_role(self, resume):
        """Remote used to push location to 1.0 and carry the whole score over."""
        onsite = score_fit(
            resume, self._thin("Registered Nurse", "Mercy", remote=False), explain=False
        )
        remote = score_fit(
            resume, self._thin("Registered Nurse", "Mercy", remote=True), explain=False
        )
        assert remote.overall < 70
        assert onsite.overall < 70


class TestStatedPreferencesOutrankTheResume:
    """A resume is evidence about the past; a profile is a claim about the next job."""

    def test_a_stated_salary_floor_beats_one_inferred_from_seniority(self, resume):
        from app.services.fit_scorer import Targeting, score_salary

        job = ParsedJob(title="Backend Engineer", salary_min=95_000, salary_max=95_000,
                        salary_text="$95,000")
        # A senior candidate's inferred floor is 110k, so this band looks low...
        assert score_salary(resume, job).score < 1.0
        # ...but they said 90k, and their own number is the one that counts.
        stated = score_salary(resume, job, Targeting(salary_min=90_000))
        assert stated.score == 1.0
        assert "90,000" in stated.note

    def test_a_band_under_the_stated_floor_is_scored_on_how_far_short(self, resume):
        from app.services.fit_scorer import Targeting, score_salary

        want = Targeting(salary_min=100_000)
        close = ParsedJob(title="x", salary_max=95_000, salary_text="$95,000")
        far = ParsedJob(title="x", salary_max=40_000, salary_text="$40,000")
        assert score_salary(resume, close, want).score > score_salary(resume, far, want).score

    def test_a_profiles_roles_replace_the_resumes(self, resume):
        """Two profiles on one resume must be able to disagree about a posting.

        Merging the profile's roles with the resume's would leave both profiles
        inheriting "Senior Backend Engineer", scoring identically on everything,
        and "which profile matched?" meaning nothing.
        """
        from app.services.fit_scorer import Targeting, role_targets, score_role

        assert resume.target_roles == ["Senior Backend Engineer"]
        want = Targeting(roles=["DevOps Engineer"])

        assert role_targets(resume, want) == ["DevOps Engineer"]
        assert score_role(resume, ParsedJob(title="DevOps Engineer"), want).score == 1.0
        # The resume's own title no longer scores as an exact target — it is
        # merely adjacent, on the shared "engineer" head noun.
        assert score_role(resume, ParsedJob(title="Backend Engineer"), want).score < 1.0

    def test_the_resume_still_answers_when_the_profile_states_no_roles(self, resume):
        from app.services.fit_scorer import Targeting, role_targets

        assert "Senior Backend Engineer" in role_targets(resume, Targeting())

    def test_a_profiles_level_is_what_gets_matched(self, resume):
        from app.services.fit_scorer import Targeting, score_experience

        """Someone stepping up has a senior resume and a lead intent."""
        assert resume.seniority == "senior"
        job = ParsedJob(title="Engineering Lead", seniority="lead")
        stepping_up = score_experience(resume, job, Targeting(seniority="lead"))
        assert stepping_up.score > score_experience(resume, job).score

    def test_a_profiles_skills_count_towards_the_match(self, resume):
        from app.services.fit_scorer import Targeting, score_skills

        job = ParsedJob(title="x", required_skills=["terraform"])
        assert score_skills(resume, job)[0].score < 1.0
        want = Targeting(skills=["terraform"])
        assert score_skills(resume, job, want)[0].score == 1.0

    def test_the_overall_score_reflects_the_stated_locations(self, resume):
        from app.services.fit_scorer import Targeting, score_fit

        job = ParsedJob(title="Senior Backend Engineer", location="Berlin, Germany",
                        remote=False)
        wants_berlin = score_fit(resume, job, targeting=Targeting(locations=["Berlin"]),
                                 explain=False)
        wants_london = score_fit(resume, job, targeting=Targeting(locations=["London"]),
                                 explain=False)
        assert wants_berlin.overall > wants_london.overall
