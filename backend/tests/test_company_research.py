"""Company research: two tiers, a weekly cache, and a bias toward saying nothing.

The posting is read first because it is evidence; the model is asked second
because it is recall. Where they disagree the posting wins, since it was
actually read. The property worth defending throughout is that an unknown stays
unknown — a card that confidently describes the wrong company is worse than a
card with three fields on it, and a candidate walking into an interview having
believed it is the concrete harm.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.company_profile import CompanyProfile
from app.services import company_research as svc
from app.services.openrouter_client import OpenRouterError

POSTING = """\
Northwind Labs is a Series B fintech payments company. We were founded in 2017
and are a team of 240 people, headquartered in San Francisco.
Our stack is Python, FastAPI, PostgreSQL, Kubernetes and AWS.
"""


class TestPostingTier:
    def test_reads_the_stack_out_of_the_posting(self):
        found = svc.extract_tech_stack(POSTING)
        assert "python" in [t.lower() for t in found]
        assert any("postgres" in t.lower() for t in found)

    def test_no_text_means_no_stack(self):
        assert svc.extract_tech_stack(None) == []
        assert svc.extract_tech_stack("") == []

    def test_reads_the_funding_stage_a_company_claims(self):
        assert svc.detect_funding_stage(POSTING) == "series_b"

    def test_a_posting_that_claims_nothing_gets_no_stage(self):
        assert svc.detect_funding_stage("We are hiring a backend engineer.") is None

    def test_reads_a_headcount(self):
        assert svc.extract_headcount("we are a team of 240 people") == 240

    def test_ignores_a_number_that_is_not_a_payroll(self):
        """Job ads throw big round numbers around — "2 million users" is not staff."""
        assert svc.extract_headcount("join 2000000 users on our platform") is None

    @pytest.mark.parametrize(
        "count,expected",
        [(3, "1-10"), (40, "11-50"), (240, "201-500"), (12000, "5000+")],
    )
    def test_buckets_a_headcount(self, count, expected):
        assert svc.size_bucket(count) == expected

    def test_an_absent_headcount_has_no_bucket(self):
        assert svc.size_bucket(None) is None
        assert svc.size_bucket(0) is None

    def test_builds_a_profile_from_the_posting_alone(self):
        facts = svc.research_from_posting("Northwind Labs", POSTING, "San Francisco, CA")
        assert facts["name"] == "Northwind Labs"
        assert facts["funding_stage"] == "series_b"
        assert facts["employee_count"] == 240
        assert facts["size"] == "201-500"
        assert facts["founded_year"] == 2017


class TestLLMTier:
    def test_returns_nothing_without_a_key(self, monkeypatch):
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "")
        assert svc.research_with_llm("Northwind Labs") is None

    def test_returns_nothing_when_the_provider_fails(self, monkeypatch):
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "k")
        with patch(
            "app.services.company_research.chat_completion",
            side_effect=OpenRouterError("down"),
        ):
            assert svc.research_with_llm("Northwind Labs") is None

    def test_parses_a_clean_recall(self, monkeypatch):
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "k")
        with patch(
            "app.services.company_research.chat_completion",
            return_value=(
                '{"summary": "Payments infrastructure.", "industry": "Fintech", '
                '"size": "201-500", "founded_year": 2017, "funding_stage": "series_b", '
                '"tech_stack": ["Python"], "news": []}'
            ),
        ):
            facts = svc.research_with_llm("Northwind Labs")

        assert facts["industry"] == "Fintech"
        assert facts["founded_year"] == 2017

    def test_rejects_an_implausible_year(self, monkeypatch):
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "k")
        with patch(
            "app.services.company_research.chat_completion",
            return_value='{"industry": "Fintech", "founded_year": 1066}',
        ):
            facts = svc.research_with_llm("Northwind Labs")
        assert facts is None or facts.get("founded_year") is None


class TestCacheAndMerge:
    def test_creates_a_row_and_reuses_it(self, db_session, monkeypatch):
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "")
        first = svc.research_company(db_session, "Northwind Labs", posting_text=POSTING)
        second = svc.research_company(db_session, "Northwind Labs", posting_text=POSTING)

        assert first is not None and second is not None
        assert first.id == second.id
        assert db_session.query(CompanyProfile).count() == 1
        # The second call was served from cache, and says so.
        assert second.hit_count >= 1

    def test_normalizes_the_cache_key(self, db_session, monkeypatch):
        """"Acme, Inc." and "Acme" are one employer, so one row."""
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "")
        svc.research_company(db_session, "Acme, Inc.", posting_text=POSTING)
        svc.research_company(db_session, "Acme", posting_text=POSTING)

        assert db_session.query(CompanyProfile).count() == 1

    def test_no_company_name_means_no_row(self, db_session):
        assert svc.research_company(db_session, None) is None
        assert svc.research_company(db_session, "   ") is None
        assert db_session.query(CompanyProfile).count() == 0

    def test_a_company_nothing_is_known_about_still_gets_a_row(
        self, db_session, monkeypatch
    ):
        """So the next card open is a cache hit rather than another round trip."""
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "")
        row = svc.research_company(db_session, "Totally Unknown Ltd", posting_text=None)

        assert row is not None
        assert row.status == "empty"

    def test_the_posting_beats_the_model_where_they_disagree(
        self, db_session, monkeypatch
    ):
        """The posting was read; the model was remembering."""
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "k")
        with patch(
            "app.services.company_research.chat_completion",
            return_value=(
                '{"industry": "Healthcare", "founded_year": 1999, '
                '"funding_stage": "seed", "tech_stack": ["Java"], "news": []}'
            ),
        ):
            row = svc.research_company(
                db_session, "Northwind Labs", posting_text=POSTING
            )

        assert row.founded_year == 2017  # from the posting, not 1999
        assert row.funding_stage == "series_b"  # from the posting, not seed

    def test_stacks_are_additive_across_tiers(self, db_session, monkeypatch):
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "k")
        with patch(
            "app.services.company_research.chat_completion",
            return_value='{"industry": "Fintech", "tech_stack": ["Terraform"], "news": []}',
        ):
            row = svc.research_company(
                db_session, "Northwind Labs", posting_text=POSTING
            )

        stack = [t.lower() for t in row.tech_stack]
        assert "terraform" in stack  # only the model knew this
        assert "python" in stack  # only the posting said this

    def test_use_llm_false_skips_the_model_entirely(self, db_session, monkeypatch):
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "k")
        with patch("app.services.company_research.chat_completion") as call:
            row = svc.research_company(
                db_session, "Northwind Labs", posting_text=POSTING, use_llm=False
            )
        call.assert_not_called()
        assert row.source == "heuristic"

    def test_force_refreshes_a_fresh_row(self, db_session, monkeypatch):
        monkeypatch.setattr("app.services.company_research.settings.openrouter_api_key", "")
        first = svc.research_company(db_session, "Northwind Labs", posting_text=POSTING)
        before = first.researched_at

        again = svc.research_company(
            db_session, "Northwind Labs", posting_text=POSTING, force=True
        )
        assert again.id == first.id
        assert again.researched_at >= before


class TestFreshness:
    def test_a_row_with_no_timestamp_is_stale(self):
        assert CompanyProfile(name="x", normalized_name="x").is_fresh(7) is False
