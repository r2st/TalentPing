"""Salary intelligence: deterministic bands, and an honest comparison.

The whole point of modelling rather than scraping is that the same posting
always produces the same band — a number that moves between page loads is worse
than no number. So the tests pin determinism, the ordering the model is supposed
to encode (senior > mid > junior, SF > a cheap market), and the cases where the
right answer is to say nothing: an hourly rate, a posting with no salary, a
title nothing recognises.
"""
from __future__ import annotations

import pytest

from app.services import salary_service as svc


class TestRoleClassification:
    @pytest.mark.parametrize(
        "title,expected_family",
        [
            ("Senior Backend Engineer", "backend_engineer"),
            ("Frontend Developer", "frontend_engineer"),
            ("Data Scientist", "data_scientist"),
            ("Product Manager", "product_manager"),
        ],
    )
    def test_reads_the_family_off_the_title(self, title, expected_family):
        family, _label, base = svc.classify_role(title)
        assert family == expected_family
        assert base > 0

    def test_a_generic_title_falls_back_to_the_description(self):
        family, _label, _base = svc.classify_role(
            "Engineer", "You will build backend services and REST APIs in Python."
        )
        assert family != "generic"

    def test_the_title_outvotes_the_description(self):
        """A backend posting that name-drops ML is still a backend role."""
        family, _label, _base = svc.classify_role(
            "Backend Engineer", "Our team works alongside machine learning researchers."
        )
        assert family == "backend_engineer"

    def test_an_unrecognised_title_still_returns_a_band(self):
        family, label, base = svc.classify_role("Chief Vibes Officer")
        assert family and label and base > 0


class TestSeniority:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Senior Backend Engineer", "senior"),
            ("Staff Engineer", "lead"),
            ("Junior Developer", "junior"),
            ("Backend Engineer", "mid"),
        ],
    )
    def test_reads_seniority_from_the_title(self, title, expected):
        assert svc.classify_seniority(title) == expected

    def test_an_unlevelled_title_defaults_to_mid(self):
        """Mid rather than junior: guessing low flatters every band on the page."""
        assert svc.classify_seniority("Developer") == "mid"
        assert svc.classify_seniority(None) == "mid"


class TestMarket:
    def test_a_named_city_sets_the_market(self):
        key, _label, multiplier = svc.classify_market("San Francisco, CA")
        assert key != "unknown"
        assert multiplier > 1.0

    def test_a_city_wins_over_a_remote_flag(self):
        """An employer who anchored the req somewhere prices it there."""
        city, _l, city_mult = svc.classify_market("San Francisco, CA", remote=True)
        remote, _l2, _m = svc.classify_market(None, remote=True)
        assert city != remote
        assert city_mult > 1.0

    def test_no_location_and_no_flag_is_unknown(self):
        key, _label, _m = svc.classify_market(None)
        assert key == "unknown"


class TestEstimateBand:
    def test_is_deterministic(self):
        first = svc.estimate_band("Senior Backend Engineer", "San Francisco, CA")
        second = svc.estimate_band("Senior Backend Engineer", "San Francisco, CA")
        assert first.as_dict() == second.as_dict()

    def test_orders_min_median_max(self):
        band = svc.estimate_band("Senior Backend Engineer", "Austin, TX")
        assert band.minimum < band.median < band.maximum

    def test_seniority_raises_the_band(self):
        junior = svc.estimate_band("Junior Backend Engineer", "Austin, TX")
        senior = svc.estimate_band("Senior Backend Engineer", "Austin, TX")
        assert senior.median > junior.median

    def test_an_expensive_market_raises_the_band(self):
        sf = svc.estimate_band("Backend Engineer", "San Francisco, CA")
        remote = svc.estimate_band("Backend Engineer", "Remote", remote=True)
        assert sf.median > remote.median

    def test_an_explicit_seniority_overrides_the_title(self):
        band = svc.estimate_band("Backend Engineer", "Austin, TX", seniority="lead")
        assert band.seniority == "lead"

    def test_a_modelled_band_says_so(self):
        assert svc.estimate_band("Backend Engineer").is_estimate is True

    def test_rounds_to_whole_thousands(self):
        band = svc.estimate_band("Senior Backend Engineer", "San Francisco, CA")
        assert band.minimum % 1000 == 0
        assert band.median % 1000 == 0
        assert band.maximum % 1000 == 0


class TestParseOffered:
    def test_reads_a_range(self):
        assert svc.parse_offered("$160,000 - $200,000") == (160_000, 200_000)

    def test_a_single_figure_is_the_whole_band(self):
        low, high = svc.parse_offered("$150,000")
        assert low == high == 150_000

    @pytest.mark.parametrize(
        "text", ["$60/hr", "$75 per hour", "£6,000 / month", "$3000 per week"]
    )
    def test_refuses_to_annualise_a_non_annual_figure(self, text):
        """Annualising needs an hours assumption we would be inventing."""
        assert svc.parse_offered(text) == (None, None)

    def test_no_salary_text_is_no_answer(self):
        assert svc.parse_offered(None) == (None, None)
        assert svc.parse_offered("Competitive salary") == (None, None)


class TestComparison:
    def _band(self):
        return svc.SalaryBand(
            role_family="backend_engineer",
            role_label="Backend Engineer",
            seniority="senior",
            location_key="us_remote",
            location_label="US (remote)",
            currency="USD",
            minimum=150_000,
            median=180_000,
            maximum=210_000,
        )

    def test_a_posting_with_no_salary_is_unknown_not_average(self):
        result = svc.compare_to_market(self._band(), None)
        assert result.verdict == "unknown"
        assert result.offered_mid is None
        assert "doesn't publish" in result.label

    def test_a_generous_posting_reads_above(self):
        result = svc.compare_to_market(self._band(), "$220,000 - $250,000")
        assert result.verdict == "above"
        assert result.delta > 0

    def test_a_low_posting_reads_below(self):
        result = svc.compare_to_market(self._band(), "$120,000 - $130,000")
        assert result.verdict == "below"
        assert result.delta < 0

    def test_a_matching_posting_reads_in_line(self):
        result = svc.compare_to_market(self._band(), "$175,000 - $185,000")
        assert result.verdict == "at"
        assert "In line" in result.label


class TestBenchmarkCache:
    def test_stores_a_row_and_reuses_it(self, db_session):
        first = svc.get_benchmark(db_session, "Senior Backend Engineer", "Austin, TX")
        second = svc.get_benchmark(db_session, "Senior Backend Engineer", "Austin, TX")

        assert first.median == second.median
        from app.models.salary_benchmark import SalaryBenchmark

        rows = db_session.query(SalaryBenchmark).all()
        # One row per (family, seniority, market) — the second call was a hit.
        assert len(rows) == 1

    def test_different_markets_get_different_rows(self, db_session):
        svc.get_benchmark(db_session, "Senior Backend Engineer", "Austin, TX")
        svc.get_benchmark(db_session, "Senior Backend Engineer", "San Francisco, CA")

        from app.models.salary_benchmark import SalaryBenchmark

        assert db_session.query(SalaryBenchmark).count() == 2

    def test_insight_for_a_posting_carries_band_and_comparison(self, db_session):
        posting = type(
            "P",
            (),
            {
                "title": "Senior Backend Engineer",
                "location": "San Francisco, CA",
                "remote": False,
                "description": "Build payment APIs.",
                "salary_text": "$160,000 - $200,000",
            },
        )()
        insight = svc.insight_for_posting(db_session, posting)
        payload = insight.as_dict()

        assert payload["band"]["median"] > 0
        assert payload["comparison"]["verdict"] in {"above", "at", "below"}
        assert "is_estimate" in payload
