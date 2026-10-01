"""How a cover letter reaches the recruiter: inline in the body, or attached.

``cover_letter_delivery`` was a preference with nothing behind it — the letter
was written by the Smart Apply UI on request and never by the pipeline, and
``Email.cover_letter_id`` was a column no producer wrote and no sender read. So
"attachment" delivered nothing and "inline" delivered nothing either.

These cover the whole path: the fold that puts a letter inside an email body,
the PDF the attached version travels as, the composer that decides which of the
two happens, and the sender that has to carry it out and record what went.
"""
from __future__ import annotations

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.cover_letter import CoverLetter
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.job import JobPosting, JobStatus, job_fingerprint
from app.models.recruiter import Recruiter
from app.services import (
    auto_apply_service,
    cover_letter_service,
    gmail_service,
    recruiter_discovery,
    resume_pdf,
)
from app.services.career_scraper import Contact, ScrapeResult
from app.tasks import email_tasks

# --------------------------------------------------------------------------- #
# Folding a letter into an email body                                          #
# --------------------------------------------------------------------------- #


def _letter(**overrides) -> CoverLetter:
    row = CoverLetter(
        greeting="Dear Acme hiring team,",
        body="I'm writing about the Backend Engineer position at Acme.\n\n"
        "In my time as Backend Engineer at Northwind, I worked with Python.",
        sign_off="Best regards,\nJordan Candidate",
        job_title="Backend Engineer",
        job_company="Acme",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class TestFoldIntoBody:
    def test_letter_slots_in_under_the_email_greeting(self):
        body = "Hi Sam,\n\nI came across the Backend Engineer role.\n\nBest,\nJordan"
        folded = cover_letter_service.fold_into_body(body, _letter())

        # One greeting, one sign-off — the letter's own are dropped, because the
        # email already supplies both.
        assert folded.startswith("Hi Sam,\n\n")
        assert folded.count("Hi Sam,") == 1
        assert "Dear Acme hiring team," not in folded
        assert folded.rstrip().endswith("Best,\nJordan")

        # And the letter leads the message, ahead of the outreach pitch.
        assert folded.index("I'm writing about") < folded.index("I came across")

    def test_a_body_with_no_greeting_gets_a_plain_prepend(self):
        folded = cover_letter_service.fold_into_body(
            "Straight into the pitch with no greeting at all.", _letter()
        )
        assert folded.startswith("I'm writing about")
        assert folded.endswith("no greeting at all.")

    def test_a_long_first_line_is_not_mistaken_for_a_greeting(self):
        """Only a short line ending in a comma is a greeting."""
        body = (
            "I came across the Senior Backend Engineer role at Acme this morning, "
            "and wanted to reach out.\n\nBest,\nJordan"
        )
        folded = cover_letter_service.fold_into_body(body, _letter())
        assert folded.startswith("I'm writing about")

    def test_an_empty_letter_leaves_the_body_untouched(self):
        body = "Hi Sam,\n\nThe pitch.\n\nBest,\nJordan"
        assert cover_letter_service.fold_into_body(body, _letter(body="  ")) == body


# --------------------------------------------------------------------------- #
# The PDF an attached letter travels as                                        #
# --------------------------------------------------------------------------- #


class TestLetterPDF:
    def test_renders_a_pdf_carrying_the_letter(self, resume):
        content = resume_pdf.render_letter_pdf(_letter(), resume)
        assert content.startswith(b"%PDF")
        assert len(content) > 500

    def test_renders_without_a_resume_to_take_contact_details_from(self):
        assert resume_pdf.render_letter_pdf(_letter()).startswith(b"%PDF")

    def test_filename_names_the_candidate_and_the_employer(self, resume):
        assert (
            resume_pdf.letter_filename_for(_letter(), resume)
            == "jordan-candidate-acme-cover-letter.pdf"
        )

    def test_filename_without_a_resume_still_reads_cleanly(self):
        assert resume_pdf.letter_filename_for(_letter()) == "acme-cover-letter.pdf"


# --------------------------------------------------------------------------- #
# The composer honours the preference                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def stub_scraper(monkeypatch):
    def _fake(db, company, domain=None, **kwargs):
        slug = company.lower().replace(" ", "")
        return ScrapeResult(
            company=company,
            domain=f"{slug}.com",
            contacts=[
                Contact(
                    email=f"talent@{slug}.com",
                    name="Alex Recruiter",
                    title="Technical Recruiter",
                    kind="careers_page",
                    confidence=0.95,
                    source_url=f"https://{slug}.com/careers",
                )
            ],
            careers_url=f"https://{slug}.com/careers",
        )

    monkeypatch.setattr(recruiter_discovery, "get_or_scrape", _fake)


@pytest.fixture()
def ready(auth_client, connected_gmail, resume, stub_scraper, monkeypatch):
    """A fully onboarded user, with the network discovery step neutralised."""
    monkeypatch.setattr(auto_apply_service, "run_search", lambda db, search: None)
    return auth_client


def _seed_job(db, user, *, title="Senior Backend Engineer", company="Northwind", fit=90):
    posting = JobPosting(
        user_id=user.id,
        title=title,
        company=company,
        location="Remote",
        url=f"https://example.com/{company}".lower(),
        description=f"{title} at {company}. Python, FastAPI, AWS.",
        remote=True,
        source="test",
        fingerprint=job_fingerprint(title, company, None),
        status=JobStatus.NEW,
        fit_score=fit,
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    return posting


def _activate(client, **overrides):
    payload = {"is_active": True, "min_fit_score": 70, "daily_application_limit": 10}
    payload.update(overrides)
    return client.put("/api/v1/autopilot", json=payload)


def _outreach(db) -> Email:
    return db.query(Email).filter_by(direction=EmailDirection.SENT).one()


class TestComposerHonoursDelivery:
    def test_inline_delivery_folds_the_letter_into_the_body(
        self, ready, db_session, current_user
    ):
        _seed_job(db_session, current_user)
        _activate(ready, cover_letter_delivery="inline")

        assert ready.post("/api/v1/autopilot/run").json()["applied"] == 1

        letter = db_session.query(CoverLetter).one()
        email = _outreach(db_session)
        assert letter.body in email.body_text
        # Inline means inline: nothing for the sender to attach.
        assert email.cover_letter_id is None
        assert letter.delivery == "inline"

    def test_attachment_delivery_records_the_letter_on_the_email(
        self, ready, db_session, current_user
    ):
        _seed_job(db_session, current_user)
        _activate(ready, cover_letter_delivery="attachment")

        assert ready.post("/api/v1/autopilot/run").json()["applied"] == 1

        letter = db_session.query(CoverLetter).one()
        email = _outreach(db_session)
        assert email.cover_letter_id == letter.id
        # The body is the outreach email, not the outreach email plus a letter.
        assert letter.body not in (email.body_text or "")
        assert letter.delivery == "attachment"

    def test_the_letter_is_written_for_this_posting(
        self, ready, db_session, current_user
    ):
        posting = _seed_job(db_session, current_user, company="Northwind")
        _activate(ready, cover_letter_delivery="attachment")
        ready.post("/api/v1/autopilot/run")

        letter = db_session.query(CoverLetter).one()
        assert letter.job_posting_id == posting.id
        assert letter.job_company == "Northwind"
        assert letter.resume_id is not None

    def test_the_letter_is_linked_to_the_application_it_went_with(
        self, ready, db_session, current_user
    ):
        _seed_job(db_session, current_user)
        _activate(ready, cover_letter_delivery="attachment")
        ready.post("/api/v1/autopilot/run")

        application = db_session.query(Application).one()
        assert db_session.query(CoverLetter).one().application_id == application.id

    def test_letters_switched_off_means_no_letter_at_all(
        self, ready, db_session, current_user
    ):
        _seed_job(db_session, current_user)
        _activate(ready, cover_letter_enabled=False, cover_letter_delivery="attachment")

        assert ready.post("/api/v1/autopilot/run").json()["applied"] == 1
        assert db_session.query(CoverLetter).count() == 0
        assert _outreach(db_session).cover_letter_id is None

    def test_a_letter_the_user_edited_is_sent_as_they_wrote_it(
        self, ready, db_session, current_user, resume
    ):
        """Regeneration must not overwrite the user's own words on the way out."""
        posting = _seed_job(db_session, current_user)
        edited = CoverLetter(
            user_id=current_user.id,
            resume_id=resume.id,
            job_posting_id=posting.id,
            greeting="Dear Northwind hiring team,",
            body="My own words, which the pipeline may not touch.",
            sign_off="Best regards,\nJordan Candidate",
            delivery="inline",
            edited=True,
        )
        db_session.add(edited)
        db_session.commit()

        _activate(ready, cover_letter_delivery="attachment")
        ready.post("/api/v1/autopilot/run")

        db_session.refresh(edited)
        assert edited.body == "My own words, which the pipeline may not touch."
        # The words are theirs; how it travels is still the live preference.
        assert edited.delivery == "attachment"
        assert _outreach(db_session).cover_letter_id == edited.id

    def test_a_letter_failure_does_not_cost_the_application(
        self, ready, db_session, current_user, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("the model fell over")

        monkeypatch.setattr(cover_letter_service, "upsert_letter", _boom)
        _seed_job(db_session, current_user)
        _activate(ready, cover_letter_delivery="attachment")

        assert ready.post("/api/v1/autopilot/run").json()["applied"] == 1
        assert _outreach(db_session).cover_letter_id is None


# --------------------------------------------------------------------------- #
# The sender attaches it                                                       #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def posting(db_session, current_user) -> JobPosting:
    return _seed_job(db_session, current_user, company="Acme")


@pytest.fixture()
def application(db_session, current_user, resume, posting) -> Application:
    recruiter = Recruiter(
        user_id=current_user.id, email="recruiter@acme.com", name="Sam", company="Acme"
    )
    db_session.add(recruiter)
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


@pytest.fixture()
def stored_letter(db_session, current_user, resume, posting) -> CoverLetter:
    row = CoverLetter(
        user_id=current_user.id,
        resume_id=resume.id,
        job_posting_id=posting.id,
        greeting="Dear Acme hiring team,",
        body="I'm writing about the Senior Backend Engineer position at Acme.",
        sign_off="Best regards,\nJordan Candidate",
        job_title="Senior Backend Engineer",
        job_company="Acme",
        delivery="attachment",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _queue(db, application, *, cover_letter_id=None) -> Email:
    thread = EmailThread(application_id=application.id, subject="Jordan — Backend")
    db.add(thread)
    db.flush()
    row = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.QUEUED,
        to_address="recruiter@acme.com",
        subject="Jordan — Backend",
        body_text="Hi Sam,\n\nThe pitch.\n\nBest,\nJordan",
        cover_letter_id=cover_letter_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture()
def capture_send(monkeypatch, db_session):
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


class TestSenderAttachesTheLetter:
    def test_the_letter_goes_out_beside_the_resume(
        self, db_session, application, stored_letter, connected_gmail, capture_send
    ):
        email = _queue(db_session, application, cover_letter_id=stored_letter.id)

        assert email_tasks.send_outreach_email(email.id)["status"] == "sent"

        attachments = capture_send[0]["attachments"]
        assert len(attachments) == 2
        # The resume leads — it is what the recruiter opens first.
        assert attachments[0].filename == "jordan-candidate-resume.pdf"
        assert attachments[1].filename == "jordan-candidate-acme-cover-letter.pdf"
        assert attachments[1].content.startswith(b"%PDF")

        db_session.refresh(email)
        assert email.cover_letter_id == stored_letter.id

    def test_the_letter_is_rendered_as_it_stands_at_send_time(
        self, db_session, application, stored_letter, connected_gmail, capture_send
    ):
        """A draft can wait in the review queue; the last edit is what travels."""
        email = _queue(db_session, application, cover_letter_id=stored_letter.id)
        first = resume_pdf.render_letter_pdf(stored_letter, None)

        cover_letter_service.apply_edit(
            db_session, stored_letter, "Rewritten while the draft waited."
        )
        db_session.commit()

        email_tasks.send_outreach_email(email.id)
        assert capture_send[0]["attachments"][1].content != first

    def test_an_email_with_no_letter_carries_only_the_resume(
        self, db_session, application, stored_letter, connected_gmail, capture_send
    ):
        """Inline delivery, follow-ups and replies all leave cover_letter_id null."""
        email = _queue(db_session, application)

        email_tasks.send_outreach_email(email.id)

        assert len(capture_send[0]["attachments"]) == 1
        db_session.refresh(email)
        assert email.cover_letter_id is None

    def test_a_letter_that_cannot_be_rendered_is_not_claimed_as_sent(
        self, db_session, application, stored_letter, connected_gmail, capture_send,
        monkeypatch,
    ):
        def _boom(letter, resume=None):
            raise RuntimeError("reportlab exploded")

        monkeypatch.setattr(resume_pdf, "render_letter_pdf", _boom)
        email = _queue(db_session, application, cover_letter_id=stored_letter.id)

        assert email_tasks.send_outreach_email(email.id)["status"] == "sent"
        # The resume still travelled; the letter didn't, and the row says so.
        assert len(capture_send[0]["attachments"]) == 1
        db_session.refresh(email)
        assert email.status == EmailStatus.SENT
        assert email.cover_letter_id is None

    def test_a_deleted_letter_does_not_block_the_send(
        self, db_session, application, stored_letter, connected_gmail, capture_send
    ):
        email = _queue(db_session, application, cover_letter_id=stored_letter.id)
        db_session.delete(stored_letter)
        db_session.commit()

        assert email_tasks.send_outreach_email(email.id)["status"] == "sent"
        assert len(capture_send[0]["attachments"]) == 1

    def test_the_message_is_multipart_with_both_documents(
        self, db_session, application, stored_letter
    ):
        """The MIME builder carries two attachments as readily as one."""
        import base64
        from email import message_from_bytes

        raw = gmail_service._build_mime(
            "candidate@gmail.com",
            "recruiter@acme.com",
            "Jordan — Backend Engineer",
            "The pitch.",
            "footer",
            attachments=[
                gmail_service.Attachment(filename="resume.pdf", content=b"%PDF-resume"),
                gmail_service.Attachment(filename="letter.pdf", content=b"%PDF-letter"),
            ],
        )
        parts = message_from_bytes(base64.urlsafe_b64decode(raw)).get_payload()

        assert len(parts) == 3
        assert [p.get_filename() for p in parts[1:]] == ["resume.pdf", "letter.pdf"]
