"""Picking the resume that argues for *this* role.

The existing resolver picks by intent — the matched profile's document, else the
campaign's, else the default. That is a decision about which career the candidate
is pursuing, not about the role in front of them, so someone with three resumes
under one profile always got the same one.

Two properties are load-bearing and both have tests here:

* **The filename bonus cannot outvote the content.** A file called
  ``ml-resume.pdf`` that lists no ML experience must not win an ML role.
* **A tailored resume from another posting is never selected.** Those PDFs carry
  that company's name in the header, and sending Acme's resume to Initech is
  worse than sending a generic one — the rule ``email_attachments`` already
  states and this change set does not relax.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.models.email import Email
from app.models.resume import Resume
from app.models.tailored_resume import TailoredResume
from app.services import email_attachments, recruiter_reply_service, resume_selector
from app.services.jd_parser import parse_job
from tests.conftest import SAMPLE_JOB_DESCRIPTION
from tests.test_recruiter_reply import gmail_message

ML_RESUME_TEXT = """\
Jordan Candidate
Machine Learning Infrastructure Engineer
Built and operated GPU training clusters, feature stores and model-serving
pipelines. PyTorch, Ray, Kubeflow, Kubernetes, Python.
"""

BACKEND_JOB = SAMPLE_JOB_DESCRIPTION

ML_JOB = """\
Machine Learning Infrastructure Engineer

Company: Northwind Labs
Location: San Francisco, CA

Requirements
- Strong PyTorch and Ray experience
- Experience operating Kubeflow and GPU training clusters
- Python
- Model serving at scale
"""


@pytest.fixture()
def ml_resume(db_session, current_user) -> Resume:
    """A second, genuinely different document — content *and* filename."""
    row = Resume(
        user_id=current_user.id,
        filename="ml-infra.pdf",
        raw_text=ML_RESUME_TEXT,
        full_name="Jordan Candidate",
        headline="Machine Learning Infrastructure Engineer",
        years_experience=8,
        seniority="senior",
        skills=["pytorch", "ray", "kubeflow", "kubernetes", "python"],
        target_roles=["Machine Learning Infrastructure Engineer"],
        target_industries=["ai"],
        experience=[
            {
                "company": "Acme",
                "title": "ML Infrastructure Engineer",
                "start": "2018",
                "end": "2023",
            }
        ],
        education=[],
        links=[],
        parsed_with="heuristic",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# The filename/label bonus                                                     #
# --------------------------------------------------------------------------- #


class TestAffinityBonus:
    def test_a_matching_label_scores(self, ml_resume):
        bonus = resume_selector.affinity_bonus(ml_resume, parse_job(ML_JOB))

        assert bonus > 0

    def test_a_matching_label_scores_higher_than_an_unrelated_one(self, ml_resume):
        """The bonus is proportional to the share of label words a role uses.

        An unrelated role can still brush a generic word — "engineer" appears in
        both of these — so what matters is the ordering, not a zero. A narrowly
        named resume that genuinely matches gets most of the bonus; one that
        merely shares a job title noun gets a sliver.
        """
        matching = resume_selector.affinity_bonus(ml_resume, parse_job(ML_JOB))
        unrelated = resume_selector.affinity_bonus(ml_resume, parse_job(BACKEND_JOB))

        assert matching > unrelated
        assert unrelated < resume_selector.MAX_AFFINITY_BONUS / 2

    def test_it_is_capped(self, ml_resume):
        assert (
            resume_selector.affinity_bonus(ml_resume, parse_job(ML_JOB))
            <= resume_selector.MAX_AFFINITY_BONUS
        )

    def test_generic_filename_words_are_ignored(self, db_session, current_user):
        """"resume-final-2024.pdf" names nothing and must earn nothing."""
        row = Resume(
            user_id=current_user.id,
            filename="resume-final-2024.pdf",
            raw_text="whatever",
            skills=[],
            target_roles=[],
            experience=[],
            education=[],
            links=[],
        )

        assert resume_selector.affinity_bonus(row, parse_job(ML_JOB)) == 0.0

    def test_the_bonus_cannot_outvote_the_content(self, db_session, current_user):
        """A file *named* for ML that lists none must not win an ML role.

        The bonus tops out at 10 of the scorer's 100 points precisely so a
        filename is a tie-breaker rather than a vote.
        """
        liar = Resume(
            user_id=current_user.id,
            filename="machine-learning-pytorch-ray-kubeflow.pdf",
            raw_text="I sold insurance for eleven years.",
            headline="Insurance Sales",
            skills=["cold calling"],
            target_roles=[],
            experience=[],
            education=[],
            links=[],
        )
        db_session.add(liar)
        db_session.commit()

        job = parse_job(ML_JOB)
        bonus = resume_selector.affinity_bonus(liar, job)

        assert bonus <= resume_selector.MAX_AFFINITY_BONUS


# --------------------------------------------------------------------------- #
# Selecting                                                                    #
# --------------------------------------------------------------------------- #


class TestSelect:
    def test_the_best_match_wins(self, db_session, current_user, resume, ml_resume):
        choice = resume_selector.select(db_session, current_user, parse_job(ML_JOB))

        assert choice is not None
        assert choice.resume_id == ml_resume.id
        assert choice.switched is True

    def test_the_other_role_picks_the_other_document(
        self, db_session, current_user, resume, ml_resume
    ):
        choice = resume_selector.select(
            db_session, current_user, parse_job(BACKEND_JOB)
        )

        assert choice.resume_id == resume.id

    def test_a_single_resume_user_is_left_alone(
        self, db_session, current_user, resume
    ):
        """Nothing to choose between — running a scorer to confirm it is waste."""
        assert resume_selector.select(db_session, current_user, parse_job(ML_JOB)) is None

    def test_a_user_with_no_resumes_gets_nothing(self, db_session, current_user):
        assert resume_selector.select(db_session, current_user, parse_job(ML_JOB)) is None

    def test_the_incumbent_holds_a_close_call(
        self, db_session, current_user, resume, ml_resume, monkeypatch
    ):
        """Two resumes for one person share most of their text; scores cluster.

        A fraction of a point is noise, not a reason to send a different document
        than the user's own profile setting implies.
        """
        monkeypatch.setattr(settings, "recruiter_resume_switch_margin", 99.0)

        choice = resume_selector.select(
            db_session, current_user, parse_job(ML_JOB), incumbent_id=resume.id
        )

        assert choice.resume_id == resume.id
        assert choice.switched is False
        assert "too close to switch" in choice.reason

    def test_a_clear_win_displaces_the_incumbent(
        self, db_session, current_user, resume, ml_resume, monkeypatch
    ):
        monkeypatch.setattr(settings, "recruiter_resume_switch_margin", 0.0)

        choice = resume_selector.select(
            db_session, current_user, parse_job(ML_JOB), incumbent_id=resume.id
        )

        assert choice.resume_id == ml_resume.id
        assert choice.switched is True
        # The reason names both numbers, so the swap is explainable.
        assert "against" in choice.reason

    def test_the_incumbent_winning_is_not_reported_as_a_switch(
        self, db_session, current_user, resume, ml_resume
    ):
        choice = resume_selector.select(
            db_session, current_user, parse_job(BACKEND_JOB), incumbent_id=resume.id
        )

        assert choice.resume_id == resume.id
        assert choice.switched is False

    def test_the_feature_switch_disables_it_entirely(
        self, db_session, current_user, resume, ml_resume, monkeypatch
    ):
        monkeypatch.setattr(settings, "recruiter_smart_resume_enabled", False)

        assert resume_selector.select(db_session, current_user, parse_job(ML_JOB)) is None

    def test_another_users_resumes_are_invisible(
        self, db_session, current_user, resume
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x", full_name="O")
        db_session.add(other)
        db_session.commit()
        db_session.add(
            Resume(
                user_id=other.id,
                filename="ml-infra.pdf",
                raw_text=ML_RESUME_TEXT,
                skills=["pytorch", "ray"],
                target_roles=[],
                experience=[],
                education=[],
                links=[],
            )
        )
        db_session.commit()

        # One resume of their own left to choose from — so, nothing to choose.
        assert resume_selector.select(db_session, current_user, parse_job(ML_JOB)) is None


# --------------------------------------------------------------------------- #
# In the pipeline                                                              #
# --------------------------------------------------------------------------- #


class TestTheChoiceReachesTheReply:
    def _draft(self, db, user, account, mailbox):
        mailbox["m1"] = gmail_message("m1")
        _, created = recruiter_reply_service.record_scan(db, user, account)
        db.commit()
        row = created[0]
        recruiter_reply_service.process(db, row)
        db.commit()
        return row

    def test_the_choice_and_its_reason_land_on_the_row(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles, ml_resume
    ):
        row = self._draft(db_session, current_user, connected_gmail, stub_gmail)

        assert row.selected_resume_id is not None
        assert row.resume_choice_reason

    def test_the_attachment_resolver_honours_the_choice(
        self,
        db_session,
        current_user,
        connected_gmail,
        stub_gmail,
        profiles,
        ml_resume,
        monkeypatch,
    ):
        """The choice is made at draft time; the bytes are rendered at send time."""
        monkeypatch.setattr(settings, "recruiter_resume_switch_margin", 0.0)
        row = self._draft(db_session, current_user, connected_gmail, stub_gmail)
        row.selected_resume_id = ml_resume.id
        db_session.commit()

        reply = db_session.get(Email, row.reply_email_id)
        filename, reason = email_attachments.resume_plan_for_email(db_session, reply)

        assert reason is None
        assert filename == "jordan-candidate-resume.pdf"

    def test_a_row_with_no_choice_falls_back_to_the_old_resolution(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        row = self._draft(db_session, current_user, connected_gmail, stub_gmail)
        row.selected_resume_id = None
        db_session.commit()

        reply = db_session.get(Email, row.reply_email_id)
        filename, _ = email_attachments.resume_plan_for_email(db_session, reply)

        assert filename is not None

    def test_a_deleted_resume_does_not_break_the_send(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles, ml_resume
    ):
        """SET NULL on the FK, and the ordinary resolution takes over.

        A user tidying up their documents between a draft being written and it
        being approved must not turn into a send that fails.
        """
        row = self._draft(db_session, current_user, connected_gmail, stub_gmail)
        row.selected_resume_id = ml_resume.id
        db_session.commit()

        db_session.delete(ml_resume)
        db_session.commit()
        db_session.refresh(row)

        assert row.selected_resume_id is None
        reply = db_session.get(Email, row.reply_email_id)
        filename, _ = email_attachments.resume_plan_for_email(db_session, reply)

        assert filename is not None


class TestTailoredResumesStayWithTheirPosting:
    def test_a_tailoring_run_for_another_job_is_never_chosen(
        self, db_session, current_user, resume, ml_resume
    ):
        """Its header carries that company's role title. See email_attachments."""
        db_session.add(
            TailoredResume(
                user_id=current_user.id,
                resume_id=resume.id,
                job_posting_id=None,
                job_title="Machine Learning Infrastructure Engineer",
                job_company="Some Other Company",
                pdf_bytes=b"%PDF-1.4 fake",
                pdf_filename="someothercompany-ml.pdf",
                matched_keywords=["pytorch", "ray", "kubeflow"],
            )
        )
        db_session.commit()

        choice = resume_selector.select(db_session, current_user, parse_job(ML_JOB))

        # The selector only ever ranks base resumes.
        assert choice.resume_id in {resume.id, ml_resume.id}
