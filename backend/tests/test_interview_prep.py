"""Interview prep: the briefing for an application that reached a real person.

The invariant worth most of these tests is the one prep shares with tailoring:
**it never invents**. The matched/missing skills split comes from the
deterministic fit scorer, so a talking point can't claim a strength the resume
doesn't support, and with no posting attached the research says so rather than
writing a confident paragraph about a company we know nothing about.

No OpenRouter key is set in the test environment, so every call here runs the
template path unless a test monkeypatches the client.
"""
from __future__ import annotations

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.job import JobPosting
from app.models.recruiter import Recruiter
from app.services import interview_prep_service as svc


def _url(application_id: int) -> str:
    return f"/api/v1/interview-prep/{application_id}"


@pytest.fixture()
def application(db_session, current_user, resume, job_description) -> Application:
    """An application with a full posting behind it, at interview stage."""
    campaign = Campaign(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend outreach",
        target_roles=["Senior Backend Engineer"],
        target_industries=["fintech"],
    )
    recruiter = Recruiter(
        user_id=current_user.id,
        email="talent@northwind.example",
        name="Dana Recruiter",
        company="Northwind Labs",
        industry="fintech",
    )
    posting = JobPosting(
        user_id=current_user.id,
        title="Senior Backend Engineer",
        company="Northwind Labs",
        location="San Francisco, CA",
        salary_text="$160,000 - $200,000",
        remote=False,
        description=job_description,
        fingerprint="fp-prep-1",
    )
    db_session.add_all([campaign, recruiter, posting])
    db_session.flush()

    row = Application(
        user_id=current_user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        job_posting_id=posting.id,
        status=ApplicationStatus.INTERVIEW_SCHEDULED,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def bare_application(db_session, current_user, resume) -> Application:
    """An application with no posting attached — nothing to research from."""
    campaign = Campaign(user_id=current_user.id, resume_id=resume.id, name="Blind run")
    recruiter = Recruiter(
        user_id=current_user.id, email="unknown@example.com", name="Unknown"
    )
    db_session.add_all([campaign, recruiter])
    db_session.flush()
    row = Application(
        user_id=current_user.id, campaign_id=campaign.id, recruiter_id=recruiter.id
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


class TestBriefing:
    def test_returns_every_section(self, auth_client, application):
        body = auth_client.post(_url(application.id)).json()

        assert body["application_id"] == application.id
        assert body["company"] == "Northwind Labs"
        assert body["role"] == "Senior Backend Engineer"
        assert body["company_research"]
        assert body["company_facts"]
        assert body["questions"]
        assert body["questions_to_ask"]

    def test_falls_back_to_templates_without_a_provider(self, auth_client, application):
        """No OpenRouter key in tests — the briefing still renders in full."""
        body = auth_client.post(_url(application.id)).json()
        assert body["generated_with"] == "template"
        assert len(body["questions"]) >= 4

    def test_facts_are_lifted_from_the_posting(self, auth_client, application):
        facts = " | ".join(auth_client.post(_url(application.id)).json()["company_facts"])

        assert "Northwind Labs" in facts
        assert "San Francisco, CA" in facts
        assert "$160,000 - $200,000" in facts

    def test_questions_come_from_the_postings_own_requirements(
        self, auth_client, application
    ):
        questions = auth_client.post(_url(application.id)).json()["questions"]
        text = " ".join(q["question"].lower() for q in questions)

        # The posting names these; a generic list would not.
        assert "postgresql" in text or "fastapi" in text or "python" in text

    def test_universal_questions_backfill_a_thin_posting(
        self, auth_client, bare_application
    ):
        questions = auth_client.post(_url(bare_application.id)).json()["questions"]
        assert any("Walk me through your background" in q["question"] for q in questions)

    def test_each_question_can_explain_why_it_is_asked(self, auth_client, application):
        questions = auth_client.post(_url(application.id)).json()["questions"]
        assert any(q["why"] for q in questions)


class TestNeverInvents:
    def test_talking_points_only_claim_matched_skills(self, auth_client, application):
        """The resume has python/fastapi/aws; the posting also asks for Kafka.

        Kafka must not appear as a strength — the split comes from the fit
        scorer, not from prose.
        """
        body = auth_client.post(_url(application.id)).json()
        points = " ".join(p["point"].lower() for p in body["talking_points"])

        assert "python" in points or "fastapi" in points or "aws" in points
        assert "kafka" not in points

    def test_uncovered_requirements_surface_as_gaps(self, auth_client, application):
        gaps = " ".join(auth_client.post(_url(application.id)).json()["gaps"]).lower()
        # Asked for in the posting, absent from the resume fixture's skills.
        assert "kafka" in gaps or "kubernetes" in gaps

    def test_a_gap_is_never_also_a_talking_point(self, auth_client, application):
        body = auth_client.post(_url(application.id)).json()
        points = " ".join(p["point"].lower() for p in body["talking_points"])

        for gap in body["gaps"]:
            skill = gap.split(" — ")[0].lower()
            assert skill not in points

    def test_talking_points_cite_evidence_from_the_resume(self, auth_client, application):
        points = auth_client.post(_url(application.id)).json()["talking_points"]
        assert any(p["evidence"] for p in points)

    def test_says_so_when_there_is_nothing_to_research(
        self, auth_client, bare_application
    ):
        """The one output that could actively mislead someone walking into a
        room is a confident paragraph about a company we know nothing about."""
        body = auth_client.post(_url(bare_application.id)).json()

        assert "no job posting attached" in body["company_research"].lower()
        assert body["company_facts"] == []


class TestConversationContext:
    def test_the_recruiters_own_words_reach_the_briefing(
        self, auth_client, db_session, application
    ):
        thread = EmailThread(application_id=application.id, subject="Next steps")
        db_session.add(thread)
        db_session.flush()
        db_session.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.RECEIVED,
                status=EmailStatus.RECEIVED,
                from_address="talent@northwind.example",
                subject="Next steps",
                body_text="We'd like to dig into your payments work.",
                sent_at=None,
            )
        )
        db_session.commit()

        research = auth_client.post(_url(application.id)).json()["company_research"]
        assert "recruiter" in research.lower()

    def test_outbound_email_is_not_treated_as_the_recruiters_words(
        self, auth_client, db_session, application
    ):
        thread = EmailThread(application_id=application.id, subject="Hello")
        db_session.add(thread)
        db_session.flush()
        db_session.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.SENT,
                status=EmailStatus.SENT,
                to_address="talent@northwind.example",
                subject="Hello",
                body_text="Our own outreach.",
            )
        )
        db_session.commit()

        research = auth_client.post(_url(application.id)).json()["company_research"]
        assert "recruiter has already told you" not in research.lower()


class TestModelPath:
    def test_uses_the_model_when_one_answers(self, auth_client, application, monkeypatch):
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **k: (
                '{"company_research": "Northwind Labs builds payment rails. '
                'Expect depth on reliability.", '
                '"questions": [{"question": "How do you keep a payments API up?", '
                '"why": "Reliability is the job."}], '
                '"talking_points": [{"point": "Lead with your Python work.", '
                '"evidence": "Backend Engineer at Acme"}], '
                '"questions_to_ask": ["What does the first 90 days look like?"]}'
            ),
        )

        body = auth_client.post(_url(application.id)).json()
        assert body["generated_with"] == "llm"
        assert "payment rails" in body["company_research"]
        assert body["questions"][0]["question"] == "How do you keep a payments API up?"

    def test_a_malformed_section_falls_back_on_its_own(
        self, auth_client, application, monkeypatch
    ):
        """Good prose plus a broken question list shouldn't cost the whole
        briefing — each section falls back independently."""
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **k: (
                '{"company_research": "Northwind Labs builds payment rails.", '
                '"questions": "not a list", "talking_points": [], '
                '"questions_to_ask": []}'
            ),
        )

        body = auth_client.post(_url(application.id)).json()
        assert "payment rails" in body["company_research"]
        assert len(body["questions"]) >= 4          # template questions survived
        assert body["talking_points"]                # template points survived

    def test_a_failing_provider_degrades_to_the_template(
        self, auth_client, application, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise svc.OpenRouterError("no provider reachable")

        monkeypatch.setattr(svc, "chat_completion", boom)

        body = auth_client.post(_url(application.id)).json()
        assert body["generated_with"] == "template"
        assert body["company_research"]

    def test_reasoning_leakage_is_rejected(self, auth_client, application, monkeypatch):
        """Free-tier models answer in `reasoning`; that text must never ship."""
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **k: (
                '{"company_research": "We need to think about what the user is '
                'asking. First, let me consider the company. Okay, so the user '
                'wants research.", "questions": [], "talking_points": [], '
                '"questions_to_ask": []}'
            ),
        )

        body = auth_client.post(_url(application.id)).json()
        assert body["generated_with"] == "template"


class TestAccess:
    def test_any_status_may_be_prepped_for(self, auth_client, db_session, application):
        """Prep is not gated on reaching interview — a candidate who wants to
        prepare the moment they apply should not be told no."""
        application.status = ApplicationStatus.OUTREACH_SENT
        db_session.commit()

        assert auth_client.post(_url(application.id)).status_code == 200

    def test_an_unknown_application_is_404(self, auth_client, resume):
        assert auth_client.post(_url(999_999)).status_code == 404

    def test_another_users_application_is_not_reachable(
        self, auth_client, db_session, resume
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        campaign = Campaign(user_id=other.id, name="Theirs")
        recruiter = Recruiter(user_id=other.id, email="them@example.com")
        db_session.add_all([campaign, recruiter])
        db_session.flush()
        theirs = Application(
            user_id=other.id, campaign_id=campaign.id, recruiter_id=recruiter.id
        )
        db_session.add(theirs)
        db_session.commit()

        assert auth_client.post(_url(theirs.id)).status_code == 404

    def test_requires_authentication(self, client):
        # No `application` fixture here on purpose: it pulls in `auth_client`,
        # which would have already put a token on this very client.
        assert client.post(_url(1)).status_code == 401
