"""What actually reaches the recruiter, and the user's say over it.

Two bugs, one subject.

The first is "the wrong CV was attached". The selection was never the problem —
it picks between the candidate's documents perfectly well. The problem was that
*no* selection could help, because the uploaded file was parsed and thrown away
and every outbound resume was a PDF rebuilt from the extracted text, under a
filename derived from the candidate's name. Two resumes for one person rendered
to the same name and neither was the document they uploaded. These tests pin the
original bytes travelling verbatim, and the re-render surviving only as the
fallback for rows that predate the column.

The second is that there was no way to change any of it. A draft resolved its
own attachments and the user could read the filename and nothing else. So:
pinning a resume, removing one, and attaching files of their own — asserted
through the API a person actually presses, and then through the sender, because
a control that changes a draft and not the send is worse than no control.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_attachment import EmailAttachment
from app.models.email_thread import EmailThread
from app.models.job import JobPosting, job_fingerprint
from app.models.recruiter import Recruiter
from app.models.resume import Resume
from app.models.tailored_resume import TailoredResume
from app.models.user import User
from app.routers import inbox as inbox_router
from app.services import document_preview, email_attachments, resume_pdf
from tests.conftest import SAMPLE_RESUME_TEXT
from tests.docx_fixtures import RESUME_DOCX

PDF = b"%PDF-1.4 the candidate's actual resume, byte for byte"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def uploaded_resume(db_session, current_user) -> Resume:
    """A resume stored the way the fixed uploader stores one: with its file."""
    row = Resume(
        user_id=current_user.id,
        filename="jordan-platform-eng.pdf",
        raw_text=SAMPLE_RESUME_TEXT,
        full_name="Jordan Candidate",
        headline="Staff Platform Engineer",
        skills=["kubernetes", "terraform"],
        target_roles=["Staff Platform Engineer"],
        experience=[{"company": "Acme", "title": "SRE", "start": "2019", "end": "2024"}],
        is_default=True,
        parsed_with="heuristic",
        file_bytes=PDF,
        file_content_type="application/pdf",
        file_size=len(PDF),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def second_resume(db_session, current_user) -> Resume:
    """A second document for the same person — the case that used to collide."""
    body = b"%PDF-1.4 the ML resume"
    row = Resume(
        user_id=current_user.id,
        filename="jordan-ml-infra.pdf",
        raw_text="Jordan Candidate — ML Infrastructure Engineer.",
        full_name="Jordan Candidate",
        headline="ML Infrastructure Engineer",
        skills=["pytorch", "ray"],
        target_roles=["ML Infrastructure Engineer"],
        experience=[],
        is_default=False,
        parsed_with="heuristic",
        file_bytes=body,
        file_content_type="application/pdf",
        file_size=len(body),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def legacy_resume(db_session, current_user) -> Resume:
    """A row from before the bytes were kept. All it has is the parsed text."""
    row = Resume(
        user_id=current_user.id,
        filename="old-upload.pdf",
        raw_text=SAMPLE_RESUME_TEXT,
        full_name="Jordan Candidate",
        headline="Senior Backend Engineer",
        skills=["python"],
        experience=[{"company": "Initech", "title": "Engineer", "start": "2016", "end": "2018"}],
        is_default=True,
        parsed_with="heuristic",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def posting(db_session, current_user) -> JobPosting:
    row = JobPosting(
        user_id=current_user.id,
        title="Staff Platform Engineer",
        company="Acme",
        url="https://acme.com/jobs/9",
        fingerprint=job_fingerprint("Staff Platform Engineer", "Acme", None),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _application(db, user, resume, *, posting_id=None) -> Application:
    recruiter = Recruiter(
        user_id=user.id, email="talent@acme.com", company="Acme", name="Ada"
    )
    db.add(recruiter)
    campaign = Campaign(
        user_id=user.id, name="Autopilot", resume_id=resume.id, status=CampaignStatus.ACTIVE
    )
    db.add(campaign)
    db.flush()
    row = Application(
        user_id=user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        job_posting_id=posting_id,
        status=ApplicationStatus.QUEUED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _draft(db, application, *, status=EmailStatus.DRAFT) -> Email:
    thread = EmailThread(application_id=application.id, subject="Jordan — Platform")
    db.add(thread)
    db.flush()
    row = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=status,
        to_address="talent@acme.com",
        subject="Jordan — Platform",
        body_text="Hi Ada,",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture()
def draft(db_session, current_user, uploaded_resume) -> Email:
    return _draft(db_session, _application(db_session, current_user, uploaded_resume))


def _url(email: Email) -> str:
    return f"/api/v1/inbox/emails/{email.id}/attachments"


# --------------------------------------------------------------------------- #
# The uploaded file is kept                                                    #
# --------------------------------------------------------------------------- #


class TestUploadKeepsTheFile:
    def test_upload_stores_the_bytes_it_parsed(
        self, auth_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_pdf", lambda _: SAMPLE_RESUME_TEXT
        )
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("jordan.pdf", PDF, "application/pdf")},
        )
        assert resp.status_code == 201, resp.text

        row = db_session.query(Resume).filter_by(filename="jordan.pdf").one()
        assert row.file_bytes == PDF
        assert row.file_content_type == "application/pdf"
        assert row.file_size == len(PDF)
        assert row.has_original_file is True

    def test_word_uploads_keep_their_own_type(self, auth_client, db_session, monkeypatch):
        """The extension decides, not the browser — a .docx dragged in from the
        desktop arrives as application/octet-stream and must not be sent as one."""
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_docx", lambda _: SAMPLE_RESUME_TEXT
        )
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("dana.docx", b"PK\x03\x04 docx", "application/octet-stream")},
        )
        assert resp.status_code == 201, resp.text

        row = db_session.query(Resume).filter_by(filename="dana.docx").one()
        assert row.file_content_type == DOCX_MIME


class TestOriginalFileTravels:
    def test_the_bytes_the_candidate_uploaded_are_what_is_attached(
        self, db_session, current_user, uploaded_resume
    ):
        application = _application(db_session, current_user, uploaded_resume)

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        assert found.content == PDF
        assert found.filename == "jordan-platform-eng.pdf"
        assert found.mime_type == "application/pdf"

    def test_two_resumes_for_one_person_no_longer_collide(
        self, db_session, current_user, uploaded_resume, second_resume
    ):
        """The old renderer named both ``jordan-candidate-resume.pdf``, which is
        precisely why "the wrong CV" was undetectable from the outside."""
        assert email_attachments.filename_for(uploaded_resume) != (
            email_attachments.filename_for(second_resume)
        )

    def test_a_row_without_bytes_still_falls_back_to_a_render(
        self, db_session, current_user, legacy_resume
    ):
        application = _application(db_session, current_user, legacy_resume)

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        # Rendered, not the upload — and named the derived way, which is the
        # signal that this is a reconstruction.
        assert found is not None
        assert found.filename == "jordan-candidate-resume.pdf"
        assert found.content != PDF

    def test_a_missing_renderer_no_longer_blocks_a_stored_file(
        self, db_session, current_user, uploaded_resume, monkeypatch
    ):
        """reportlab is only needed to rebuild a resume. A file we already hold
        must not be withheld because the server can't render one."""
        monkeypatch.setattr(resume_pdf, "is_available", lambda: False)
        application = _application(db_session, current_user, uploaded_resume)

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        assert found is not None and found.content == PDF

    def test_a_row_claiming_bytes_it_does_not_have_degrades_to_a_render(
        self, db_session, current_user, uploaded_resume
    ):
        uploaded_resume.file_bytes = None  # corrupt: size says otherwise
        db_session.commit()
        application = _application(db_session, current_user, uploaded_resume)

        found = email_attachments.resume_attachment_for_application(
            db_session, application, current_user
        )
        assert found is not None and found.content != PDF

    def test_the_plan_names_the_file_that_will_travel(
        self, db_session, current_user, uploaded_resume
    ):
        draft = _draft(db_session, _application(db_session, current_user, uploaded_resume))
        plan = email_attachments.plan_for_email(db_session, draft)

        assert plan.filenames == ["jordan-platform-eng.pdf"]
        assert [f.kind for f in plan.files] == [email_attachments.RESUME]
        assert (
            email_attachments.files_for_email(db_session, draft).attachments[0].filename
            == plan.filenames[0]
        )


# --------------------------------------------------------------------------- #
# Choosing the resume                                                          #
# --------------------------------------------------------------------------- #


class TestPinningAResume:
    def test_listing_offers_every_resume_the_user_owns(
        self, auth_client, draft, uploaded_resume, second_resume
    ):
        body = auth_client.get(_url(draft)).json()

        assert body["editable"] is True
        assert body["resume_id"] is None
        assert {r["id"] for r in body["resume_options"]} == {
            uploaded_resume.id,
            second_resume.id,
        }
        assert all(r["has_original_file"] for r in body["resume_options"])

    def test_pinning_changes_what_is_queued(
        self, auth_client, db_session, draft, second_resume
    ):
        body = auth_client.patch(
            _url(draft), json={"resume_id": second_resume.id}
        ).json()

        assert body["resume_id"] == second_resume.id
        assert body["files"][0]["filename"] == "jordan-ml-infra.pdf"

        db_session.refresh(draft)
        sent = email_attachments.files_for_email(db_session, draft)
        assert sent.attachments[0].content == second_resume.file_bytes

    def test_a_pin_outranks_even_a_tailored_render(
        self, auth_client, db_session, current_user, uploaded_resume, second_resume, posting
    ):
        """Tailoring is the better *automatic* answer and stays first by default.
        It is still inference, and the user overrules it."""
        application = _application(
            db_session, current_user, uploaded_resume, posting_id=posting.id
        )
        db_session.add(
            TailoredResume(
                user_id=current_user.id,
                resume_id=uploaded_resume.id,
                job_posting_id=posting.id,
                job_title="Staff Platform Engineer",
                job_company="Acme",
                pdf_bytes=b"%PDF-1.4 tailored",
                pdf_filename="jordan-acme.pdf",
                pdf_generated_at=datetime.now(UTC),
            )
        )
        db_session.commit()
        draft = _draft(db_session, application)

        # Untouched: the tailored PDF wins.
        assert (
            email_attachments.plan_for_email(db_session, draft).filenames
            == ["jordan-acme.pdf"]
        )

        auth_client.patch(_url(draft), json={"resume_id": second_resume.id})
        db_session.refresh(draft)

        assert (
            email_attachments.plan_for_email(db_session, draft).filenames
            == ["jordan-ml-infra.pdf"]
        )

    def test_clearing_the_pin_hands_the_choice_back(
        self, auth_client, db_session, draft, uploaded_resume, second_resume
    ):
        auth_client.patch(_url(draft), json={"resume_id": second_resume.id})
        body = auth_client.patch(_url(draft), json={"resume_id": None}).json()

        assert body["resume_id"] is None
        assert body["files"][0]["filename"] == uploaded_resume.filename

    def test_another_users_resume_is_not_found(
        self, auth_client, db_session, draft
    ):
        stranger = User(email="stranger@example.com", hashed_password="x")
        db_session.add(stranger)
        db_session.flush()
        theirs = Resume(user_id=stranger.id, filename="not-yours.pdf", full_name="Someone")
        db_session.add(theirs)
        db_session.commit()

        resp = auth_client.patch(_url(draft), json={"resume_id": theirs.id})
        assert resp.status_code == 404

    def test_pinning_puts_a_removed_resume_back(
        self, auth_client, db_session, draft, second_resume
    ):
        auth_client.delete(f"{_url(draft)}/0")
        db_session.refresh(draft)
        assert email_attachments.RESUME in draft.suppressed_attachments

        body = auth_client.patch(_url(draft), json={"resume_id": second_resume.id}).json()
        assert body["resume_removed"] is False
        assert body["files"][0]["filename"] == "jordan-ml-infra.pdf"


# --------------------------------------------------------------------------- #
# Removing what was resolved                                                   #
# --------------------------------------------------------------------------- #


class TestRemoving:
    def test_removing_the_resume_stops_it_being_sent(
        self, auth_client, db_session, draft
    ):
        body = auth_client.delete(f"{_url(draft)}/0").json()

        assert body["files"] == []
        assert body["resume_removed"] is True

        db_session.refresh(draft)
        sent = email_attachments.files_for_email(db_session, draft)
        assert sent.attachments == []
        # Null here means the send genuinely carried nothing — the distinction
        # the row exists to record.
        assert sent.resume_filename is None

    def test_removing_a_position_that_carries_nothing_is_a_404(
        self, auth_client, draft
    ):
        assert auth_client.delete(f"{_url(draft)}/7").status_code == 404
        assert auth_client.delete(f"{_url(draft)}/-1").status_code == 404

    def test_an_approved_message_can_no_longer_be_changed(
        self, auth_client, db_session, current_user, uploaded_resume
    ):
        queued = _draft(
            db_session,
            _application(db_session, current_user, uploaded_resume),
            status=EmailStatus.QUEUED,
        )

        assert auth_client.get(_url(queued)).json()["editable"] is False
        assert auth_client.delete(f"{_url(queued)}/0").status_code == 409
        assert (
            auth_client.patch(_url(queued), json={"resume_id": None}).status_code == 409
        )
        assert (
            auth_client.post(
                _url(queued), files={"file": ("x.pdf", b"%PDF", "application/pdf")}
            ).status_code
            == 409
        )

    def test_another_users_message_is_not_found(
        self, auth_client, client, db_session, draft
    ):
        """404 rather than 403 — a 403 would confirm the id exists."""
        client.post(
            "/api/v1/auth/register",
            json={"email": "other@example.com", "password": "supersecret123"},
        )
        token = client.post(
            "/api/v1/auth/login",
            data={"username": "other@example.com", "password": "supersecret123"},
        ).json()["access_token"]

        resp = client.get(
            _url(draft), headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Adding files of the user's own                                               #
# --------------------------------------------------------------------------- #


class TestAdding:
    def test_an_added_file_travels_last_and_verbatim(
        self, auth_client, db_session, draft
    ):
        body = auth_client.post(
            _url(draft),
            files={"file": ("portfolio.pdf", b"%PDF-1.4 portfolio", "application/pdf")},
        ).json()

        assert [f["filename"] for f in body["files"]] == [
            "jordan-platform-eng.pdf",
            "portfolio.pdf",
        ]
        assert [f["kind"] for f in body["files"]] == ["resume", "upload"]
        assert body["files"][1]["attachment_id"] is not None

        db_session.refresh(draft)
        sent = email_attachments.files_for_email(db_session, draft)
        assert [a.filename for a in sent.attachments] == [
            "jordan-platform-eng.pdf",
            "portfolio.pdf",
        ]
        assert sent.attachments[1].content == b"%PDF-1.4 portfolio"
        # The resume is still what the row records as the resume — an extra file
        # is not a resume.
        assert sent.resume_filename == "jordan-platform-eng.pdf"

    def test_a_non_pdf_keeps_its_own_type(self, auth_client, db_session, draft):
        auth_client.post(
            _url(draft),
            files={"file": ("references.docx", b"PK\x03\x04", DOCX_MIME)},
        )
        db_session.refresh(draft)

        attached = email_attachments.files_for_email(db_session, draft).attachments[-1]
        assert attached.mime_type == DOCX_MIME

    def test_files_keep_the_order_they_were_added(
        self, auth_client, db_session, draft
    ):
        for name in ("one.pdf", "two.pdf", "three.pdf"):
            auth_client.post(
                _url(draft), files={"file": (name, b"%PDF " + name.encode(), "application/pdf")}
            )
        body = auth_client.get(_url(draft)).json()

        assert [f["filename"] for f in body["files"]][1:] == [
            "one.pdf",
            "two.pdf",
            "three.pdf",
        ]

    def test_removing_an_upload_leaves_the_others(
        self, auth_client, db_session, draft
    ):
        for name in ("one.pdf", "two.pdf"):
            auth_client.post(
                _url(draft), files={"file": (name, b"%PDF " + name.encode(), "application/pdf")}
            )

        # Index 1 is the first upload; the resume is 0.
        body = auth_client.delete(f"{_url(draft)}/1").json()

        assert [f["filename"] for f in body["files"]] == [
            "jordan-platform-eng.pdf",
            "two.pdf",
        ]
        assert db_session.query(EmailAttachment).count() == 1

    def test_an_empty_file_is_refused(self, auth_client, draft):
        resp = auth_client.post(
            _url(draft), files={"file": ("empty.pdf", b"", "application/pdf")}
        )
        assert resp.status_code == 422

    def test_the_per_message_cap_is_enforced(self, auth_client, db_session, draft):
        for index in range(inbox_router.MAX_ATTACHMENTS_PER_EMAIL):
            db_session.add(
                EmailAttachment(
                    email_id=draft.id,
                    filename=f"f{index}.pdf",
                    content_type="application/pdf",
                    size=4,
                    content=b"%PDF",
                )
            )
        db_session.commit()

        resp = auth_client.post(
            _url(draft), files={"file": ("one-too-many.pdf", b"%PDF", "application/pdf")}
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# The list and the bytes stay in step                                          #
# --------------------------------------------------------------------------- #


class TestPositionsLineUp:
    def test_previewing_a_position_opens_the_file_listed_there(
        self, auth_client, db_session, draft
    ):
        """The whole preview contract: the badge at position N opens the document
        position N will carry. An added file must not shift that."""
        auth_client.post(
            _url(draft),
            files={"file": ("portfolio.pdf", b"%PDF-1.4 portfolio", "application/pdf")},
        )
        listed = auth_client.get(_url(draft)).json()["files"]

        for item in listed:
            resp = auth_client.get(f"{_url(draft)}/{item['index']}")
            assert resp.status_code == 200, resp.text
            assert item["filename"] in resp.headers["content-disposition"]

        assert auth_client.get(f"{_url(draft)}/0").content == PDF
        assert auth_client.get(f"{_url(draft)}/1").content == b"%PDF-1.4 portfolio"

    def test_the_thread_view_describes_the_same_files(
        self, auth_client, db_session, draft
    ):
        auth_client.post(
            _url(draft),
            files={"file": ("portfolio.pdf", b"%PDF-1.4 portfolio", "application/pdf")},
        )
        detail = auth_client.get(f"/api/v1/inbox/threads/{draft.thread_id}").json()
        [message] = [m for m in detail["messages"] if m["id"] == draft.id]

        assert message["attachments"] == [
            "jordan-platform-eng.pdf",
            "portfolio.pdf",
        ]
        assert [i["kind"] for i in message["attachment_items"]] == ["resume", "upload"]
        assert [i["index"] for i in message["attachment_items"]] == [0, 1]


# --------------------------------------------------------------------------- #
# Reading a Word attachment without changing what is sent                      #
# --------------------------------------------------------------------------- #


class TestReadingAWordAttachment:
    """A .docx on a draft was as unreadable as a .docx resume: the frame drew
    nothing. It is converted for reading now — and only for reading, which on
    this path is the same invariant as everywhere else. What the recruiter opens
    is the file the user attached, byte for byte.
    """

    def _attach_docx(self, client, draft) -> int:
        """Attach a real Word document and return the position it took."""
        body = client.post(
            _url(draft),
            files={"file": ("job-spec.docx", RESUME_DOCX, DOCX_MIME)},
        ).json()
        return next(f["index"] for f in body["files"] if f["filename"] == "job-spec.docx")

    def test_the_preview_endpoint_renders_it_for_reading(self, auth_client, draft):
        index = self._attach_docx(auth_client, draft)

        resp = auth_client.get(f"{_url(draft)}/{index}/preview")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "Jordan Candidate" in resp.text
        assert resp.headers["x-preview-of"] == "job-spec.docx"

    def test_the_file_endpoint_still_hands_back_the_word_document(
        self, auth_client, draft
    ):
        index = self._attach_docx(auth_client, draft)
        auth_client.get(f"{_url(draft)}/{index}/preview")

        resp = auth_client.get(f"{_url(draft)}/{index}")

        assert resp.content == RESUME_DOCX
        assert resp.headers["content-type"].startswith(DOCX_MIME)

    def test_the_sender_carries_the_word_document_not_the_rendering(
        self, auth_client, db_session, draft
    ):
        # The test that matters. A control that changes what the screen shows and
        # not what the send carries would be bad; one that changed the send is
        # the failure this whole module exists to prevent.
        index = self._attach_docx(auth_client, draft)
        auth_client.get(f"{_url(draft)}/{index}/preview")
        db_session.refresh(draft)

        attached = email_attachments.files_for_email(db_session, draft).attachments[-1]

        assert attached.filename == "job-spec.docx"
        assert attached.content == RESUME_DOCX
        assert attached.mime_type == DOCX_MIME

    def test_a_pdf_attachment_previews_as_the_pdf_itself(self, auth_client, draft):
        # No regression for the format that never needed converting: one
        # endpoint the page can always read from, returning the bytes unchanged.
        resp = auth_client.get(f"{_url(draft)}/0/preview")

        assert resp.status_code == 200
        assert resp.content == PDF
        assert resp.headers["content-type"].startswith("application/pdf")

    def test_a_format_with_no_rendering_says_so(self, auth_client, draft):
        # A spreadsheet. The file is attached and will be sent; there is simply
        # no way to draw it, and the UI turns this into a download.
        body = auth_client.post(
            _url(draft), files={"file": ("rates.xlsx", b"PK\x03\x04 sheet", XLSX_MIME)}
        ).json()
        index = next(f["index"] for f in body["files"] if f["filename"] == "rates.xlsx")

        resp = auth_client.get(f"{_url(draft)}/{index}/preview")

        assert resp.status_code == 409
        assert resp.json()["detail"] == document_preview.UNAVAILABLE_DETAIL

    def test_a_position_carrying_nothing_is_not_found(self, auth_client, draft):
        # The two endpoints resolve through one function, so a position that
        # 404s on the file cannot 200 on the preview.
        assert auth_client.get(f"{_url(draft)}/9/preview").status_code == 404
        assert auth_client.get(f"{_url(draft)}/-1/preview").status_code == 404

    def test_another_users_attachment_is_not_previewable(
        self, auth_client, client, draft
    ):
        index = self._attach_docx(auth_client, draft)
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "nosy@example.com",
                "password": "supersecret123",
                "full_name": "Nosy Parker",
            },
        )
        token = client.post(
            "/api/v1/auth/login",
            data={"username": "nosy@example.com", "password": "supersecret123"},
        ).json()["access_token"]

        resp = client.get(
            f"{_url(draft)}/{index}/preview",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 404
