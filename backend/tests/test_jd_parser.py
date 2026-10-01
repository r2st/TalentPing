"""Job-description parsing: heuristics only (no network, no LLM key in tests)."""
from __future__ import annotations

import pytest

from app.services import jd_parser
from app.services.jd_parser import JobFetchError, parse_heuristic, parse_job_input


class TestHeuristicParsing:
    def test_extracts_the_core_fields(self, job_description):
        job = parse_heuristic(job_description)

        assert job.title == "Senior Backend Engineer"
        assert job.company == "Northwind Labs"
        assert job.location == "San Francisco, CA"
        assert job.seniority == "senior"
        assert job.years_required == 6
        assert job.industry == "fintech"
        assert job.parsed_with == "heuristic"

    def test_separates_required_from_nice_to_have(self, job_description):
        job = parse_heuristic(job_description)

        assert {"python", "fastapi", "postgresql", "aws"} <= set(job.required_skills)
        # "Nice to have" entries must never be counted as requirements — they
        # would drag the fit score down for something the employer called optional.
        assert "terraform" in job.preferred_skills
        assert "rust" in job.preferred_skills
        assert "terraform" not in job.required_skills
        assert "rust" not in job.required_skills

    def test_hybrid_is_not_remote(self, job_description):
        assert parse_heuristic(job_description).remote is False

    def test_remote_posting_is_detected(self):
        job = parse_heuristic("Backend Engineer\nThis is a fully remote position.")
        assert job.remote is True

    def test_unstated_location_mode_stays_unknown(self):
        assert parse_heuristic("Backend Engineer\nWe build APIs.").remote is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Salary: $160,000 - $200,000", (160000, 200000)),
            ("Compensation: $120k-$150k", (120000, 150000)),
            ("We pay USD 90,000", (90000, None)),
            # Plenty of boards publish the band with no currency at all.
            ("Salary: 155000 - 185000", (155000, 185000)),
            ("Compensation range: 155,000 – 185,000", (155000, 185000)),
            ("Base salary 120k to 150k", (120000, 150000)),
            ("Salary: 155000", (155000, None)),
        ],
    )
    def test_salary_ranges(self, text, expected):
        job = parse_heuristic(f"Engineer\n{text}")
        assert (job.salary_min, job.salary_max) == expected

    def test_no_salary_leaves_the_band_empty(self):
        job = parse_heuristic("Engineer\nCompetitive compensation and equity.")
        assert job.salary_min is None and job.salary_max is None

    @pytest.mark.parametrize(
        "text",
        [
            # Bare numbers are everywhere in a posting; only money-shaped ones,
            # or ones a pay word introduces, may become a band.
            "Requirements\n- 12 years of experience",
            "Founded in 2019, we serve 250 enterprise customers.",
            "Trusted by 250,000 developers worldwide.",
            "We match 401k contributions.",
        ],
    )
    def test_a_number_that_is_not_pay_is_not_a_band(self, text):
        job = parse_heuristic(f"Engineer\n{text}")
        assert job.salary_min is None and job.salary_max is None

    def test_a_currency_band_wins_over_a_bare_number(self):
        job = parse_heuristic(
            "Engineer\nWe serve 250,000 users.\nSalary: $160,000 - $200,000"
        )
        assert (job.salary_min, job.salary_max) == (160000, 200000)

    def test_a_five_digit_band_is_not_read_as_hundreds_of_thousands(self):
        job = parse_heuristic("Engineer\nSalary: 85000 - 95000")
        assert (job.salary_min, job.salary_max) == (85000, 95000)

    def test_years_takes_the_floor_of_a_range(self):
        job = parse_heuristic("Engineer\nRequirements\n- 3-5 years of experience")
        assert job.years_required == 3

    def test_responsibilities_are_pulled_from_their_section(self, job_description):
        job = parse_heuristic(job_description)
        assert "Design and ship payment APIs" in job.responsibilities

    def test_seniority_falls_back_to_the_year_count(self):
        job = parse_heuristic("Backend Engineer\nRequirements\n- 12 years of experience")
        assert job.seniority == "lead"

    def test_empty_input_does_not_raise(self):
        job = parse_heuristic("")
        assert job.title is None
        assert job.required_skills == []

    def test_title_drops_a_trailing_company(self):
        assert parse_heuristic("Staff Engineer at Acme Corp\nWe build things.").title == (
            "Staff Engineer"
        )


class TestKeywords:
    def test_taxonomy_skills_lead_the_keyword_list(self, job_description):
        keywords = jd_parser.extract_keywords(job_description)
        assert keywords[0] in {"python", "postgresql", "fastapi", "aws", "kubernetes", "kafka"}

    def test_stopwords_are_excluded(self, job_description):
        assert "the" not in jd_parser.extract_keywords(job_description)


class TestJdHash:
    def test_is_stable_across_whitespace_changes(self):
        assert jd_parser.jd_hash("Senior  Engineer\n\nPython") == jd_parser.jd_hash(
            "senior engineer python"
        )

    def test_differs_for_different_postings(self):
        assert jd_parser.jd_hash("Backend Engineer") != jd_parser.jd_hash("Frontend Engineer")


class TestParseJobInput:
    def test_pasted_text_wins_over_a_url(self, job_description, monkeypatch):
        """A fetch can land on a cookie wall; the text the user saw is the truth."""

        def _boom(*args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("fetch_job_text should not run when text is supplied")

        monkeypatch.setattr(jd_parser, "fetch_job_text", _boom)
        job = parse_job_input(
            description=job_description, url="https://example.com/job", use_llm=False
        )
        assert job.company == "Northwind Labs"

    def test_url_is_fetched_when_no_text_is_given(self, job_description, monkeypatch):
        monkeypatch.setattr(
            jd_parser,
            "fetch_job_text",
            lambda url, **kw: (job_description, "Senior Backend Engineer"),
        )
        job = parse_job_input(url="https://example.com/job", use_llm=False)
        assert job.title == "Senior Backend Engineer"

    def test_neither_source_is_an_error(self):
        with pytest.raises(JobFetchError):
            parse_job_input(use_llm=False)


class TestLlmMerge:
    """The LLM may only add to the heuristic parse — never erase a good value."""

    def test_bad_llm_fields_are_ignored(self, job_description):
        base = parse_heuristic(job_description)
        merged = jd_parser._merge_llm(
            base,
            {
                "title": "",              # empty -> keep heuristic
                "seniority": "wizard",    # not in the enum -> keep heuristic
                "years_required": 999,    # out of range -> keep heuristic
                "remote": "yes",          # not a bool -> keep heuristic
            },
        )
        assert merged.title == base.title
        assert merged.seniority == "senior"
        assert merged.years_required == 6
        assert merged.remote is False

    def test_good_llm_fields_are_applied(self, job_description):
        base = parse_heuristic(job_description)
        merged = jd_parser._merge_llm(
            base,
            {
                "company": "Northwind Labs Inc",
                "required_skills": ["python", "go"],
                "responsibilities": ["Ship payment APIs"],
            },
        )
        assert merged.company == "Northwind Labs Inc"
        assert merged.required_skills == ["python", "go"]
        assert merged.parsed_with == "llm"
