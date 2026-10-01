"""Resume tailoring: the deterministic core, and the anti-hallucination guards."""
from __future__ import annotations

from app.services import resume_tailor
from app.services.jd_parser import parse_heuristic
from app.services.resume_tailor import (
    invented_metric,
    mentions_missing,
    highlight_experience,
    match_keywords,
    order_skills,
    render_markdown,
    tailor_resume,
)


class TestKeywordMatching:
    def test_splits_the_jd_into_have_and_have_not(self, resume, job_description):
        job = parse_heuristic(job_description)
        matched, missing = match_keywords(resume, job)

        # The fixture resume lists python, fastapi and aws.
        assert {"python", "fastapi", "aws"} <= set(matched)
        assert "kubernetes" in missing
        assert "kafka" in missing

    def test_no_skill_is_both_matched_and_missing(self, resume, job_description):
        matched, missing = match_keywords(resume, parse_heuristic(job_description))
        assert not set(matched) & set(missing)

    def test_matches_against_the_whole_resume_not_just_the_skills_list(
        self, db_session, resume, job_description
    ):
        """A tool named only in a job bullet still counts as experience with it."""
        resume.raw_text = f"{resume.raw_text} Ran production Kubernetes clusters."
        db_session.commit()

        matched, missing = match_keywords(resume, parse_heuristic(job_description))
        assert "kubernetes" in matched
        assert "kubernetes" not in missing


class TestSkillOrdering:
    def test_required_skills_lead(self, resume, job_description):
        ordered = order_skills(resume, parse_heuristic(job_description))
        assert ordered[0] in {"python", "fastapi", "aws"}

    def test_never_adds_a_skill_the_candidate_lacks(self, resume, job_description):
        """The single most important property: ordering is a permutation."""
        ordered = order_skills(resume, parse_heuristic(job_description))
        assert sorted(ordered) == sorted(resume.skills)
        assert "kubernetes" not in ordered

    def test_handles_a_resume_with_no_skills(self, db_session, resume, job_description):
        resume.skills = []
        db_session.commit()
        assert order_skills(resume, parse_heuristic(job_description)) == []


class TestExperienceHighlighting:
    def test_ranks_roles_and_explains_each(self, resume, job_description):
        highlights = highlight_experience(resume, parse_heuristic(job_description))
        assert highlights
        assert highlights[0]["company"] == "Acme"
        assert highlights[0]["why_relevant"]

    def test_empty_history_yields_nothing(self, db_session, resume, job_description):
        resume.experience = []
        db_session.commit()
        assert highlight_experience(resume, parse_heuristic(job_description)) == []


class TestHallucinationGuards:
    def test_flags_a_skill_the_candidate_lacks(self):
        assert mentions_missing(
            "I have deep Kubernetes experience.", ["kubernetes"]
        ) == "kubernetes"

    def test_ignores_substrings(self):
        # "go" must not match "goal"; word boundaries are the whole point.
        assert mentions_missing("My goal is to help.", ["go"]) is None

    def test_flags_a_metric_the_resume_does_not_support(self, resume):
        assert invented_metric("I cut latency by 30%.", resume) == "30%"

    def test_allows_a_metric_the_resume_states(self, db_session, resume):
        resume.raw_text = f"{resume.raw_text} Cut p99 latency by 30%."
        db_session.commit()
        assert invented_metric("I cut latency by 30%.", resume) is None

    def test_flags_vague_scale_claims(self, resume):
        assert invented_metric("Served millions of users.", resume) == "millions"

    def test_small_counts_are_not_flagged(self, resume):
        """"A team of 5" is rarely the lie, and flagging it rejects good letters."""
        assert invented_metric("I led a team of 5 engineers.", resume) is None


class TestTailorResume:
    def test_produces_every_artifact(self, resume, job_description):
        job = parse_heuristic(job_description)
        out = tailor_resume(resume, job)

        assert out.tailored_summary
        assert out.cover_letter
        assert out.ordered_skills
        assert out.matched_keywords
        assert out.missing_keywords

    def test_falls_back_to_heuristics_without_an_api_key(self, resume, job_description):
        """No OPENROUTER_API_KEY is set in tests, so this is the exercised path."""
        out = tailor_resume(resume, parse_heuristic(job_description))
        assert out.generated_with == "heuristic"
        assert out.model is None

    def test_output_never_claims_a_missing_skill(self, resume, job_description):
        job = parse_heuristic(job_description)
        out = tailor_resume(resume, job)
        blob = f"{out.tailored_summary} {out.cover_letter}".lower()
        for skill in out.missing_keywords:
            assert skill.lower() not in blob, f"tailored output claimed {skill!r}"

    def test_cover_letter_addresses_the_company(self, resume, job_description):
        out = tailor_resume(resume, parse_heuristic(job_description))
        assert "Northwind Labs" in out.cover_letter

    def test_rejects_llm_prose_that_invents_a_skill(
        self, resume, job_description, monkeypatch
    ):
        """A model that claims Kubernetes must not reach the candidate's resume."""
        monkeypatch.setattr(
            resume_tailor.settings, "openrouter_api_key", "test-key", raising=False
        )
        monkeypatch.setattr(
            resume_tailor,
            "chat_completion",
            lambda *a, **kw: (
                '{"tailored_summary": "Expert in Kubernetes and Kafka.", '
                '"cover_letter": "I run Kubernetes at scale."}'
            ),
        )
        out = tailor_resume(resume, parse_heuristic(job_description))

        assert out.generated_with == "heuristic"
        assert "kubernetes" not in out.tailored_summary.lower()

    def test_rejects_llm_prose_that_invents_a_metric(
        self, resume, job_description, monkeypatch
    ):
        monkeypatch.setattr(
            resume_tailor.settings, "openrouter_api_key", "test-key", raising=False
        )
        monkeypatch.setattr(
            resume_tailor,
            "chat_completion",
            lambda *a, **kw: (
                '{"tailored_summary": "Cut costs by 47%.", '
                '"cover_letter": "I reduced spend 47%."}'
            ),
        )
        out = tailor_resume(resume, parse_heuristic(job_description))
        assert out.generated_with == "heuristic"
        assert "47%" not in out.cover_letter

    def test_rejects_leaked_chain_of_thought(self, resume, job_description, monkeypatch):
        """Free reasoning models sometimes return their scratchpad as the answer."""
        monkeypatch.setattr(
            resume_tailor.settings, "openrouter_api_key", "test-key", raising=False
        )
        monkeypatch.setattr(
            resume_tailor,
            "chat_completion",
            lambda *a, **kw: (
                '{"tailored_summary": "We need to write a summary. The user wants '
                'a senior engineer pitch.", "cover_letter": "ok"}'
            ),
        )
        out = tailor_resume(resume, parse_heuristic(job_description))
        assert "We need to" not in out.tailored_summary

    def test_accepts_grounded_llm_prose(self, resume, job_description, monkeypatch):
        monkeypatch.setattr(
            resume_tailor.settings, "openrouter_api_key", "test-key", raising=False
        )
        monkeypatch.setattr(
            resume_tailor,
            "chat_completion",
            lambda *a, **kw: (
                '{"tailored_summary": "Backend engineer focused on Python and AWS.", '
                '"cover_letter": "I build FastAPI services on AWS."}'
            ),
        )
        out = tailor_resume(resume, parse_heuristic(job_description))

        assert out.generated_with == "llm"
        assert out.tailored_summary == "Backend engineer focused on Python and AWS."

    def test_survives_an_llm_outage(self, resume, job_description, monkeypatch):
        monkeypatch.setattr(
            resume_tailor.settings, "openrouter_api_key", "test-key", raising=False
        )

        def _fail(*args, **kwargs):
            raise resume_tailor.OpenRouterError("upstream down")

        monkeypatch.setattr(resume_tailor, "chat_completion", _fail)
        out = tailor_resume(resume, parse_heuristic(job_description))
        assert out.generated_with == "heuristic"
        assert out.cover_letter


class TestRenderMarkdown:
    def test_renders_the_tailored_view(self, resume, job_description):
        job = parse_heuristic(job_description)
        out = tailor_resume(resume, job)

        class _Row:
            job_title = job.title
            tailored_summary = out.tailored_summary
            ordered_skills = out.ordered_skills
            highlighted_experience = out.highlighted_experience

        markdown = render_markdown(resume, _Row())

        assert markdown.startswith("# Jordan Candidate")
        assert "## Summary" in markdown
        assert "## Skills" in markdown
        assert "## Experience" in markdown
