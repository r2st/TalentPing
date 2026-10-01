"""Cover letters: grounding in the resume, and grounding in the company facts.

Two invariants carry the feature. The first is the tailorer's — nothing about
the candidate that their resume doesn't say. The second is specific to letters:
the model gets a short list of verified company facts and may not add to it,
because a letter that confidently mis-describes what the employer does is worse
than one that says nothing about them at all.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.job import JobPosting, job_fingerprint
from app.services import cover_letter_service as svc
from app.services.jd_parser import parse_heuristic
from app.services.openrouter_client import OpenRouterError


@pytest.fixture()
def job_posting(db_session, current_user) -> JobPosting:
    """A saved posting — the anchor letters are keyed on (one letter per job)."""
    row = JobPosting(
        user_id=current_user.id,
        title="Senior Backend Engineer",
        company="Northwind Labs",
        location="San Francisco, CA",
        source="manual",
        fingerprint=job_fingerprint("Senior Backend Engineer", "Northwind Labs", None),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def profile(**kwargs) -> SimpleNamespace:
    base = dict(
        name="Acme",
        industry=None,
        size=None,
        funding_stage=None,
        funding_total=None,
        founded_year=None,
        headquarters=None,
        tech_stack=[],
        news=[],
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestCompanyFacts:
    def test_no_profile_means_no_facts(self):
        assert svc.company_facts(None) == []

    def test_renders_only_what_is_known(self):
        facts = svc.company_facts(profile(industry="Fintech", size="51-200"))
        assert any("Fintech" in f for f in facts)
        assert any("51-200" in f for f in facts)
        # Nothing was invented for the fields the profile doesn't have.
        assert not any("Founded" in f for f in facts)
        assert not any("unknown" in f.lower() for f in facts)

    def test_unknown_funding_stage_is_omitted_not_rendered(self):
        assert svc.company_facts(profile(funding_stage="unknown")) == []

    def test_includes_recent_news_with_its_date(self):
        facts = svc.company_facts(
            profile(news=[{"title": "Raised a Series B", "published": "2026-05"}])
        )
        assert any("Series B" in f and "2026-05" in f for f in facts)


class TestDeterministicLetter:
    def test_works_without_a_key(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        out = svc.generate_letter(resume, parse_heuristic(job_description))

        assert out.generated_with == "heuristic"
        assert out.body
        assert resume.full_name in out.sign_off

    def test_never_mentions_a_missing_requirement(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        job = parse_heuristic(job_description)
        out = svc.generate_letter(resume, job)

        # The fixture resume has no Kubernetes and no Kafka.
        assert "kubernetes" not in out.full_text.lower()
        assert "kafka" not in out.full_text.lower()
        assert "kubernetes" in out.missing_keywords

    def test_greeting_never_invents_a_name(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        out = svc.generate_letter(resume, parse_heuristic(job_description))
        assert "hiring team" in out.greeting.lower() or out.greeting == "Hello,"

    def test_uses_one_company_fact_when_available(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        out = svc.generate_letter(
            resume, parse_heuristic(job_description), profile=profile(industry="Fintech")
        )
        assert "Fintech" in out.body
        assert out.company_research


class TestLLMLetter:
    def test_uses_a_clean_generation(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "k")
        with patch(
            "app.services.cover_letter_service.chat_completion",
            return_value=(
                '{"body": "I build backend services in Python and FastAPI, and '
                'would welcome a conversation.", "highlights": '
                '[{"point": "Python depth", "evidence": "Python, FastAPI"}]}'
            ),
        ):
            out = svc.generate_letter(resume, parse_heuristic(job_description))

        assert out.generated_with == "llm"
        assert "FastAPI" in out.body
        assert out.highlights[0]["evidence"] == "Python, FastAPI"

    def test_rejects_a_letter_claiming_a_missing_skill(
        self, monkeypatch, resume, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "k")
        with patch(
            "app.services.cover_letter_service.chat_completion",
            return_value='{"body": "I have deep Kubernetes and Kafka experience."}',
        ):
            out = svc.generate_letter(resume, parse_heuristic(job_description))

        assert out.generated_with == "heuristic"
        assert "kubernetes" not in out.body.lower()

    def test_rejects_a_letter_inventing_a_metric(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "k")
        with patch(
            "app.services.cover_letter_service.chat_completion",
            return_value='{"body": "I cut p99 latency by 45% across the platform."}',
        ):
            out = svc.generate_letter(resume, parse_heuristic(job_description))

        assert out.generated_with == "heuristic"
        assert "45%" not in out.body

    def test_falls_back_when_the_provider_fails(self, monkeypatch, resume, job_description):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "k")
        with patch(
            "app.services.cover_letter_service.chat_completion",
            side_effect=OpenRouterError("down"),
        ):
            out = svc.generate_letter(resume, parse_heuristic(job_description))
        assert out.generated_with == "heuristic"
        assert out.body

    def test_company_facts_are_the_only_employer_input(
        self, monkeypatch, resume, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "k")
        with patch(
            "app.services.cover_letter_service.chat_completion", return_value='{"body": "Hi."}'
        ) as call:
            svc.generate_letter(
                resume,
                parse_heuristic(job_description),
                profile=profile(industry="Fintech", size="51-200"),
            )
        prompt = call.call_args[0][0][1]["content"]
        assert "VERIFIED COMPANY FACTS" in prompt
        assert "Fintech" in prompt

    def test_says_nothing_about_an_unknown_company(
        self, monkeypatch, resume, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "k")
        with patch(
            "app.services.cover_letter_service.chat_completion", return_value='{"body": "Hi."}'
        ) as call:
            svc.generate_letter(resume, parse_heuristic(job_description), profile=None)
        prompt = call.call_args[0][0][1]["content"]
        assert "say nothing about the company" in prompt

    def test_drops_highlights_that_came_without_evidence(
        self, monkeypatch, resume, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "k")
        with patch(
            "app.services.cover_letter_service.chat_completion",
            return_value=(
                '{"body": "I work in Python.", "highlights": '
                '[{"point": "Great engineer"}, {"point": "Python", "evidence": "Python"}]}'
            ),
        ):
            out = svc.generate_letter(resume, parse_heuristic(job_description))
        assert len(out.highlights) == 1
        assert out.highlights[0]["point"] == "Python"


class TestPersistence:
    def test_stores_and_reloads_a_letter(
        self, db_session, monkeypatch, current_user, resume, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        job = parse_heuristic(job_description)
        letter = svc.upsert_letter(db_session, user_id=current_user.id, resume=resume, job=job)
        db_session.commit()

        assert letter.id is not None
        assert letter.full_text.startswith(letter.greeting)
        assert letter.edited is False
        assert letter.delivery == "inline"

    def test_regeneration_refuses_to_clobber_an_edited_letter(
        self, db_session, monkeypatch, current_user, resume, job_posting, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        job = parse_heuristic(job_description)
        letter = svc.upsert_letter(
            db_session,
            user_id=current_user.id,
            resume=resume,
            job=job,
            job_posting_id=job_posting.id,
        )
        svc.apply_edit(db_session, letter, "My own words, thanks.")
        db_session.commit()

        again = svc.upsert_letter(
            db_session,
            user_id=current_user.id,
            resume=resume,
            job=job,
            job_posting_id=job_posting.id,
        )
        assert again.id == letter.id
        assert again.body == "My own words, thanks."
        assert again.edited is True

    def test_force_overwrites_an_edited_letter(
        self, db_session, monkeypatch, current_user, resume, job_posting, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        job = parse_heuristic(job_description)
        letter = svc.upsert_letter(
            db_session,
            user_id=current_user.id,
            resume=resume,
            job=job,
            job_posting_id=job_posting.id,
        )
        svc.apply_edit(db_session, letter, "My own words.")
        db_session.commit()

        again = svc.upsert_letter(
            db_session,
            user_id=current_user.id,
            resume=resume,
            job=job,
            job_posting_id=job_posting.id,
            force=True,
        )
        assert again.body != "My own words."
        assert again.edited is False

    def test_an_invalid_delivery_falls_back_to_inline(
        self, db_session, monkeypatch, current_user, resume, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        letter = svc.upsert_letter(
            db_session,
            user_id=current_user.id,
            resume=resume,
            job=parse_heuristic(job_description),
            delivery="carrier-pigeon",
        )
        assert letter.delivery == "inline"

    def test_renders_markdown_for_download(
        self, db_session, monkeypatch, current_user, resume, job_description
    ):
        monkeypatch.setattr("app.services.cover_letter_service.settings.openrouter_api_key", "")
        letter = svc.upsert_letter(
            db_session,
            user_id=current_user.id,
            resume=resume,
            job=parse_heuristic(job_description),
        )
        db_session.commit()
        markdown = svc.render_markdown(letter)
        assert markdown.startswith("# Cover letter")
        assert letter.body in markdown
