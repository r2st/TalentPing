"""Resume → preference suggestions: the mapper, and the endpoint that serves it.

The wizard pre-fills step 3 from these, so the contract that matters is: values
the resume supports, *nothing* where it doesn't, and a source for every value so
the UI can say what it filled in.
"""
from __future__ import annotations

import pytest

from app.models.resume import Resume
from app.services.preference_suggester import (
    DEFAULT_AUTO_SEND,
    DEFAULT_DAILY_APPLICATION_LIMIT,
    DEFAULT_MIN_FIT_SCORE,
    canonical_industry,
    extract_profile,
    merge_suggestions,
    suggest_industries,
    suggest_locations,
    suggest_preferences,
    suggest_roles,
    suggest_salary_min,
)
from app.services.resume_parser import extract_salary_expectation


def _resume(**overrides) -> Resume:
    """An unsaved Resume with just the fields the suggester reads."""
    fields = {
        "target_roles": [],
        "target_industries": [],
        "skills": [],
        "location": None,
        "headline": None,
        "raw_text": "",
        "experience": [],
        "years_experience": None,
        "seniority": None,
        "is_default": False,
    }
    fields.update(overrides)
    return Resume(**fields)


class TestRoleSuggestion:
    def test_stated_roles_win(self):
        roles, source = suggest_roles(
            ["Staff Engineer"], "Senior Backend Engineer", [{"title": "Intern"}]
        )
        assert (roles, source) == (["Staff Engineer"], "resume")

    def test_falls_back_to_job_titles_when_no_roles_were_parsed(self):
        roles, source = suggest_roles(
            [],
            None,
            [
                {"title": "Staff Engineer", "start": "2021", "end": "Present"},
                {"title": "Senior Backend Engineer", "start": "2018", "end": "2021"},
            ],
        )
        assert source == "inferred"
        assert roles == ["Staff Engineer", "Senior Backend Engineer"]

    def test_the_current_job_leads_even_when_listed_last(self):
        """Some resumes run oldest-first; 'Present' still means most recent."""
        roles, _ = suggest_roles(
            [],
            None,
            [
                {"title": "Junior Developer", "start": "2015", "end": "2018"},
                {"title": "Staff Engineer", "start": "2021", "end": "Present"},
                {"title": "Senior Engineer", "start": "2018", "end": "2021"},
            ],
        )
        assert roles == ["Staff Engineer", "Senior Engineer", "Junior Developer"]

    def test_undated_jobs_keep_their_resume_order(self):
        roles, _ = suggest_roles(
            [], None, [{"title": "Staff Engineer"}, {"title": "Backend Engineer"}]
        )
        assert roles == ["Staff Engineer", "Backend Engineer"]

    def test_repeated_titles_are_listed_once(self):
        roles, _ = suggest_roles(
            [],
            None,
            [
                {"title": "Backend Engineer", "end": "2023"},
                {"title": "backend  engineer", "end": "2020"},
            ],
        )
        assert roles == ["Backend Engineer"]

    def test_headline_is_the_last_resort(self):
        roles, source = suggest_roles([], "Senior Backend Engineer", [])
        assert (roles, source) == (["Senior Backend Engineer"], "inferred")

    def test_headline_backs_up_a_titleless_experience_section(self):
        roles, _ = suggest_roles([], "Data Engineer", [{"company": "Acme"}])
        assert roles == ["Data Engineer"]

    def test_junk_titles_are_dropped(self):
        roles, _ = suggest_roles(
            [],
            None,
            [
                {"title": "  "},
                {"title": "-"},
                {"title": None},
                {"title": "x" * 80},
                {"title": "• Staff Engineer —"},
            ],
        )
        assert roles == ["Staff Engineer"]

    def test_nothing_to_go_on_suggests_nothing(self):
        assert suggest_roles([], None, []) == ([], "inferred")
        assert suggest_roles(None, None, None) == ([], "inferred")

    def test_caps_inferred_roles(self):
        roles, _ = suggest_roles(
            [], None, [{"title": f"Engineer {n}"} for n in range(9)]
        )
        assert len(roles) == 5


class TestIndustryMapping:
    def test_folds_free_text_onto_the_ui_vocabulary(self):
        assert canonical_industry("Financial Services") == "fintech"
        assert canonical_industry("Artificial Intelligence") == "ai"
        assert canonical_industry("Digital Health") == "healthcare"
        assert canonical_industry("SaaS") == "tech"

    def test_specific_aliases_beat_the_generic_tech_bucket(self):
        assert canonical_industry("financial technology") == "fintech"

    def test_short_aliases_match_whole_words_only(self):
        # "retail" contains "ai" — matching it would be an ecommerce → ai mix-up.
        assert canonical_industry("Retail") == "ecommerce"

    def test_unmappable_industry_returns_none(self):
        assert canonical_industry("Municipal Waste Management") is None

    def test_stated_industries_win_and_are_marked_as_resume_sourced(self):
        industries, source = suggest_industries(
            ["Fintech", "Payments"], ["pytorch", "kafka"]
        )
        assert source == "resume"
        # Both fold to fintech; the duplicate is dropped rather than shown twice.
        assert industries == ["fintech"]

    def test_unmappable_stated_industry_is_kept_verbatim(self):
        industries, _ = suggest_industries(["Municipal  Waste Management"], [])
        assert industries == ["municipal waste management"]

    def test_falls_back_to_skills_when_no_industry_is_stated(self):
        industries, source = suggest_industries([], ["pytorch", "langchain", "dbt"])
        assert source == "inferred"
        assert industries == ["ai", "data"]

    def test_generic_engineering_skills_only_suggest_tech(self):
        industries, source = suggest_industries([], ["python", "react", "aws"])
        assert (industries, source) == (["tech"], "inferred")

    def test_no_evidence_suggests_nothing(self):
        assert suggest_industries([], []) == ([], "inferred")

    def test_caps_the_number_of_suggestions(self):
        industries, _ = suggest_industries(
            ["fintech", "ai", "healthcare", "gaming", "security", "data"], []
        )
        assert len(industries) == 4


class TestSalarySuggestion:
    def test_scales_with_seniority(self):
        floors = []
        for band in ("junior", "mid", "senior", "lead", "exec"):
            floor = suggest_salary_min(None, band)
            assert floor is not None and floor % 5_000 == 0
            floors.append(floor)
        assert floors == sorted(floors)

    def test_years_top_up_the_band_floor(self):
        at_band_start = suggest_salary_min(6, "senior")
        well_past_it = suggest_salary_min(10, "senior")
        assert at_band_start is not None and well_past_it is not None
        assert well_past_it > at_band_start

    def test_the_top_up_is_capped(self):
        assert suggest_salary_min(11, "senior") == suggest_salary_min(40, "senior")

    def test_falls_back_to_years_when_seniority_is_missing(self):
        assert suggest_salary_min(8, None) == suggest_salary_min(8, "senior")

    def test_unknown_seniority_string_is_ignored(self):
        assert suggest_salary_min(8, "wizard") == suggest_salary_min(8, None)

    def test_returns_none_without_any_evidence(self):
        assert suggest_salary_min(None, None) is None
        assert suggest_salary_min(0, None) is None


class TestLocationSuggestion:
    def test_a_city_becomes_a_location(self):
        assert suggest_locations("San Francisco, CA") == (["San Francisco, CA"], False)

    def test_remote_flips_the_switch_instead_of_becoming_a_place(self):
        assert suggest_locations("Remote") == ([], True)

    def test_remote_plus_a_city_keeps_both_signals(self):
        assert suggest_locations("Remote — Austin, TX") == (["Austin, TX"], True)

    def test_empty_location_suggests_nothing(self):
        assert suggest_locations(None) == ([], False)
        assert suggest_locations("   ") == ([], False)


class TestSuggestPreferences:
    def test_maps_a_fully_parsed_resume(self):
        suggested = suggest_preferences(
            _resume(
                target_roles=["Senior Backend Engineer", "Staff Engineer"],
                target_industries=["Fintech"],
                location="San Francisco, CA",
                years_experience=8,
                seniority="senior",
                skills=["python", "kafka"],
            )
        )

        assert suggested.target_roles == ["Senior Backend Engineer", "Staff Engineer"]
        assert suggested.locations == ["San Francisco, CA"]
        assert suggested.target_industries == ["fintech"]
        assert suggested.salary_min is not None
        assert suggested.sources["target_roles"] == "resume"
        assert suggested.sources["locations"] == "resume"
        assert suggested.sources["target_industries"] == "resume"
        assert suggested.sources["salary_min"] == "inferred"

    def test_keeps_the_product_defaults(self):
        suggested = suggest_preferences(_resume())
        assert suggested.daily_application_limit == DEFAULT_DAILY_APPLICATION_LIMIT == 5
        assert suggested.min_fit_score == DEFAULT_MIN_FIT_SCORE == 60
        # Auto-send on, behind a trial — not off. The old default was off with a
        # note promising the user would read the first few, and nothing ever
        # turned it on afterwards; the trial keeps the promise instead of the
        # switch position.
        assert suggested.auto_send is DEFAULT_AUTO_SEND is True
        assert suggested.auto_send_trial_approvals == 3
        # Defaults are not "suggestions" — the UI must not badge them.
        assert suggested.prefilled_fields == []

    def test_infers_roles_when_the_parse_produced_none(self):
        """The reported bug: roles left empty while the other fields filled in."""
        suggested = suggest_preferences(
            _resume(
                target_roles=[],
                location="Austin, TX",
                skills=["python", "aws"],
                years_experience=7,
                seniority="senior",
                experience=[
                    {"title": "Staff Engineer", "start": "2021", "end": "Present"},
                    {"title": "Backend Engineer", "start": "2017", "end": "2021"},
                ],
            )
        )
        assert suggested.target_roles == ["Staff Engineer", "Backend Engineer"]
        assert suggested.sources["target_roles"] == "inferred"
        assert "target_roles" in suggested.prefilled_fields
        assert suggested.notes["target_roles"] == "From your most recent job titles."

    def test_an_empty_resume_suggests_nothing_it_cannot_support(self):
        suggested = suggest_preferences(_resume())
        assert suggested.target_roles == []
        assert suggested.locations == []
        assert suggested.target_industries == []
        assert suggested.salary_min is None
        assert "target_roles" not in suggested.sources
        assert "salary_min" not in suggested.sources

    def test_deduplicates_and_tidies_roles(self):
        suggested = suggest_preferences(
            _resume(target_roles=["Backend  Engineer", "backend engineer", "  ", ""])
        )
        assert suggested.target_roles == ["Backend Engineer"]

    def test_caps_the_number_of_roles(self):
        suggested = suggest_preferences(
            _resume(target_roles=[f"Engineer {n}" for n in range(9)])
        )
        assert len(suggested.target_roles) == 5

    def test_remote_resume_sets_remote_only(self):
        suggested = suggest_preferences(_resume(location="Remote"))
        assert suggested.remote_only is True
        assert suggested.locations == []
        assert suggested.sources["remote_only"] == "resume"

    def test_every_prefilled_field_carries_a_note(self):
        suggested = suggest_preferences(
            _resume(
                target_roles=["Data Engineer"],
                location="Remote — Berlin, Germany",
                years_experience=9,
                seniority="senior",
                skills=["spark", "airflow"],
            )
        )
        assert set(suggested.prefilled_fields) == {
            "target_roles",
            "locations",
            "remote_only",
            "target_industries",
            "salary_min",
        }
        for name in suggested.sources:
            assert suggested.notes[name]


class TestSuggestedPreferencesEndpoint:
    def test_returns_suggestions_with_provenance(self, auth_client, resume):
        resp = auth_client.get(f"/api/v1/resumes/{resume.id}/suggested-preferences")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["resume_id"] == resume.id
        assert body["suggestions"]["target_roles"] == ["Senior Backend Engineer"]
        assert body["suggestions"]["locations"] == ["San Francisco, CA"]
        assert body["suggestions"]["target_industries"] == ["fintech"]
        assert body["suggestions"]["salary_min"] > 0
        assert body["suggestions"]["daily_application_limit"] == 5
        assert body["suggestions"]["min_fit_score"] == 60
        assert body["suggestions"]["auto_send"] is True
        assert body["suggestions"]["auto_send_trial_approvals"] == 3
        assert body["sources"]["target_roles"] == "resume"
        assert body["sources"]["auto_send"] == "default"
        assert "auto_send" not in body["prefilled_fields"]
        assert "target_roles" in body["prefilled_fields"]

    def test_serves_inferred_roles_for_a_resume_with_none_parsed(
        self, auth_client, db_session, current_user
    ):
        row = Resume(
            user_id=current_user.id,
            filename="no-roles.pdf",
            location="Austin, TX",
            skills=["python"],
            target_roles=[],
            target_industries=[],
            experience=[
                {"company": "Northwind", "title": "Staff Engineer", "end": "Present"},
                {"company": "Acme", "title": "Backend Engineer", "end": "2021"},
            ],
            education=[],
            links=[],
        )
        db_session.add(row)
        db_session.commit()

        body = auth_client.get(
            f"/api/v1/resumes/{row.id}/suggested-preferences"
        ).json()

        assert body["suggestions"]["target_roles"] == [
            "Staff Engineer",
            "Backend Engineer",
        ]
        assert body["sources"]["target_roles"] == "inferred"
        assert "target_roles" in body["prefilled_fields"]

    def test_the_payload_is_accepted_by_the_autopilot_endpoint(
        self, auth_client, resume
    ):
        """The suggestion shape has to be savable as-is — that's the whole flow."""
        suggestions = auth_client.get(
            f"/api/v1/resumes/{resume.id}/suggested-preferences"
        ).json()["suggestions"]

        resp = auth_client.put("/api/v1/autopilot", json=suggestions)
        assert resp.status_code == 200, resp.text
        saved = resp.json()
        assert saved["target_roles"] == suggestions["target_roles"]
        assert saved["locations"] == suggestions["locations"]
        assert saved["salary_min"] == suggestions["salary_min"]
        assert saved["configured_at"] is not None

    def test_reading_suggestions_saves_nothing(self, auth_client, resume, db_session):
        auth_client.get(f"/api/v1/resumes/{resume.id}/suggested-preferences")
        from app.models.autopilot import AutopilotPreference

        assert db_session.query(AutopilotPreference).count() == 0

    def test_unknown_resume_is_404(self, auth_client):
        resp = auth_client.get("/api/v1/resumes/9999/suggested-preferences")
        assert resp.status_code == 404

    def test_another_users_resume_is_404(self, client, resume):
        """Suggestions leak the parse, so ownership is checked like every other read."""
        client.post(
            "/api/v1/auth/register",
            json={"email": "other@example.com", "password": "supersecret123"},
        )
        token = client.post(
            "/api/v1/auth/login",
            data={"username": "other@example.com", "password": "supersecret123"},
        ).json()["access_token"]

        resp = client.get(
            f"/api/v1/resumes/{resume.id}/suggested-preferences",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_requires_authentication(self, client, resume):
        resp = client.get(
            f"/api/v1/resumes/{resume.id}/suggested-preferences",
            headers={"Authorization": ""},
        )
        assert resp.status_code == 401


class TestStatedSalaryExpectation:
    """A figure the candidate wrote down beats one we inferred from their band."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Salary expectation: $185,000", 185_000),
            ("Salary Expectations — 185,000 USD", 185_000),
            ("Expected salary: 185k", 185_000),
            ("Desired compensation: $185,000 per year", 185_000),
            ("Minimum salary 185000", 185_000),
            ("Compensation range: $185,000 - $210,000", 185_000),
            ("Target comp: 185k-210k", 185_000),
        ],
    )
    def test_reads_the_stated_figure(self, text, expected):
        assert extract_salary_expectation(text) == expected

    def test_a_range_yields_its_floor(self):
        """The preference is a minimum; reading the top would hide real jobs."""
        assert extract_salary_expectation("Salary: $150,000 to $180,000") == 150_000

    def test_several_statements_settle_on_the_lowest(self):
        text = "Expected base: $200,000\nMinimum salary: $170,000"
        assert extract_salary_expectation(text) == 170_000

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Managed a $4,000,000 marketing budget",
            "Grew revenue to $12,000,000",
            "Company 2021 - Present",
            "Reduced payment processing costs by 30%",
            "Salary history available on request",
        ],
    )
    def test_unlabelled_or_implausible_numbers_are_not_expectations(self, text):
        assert extract_salary_expectation(text) is None

    def test_a_monthly_figure_is_out_of_range(self):
        assert extract_salary_expectation("Expected salary: 8,000") is None

    def test_the_stated_figure_wins_over_the_inferred_band(self):
        inferred = suggest_preferences(_resume(years_experience=8, seniority="senior"))
        stated = suggest_preferences(
            _resume(
                years_experience=8,
                seniority="senior",
                raw_text="Salary expectation: $250,000",
            )
        )
        assert inferred.sources["salary_min"] == "inferred"
        assert stated.salary_min == 250_000
        assert stated.sources["salary_min"] == "resume"
        assert "stated" in stated.notes["salary_min"]


class TestMergingSeveralResumes:
    """Several resumes mean "consider me for all of this" — so merging widens."""

    def _backend(self) -> Resume:
        return _resume(
            id=1,
            is_default=True,
            target_roles=["Staff Backend Engineer"],
            target_industries=["fintech"],
            location="San Francisco, CA",
            skills=["python", "kafka"],
            years_experience=10,
            seniority="lead",
        )

    def _platform(self) -> Resume:
        return _resume(
            id=2,
            target_roles=["Platform Engineer", "SRE"],
            target_industries=["security"],
            location="Austin, TX",
            skills=["kubernetes", "terraform"],
            years_experience=8,
            seniority="senior",
        )

    def test_no_resumes_yields_bare_defaults(self):
        merged = merge_suggestions([])
        assert merged.target_roles == []
        assert merged.salary_min is None
        assert merged.prefilled_fields == []

    def test_one_resume_matches_the_single_resume_path(self):
        one = self._backend()
        assert merge_suggestions([one]).values == suggest_preferences(one).values

    def test_roles_are_unioned_with_the_default_resume_first(self):
        merged = merge_suggestions([self._platform(), self._backend()])
        assert merged.target_roles == [
            "Staff Backend Engineer",
            "Platform Engineer",
            "SRE",
        ]
        assert merged.sources["target_roles"] == "resume"

    def test_industries_and_locations_are_unioned(self):
        merged = merge_suggestions([self._backend(), self._platform()])
        assert set(merged.target_industries) == {"fintech", "security"}
        assert set(merged.locations) == {"San Francisco, CA", "Austin, TX"}

    def test_one_remote_resume_flips_remote_for_the_set(self):
        remote = self._platform()
        remote.location = "Remote"
        merged = merge_suggestions([self._backend(), remote])
        assert merged.remote_only is True
        assert merged.sources["remote_only"] == "resume"

    def test_the_salary_floor_is_the_lowest_across_the_set(self):
        """Merging must never raise the floor — that hides jobs the user wants."""
        merged = merge_suggestions([self._backend(), self._platform()])
        lead = suggest_preferences(self._backend()).salary_min
        senior = suggest_preferences(self._platform()).salary_min
        assert lead is not None and senior is not None and lead > senior
        assert merged.salary_min == senior

    def test_a_stated_expectation_beats_an_inferred_band_across_resumes(self):
        stated = self._platform()
        stated.raw_text = "Expected salary: $210,000"
        merged = merge_suggestions([self._backend(), stated])
        assert merged.salary_min == 210_000
        assert merged.sources["salary_min"] == "resume"

    def test_fields_no_resume_informed_are_left_unbadged(self):
        blank = [_resume(id=1, is_default=True), _resume(id=2)]
        merged = merge_suggestions(blank)
        assert merged.target_roles == []
        assert "target_roles" not in merged.sources
        assert "target_roles" not in merged.notes
        assert "locations" not in merged.sources
        assert "salary_min" not in merged.sources
        # The product defaults still ride along.
        assert merged.min_fit_score == DEFAULT_MIN_FIT_SCORE
        assert merged.daily_application_limit == DEFAULT_DAILY_APPLICATION_LIMIT
        assert merged.auto_send == DEFAULT_AUTO_SEND

    def test_every_suggested_field_carries_a_note(self):
        merged = merge_suggestions([self._backend(), self._platform()])
        for name in merged.sources:
            assert merged.notes.get(name)


class TestExtractedProfile:
    def test_folds_the_set_into_one_view_of_the_candidate(self):
        profile = extract_profile(
            [
                _resume(
                    id=2,
                    target_roles=["Platform Engineer"],
                    skills=["kubernetes", "python"],
                    years_experience=8,
                    seniority="senior",
                ),
                _resume(
                    id=1,
                    is_default=True,
                    full_name="Jordan Candidate",
                    location="San Francisco, CA",
                    target_roles=["Staff Backend Engineer"],
                    skills=["python", "kafka"],
                    years_experience=10,
                    seniority="lead",
                ),
            ]
        )
        assert profile.resume_ids == [1, 2]
        assert profile.full_name == "Jordan Candidate"
        assert profile.location == "San Francisco, CA"
        # The strongest band and the longest history the set can evidence.
        assert profile.seniority == "lead"
        assert profile.years_experience == 10
        # Union, first spelling kept, no repeats.
        assert profile.skills == ["python", "kafka", "kubernetes"]
        assert profile.titles == ["Staff Backend Engineer", "Platform Engineer"]

    def test_an_empty_set_is_an_empty_profile(self):
        profile = extract_profile([])
        assert profile.resume_ids == [] and profile.skills == []
        assert profile.seniority is None and profile.years_experience is None

    def test_an_unrecognised_seniority_is_ignored_rather_than_ranked(self):
        profile = extract_profile([_resume(id=1, seniority="wizard")])
        assert profile.seniority is None


class TestMergedSuggestionsEndpoint:
    """GET /resumes/suggested-preferences — what the setup page pre-fills from."""

    def _upload(self, db_session, current_user, **overrides) -> Resume:
        fields = {
            "user_id": current_user.id,
            "filename": "extra.pdf",
            "raw_text": "",
            "target_roles": ["Platform Engineer"],
            "target_industries": ["security"],
            "skills": ["kubernetes"],
            "location": "Austin, TX",
            "years_experience": 8,
            "seniority": "senior",
            "experience": [],
            "education": [],
            "links": [],
        }
        fields.update(overrides)
        row = Resume(**fields)
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        return row

    def test_covers_every_resume_not_just_the_default(
        self, auth_client, db_session, current_user, resume
    ):
        self._upload(db_session, current_user)

        body = auth_client.get("/api/v1/resumes/suggested-preferences").json()

        assert body["resume_id"] is None
        assert set(body["suggestions"]["target_roles"]) == {
            "Senior Backend Engineer",
            "Platform Engineer",
        }
        assert set(body["suggestions"]["target_industries"]) == {"fintech", "security"}
        assert set(body["suggestions"]["locations"]) == {
            "San Francisco, CA",
            "Austin, TX",
        }

    def test_reports_the_profile_it_read(
        self, auth_client, db_session, current_user, resume
    ):
        self._upload(db_session, current_user)

        profile = auth_client.get("/api/v1/resumes/suggested-preferences").json()[
            "profile"
        ]

        assert profile["resume_count"] == 2
        assert sorted(profile["resume_ids"]) == sorted([resume.id, resume.id + 1])
        assert profile["full_name"] == "Jordan Candidate"
        assert profile["seniority"] == "senior"
        assert profile["years_experience"] == 8
        assert "python" in profile["skills"] and "kubernetes" in profile["skills"]

    def test_no_resumes_yields_defaults_rather_than_a_404(self, auth_client):
        """The setup page asks for this before anything has been uploaded."""
        resp = auth_client.get("/api/v1/resumes/suggested-preferences")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["suggestions"]["target_roles"] == []
        assert body["suggestions"]["min_fit_score"] == DEFAULT_MIN_FIT_SCORE
        assert body["prefilled_fields"] == []
        assert body["profile"]["resume_count"] == 0

    def test_the_merged_payload_is_savable_as_is(
        self, auth_client, db_session, current_user, resume
    ):
        """The whole flow is: read suggestions, show them, save them unchanged."""
        self._upload(db_session, current_user)
        suggestions = auth_client.get("/api/v1/resumes/suggested-preferences").json()[
            "suggestions"
        ]

        resp = auth_client.put("/api/v1/autopilot", json=suggestions)
        assert resp.status_code == 200, resp.text
        saved = resp.json()
        assert set(saved["target_roles"]) == set(suggestions["target_roles"])
        assert saved["configured_at"] is not None

    def test_reading_the_merged_suggestions_saves_nothing(self, auth_client, resume, db_session):
        auth_client.get("/api/v1/resumes/suggested-preferences")
        from app.models.autopilot import AutopilotPreference

        assert db_session.query(AutopilotPreference).count() == 0

    def test_only_the_callers_resumes_are_read(self, client, resume):
        """A second account sees its own (empty) set, never the first user's."""
        client.post(
            "/api/v1/auth/register",
            json={"email": "other@example.com", "password": "supersecret123"},
        )
        token = client.post(
            "/api/v1/auth/login",
            data={"username": "other@example.com", "password": "supersecret123"},
        ).json()["access_token"]

        body = client.get(
            "/api/v1/resumes/suggested-preferences",
            headers={"Authorization": f"Bearer {token}"},
        ).json()

        assert body["profile"]["resume_count"] == 0
        assert body["suggestions"]["target_roles"] == []

    def test_requires_authentication(self, client):
        resp = client.get(
            "/api/v1/resumes/suggested-preferences", headers={"Authorization": ""}
        )
        assert resp.status_code == 401
