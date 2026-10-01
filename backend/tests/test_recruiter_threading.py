"""Replying inside the recruiter's own thread, in every mail client.

The bug these tests pin: the reply carried Gmail's ``threadId`` and nothing else.
That threads the conversation for a recruiter reading in Gmail, and for nobody
else — Outlook, Apple Mail, Thunderbird and every ATS that ingests mail thread on
``In-Reply-To``/``References``. So roughly half of recipients saw the reply as a
brand-new message that happened to share a subject line.

The headers were *available* the whole time. ``inbound_scanner`` read them off
the message and dropped them on the floor, and ``gmail_service.send_email``
already accepted both arguments with nothing passing them.

The assertions here are deliberately at the **MIME level** rather than on the
arguments handed to the sender: what matters is what the recruiter's client
receives, and an assertion on the wire format survives a refactor of how the
arguments get there.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
from email import message_from_bytes

import pytest

from app.models.email import Email, EmailDirection, EmailStatus
from app.models.recruiter_email import RecruiterEmail, RecruiterEmailStatus
from app.services import gmail_service, inbound_scanner, recruiter_reply_service
from app.tasks import email_tasks
from tests.test_recruiter_reply import RECRUITER_BODY, _b64

MESSAGE_ID = "<CAF9x2h1-abc123@mail.gmail.com>"
OLDER_ID = "<older-message@northwind.com>"


def threaded_message(
    message_id: str = "m1",
    *,
    rfc_message_id: str | None = MESSAGE_ID,
    references: str | None = None,
    body: str = RECRUITER_BODY,
) -> dict:
    """A Gmail message resource carrying real RFC 5322 threading headers."""
    headers = [
        {"name": "From", "value": "Alex Recruiter <alex@northwind.com>"},
        {"name": "To", "value": "candidate@gmail.com"},
        {"name": "Subject", "value": "Senior Backend Engineer at Northwind"},
    ]
    if rfc_message_id:
        headers.append({"name": "Message-ID", "value": rfc_message_id})
    if references:
        headers.append({"name": "References", "value": references})
    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "internalDate": str(int(datetime.now(UTC).timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64(body)},
        },
    }


# --------------------------------------------------------------------------- #
# Reading the headers off the message                                          #
# --------------------------------------------------------------------------- #


class TestScannerCapturesHeaders:
    def test_the_message_id_is_kept(
        self, db_session, current_user, connected_gmail, stub_gmail
    ):
        stub_gmail["m1"] = threaded_message()

        result = inbound_scanner.scan(db_session, current_user, connected_gmail)

        assert result.messages[0].rfc_message_id == MESSAGE_ID

    def test_an_existing_references_chain_is_kept(
        self, db_session, current_user, connected_gmail, stub_gmail
    ):
        stub_gmail["m1"] = threaded_message(references=OLDER_ID)

        result = inbound_scanner.scan(db_session, current_user, connected_gmail)

        assert result.messages[0].rfc_references == OLDER_ID

    def test_a_message_with_no_headers_yields_none_rather_than_a_guess(
        self, db_session, current_user, connected_gmail, stub_gmail
    ):
        """A fabricated id threads the reply to a message that does not exist."""
        stub_gmail["m1"] = threaded_message(rfc_message_id=None)

        result = inbound_scanner.scan(db_session, current_user, connected_gmail)

        assert result.messages[0].rfc_message_id is None
        assert result.messages[0].rfc_references is None

    def test_folded_headers_are_collapsed(
        self, db_session, current_user, connected_gmail, stub_gmail
    ):
        """Transports fold long headers across lines; a reply must not re-send that."""
        stub_gmail["m1"] = threaded_message(
            references=f"{OLDER_ID}\r\n\t{MESSAGE_ID}"
        )

        result = inbound_scanner.scan(db_session, current_user, connected_gmail)

        assert result.messages[0].rfc_references == f"{OLDER_ID} {MESSAGE_ID}"

    def test_headers_are_persisted_on_the_row(
        self, db_session, current_user, connected_gmail, stub_gmail
    ):
        stub_gmail["m1"] = threaded_message(references=OLDER_ID)

        _, created = recruiter_reply_service.record_scan(
            db_session, current_user, connected_gmail
        )
        db_session.commit()

        assert created[0].rfc_message_id == MESSAGE_ID
        assert created[0].rfc_references == OLDER_ID


# --------------------------------------------------------------------------- #
# Building the chain                                                           #
# --------------------------------------------------------------------------- #


class TestReplyHeaders:
    """RFC 5322 §3.6.4: In-Reply-To is the one id, References is the whole chain."""

    def test_a_first_message_produces_a_single_id_chain(self):
        in_reply_to, references = inbound_scanner.reply_headers_for(MESSAGE_ID, None)

        assert in_reply_to == MESSAGE_ID
        assert references == MESSAGE_ID

    def test_an_existing_chain_is_extended_not_replaced(self):
        in_reply_to, references = inbound_scanner.reply_headers_for(
            MESSAGE_ID, OLDER_ID
        )

        assert in_reply_to == MESSAGE_ID
        # Order matters: a client walking the chain reads it oldest-first.
        assert references == f"{OLDER_ID} {MESSAGE_ID}"

    def test_no_message_id_produces_no_headers_at_all(self):
        """Better an unthreaded reply than one threaded to a fiction."""
        assert inbound_scanner.reply_headers_for(None, OLDER_ID) == (None, None)
        assert inbound_scanner.reply_headers_for("", None) == (None, None)


# --------------------------------------------------------------------------- #
# What actually goes on the wire                                               #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def sent_mime(monkeypatch):
    """Capture the raw MIME a send would put on the wire, and decode it."""
    captured: dict = {}

    def _send(**kwargs):
        captured.update(kwargs)
        return gmail_service.SentMessage(
            gmail_message_id="sent-1", gmail_thread_id=kwargs.get("thread_id") or "t-1"
        )

    monkeypatch.setattr(email_tasks.gmail_service, "send_email", _send)
    return captured


def _mime_for(**kwargs):
    """Build the MIME the sender would build, and parse it back."""
    raw = gmail_service._build_mime(
        "candidate@gmail.com",
        "alex@northwind.com",
        "Re: Senior Backend Engineer at Northwind",
        "Thanks for reaching out.",
        "",
        **kwargs,
    )
    return message_from_bytes(base64.urlsafe_b64decode(raw))


class TestTheWireFormat:
    def test_the_headers_reach_the_message(self):
        message = _mime_for(
            in_reply_to=MESSAGE_ID, references=f"{OLDER_ID} {MESSAGE_ID}"
        )

        assert message["In-Reply-To"] == MESSAGE_ID
        assert message["References"] == f"{OLDER_ID} {MESSAGE_ID}"

    def test_a_message_starting_a_conversation_carries_neither(self):
        """Cold outreach must produce exactly the MIME it produced before this existed."""
        message = _mime_for()

        assert message["In-Reply-To"] is None
        assert message["References"] is None


class TestTheDraftedReplyThreads:
    def _row(self, db_session, current_user, connected_gmail, stub_gmail, **kwargs):
        stub_gmail["m1"] = threaded_message(**kwargs)
        _, created = recruiter_reply_service.record_scan(
            db_session, current_user, connected_gmail
        )
        db_session.commit()
        row = created[0]
        recruiter_reply_service.process(db_session, row)
        db_session.commit()
        return row

    def test_the_reply_carries_in_reply_to(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        row = self._row(db_session, current_user, connected_gmail, stub_gmail)

        reply = db_session.get(Email, row.reply_email_id)
        assert reply.in_reply_to == MESSAGE_ID
        assert reply.email_references == MESSAGE_ID

    def test_the_reply_extends_an_existing_chain(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        row = self._row(
            db_session, current_user, connected_gmail, stub_gmail, references=OLDER_ID
        )

        reply = db_session.get(Email, row.reply_email_id)
        assert reply.email_references == f"{OLDER_ID} {MESSAGE_ID}"

    def test_the_recruiters_own_message_keeps_its_id(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        """So a later message on this thread can chain off it without asking Gmail."""
        row = self._row(db_session, current_user, connected_gmail, stub_gmail)

        inbound = db_session.scalar(
            db_session.query(Email)
            .filter(Email.direction == EmailDirection.RECEIVED)
            .statement
        )
        assert inbound.in_reply_to == MESSAGE_ID

    def test_a_message_with_no_id_still_drafts_a_reply(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        """Threading is a nicety; answering the recruiter is the point."""
        row = self._row(
            db_session,
            current_user,
            connected_gmail,
            stub_gmail,
            rfc_message_id=None,
        )

        assert row.status is RecruiterEmailStatus.DRAFTED
        reply = db_session.get(Email, row.reply_email_id)
        assert reply.in_reply_to is None

    def test_the_send_puts_both_headers_on_the_wire(
        self,
        db_session,
        current_user,
        connected_gmail,
        stub_gmail,
        profiles,
        sent_mime,
        monkeypatch,
    ):
        """The end-to-end assertion: draft it, send it, read the headers back."""
        row = self._row(db_session, current_user, connected_gmail, stub_gmail)
        reply = db_session.get(Email, row.reply_email_id)
        reply.status = EmailStatus.QUEUED
        db_session.commit()

        monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
        monkeypatch.setattr(db_session, "close", lambda: None)
        email_tasks.send_outreach_email(reply.id)

        assert sent_mime["in_reply_to"] == MESSAGE_ID
        assert sent_mime["references"] == MESSAGE_ID
        # And Gmail's own threading is unchanged — both routes, not one instead
        # of the other.
        assert sent_mime["thread_id"] == "t-m1"

    def test_outreach_sends_without_threading_headers(
        self, db_session, current_user, connected_gmail, sent_mime, monkeypatch
    ):
        """A cold email starts a conversation; there is nothing to thread it to."""
        from app.models.application import Application
        from app.models.campaign import Campaign
        from app.models.email_thread import EmailThread
        from app.models.recruiter import Recruiter

        campaign = Campaign(user_id=current_user.id, name="Outbound")
        db_session.add(campaign)
        db_session.flush()
        recruiter = Recruiter(user_id=current_user.id, email="alex@northwind.com")
        db_session.add(recruiter)
        db_session.flush()
        application = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
        )
        db_session.add(application)
        db_session.flush()
        thread = EmailThread(application_id=application.id)
        db_session.add(thread)
        db_session.flush()
        email = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.QUEUED,
            to_address="alex@northwind.com",
            subject="Hello",
            body_text="Hi there",
        )
        db_session.add(email)
        db_session.commit()

        monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
        monkeypatch.setattr(db_session, "close", lambda: None)
        email_tasks.send_outreach_email(email.id)

        assert sent_mime["in_reply_to"] is None
        assert sent_mime["references"] is None
