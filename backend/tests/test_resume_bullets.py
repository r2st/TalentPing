"""Per-job bullet rewriting, and the line it must not cross.

The rewrite is an edit of the candidate's own claims. The tests that matter are
the ones that pin what "edit" excludes: a rewritten bullet may re-order, re-word
and drop, but the moment it names a technology, a number or an outcome the
original didn't, it is thrown away and the original stands.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import resume_bullets
from app.services.jd_parser import parse_heuristic
from app.services.openrouter_client import OpenRouterError

RESUME_WITH_BULLETS = """\
Jordan Candidate
Senior Backend Engineer

Experience
Staff Backend Engineer, Northwind Payments 2021 - Present
- Built and owned the payment settlement service in Python
- Migrated the ledger from a monolith to a service, cutting deploy time
Senior Backend Engineer at Acme Corp 2018 - 2021
- Designed the billing API used by every internal team
* Ran the on-call rotation for the payments platform

Education
B.S. Computer Science, State University 2016

Skills
Python, FastAPI, PostgreSQL, AWS
"""


@pytest.fixture()
def bulleted_resume(db_session, resume):
    resume.raw_text = RESUME_WITH_BULLETS
    db_session.commit()
    return resume


class TestExtraction:
    def test_groups_bullets_under_their_role(self, bulleted_resume):
        found = resume_bullets.extract_role_bullets(bulleted_resume)
        northwind = resume_bullets.bullets_for(found, "Northwind Payments", "Staff Backend Engineer")
        acme = resume_bullets.bullets_for(found, "Acme Corp", "Senior Backend Engineer")

        assert any("settlement service" in b for b in northwind)
        assert any("ledger" in b for b in northwind)
        assert any("billing API" in b for b in acme)
        # A bullet under Acme must not leak into Northwind's list.
        assert not any("billing API" in b for b in northwind)

    def test_handles_several_bullet_characters(self, bulleted_resume):
        found = resume_bullets.extract_role_bullets(bulleted_resume)
        acme = resume_bullets.bullets_for(found, "Acme Corp", None)
        # One "-" bullet and one "*" bullet, both under Acme.
        assert len(acme) == 2

    def test_stops_at_the_next_section(self, bulleted_resume):
        found = resume_bullets.extract_role_bullets(bulleted_resume)
        every = [b for bullets in found.values() for b in bullets]
        assert not any("Computer Science" in b for b in every)
        assert not any("PostgreSQL" in b for b in every)

    def test_a_resume_with_no_bullets_yields_nothing(self, resume):
        # The default fixture resume is prose with no bullet list at all.
        assert resume_bullets.extract_role_bullets(resume) == {}

    def test_empty_raw_text_is_safe(self, db_session, resume):
        resume.raw_text = ""
        db_session.commit()
        assert resume_bullets.extract_role_bullets(resume) == {}

    def test_an_unmatched_company_returns_nothing(self, bulleted_resume):
        found = resume_bullets.extract_role_bullets(bulleted_resume)
        assert resume_bullets.bullets_for(found, "Nowhere Ltd", "Engineer") == []


class TestGrounding:
    def test_a_reordering_rewrite_is_grounded(self, resume):
        original = "Built and owned the payment settlement service in Python"
        rewritten = "Owned the Python payment settlement service end to end"
        assert resume_bullets.bullet_is_grounded(rewritten, original, resume, []) is True

    def test_introducing_a_technology_is_not_grounded(self, resume):
        original = "Built the payment settlement service in Python"
        rewritten = "Built the payment settlement service in Python on Kubernetes"
        assert resume_bullets.bullet_is_grounded(rewritten, original, resume, []) is False

    def test_introducing_a_metric_is_not_grounded(self, resume):
        original = "Migrated the ledger from a monolith to a service"
        rewritten = "Migrated the ledger to a service, cutting latency 40%"
        assert resume_bullets.bullet_is_grounded(rewritten, original, resume, []) is False

    def test_mentioning_a_missing_requirement_is_not_grounded(self, resume):
        original = "Built the payment settlement service"
        rewritten = "Built the payment settlement service with Kafka"
        assert (
            resume_bullets.bullet_is_grounded(rewritten, original, resume, ["kafka"])
            is False
        )

    def test_a_skill_from_elsewhere_on_the_resume_is_allowed(self, resume):
        """Moving a real skill into the bullet where it's relevant is re-angling."""
        original = "Built the settlement service"
        # The fixture resume lists fastapi among its skills.
        rewritten = "Built the settlement service using fastapi"
        assert resume_bullets.bullet_is_grounded(rewritten, original, resume, []) is True

    def test_an_empty_rewrite_is_not_grounded(self, resume):
        assert resume_bullets.bullet_is_grounded("   ", "Built things", resume, []) is False


class TestRewrite:
    def test_no_key_returns_the_originals(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.resume_bullets.settings.openrouter_api_key", "")
        bullets = ["Built the settlement service in Python"]
        out, how = resume_bullets.rewrite_bullets(
            bullets, parse_heuristic(job_description), resume, []
        )
        assert out == bullets
        assert how == "heuristic"

    def test_provider_failure_returns_the_originals(
        self, monkeypatch, resume, job_description
    ):
        monkeypatch.setattr("app.services.resume_bullets.settings.openrouter_api_key", "k")
        bullets = ["Built the settlement service in Python"]
        with patch(
            "app.services.resume_bullets.chat_completion",
            side_effect=OpenRouterError("down"),
        ):
            out, how = resume_bullets.rewrite_bullets(
                bullets, parse_heuristic(job_description), resume, []
            )
        assert out == bullets
        assert how == "heuristic"

    def test_accepts_a_grounded_rewrite(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.resume_bullets.settings.openrouter_api_key", "k")
        bullets = ["Built and owned the payment settlement service in Python"]
        with patch(
            "app.services.resume_bullets.chat_completion",
            return_value=(
                '{"bullets": [{"index": 0, "text": '
                '"Owned the Python payment settlement service end to end"}]}'
            ),
        ):
            out, how = resume_bullets.rewrite_bullets(
                bullets, parse_heuristic(job_description), resume, []
            )
        assert out[0].startswith("Owned the Python")
        assert how == "llm"

    def test_rejects_only_the_bad_bullet(self, monkeypatch, resume, job_description):
        """One invented metric must not cost the bullets that were fine."""
        monkeypatch.setattr("app.services.resume_bullets.settings.openrouter_api_key", "k")
        bullets = [
            "Built and owned the payment settlement service in Python",
            "Migrated the ledger from a monolith to a service",
        ]
        with patch(
            "app.services.resume_bullets.chat_completion",
            return_value=(
                '{"bullets": ['
                '{"index": 0, "text": "Owned the Python payment settlement service"},'
                '{"index": 1, "text": "Migrated the ledger, cutting latency by 40%"}'
                "]}"
            ),
        ):
            out, how = resume_bullets.rewrite_bullets(
                bullets, parse_heuristic(job_description), resume, []
            )

        assert out[0] == "Owned the Python payment settlement service"
        assert out[1] == bullets[1]  # the invented metric was rejected
        assert how == "llm"

    def test_an_out_of_range_index_is_ignored(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.resume_bullets.settings.openrouter_api_key", "k")
        bullets = ["Built the settlement service"]
        with patch(
            "app.services.resume_bullets.chat_completion",
            return_value='{"bullets": [{"index": 7, "text": "Something else"}]}',
        ):
            out, how = resume_bullets.rewrite_bullets(
                bullets, parse_heuristic(job_description), resume, []
            )
        assert out == bullets
        assert how == "heuristic"

    def test_a_non_list_payload_is_ignored(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.resume_bullets.settings.openrouter_api_key", "k")
        bullets = ["Built the settlement service"]
        with patch(
            "app.services.resume_bullets.chat_completion",
            return_value='{"bullets": "nope"}',
        ):
            out, how = resume_bullets.rewrite_bullets(
                bullets, parse_heuristic(job_description), resume, []
            )
        assert out == bullets
        assert how == "heuristic"


class TestTailorExperienceBullets:
    def test_stores_both_the_original_and_the_rewrite(
        self, monkeypatch, bulleted_resume, job_description
    ):
        monkeypatch.setattr("app.services.resume_bullets.settings.openrouter_api_key", "")
        highlights = [
            {"company": "Northwind Payments", "title": "Staff Backend Engineer"}
        ]
        out, how = resume_bullets.tailor_experience_bullets(
            bulleted_resume, parse_heuristic(job_description), highlights, []
        )
        entry = out[0]
        assert entry["original_bullets"]
        # Without a key the rewrite is a no-op, so the two agree — which is
        # exactly the invariant the UI relies on to show a diff.
        assert entry["bullets"] == entry["original_bullets"]
        assert how == "heuristic"

    def test_a_resume_without_bullets_yields_empty_lists(
        self, monkeypatch, resume, job_description
    ):
        monkeypatch.setattr("app.services.resume_bullets.settings.openrouter_api_key", "")
        highlights = [{"company": "Acme", "title": "Backend Engineer"}]
        out, how = resume_bullets.tailor_experience_bullets(
            resume, parse_heuristic(job_description), highlights, []
        )
        assert out[0]["original_bullets"] == []
        assert out[0]["bullets"] == []
        assert how == "heuristic"
