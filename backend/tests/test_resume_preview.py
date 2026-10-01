"""Reading a resume back, from the page that took it.

Setup accepted a file and then only ever described it: a headline, a filename, a
row of skills. That is enough to *list* two uploads of one CV and not nearly
enough to tell them apart — and "make this one the default" and "delete this one"
are both decisions about a document the user could no longer see. So the document
opens.

The load-bearing property is not that bytes come back. It is that they are the
*same* bytes the sender attaches, resolved by the same function: a preview with
its own resolver would be free to show the candidate's upload while the sender
attached a PDF reconstruction of it, and the user would have no way to know. So
these tests pin the shared resolution, the inline header that makes a PDF render
in place, and the two honest failures — someone else's resume, and a row from
before the bytes were kept on a box with no renderer.
"""
from __future__ import annotations

import pytest

from app.core.security import hash_password
from app.models.resume import Resume
from app.models.user import User
from app.services import document_preview, email_attachments, resume_pdf
from tests.conftest import SAMPLE_RESUME_TEXT
from tests.docx_fixtures import RESUME_DOCX

PDF = b"%PDF-1.4 the candidate's actual resume, byte for byte"
DOCX = b"PK\x03\x04 the candidate's Word resume"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _url(resume: Resume | int) -> str:
    rid = resume if isinstance(resume, int) else resume.id
    return f"/api/v1/resumes/{rid}/file"


def _preview_url(resume: Resume | int) -> str:
    rid = resume if isinstance(resume, int) else resume.id
    return f"/api/v1/resumes/{rid}/preview"


@pytest.fixture()
def uploaded_pdf(db_session, current_user) -> Resume:
    """A resume stored the way the uploader stores one: with its file."""
    row = Resume(
        user_id=current_user.id,
        filename="jordan-platform-eng.pdf",
        raw_text=SAMPLE_RESUME_TEXT,
        full_name="Jordan Candidate",
        headline="Staff Platform Engineer",
        skills=["kubernetes", "terraform"],
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
def uploaded_docx(db_session, current_user) -> Resume:
    """A Word resume, stored exactly as the uploader stores one.

    The commonest upload there is: a resume lives in Word until the moment it is
    sent. It is also the one a browser cannot draw, which is what
    ``/preview`` exists for.
    """
    row = Resume(
        user_id=current_user.id,
        filename="jordan-backend.docx",
        raw_text=SAMPLE_RESUME_TEXT,
        full_name="Jordan Candidate",
        headline="Staff Backend Engineer",
        skills=["python", "fastapi"],
        is_default=True,
        parsed_with="heuristic",
        file_bytes=RESUME_DOCX,
        file_content_type=DOCX_MIME,
        file_size=len(RESUME_DOCX),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def legacy_resume(db_session, current_user) -> Resume:
    """A row from before the upload was kept — parse only, no bytes."""
    row = Resume(
        user_id=current_user.id,
        filename="old-upload.pdf",
        raw_text=SAMPLE_RESUME_TEXT,
        full_name="Jordan Candidate",
        headline="Senior Backend Engineer",
        skills=["python", "fastapi"],
        experience=[
            {"company": "Acme", "title": "Senior Backend Engineer", "start": "2018"}
        ],
        is_default=True,
        parsed_with="heuristic",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# The document itself                                                          #
# --------------------------------------------------------------------------- #


class TestPreviewServesTheDocument:
    def test_serves_the_uploaded_bytes_verbatim(self, auth_client, uploaded_pdf):
        # Not a re-render, not a conversion: the file the candidate chose.
        resp = auth_client.get(_url(uploaded_pdf))

        assert resp.status_code == 200
        assert resp.content == PDF

    def test_serves_it_as_a_pdf_so_the_browser_renders_it(
        self, auth_client, uploaded_pdf
    ):
        resp = auth_client.get(_url(uploaded_pdf))

        assert resp.headers["content-type"].startswith("application/pdf")

    def test_inline_rather_than_a_download(self, auth_client, uploaded_pdf):
        # `inline` is the whole feature. As an attachment the browser saves a file
        # the user then has to go find, which is not a preview.
        disposition = auth_client.get(_url(uploaded_pdf)).headers[
            "content-disposition"
        ]

        assert disposition.startswith("inline")
        assert "jordan-platform-eng.pdf" in disposition

    def test_a_word_upload_keeps_its_own_type(self, db_session, auth_client, current_user):
        # A .docx has no in-page viewer, and saying it is a PDF would not give it
        # one — it would only make the browser draw an empty frame. The type is
        # what lets the UI offer a download instead.
        row = Resume(
            user_id=current_user.id,
            filename="jordan.docx",
            raw_text=SAMPLE_RESUME_TEXT,
            full_name="Jordan Candidate",
            is_default=True,
            file_bytes=DOCX,
            file_content_type=DOCX_MIME,
            file_size=len(DOCX),
        )
        db_session.add(row)
        db_session.commit()

        resp = auth_client.get(_url(row))

        assert resp.content == DOCX
        assert resp.headers["content-type"].startswith(DOCX_MIME)

    def test_header_cannot_be_injected_through_a_filename(
        self, db_session, auth_client, current_user
    ):
        # The filename comes off an upload, so it is user-influenced data landing
        # in a response header.
        row = Resume(
            user_id=current_user.id,
            filename='evil".pdf\r\nSet-Cookie: a=b',
            raw_text=SAMPLE_RESUME_TEXT,
            is_default=True,
            file_bytes=PDF,
            file_content_type="application/pdf",
            file_size=len(PDF),
        )
        db_session.add(row)
        db_session.commit()

        disposition = auth_client.get(_url(row)).headers["content-disposition"]

        assert "\r" not in disposition and "\n" not in disposition
        assert disposition == 'inline; filename="evil-.pdf-Set-Cookie-a-b"'


class TestPreviewMatchesWhatIsSent:
    def test_same_resolver_as_the_sender(self, auth_client, uploaded_pdf, db_session):
        # The point of the endpoint. If these two could disagree, the preview
        # would be a second implementation of the resolver and the user would be
        # checking a document that isn't the one going out.
        served = auth_client.get(_url(uploaded_pdf)).content
        attached = email_attachments.document_for_resume(
            db_session.get(Resume, uploaded_pdf.id)
        )

        assert attached is not None
        assert served == attached.content == PDF

    def test_falls_back_to_the_render_for_a_row_with_no_upload(
        self, auth_client, legacy_resume
    ):
        # Uploaded before the bytes were kept. The sender re-renders the parse for
        # these, so the preview shows that render — what the recruiter would get.
        if not resume_pdf.is_available():
            pytest.skip("reportlab is not installed")

        resp = auth_client.get(_url(legacy_resume))

        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")
        assert resp.headers["content-type"].startswith("application/pdf")

    def test_the_render_arrives_under_a_derived_name(self, auth_client, legacy_resume):
        # Deliberately not the uploaded filename: the same derived name for every
        # resume one person owns is the proof, visible to the user, that they are
        # looking at a reconstruction rather than their file.
        if not resume_pdf.is_available():
            pytest.skip("reportlab is not installed")

        disposition = auth_client.get(_url(legacy_resume)).headers[
            "content-disposition"
        ]

        assert "jordan-candidate-resume.pdf" in disposition
        assert "old-upload.pdf" not in disposition

    def test_a_corrupt_row_falls_back_rather_than_serving_nothing(
        self, db_session, auth_client, current_user, legacy_resume
    ):
        # file_size says there are bytes and there are none. That row is corrupt,
        # not empty, and a zero-byte document is the one answer worse than a
        # render.
        legacy_resume.file_size = len(PDF)
        legacy_resume.file_content_type = "application/pdf"
        db_session.commit()
        if not resume_pdf.is_available():
            pytest.skip("reportlab is not installed")

        resp = auth_client.get(_url(legacy_resume))

        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")


class TestPreviewFailsHonestly:
    def test_404_for_a_resume_that_is_not_yours(
        self, db_session, auth_client, uploaded_pdf
    ):
        other = User(
            email="someone.else@example.com",
            hashed_password=hash_password("supersecret123"),
            full_name="Someone Else",
        )
        db_session.add(other)
        db_session.commit()
        theirs = Resume(
            user_id=other.id,
            filename="not-yours.pdf",
            raw_text="Someone else's career.",
            file_bytes=PDF,
            file_content_type="application/pdf",
            file_size=len(PDF),
        )
        db_session.add(theirs)
        db_session.commit()

        resp = auth_client.get(_url(theirs))

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Resume not found"

    def test_404_for_a_resume_that_does_not_exist(self, auth_client):
        assert auth_client.get(_url(999_999)).status_code == 404

    def test_401_without_a_token(self, client, uploaded_pdf):
        # A resume is the most identifying document the product holds, and this
        # endpoint hands one over whole. `uploaded_pdf` needs a logged-in user to
        # own it, and the fixtures share one client, so drop the header the login
        # left behind.
        client.headers.pop("Authorization", None)

        assert client.get(_url(uploaded_pdf)).status_code == 401

    def test_409_when_there_is_no_document_to_be_had(
        self, auth_client, legacy_resume, monkeypatch
    ):
        # No upload on file and no renderer to fall back on. That is a fact about
        # the resume, not a server fault, and the message has to tell the user the
        # one thing that fixes it.
        monkeypatch.setattr(resume_pdf, "is_available", lambda: False)

        resp = auth_client.get(_url(legacy_resume))

        assert resp.status_code == 409
        assert "re-upload" in resp.json()["detail"]

    def test_a_failed_render_is_a_409_not_a_500(
        self, auth_client, legacy_resume, monkeypatch
    ):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("reportlab exploded")

        monkeypatch.setattr(resume_pdf, "is_available", lambda: True)
        monkeypatch.setattr(resume_pdf, "render_pdf", _boom)

        assert auth_client.get(_url(legacy_resume)).status_code == 409


# --------------------------------------------------------------------------- #
# Reading a Word resume, which is most of them                                  #
# --------------------------------------------------------------------------- #


class TestWordResumesCanBeRead:
    """The gap: ``/file`` serves a .docx and a browser draws nothing at all.

    Nothing on screen is indistinguishable from a broken preview, so the
    commonest resume format in the world was the one document this product would
    send under a candidate's name and could not show them.
    """

    def test_a_word_resume_comes_back_as_something_a_browser_draws(
        self, auth_client, uploaded_docx
    ):
        resp = auth_client.get(_preview_url(uploaded_docx))

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "Jordan Candidate" in resp.text
        assert "Cut checkout p99 latency by 40%" in resp.text

    def test_the_rendering_is_named_for_what_it_is(self, auth_client, uploaded_docx):
        # Not "jordan-backend.docx": a save dialog offering that name for a page
        # of HTML would be a lie about which of the two files this is.
        resp = auth_client.get(_preview_url(uploaded_docx))

        assert 'filename="jordan-backend.html"' in resp.headers["content-disposition"]
        assert resp.headers["x-preview-of"] == "jordan-backend.docx"

    def test_the_rendering_is_served_with_scripts_forbidden(
        self, auth_client, uploaded_docx
    ):
        # A whole HTML document served from the API's origin. Its body holds no
        # markup the converter didn't write, and this is free on top.
        resp = auth_client.get(_preview_url(uploaded_docx))

        assert "default-src 'none'" in resp.headers["content-security-policy"]
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_a_pdf_resume_previews_exactly_as_it_did_before(
        self, auth_client, uploaded_pdf
    ):
        # The endpoint exists for the formats that need converting, and must not
        # have taught itself to convert the one that doesn't.
        resp = auth_client.get(_preview_url(uploaded_pdf))

        assert resp.status_code == 200
        assert resp.content == PDF
        assert resp.headers["content-type"].startswith("application/pdf")
        assert 'filename="jordan-platform-eng.pdf"' in resp.headers["content-disposition"]

    def test_a_row_with_no_upload_previews_the_render_the_sender_would_send(
        self, auth_client, legacy_resume
    ):
        if not resume_pdf.is_available():
            pytest.skip("reportlab is not installed")

        resp = auth_client.get(_preview_url(legacy_resume))

        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")

    def test_a_file_that_cannot_be_rendered_says_so_in_the_words_the_ui_shows(
        self, db_session, auth_client, current_user
    ):
        # Named .docx and isn't one. The file is fine, it is on file, it will be
        # sent — the only thing missing is a way to draw it in a browser, and the
        # UI turns this into a download rather than an empty frame.
        row = Resume(
            user_id=current_user.id,
            filename="jordan.docx",
            raw_text=SAMPLE_RESUME_TEXT,
            is_default=True,
            file_bytes=DOCX,
            file_content_type=DOCX_MIME,
            file_size=len(DOCX),
        )
        db_session.add(row)
        db_session.commit()

        resp = auth_client.get(_preview_url(row))

        assert resp.status_code == 409
        assert resp.json()["detail"] == document_preview.UNAVAILABLE_DETAIL

    def test_404_for_a_resume_that_is_not_yours(self, db_session, auth_client):
        other = User(
            email="stranger@example.com",
            hashed_password=hash_password("supersecret123"),
            full_name="Stranger",
        )
        db_session.add(other)
        db_session.commit()
        theirs = Resume(
            user_id=other.id,
            filename="not-yours.docx",
            raw_text="Someone else's career.",
            file_bytes=RESUME_DOCX,
            file_content_type=DOCX_MIME,
            file_size=len(RESUME_DOCX),
        )
        db_session.add(theirs)
        db_session.commit()

        assert auth_client.get(_preview_url(theirs)).status_code == 404

    def test_401_without_a_token(self, client, uploaded_docx):
        client.headers.pop("Authorization", None)

        assert client.get(_preview_url(uploaded_docx)).status_code == 401

    def test_409_when_there_is_no_document_at_all(
        self, auth_client, legacy_resume, monkeypatch
    ):
        monkeypatch.setattr(resume_pdf, "is_available", lambda: False)

        resp = auth_client.get(_preview_url(legacy_resume))

        assert resp.status_code == 409
        assert "re-upload" in resp.json()["detail"]


class TestTheConversionNeverTravels:
    """The invariant the whole feature is balanced on.

    A rendering exists to be looked at. What a recruiter receives is the file the
    candidate uploaded, byte for byte — and the day the send path picks up a
    conversion instead is the day a carefully formatted CV arrives as our
    approximation of it. Every one of these would fail if the two ever crossed.
    """

    def test_the_file_endpoint_still_serves_the_original_word_bytes(
        self, auth_client, uploaded_docx
    ):
        auth_client.get(_preview_url(uploaded_docx))  # convert first
        resp = auth_client.get(_url(uploaded_docx))

        assert resp.content == RESUME_DOCX
        assert resp.headers["content-type"].startswith(DOCX_MIME)

    def test_what_the_sender_attaches_is_the_upload_not_the_rendering(
        self, auth_client, uploaded_docx, db_session
    ):
        # Every send path funnels through this one function, which is why the
        # preview reads through it too — and why it has to keep answering with
        # the document rather than with what a browser can draw.
        auth_client.get(_preview_url(uploaded_docx))
        attached = email_attachments.document_for_resume(
            db_session.get(Resume, uploaded_docx.id)
        )

        assert attached is not None
        assert attached.content == RESUME_DOCX
        assert attached.filename == "jordan-backend.docx"
        assert attached.mime_type == DOCX_MIME

    def test_the_rendering_and_the_document_are_not_the_same_bytes(
        self, auth_client, uploaded_docx
    ):
        # Stated as its own test because the two endpoints returning the same
        # thing is exactly how this feature would fail silently: a preview that
        # is byte-identical to a .docx is a preview that never converted, and one
        # a recruiter received would be HTML named as Word.
        preview = auth_client.get(_preview_url(uploaded_docx)).content
        document = auth_client.get(_url(uploaded_docx)).content

        assert preview != document
        assert document == RESUME_DOCX
        assert not preview.startswith(b"PK")

    def test_previewing_does_not_touch_the_stored_row(
        self, auth_client, uploaded_docx, db_session
    ):
        auth_client.get(_preview_url(uploaded_docx))
        db_session.expire_all()
        row = db_session.get(Resume, uploaded_docx.id)

        assert row.file_bytes == RESUME_DOCX
        assert row.file_content_type == DOCX_MIME
        assert row.file_size == len(RESUME_DOCX)
        assert row.filename == "jordan-backend.docx"


# --------------------------------------------------------------------------- #
# What the list says about the file, so the UI knows before it opens anything   #
# --------------------------------------------------------------------------- #


class TestListReportsTheFile:
    def test_says_the_original_is_on_file_and_what_it_is(
        self, auth_client, uploaded_pdf
    ):
        [row] = auth_client.get("/api/v1/resumes").json()

        assert row["has_original_file"] is True
        assert row["file_content_type"] == "application/pdf"
        assert row["file_size"] == len(PDF)

    def test_says_so_when_there_is_no_original(self, auth_client, legacy_resume):
        # What tells the UI the document it will get back is a rendered PDF
        # whatever the stored filename says.
        [row] = auth_client.get("/api/v1/resumes").json()

        assert row["has_original_file"] is False
        assert row["file_size"] is None

    def test_never_ships_the_bytes(self, auth_client, uploaded_pdf):
        # They are deferred on the model so listing resumes doesn't load them;
        # putting them in the payload would undo that and multiply a 10 MB upload
        # by however many resumes the user owns.
        [row] = auth_client.get("/api/v1/resumes").json()

        assert "file_bytes" not in row
