"""The resume that travels with an outbound email.

Covers the whole path the bug ran through: the MIME builder (which had no
attachment support at all), the resolver that picks *which* resume, and the
sender that has to put the two together and record what went out.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
from email import message_from_bytes

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.job import JobPosting, job_fingerprint
from app.models.profile import Profile
from app.models.recruiter import Recruiter
from app.models.resume import Resume
from app.models.tailored_resume import TailoredResume
from app.models.user import User
from app.services import email_attachments, gmail_service, resume_pdf
from app.services.gmail_service import Attachment
from app.tasks import email_tasks

PDF_BYTES = b"%PDF-1.4 tailored resume bytes"


def _decode(raw: str):
    """The MIME message the builder handed to the Gmail API."""
    return message_from_bytes(base64.urlsafe_b64decode(raw))


# --------------------------------------------------------------------------- #
# MIME construction                                                            #
# --------------------------------------------------------------------------- #


class TestBuildMime:
    def test_attachment_produces_a_multipart_message(self):
        raw = gmail_service._build_mime(
            "candidate@gmail.com",
            "recruiter@acme.com",
            "Jordan — Backend Engineer",
            "Hi there, I'd love to talk.",
            "footer",
            attachments=[Attachment(filename="jordan-acme.pdf", content=PDF_BYTES)],
        )
        message = _decode(raw)

        assert message.is_multipart()
        parts = message.get_payload()
        assert len(parts) == 2

        body, attached = parts
        assert body.get_content_type() == "text/plain"
        assert "I'd love to talk" in body.get_payload(decode=True).decode()

        assert attached.get_content_type() == "application/pdf"
        assert attached.get_filename() == "jordan-acme.pdf"
        assert attached.get_payload(decode=True) == PDF_BYTES
        assert attached.get("Content-Disposition", "").startswith("attachment")

    def test_headers_survive_the_multipart_switch(self):
        """The deliverability headers must stay on the outer message."""
        raw = gmail_service._build_mime(
            "candidate@gmail.com",
            "recruiter@acme.com",
            "Subject line",
            "Body",
            "footer",
            display_name="Jordan Candidate",
            in_reply_to="<abc@mail>",
            references="<abc@mail>",
            unsubscribe_url="https://talentping.app/u?e=x",
            attachments=[Attachment(filename="r.pdf", content=PDF_BYTES)],
        )
        message = _decode(raw)

        assert message["To"] == "recruiter@acme.com"
        assert message["From"] == "Jordan Candidate <candidate@gmail.com>"
        assert message["Subject"] == "Subject line"
        assert message["In-Reply-To"] == "<abc@mail>"
        assert message["References"] == "<abc@mail>"
        assert "https://talentping.app/u?e=x" in message["List-Unsubscribe"]
        assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_no_attachment_stays_single_part(self):
        """Regression guard: attachment-free mail keeps its original shape."""
        raw = gmail_service._build_mime(
            "candidate@gmail.com", "r@acme.com", "Subject", "Body text", "footer"
        )
        message = _decode(raw)

        assert not message.is_multipart()
        assert message.get_content_type() == "text/plain"
        payload = message.get_payload(decode=True).decode()
        assert "Body text" in payload
        assert "footer" in payload

    def test_non_pdf_mime_type_is_preserved(self):
        raw = gmail_service._build_mime(
            "c@gmail.com",
            "r@acme.com",
            "S",
            "B",
            "",
            attachments=[
                Attachment(filename="r.docx", content=b"docx", mime_type="application/msword")
            ],
        )
        attached = _decode(raw).get_payload()[1]
        assert attached.get_content_type() == "application/msword"


# --------------------------------------------------------------------------- #
# Choosing the resume                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def posting(db_session, current_user) -> JobPosting:
    row = JobPosting(
        user_id=current_user.id,
        title="Senior Backend Engineer",
        company="Acme",
        url="https://acme.com/jobs/1",
        fingerprint=job_fingerprint("Senior Backend Engineer", "Acme", None),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def nursing_resume(db_session, current_user) -> Resume:
    """A second document, for the candidate's other intent."""
    row = Resume(
        user_id=current_user.id,
        filename="casey-nursing.pdf",
        raw_text="Casey Nightingale — Registered Nurse. Triage, patient care.",
        full_name="Casey Nightingale",
        headline="Registered Nurse",
        skills=["triage", "patient care"],
        experience=[{"company": "Mercy", "title": "RN", "start": "2019", "end": "2024"}],
        is_default=False,
        parsed_with="heuristic",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def other_user_profile(db_session, nursing_resume) -> Profile:
    """A profile belonging to somebody else entirely."""
    stranger = User(email="stranger@example.com", hashed_password="x")
    db_session.add(stranger)
    db_session.flush()
    profile = Profile(
        user_id=stranger.id,
        resume_id=nursing_resume.id,
        name="Registered Nurse",
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()
    return profile


@pytest.fixture()
def recruiter(db_session, current_user) -> Recruiter:
    row = Recruiter(
        user_id=current_user.id, email="recruiter@acme.com", name="Sam", company="Acme"
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def application(db_session, current_user, resume, posting, recruiter) -> Application:
    campaign = Campaign(
        user_id=current_user.id,
        name="Autopilot",
        resume_id=resume.id,
        status=CampaignStatus.ACTIVE,
    )
    db_session.add(campaign)
    db_session.flush()
    row = Application(
        user_id=current_user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        job_posting_id=posting.id,
        status=ApplicationStatus.QUEUED,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _tailored(db, user, resume, *, job_posting_id, company, with_pdf=True) -> TailoredResume:
    row = TailoredResume(
        user_id=user.id,
        resume_id=resume.id,
        job_posting_id=job_posting_id,
        job_title="Senior Backend Engineer",
        job_company=company,
        pdf_bytes=PDF_BYTES if with_pdf else None,
        pdf_filename=f"jordan-{company.lower()}.pdf" if with_pdf else None,
        pdf_generated_at=datetime.now(UTC) if with_pdf else None,
    )
    db.add(row)
    db.commit()
    return row


class TestResolveResume:
    def test_prefers_the_resume_tailored_to_this_posting(
        self, db_session, current_user, resume, application
    ):
        _tailored(
            db_session,
            current_user,
            resume,
            job_posting_id=application.job_posting_id,
            company="Acme",
        )
        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )

        assert found is not None
        assert found.filename == "jordan-acme.pdf"
        assert found.content == PDF_BYTES

    def test_uses_the_newest_run_for_the_posting(
        self, db_session, current_user, resume, application
    ):
        _tailored(
            db_session,
            current_user,
            resume,
            job_posting_id=application.job_posting_id,
            company="Acme",
        )
        newer = _tailored(
            db_session,
            current_user,
            resume,
            job_posting_id=application.job_posting_id,
            company="AcmeV2",
        )
        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        assert found.filename == newer.pdf_filename

    def test_never_sends_a_resume_tailored_to_another_posting(
        self, db_session, current_user, resume, application
    ):
        """Initech's recruiter must not receive the resume headed 'Acme'."""
        other = JobPosting(
            user_id=current_user.id,
            title="Backend Engineer",
            company="Initech",
            fingerprint=job_fingerprint("Backend Engineer", "Initech", None),
        )
        db_session.add(other)
        db_session.commit()
        _tailored(
            db_session, current_user, resume, job_posting_id=other.id, company="Initech"
        )

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        # Falls back to a generic render rather than reusing Initech's.
        assert found is not None
        assert found.content != PDF_BYTES
        assert found.filename == "jordan-candidate-resume.pdf"

    def test_falls_back_to_a_rendered_base_resume(
        self, db_session, current_user, application
    ):
        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )

        assert found is not None
        assert found.filename == "jordan-candidate-resume.pdf"
        assert found.content.startswith(b"%PDF")

    def test_ignores_a_tailored_run_with_no_rendered_pdf(
        self, db_session, current_user, resume, application
    ):
        _tailored(
            db_session,
            current_user,
            resume,
            job_posting_id=application.job_posting_id,
            company="Acme",
            with_pdf=False,
        )
        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        assert found is not None
        assert found.content != PDF_BYTES

    def test_the_matched_profile_decides_which_resume_travels(
        self, db_session, current_user, resume, application, nursing_resume
    ):
        """Two documents, one intent: the profile's resume wins over the default."""
        profile = Profile(
            user_id=current_user.id,
            resume_id=nursing_resume.id,
            name="Registered Nurse",
            is_active=True,
        )
        db_session.add(profile)
        db_session.flush()
        application.profile_id = profile.id
        db_session.commit()

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )

        assert found is not None
        # The nursing resume, not Jordan Candidate's default backend one.
        assert found.filename == "casey-nightingale-resume.pdf"

    def test_a_profile_without_a_resume_falls_through(
        self, db_session, current_user, resume, application
    ):
        """Losing the document costs the profile its resume, not the send."""
        profile = Profile(
            user_id=current_user.id, resume_id=None, name="Tech Lead", is_active=True
        )
        db_session.add(profile)
        db_session.flush()
        application.profile_id = profile.id
        db_session.commit()

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        assert found is not None
        assert found.filename == "jordan-candidate-resume.pdf"

    def test_another_users_profile_is_never_read(
        self, db_session, current_user, resume, application, other_user_profile
    ):
        application.profile_id = other_user_profile.id
        db_session.commit()

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        assert found is not None
        assert found.filename == "jordan-candidate-resume.pdf"

    def test_a_tailored_run_still_outranks_the_profile_resume(
        self, db_session, current_user, resume, application, nursing_resume
    ):
        """Tailored to *this* posting beats a generic render of any resume."""
        profile = Profile(
            user_id=current_user.id,
            resume_id=nursing_resume.id,
            name="Registered Nurse",
            is_active=True,
        )
        db_session.add(profile)
        db_session.flush()
        application.profile_id = profile.id
        _tailored(
            db_session,
            current_user,
            resume,
            job_posting_id=application.job_posting_id,
            company="Acme",
        )

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        assert found.filename == "jordan-acme.pdf"

    def test_no_resume_on_file_yields_nothing(self, db_session, current_user, recruiter):
        campaign = Campaign(user_id=current_user.id, name="C", status=CampaignStatus.ACTIVE)
        db_session.add(campaign)
        db_session.flush()
        app_row = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=ApplicationStatus.QUEUED,
        )
        db_session.add(app_row)
        db_session.commit()

        assert (
            email_attachments.resume_attachment_for_application(
                db_session, app_row, current_user
            )
            is None
        )

    def test_render_failure_degrades_instead_of_raising(
        self, db_session, current_user, application, monkeypatch
    ):
        def _boom(resume, tailored):
            raise RuntimeError("reportlab exploded")

        monkeypatch.setattr(resume_pdf, "render_pdf", _boom)
        assert (
            email_attachments.resume_attachment_for_application(
                db_session, application, current_user
            )
            is None
        )

    def test_missing_reportlab_degrades_instead_of_raising(
        self, db_session, current_user, application, monkeypatch
    ):
        monkeypatch.setattr(resume_pdf, "is_available", lambda: False)
        assert (
            email_attachments.resume_attachment_for_application(
                db_session, application, current_user
            )
            is None
        )


# --------------------------------------------------------------------------- #
# The sender wires the two together                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def queued_email(db_session, application) -> Email:
    thread = EmailThread(application_id=application.id, subject="Jordan — Backend")
    db_session.add(thread)
    db_session.flush()
    row = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.QUEUED,
        to_address="recruiter@acme.com",
        subject="Jordan — Backend",
        body_text="Hi there,",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def capture_send(monkeypatch, db_session):
    """Point the task at the test session and record what Gmail was asked to send."""
    monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    sent: list[dict] = []

    def _fake_send(**kwargs):
        sent.append(kwargs)
        return gmail_service.SentMessage(
            gmail_message_id="gmail-1", gmail_thread_id="thread-1"
        )

    monkeypatch.setattr(gmail_service, "send_email", _fake_send)
    return sent


class TestSenderAttachesTheResume:
    def test_outreach_goes_out_with_the_tailored_resume(
        self, db_session, current_user, resume, application, queued_email,
        connected_gmail, capture_send,
    ):
        _tailored(
            db_session,
            current_user,
            resume,
            job_posting_id=application.job_posting_id,
            company="Acme",
        )

        result = email_tasks.send_outreach_email(queued_email.id)

        assert result["status"] == "sent"
        attachments = capture_send[0]["attachments"]
        assert len(attachments) == 1
        assert attachments[0].filename == "jordan-acme.pdf"
        assert attachments[0].content == PDF_BYTES

        db_session.refresh(queued_email)
        assert queued_email.attachment_filename == "jordan-acme.pdf"

    def test_send_still_happens_when_no_resume_can_be_built(
        self, db_session, current_user, application, queued_email, connected_gmail,
        capture_send, monkeypatch,
    ):
        """A missing attachment must never swallow the application email."""
        monkeypatch.setattr(resume_pdf, "is_available", lambda: False)

        result = email_tasks.send_outreach_email(queued_email.id)

        assert result["status"] == "sent"
        assert not capture_send[0]["attachments"]

        db_session.refresh(queued_email)
        assert queued_email.status == EmailStatus.SENT
        # Null records that this one genuinely went out without a resume.
        assert queued_email.attachment_filename is None

    def test_a_resolver_crash_does_not_block_the_send(
        self, db_session, application, queued_email, connected_gmail, capture_send,
        monkeypatch,
    ):
        def _boom(db, app_row, user):
            raise RuntimeError("db went away")

        monkeypatch.setattr(
            email_attachments, "resume_attachment_for_application", _boom
        )
        result = email_tasks.send_outreach_email(queued_email.id)

        assert result["status"] == "sent"
        assert queued_email.attachment_filename is None

    def test_reply_in_an_existing_thread_also_carries_the_resume(
        self, db_session, current_user, resume, application, queued_email,
        connected_gmail, capture_send,
    ):
        """Recruiter replies go through the same sender, so they attach too."""
        thread = db_session.get(EmailThread, queued_email.thread_id)
        thread.gmail_thread_id = "existing-thread"
        db_session.commit()

        email_tasks.send_outreach_email(queued_email.id)

        assert capture_send[0]["thread_id"] == "existing-thread"
        assert len(capture_send[0]["attachments"]) == 1


# --------------------------------------------------------------------------- #
# What a draft says it will carry                                              #
# --------------------------------------------------------------------------- #


class TestAttachmentPlan:
    """Attachments resolve at send time, which left a draft with nothing to show.

    That is the right design — a draft can wait days in the review queue, and
    what should go out is the resume as it stands on the day, not as it stood
    when the draft was written. But it made "it drafted a reply and I can't see
    the attachment" indistinguishable from "the reply has no attachment", and
    only one of those is a problem. The plan runs the same resolvers without
    rendering, so a reviewer can tell which.
    """

    def test_the_plan_names_the_resume_that_will_travel(
        self, db_session, current_user, resume, application, queued_email
    ):
        _tailored(
            db_session,
            current_user,
            resume,
            job_posting_id=application.job_posting_id,
            company="Acme",
        )

        plan = email_attachments.plan_for_email(db_session, queued_email)

        assert plan.filenames == ["jordan-acme.pdf"]
        assert plan.reason is None

    def test_the_plan_agrees_with_what_actually_gets_attached(
        self, db_session, current_user, resume, application, queued_email
    ):
        """A preview that disagrees with the send is worse than no preview."""
        plan = email_attachments.plan_for_email(db_session, queued_email)
        files = email_attachments.files_for_email(db_session, queued_email)

        assert plan.filenames == [a.filename for a in files.attachments]

    def test_the_plan_does_not_render_anything(
        self, db_session, queued_email, monkeypatch
    ):
        """It is read on every page load; a PDF render per read would be waste."""
        def _boom(*args, **kwargs):
            raise AssertionError("the plan must not render a PDF")

        monkeypatch.setattr(resume_pdf, "render_pdf", _boom)

        plan = email_attachments.plan_for_email(db_session, queued_email)

        assert plan.filenames  # still named it, without building it

    def test_no_resume_on_file_says_so_instead_of_going_quiet(
        self, db_session, current_user, resume, queued_email
    ):
        db_session.delete(resume)
        db_session.commit()

        plan = email_attachments.plan_for_email(db_session, queued_email)

        assert plan.filenames == []
        assert "No resume on file" in plan.reason

    def test_a_missing_renderer_says_so_too(
        self, db_session, queued_email, monkeypatch
    ):
        monkeypatch.setattr(resume_pdf, "is_available", lambda: False)

        plan = email_attachments.plan_for_email(db_session, queued_email)

        assert plan.filenames == []
        assert "PDF rendering is unavailable" in plan.reason

    def test_a_broken_lookup_never_breaks_the_page(
        self, db_session, queued_email, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("db went away")

        monkeypatch.setattr(email_attachments, "_base_resume_for", _boom)

        plan = email_attachments.plan_for_email(db_session, queued_email)

        assert plan.filenames == []
