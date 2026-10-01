"""Opening the file a recruiter sent you.

``emails.attachment_filename`` is written by the *sender* — it records the
resume that went out. The inbox read that column for every message, which meant
inbound mail reported carrying nothing no matter what was stapled to it: a job
spec, a take-home brief, a signed offer. Nothing in the backend fetched inbound
attachments, nothing stored them, and the preview endpoint answered an inbound
message by resolving the candidate's *own* resume.

These cover the whole path: reading the part list off a Gmail message, keeping
the description without the bytes, redeeming it on demand, and the ways that
can fail.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from app.models.application import Application
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.services import email_attachments, gmail_service


@pytest.fixture()
def ready(auth_client, connected_gmail, resume, stub_scraper):
    return auth_client


def _campaign(client, company="Acme"):
    return client.post(
        "/api/v1/campaigns", json={"target_companies": [company], "auto_send": False}
    )


def _thread_for(db):
    application = (
        db.query(Application)
        .join(EmailThread, EmailThread.application_id == Application.id)
        .first()
    )
    return db.query(EmailThread).filter_by(application_id=application.id).first()


def _inbound(db, thread, attachments=(), gmail_id: str | None = "gmail-msg-1"):
    """An inbound message recorded the way the poller now records one."""
    email = Email(
        thread_id=thread.id,
        direction=EmailDirection.RECEIVED,
        status=EmailStatus.RECEIVED,
        from_address="talent@acme.com",
        to_address="candidate@example.com",
        subject="Re: Backend role — spec attached",
        body_text="Details are in the attachment.",
        gmail_message_id=gmail_id,
        inbound_attachments=list(attachments),
        sent_at=datetime.now(UTC),
    )
    db.add(email)
    thread.message_count += 1
    db.commit()
    db.refresh(email)
    return email


def _spec(filename="role-spec.pdf", attachment_id="att-1", mime="application/pdf"):
    return {
        "filename": filename,
        "mime_type": mime,
        "attachment_id": attachment_id,
        "size": 1024,
    }


# --------------------------------------------------------------------------- #
# Reading the part list off a Gmail message                                    #
# --------------------------------------------------------------------------- #


class TestListingParts:
    def test_a_plain_attachment_is_found(self):
        message = {
            "payload": {
                "mimeType": "multipart/mixed",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": ""}},
                    {
                        "mimeType": "application/pdf",
                        "filename": "role-spec.pdf",
                        "body": {"attachmentId": "att-1", "size": 90210},
                    },
                ],
            }
        }

        found = gmail_service.list_attachments(message)

        assert [item.filename for item in found] == ["role-spec.pdf"]
        assert found[0].attachment_id == "att-1"
        assert found[0].mime_type == "application/pdf"
        assert found[0].size == 90210

    def test_a_nested_attachment_is_found(self):
        """A forwarded message buries the file several levels down."""
        message = {
            "payload": {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/plain", "body": {}},
                            {
                                "mimeType": "multipart/related",
                                "parts": [
                                    {
                                        "mimeType": "application/pdf",
                                        "filename": "buried.pdf",
                                        "body": {"attachmentId": "deep"},
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        }

        assert [i.filename for i in gmail_service.list_attachments(message)] == [
            "buried.pdf"
        ]

    def test_a_signature_logo_is_not_an_attachment(self):
        """Otherwise every recruiter email grows three badges nobody wants.

        An embedded image is structurally identical to a real attachment — a
        filename and an ``attachmentId``. What separates it is the Content-ID
        the HTML body references it by, plus an inline disposition.
        """
        message = {
            "payload": {
                "parts": [
                    {
                        "mimeType": "image/png",
                        "filename": "logo.png",
                        "body": {"attachmentId": "logo-att"},
                        "headers": [
                            {"name": "Content-ID", "value": "<logo@acme>"},
                            {
                                "name": "Content-Disposition",
                                "value": 'inline; filename="logo.png"',
                            },
                        ],
                    },
                    {
                        "mimeType": "application/pdf",
                        "filename": "offer.pdf",
                        "body": {"attachmentId": "real"},
                    },
                ]
            }
        }

        assert [i.filename for i in gmail_service.list_attachments(message)] == [
            "offer.pdf"
        ]

    def test_an_attached_image_is_still_an_attachment(self):
        """Content-ID *or* inline alone is not enough — a recruiter can attach
        a screenshot, and it must not be filtered out as a signature logo."""
        message = {
            "payload": {
                "parts": [
                    {
                        "mimeType": "image/png",
                        "filename": "whiteboard.png",
                        "body": {"attachmentId": "shot"},
                        "headers": [
                            {
                                "name": "Content-Disposition",
                                "value": 'attachment; filename="whiteboard.png"',
                            }
                        ],
                    }
                ]
            }
        }

        assert [i.filename for i in gmail_service.list_attachments(message)] == [
            "whiteboard.png"
        ]

    def test_a_part_with_no_attachment_id_is_skipped(self):
        """The body itself has a filename on some malformed mail."""
        message = {
            "payload": {
                "parts": [
                    {"mimeType": "text/plain", "filename": "body.txt", "body": {}}
                ]
            }
        }

        assert gmail_service.list_attachments(message) == []

    def test_a_message_with_no_payload_is_not_an_error(self):
        assert gmail_service.list_attachments({}) == []

    def test_order_is_preserved(self):
        """Position is what identifies an attachment to the preview endpoint."""
        message = {
            "payload": {
                "parts": [
                    {
                        "filename": f"{name}.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": name},
                    }
                    for name in ("first", "second", "third")
                ]
            }
        }

        assert [i.filename for i in gmail_service.list_attachments(message)] == [
            "first.pdf",
            "second.pdf",
            "third.pdf",
        ]


class TestRedeemingBytes:
    def test_unpadded_base64url_decodes(self, monkeypatch):
        """Gmail routinely strips the padding, and b64decode will not."""
        payload = b"%PDF-1.4 five bytes past a multiple of three"
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")

        class _Attachments:
            def get(self, **kwargs):
                return self

            def execute(self):
                return {"data": encoded}

        class _Messages:
            def attachments(self):
                return _Attachments()

        class _Users:
            def messages(self):
                return _Messages()

        class _Service:
            def users(self):
                return _Users()

        monkeypatch.setattr(gmail_service, "_service", lambda account: _Service())

        assert gmail_service.get_attachment(object(), "m", "a") == payload


class TestStoredDescriptions:
    def test_a_row_missing_its_id_is_dropped_rather_than_raising(self):
        """Stored JSON is not a schema — an older row must not break a page."""
        email = Email(inbound_attachments=[{"filename": "orphan.pdf"}, _spec()])

        assert email_attachments.inbound_filenames_for(email) == ["role-spec.pdf"]

    def test_junk_in_the_column_is_survivable(self):
        email = Email(inbound_attachments=["not-a-dict", None, _spec()])

        assert email_attachments.inbound_filenames_for(email) == ["role-spec.pdf"]

    def test_no_column_value_reads_as_nothing_attached(self):
        assert email_attachments.inbound_filenames_for(Email()) == []


# --------------------------------------------------------------------------- #
# Through the API                                                              #
# --------------------------------------------------------------------------- #


class TestInboundAttachmentsInTheInbox:
    def test_a_received_message_lists_what_it_carries(self, ready, db_session):
        """The bug in one line: this list used to be empty no matter what."""
        _campaign(ready)
        thread = _thread_for(db_session)
        _inbound(db_session, thread, [_spec(), _spec("nda.pdf", "att-2")])

        messages = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()["messages"]
        received = next(m for m in messages if m["direction"] == "RECEIVED")

        assert received["attachments"] == ["role-spec.pdf", "nda.pdf"]

    def test_the_bytes_come_back_as_a_readable_pdf(
        self, ready, db_session, monkeypatch
    ):
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(db_session, thread, [_spec()])
        monkeypatch.setattr(
            gmail_service,
            "get_attachment",
            lambda account, message_id, attachment_id: b"%PDF-1.4 the spec",
        )

        response = ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/0")

        assert response.status_code == 200
        assert response.content == b"%PDF-1.4 the spec"
        assert response.headers["content-type"] == "application/pdf"
        # Inline is what makes the browser render it instead of downloading it.
        assert response.headers["content-disposition"].startswith("inline;")
        assert 'filename="role-spec.pdf"' in response.headers["content-disposition"]

    def test_the_right_id_is_redeemed_for_the_right_position(
        self, ready, db_session, monkeypatch
    ):
        """A badge at position 1 must open the file the list called position 1."""
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(
            db_session, thread, [_spec(), _spec("nda.pdf", "att-2")], gmail_id="msg-9"
        )
        seen = {}

        def _fetch(account, message_id, attachment_id):
            seen["message_id"] = message_id
            seen["attachment_id"] = attachment_id
            return b"%PDF-1.4 second"

        monkeypatch.setattr(gmail_service, "get_attachment", _fetch)

        response = ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/1")

        assert response.status_code == 200
        assert seen == {"message_id": "msg-9", "attachment_id": "att-2"}
        assert 'filename="nda.pdf"' in response.headers["content-disposition"]

    def test_an_inbound_message_never_serves_the_candidates_own_resume(
        self, ready, db_session
    ):
        """The regression this fixes.

        The outbound resolver answers *any* email with the resume that would
        travel on it, so asking an inbound message for position 0 returned the
        candidate's own PDF — a document the recruiter never sent, offered
        under whatever name our renderer chose.
        """
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(db_session, thread, [])

        response = ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/0")

        assert response.status_code == 404

    def test_a_received_message_with_nothing_attached_does_not_blame_the_resume(
        self, ready, db_session
    ):
        """409 "no resume on file" is true and completely beside the point when
        the question was what a recruiter attached."""
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(db_session, thread, [])

        response = ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/0")

        assert response.status_code == 404
        assert "no attachment there" in response.json()["detail"].lower()

    def test_a_position_past_the_end_is_not_found(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(db_session, thread, [_spec()])

        assert (
            ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/1").status_code
            == 404
        )
        assert (
            ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/-1").status_code
            == 404
        )

    def test_gmail_refusing_the_file_says_so(self, ready, db_session, monkeypatch):
        """A listed file that won't open is a failure to report, not a 404 —
        the user is looking at the filename and pressing it."""
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(db_session, thread, [_spec()])

        def _boom(account, message_id, attachment_id):
            raise RuntimeError("410 gone")

        monkeypatch.setattr(gmail_service, "get_attachment", _boom)

        response = ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/0")

        assert response.status_code == 502
        assert "Gmail" in response.json()["detail"]

    def test_an_empty_body_from_gmail_is_reported(
        self, ready, db_session, monkeypatch
    ):
        """Better a stated failure than a zero-byte PDF that renders blank."""
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(db_session, thread, [_spec()])
        monkeypatch.setattr(
            gmail_service, "get_attachment", lambda *a, **k: b""
        )

        response = ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/0")

        assert response.status_code == 502

    def test_a_message_recorded_without_a_gmail_id_says_why(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(db_session, thread, [_spec()], gmail_id=None)

        response = ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/0")

        assert response.status_code == 502
        assert "Gmail id" in response.json()["detail"]

    def test_an_octet_stream_falls_back_to_the_extension(
        self, ready, db_session, monkeypatch
    ):
        """Some senders label everything octet-stream, and the type is what
        decides whether the browser renders the PDF or saves it."""
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(
            db_session, thread, [_spec(mime="application/octet-stream")]
        )
        monkeypatch.setattr(
            gmail_service, "get_attachment", lambda *a, **k: b"%PDF-1.4"
        )

        response = ready.get(f"/api/v1/inbox/emails/{email.id}/attachments/0")

        assert response.headers["content-type"] == "application/pdf"

    def test_another_users_inbound_attachment_is_not_found(
        self, ready, db_session, client
    ):
        _campaign(ready)
        thread = _thread_for(db_session)
        email = _inbound(db_session, thread, [_spec()])

        client.post(
            "/api/v1/auth/register",
            json={
                "email": "intruder@example.com",
                "password": "supersecret123",
                "full_name": "Nosy Parker",
            },
        )
        token = client.post(
            "/api/v1/auth/login",
            data={"username": "intruder@example.com", "password": "supersecret123"},
        ).json()["access_token"]

        response = client.get(
            f"/api/v1/inbox/emails/{email.id}/attachments/0",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404

class TestPollingRecordsWhatArrived:
    """The description has to be captured at ingest.

    Listing an inbox must not cost a Gmail call per message, so the part list
    is read once — when the poller already has the message resource in hand —
    and written to the row. Nothing downstream can show an attachment the
    poller failed to notice.
    """

    def _poll(self, db, monkeypatch, thread_id, messages):
        from app.tasks import inbox_tasks

        thread = db.get(EmailThread, thread_id)
        thread.gmail_thread_id = "gmail-thread-1"
        db.commit()
        monkeypatch.setattr(
            inbox_tasks.gmail_service,
            "list_thread_messages",
            lambda acct, tid: messages,
        )
        monkeypatch.setattr(inbox_tasks, "SessionLocal", lambda: db)
        return inbox_tasks.poll_thread.run(thread_id)

    def test_a_reply_records_the_file_it_arrived_with(
        self, ready, db_session, monkeypatch
    ):
        _campaign(ready)
        thread = _thread_for(db_session)
        thread_id = thread.id

        self._poll(
            db_session,
            monkeypatch,
            thread_id,
            [
                {
                    "id": "reply-1",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "talent@acme.com"},
                            {"name": "To", "value": "candidate@example.com"},
                            {"name": "Subject", "value": "Spec attached"},
                            {"name": "Date", "value": "Wed, 29 Jul 2026 10:00:00 +0000"},
                        ],
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {
                                    "data": base64.urlsafe_b64encode(
                                        b"Details attached."
                                    ).decode()
                                },
                            },
                            {
                                "mimeType": "application/pdf",
                                "filename": "role-spec.pdf",
                                "body": {"attachmentId": "att-77", "size": 4096},
                            },
                        ],
                    },
                    "snippet": "Details attached.",
                }
            ],
        )

        stored = (
            db_session.query(Email)
            .filter_by(gmail_message_id="reply-1")
            .one()
        )
        assert stored.direction == EmailDirection.RECEIVED
        assert email_attachments.inbound_filenames_for(stored) == ["role-spec.pdf"]
        # The description only — the bytes stay in Gmail until somebody opens it.
        assert stored.inbound_attachments[0]["attachment_id"] == "att-77"


class TestOutboundIsUnaffected:
    """The sent side resolves the same way it always did."""

    def test_a_draft_still_names_and_serves_its_resume(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        messages = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()["messages"]
        draft = next(m for m in messages if m["is_draft"])

        response = ready.get(f"/api/v1/inbox/emails/{draft['id']}/attachments/0")

        assert draft["attachments"][0].endswith(".pdf")
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")
