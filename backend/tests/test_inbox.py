"""The inbox — every conversation, both directions, read state, and sync.

The review queue answers "what needs approving?"; the inbox answers "what has
been said?" — the mail sent on the user's behalf as well as what came back.
These tests cover the thread list and its counts, the sent/received filter, that
no date window hides old mail, the full interleaved conversation read, marking a
thread read, and the ownership boundary between two users' mailboxes.
"""
from __future__ import annotations

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_thread import EmailThread
from app.services import recruiter_discovery
from app.services.career_scraper import Contact, ScrapeResult


@pytest.fixture()
def stub_scraper(monkeypatch):
    def _fake(db, company, domain=None, **kwargs):
        slug = company.lower().replace(" ", "")
        return ScrapeResult(
            company=company,
            domain=f"{slug}.com",
            contacts=[Contact(email=f"talent@{slug}.com", confidence=0.9)],
        )

    monkeypatch.setattr(recruiter_discovery, "get_or_scrape", _fake)


@pytest.fixture()
def ready(auth_client, connected_gmail, resume, stub_scraper):
    return auth_client


def _campaign(client, company="Acme"):
    """Run a review-mode campaign, which leaves one application + thread."""
    return client.post(
        "/api/v1/campaigns", json={"target_companies": [company], "auto_send": False}
    )


def _thread_for(db, company="acme"):
    application = (
        db.query(Application)
        .join(EmailThread, EmailThread.application_id == Application.id)
        .first()
    )
    return db.query(EmailThread).filter_by(application_id=application.id).first()


def _reply(
    db,
    thread,
    body="We'd love to talk — are you free Thursday?",
    intent=ReplyIntent.SCHEDULING,
    read=False,
):
    """Store an inbound recruiter message the way the poller would."""
    from datetime import UTC, datetime

    email = Email(
        thread_id=thread.id,
        direction=EmailDirection.RECEIVED,
        status=EmailStatus.RECEIVED,
        from_address="talent@acme.com",
        to_address="candidate@example.com",
        subject="Re: Backend role",
        body_text=body,
        intent=intent,
        sent_at=datetime.now(UTC),
        read_at=datetime.now(UTC) if read else None,
    )
    db.add(email)
    thread.message_count += 1
    db.commit()
    db.refresh(email)
    return email


def _mark_outreach_sent(db, thread, when=None):
    """Approve-and-send the campaign's outreach draft, the way the sender does."""
    from datetime import UTC, datetime

    email = (
        db.query(Email)
        .filter_by(thread_id=thread.id, direction=EmailDirection.SENT)
        .order_by(Email.id)
        .first()
    )
    email.status = EmailStatus.SENT
    email.sent_at = when or datetime.now(UTC)
    email.from_address = "candidate@example.com"
    thread.last_message_at = email.sent_at
    db.commit()
    db.refresh(email)
    return email


class TestInboxList:
    def test_a_sent_outreach_is_in_the_inbox_before_anyone_replies(
        self, ready, db_session
    ):
        _campaign(ready)
        thread = _thread_for(db_session)
        _mark_outreach_sent(db_session, thread)

        body = ready.get("/api/v1/inbox").json()
        # The regression this covers: the inbox used to require an inbound
        # message, so a mailbox full of sent applications read as empty.
        assert body["counts"]["threads"] == 1
        row = body["threads"][0]
        assert row["outbound_count"] == 1
        assert row["inbound_count"] == 0
        assert row["last_direction"] == EmailDirection.SENT.value
        assert row["last_intent"] is None
        assert row["snippet"]

    def test_a_thread_holding_only_a_draft_still_lists(self, ready, db_session):
        _campaign(ready)

        body = ready.get("/api/v1/inbox").json()
        assert body["counts"]["threads"] == 1
        row = body["threads"][0]
        # Nothing has gone out, so neither direction claims it — but the draft
        # previews the row rather than leaving it blank.
        assert (row["outbound_count"], row["inbound_count"]) == (0, 0)
        assert row["last_direction"] is None
        assert row["draft_email_id"] is not None
        assert row["snippet"]

    def test_counts_split_sent_from_received(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        _mark_outreach_sent(db_session, thread)
        _reply(db_session, thread)

        counts = ready.get("/api/v1/inbox").json()["counts"]
        # One thread, in both halves: it was sent and it was answered.
        assert counts["threads"] == 1
        assert counts["sent"] == 1
        assert counts["received"] == 1

    def test_old_mail_is_not_hidden(self, ready, db_session):
        from datetime import UTC, datetime, timedelta

        _campaign(ready)
        thread = _thread_for(db_session)
        long_ago = datetime.now(UTC) - timedelta(days=900)
        _mark_outreach_sent(db_session, thread, when=long_ago)

        # No window is applied anywhere: a two-and-a-half-year-old email is still
        # the user's mail and still listed.
        body = ready.get("/api/v1/inbox").json()
        assert body["counts"]["threads"] == 1
        assert body["threads"][0]["last_outbound_at"].startswith(
            long_ago.strftime("%Y-%m-%d")
        )

    def test_a_reply_surfaces_with_its_classification(self, ready, db_session):
        _campaign(ready)
        _reply(db_session, _thread_for(db_session))

        body = ready.get("/api/v1/inbox").json()
        assert body["counts"]["threads"] == 1
        row = body["threads"][0]
        assert row["company"] == "Acme"
        assert row["recruiter_email"] == "talent@acme.com"
        assert row["last_intent"] == ReplyIntent.SCHEDULING.value
        assert "Thursday" in row["snippet"]
        assert row["inbound_count"] == 1

    def test_unread_is_counted_until_the_thread_is_opened(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        _reply(db_session, thread)

        body = ready.get("/api/v1/inbox").json()
        assert body["counts"]["unread"] == 1
        assert body["threads"][0]["unread_count"] == 1

        ready.post(f"/api/v1/inbox/threads/{thread.id}/read")

        body = ready.get("/api/v1/inbox").json()
        assert body["counts"]["unread"] == 0
        assert body["threads"][0]["unread_count"] == 0

    def test_a_pending_reply_draft_is_linked_to_its_thread(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        _reply(db_session, thread)

        row = ready.get("/api/v1/inbox").json()["threads"][0]
        # The campaign left an outreach draft on this thread; the inbox points at
        # it so the same approve/dismiss path serves both pages.
        assert row["draft_email_id"] is not None
        assert ready.get("/api/v1/inbox").json()["counts"]["awaiting_reply"] == 1

    def test_counts_by_intent_cover_every_conversation(self, ready, db_session):
        _campaign(ready, "Acme")
        _reply(db_session, _thread_for(db_session), intent=ReplyIntent.INTERESTED)

        counts = ready.get("/api/v1/inbox").json()["counts"]
        assert counts["by_intent"] == {ReplyIntent.INTERESTED.value: 1}

    def test_a_thread_with_no_reply_has_no_intent_bucket(self, ready, db_session):
        _campaign(ready)
        _mark_outreach_sent(db_session, _thread_for(db_session))

        counts = ready.get("/api/v1/inbox").json()["counts"]
        # An unanswered outreach is not an "other" reply — the chips would claim
        # a classification nobody made.
        assert counts["by_intent"] == {}

    def test_ordering_follows_the_latest_activity_either_way(self, ready, db_session):
        from datetime import UTC, datetime, timedelta

        _campaign(ready, "Acme")
        _campaign(ready, "Northwind")
        now = datetime.now(UTC)
        threads = db_session.query(EmailThread).order_by(EmailThread.id).all()
        # Acme was answered a week ago; Northwind was written to yesterday.
        _reply(db_session, threads[0]).sent_at = now - timedelta(days=7)
        db_session.commit()
        _mark_outreach_sent(db_session, threads[1], when=now - timedelta(days=1))

        rows = ready.get("/api/v1/inbox").json()["threads"]
        assert [r["company"] for r in rows] == ["Northwind", "Acme"]

    def test_newest_conversation_comes_first(self, ready, db_session):
        from datetime import UTC, datetime, timedelta

        _campaign(ready, "Acme")
        thread = _thread_for(db_session)
        old = _reply(db_session, thread, body="First note", intent=ReplyIntent.QUESTION)
        old.sent_at = datetime.now(UTC) - timedelta(days=3)
        db_session.commit()
        _reply(db_session, thread, body="Second note", intent=ReplyIntent.INTERESTED)

        row = ready.get("/api/v1/inbox").json()["threads"][0]
        # The latest message drives the row's intent and snippet.
        assert row["last_intent"] == ReplyIntent.INTERESTED.value
        assert "Second note" in row["snippet"]


class TestDirectionFilter:
    def _two_threads(self, client, db):
        """Acme: sent and replied to. Northwind: sent, no answer."""
        _campaign(client, "Acme")
        _campaign(client, "Northwind")
        threads = db.query(EmailThread).order_by(EmailThread.id).all()
        for thread in threads:
            _mark_outreach_sent(db, thread)
        _reply(db, threads[0])
        return threads

    def test_all_is_the_default_and_shows_both(self, ready, db_session):
        self._two_threads(ready, db_session)

        default = ready.get("/api/v1/inbox").json()["threads"]
        explicit = ready.get("/api/v1/inbox?direction=all").json()["threads"]
        assert len(default) == 2
        assert default == explicit

    def test_received_keeps_only_answered_threads(self, ready, db_session):
        self._two_threads(ready, db_session)

        rows = ready.get("/api/v1/inbox?direction=received").json()["threads"]
        assert [r["company"] for r in rows] == ["Acme"]

    def test_sent_keeps_every_thread_that_had_mail_go_out(self, ready, db_session):
        self._two_threads(ready, db_session)

        rows = ready.get("/api/v1/inbox?direction=sent").json()["threads"]
        # Both were written to; a reply doesn't unsend the outreach.
        assert sorted(r["company"] for r in rows) == ["Acme", "Northwind"]

    def test_sent_excludes_a_thread_that_only_holds_a_draft(self, ready, db_session):
        _campaign(ready)

        assert ready.get("/api/v1/inbox?direction=sent").json()["threads"] == []
        assert len(ready.get("/api/v1/inbox").json()["threads"]) == 1

    def test_the_direction_filter_does_not_move_the_counts(self, ready, db_session):
        self._two_threads(ready, db_session)

        counts = ready.get("/api/v1/inbox?direction=received").json()["counts"]
        assert counts["threads"] == 2
        assert counts["sent"] == 2
        assert counts["received"] == 1

    def test_an_unknown_direction_is_rejected(self, ready):
        assert ready.get("/api/v1/inbox?direction=sideways").status_code == 422


class TestInboxFilters:
    def test_intent_filter_narrows_the_list_but_not_the_counts(self, ready, db_session):
        _campaign(ready)
        _reply(db_session, _thread_for(db_session), intent=ReplyIntent.INTERESTED)

        body = ready.get(
            f"/api/v1/inbox?intent={ReplyIntent.NOT_INTERESTED.value}"
        ).json()
        assert body["threads"] == []
        # Counts describe the whole inbox so the filter chips stay meaningful.
        assert body["counts"]["threads"] == 1

    def test_unread_filter_hides_read_conversations(self, ready, db_session):
        _campaign(ready)
        _reply(db_session, _thread_for(db_session), read=True)

        assert ready.get("/api/v1/inbox?unread=true").json()["threads"] == []
        assert len(ready.get("/api/v1/inbox").json()["threads"]) == 1

    def test_search_matches_the_company(self, ready, db_session):
        _campaign(ready)
        _reply(db_session, _thread_for(db_session))

        assert len(ready.get("/api/v1/inbox?q=acme").json()["threads"]) == 1
        assert ready.get("/api/v1/inbox?q=northwind").json()["threads"] == []


class TestConversation:
    def test_detail_returns_the_whole_history_oldest_first(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        _reply(db_session, thread)

        body = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()
        messages = body["messages"]
        assert len(messages) >= 2
        assert messages == sorted(messages, key=lambda m: m["id"])
        assert messages[0]["direction"] == EmailDirection.SENT.value
        assert messages[-1]["direction"] == EmailDirection.RECEIVED.value
        assert messages[-1]["intent"] == ReplyIntent.SCHEDULING.value

    def test_a_draft_reply_is_flagged_for_the_approval_controls(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        _reply(db_session, thread)

        body = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()
        drafts = [m for m in body["messages"] if m["is_draft"]]
        assert len(drafts) == 1
        assert drafts[0]["status"] == EmailStatus.DRAFT.value

    def test_a_thread_with_no_reply_is_still_readable(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)

        resp = ready.get(f"/api/v1/inbox/threads/{thread.id}")
        assert resp.status_code == 200
        assert resp.json()["inbound_count"] == 0

    def test_history_is_ordered_by_when_it_happened_not_by_row_id(
        self, ready, db_session
    ):
        from datetime import UTC, datetime, timedelta

        _campaign(ready)
        thread = _thread_for(db_session)
        _mark_outreach_sent(db_session, thread)
        reply = _reply(db_session, thread, body="Answering your note")

        # Mail backfilled from Gmail is written last but happened first — the row
        # id would put it at the bottom of a conversation it opened.
        backfilled = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.SENT,
            from_address="candidate@example.com",
            to_address="talent@acme.com",
            subject="An earlier note",
            body_text="Sent from my own client months ago",
            gmail_message_id="gmail-msg-old",
            sent_at=datetime.now(UTC) - timedelta(days=120),
        )
        db_session.add(backfilled)
        db_session.commit()

        messages = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()["messages"]
        assert messages[0]["id"] == backfilled.id
        assert messages[-1]["id"] == reply.id
        # Interleaved: the conversation reads sent → received down the page.
        assert [m["direction"] for m in messages] == [
            EmailDirection.SENT.value,
            EmailDirection.SENT.value,
            EmailDirection.RECEIVED.value,
        ]

    def test_marking_read_returns_the_refreshed_row(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        _reply(db_session, thread)

        body = ready.post(f"/api/v1/inbox/threads/{thread.id}/read").json()
        assert body["unread_count"] == 0
        assert body["thread_id"] == thread.id

    def test_another_users_conversation_is_not_found(self, ready, db_session, client):
        _campaign(ready)
        thread = _thread_for(db_session)
        _reply(db_session, thread)
        thread_id = thread.id

        # A second account must not be able to read the first one's mail.
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
        headers = {"Authorization": f"Bearer {token}"}

        assert client.get("/api/v1/inbox", headers=headers).json()["threads"] == []
        assert (
            client.get(f"/api/v1/inbox/threads/{thread_id}", headers=headers).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/inbox/threads/{thread_id}/read", headers=headers
            ).status_code
            == 404
        )

    def test_a_missing_conversation_is_404(self, ready):
        assert ready.get("/api/v1/inbox/threads/9999").status_code == 404


class TestSync:
    def test_sync_is_a_no_op_without_connected_threads(self, ready, db_session):
        _campaign(ready)
        # The campaign's thread has no gmail_thread_id until an email is sent,
        # so there is nothing to poll and nothing to fail.
        body = ready.post("/api/v1/inbox/sync").json()
        assert body == {
            "threads_polled": 0,
            "dispatched": False,
            "errors": 0,
            "threads_skipped": 0,
        }

    def test_sync_polls_connected_threads_inline_without_a_worker(
        self, ready, db_session, monkeypatch
    ):
        from app.tasks import inbox_tasks

        _campaign(ready)
        thread = _thread_for(db_session)
        thread.gmail_thread_id = "gmail-thread-1"
        db_session.commit()

        polled: list[int] = []
        monkeypatch.setattr(
            inbox_tasks.poll_thread, "run", lambda tid: polled.append(tid)
        )

        body = ready.post("/api/v1/inbox/sync").json()
        assert polled == [thread.id]
        assert body["threads_polled"] == 1
        assert body["dispatched"] is False

    def test_a_failing_poll_is_reported_not_raised(self, ready, db_session, monkeypatch):
        from app.tasks import inbox_tasks

        _campaign(ready)
        thread = _thread_for(db_session)
        thread.gmail_thread_id = "gmail-thread-1"
        db_session.commit()

        def _boom(tid):
            raise RuntimeError("gmail is down")

        monkeypatch.setattr(inbox_tasks.poll_thread, "run", _boom)

        resp = ready.post("/api/v1/inbox/sync")
        assert resp.status_code == 200
        assert resp.json() == {
            "threads_polled": 0,
            "dispatched": False,
            "errors": 1,
            "threads_skipped": 0,
        }

    def test_an_inline_sync_says_what_it_left_unpolled(
        self, ready, db_session, monkeypatch
    ):
        from app.routers import inbox as inbox_router
        from app.tasks import inbox_tasks

        _campaign(ready, "Acme")
        _campaign(ready, "Northwind")
        for thread in db_session.query(EmailThread).all():
            thread.gmail_thread_id = f"gmail-thread-{thread.id}"
        db_session.commit()

        monkeypatch.setattr(inbox_router, "_INLINE_SYNC_LIMIT", 1)
        monkeypatch.setattr(inbox_tasks.poll_thread, "run", lambda tid: None)

        body = ready.post("/api/v1/inbox/sync").json()
        # The cap is real, so it is reported rather than passed off as complete.
        assert body["threads_polled"] == 1
        assert body["threads_skipped"] == 1


class TestPollingRecordsBothSides:
    """Gmail hands back the whole thread. Both halves of it are history."""

    def _gmail_message(self, gmail_id, from_addr, when, body, subject="Re: Backend role"):
        return {
            "id": gmail_id,
            "internalDate": str(int(when.timestamp() * 1000)),
            "payload": {
                "headers": [
                    {"name": "From", "value": from_addr},
                    {"name": "To", "value": "talent@acme.com"},
                    {"name": "Subject", "value": subject},
                ]
            },
            "snippet": body,
        }

    def _poll(self, db, monkeypatch, thread_id, messages):
        from app.tasks import inbox_tasks

        # poll_thread closes the session it was handed, so instances from an
        # earlier call are detached — always work from the id.
        thread = db.get(EmailThread, thread_id)
        thread.gmail_thread_id = "gmail-thread-1"
        db.commit()
        monkeypatch.setattr(
            inbox_tasks.gmail_service,
            "list_thread_messages",
            lambda acct, tid: messages,
        )
        monkeypatch.setattr(
            inbox_tasks.gmail_service,
            "extract_plain_text",
            lambda msg: msg["snippet"],
        )
        # poll_thread opens its own SessionLocal — point it at the test session.
        monkeypatch.setattr(inbox_tasks, "SessionLocal", lambda: db)
        return inbox_tasks.poll_thread.run(thread_id)

    def test_our_own_mail_on_the_thread_is_recorded_as_sent(
        self, ready, db_session, monkeypatch
    ):
        from datetime import UTC, datetime, timedelta

        _campaign(ready)
        thread = _thread_for(db_session)
        long_ago = datetime.now(UTC) - timedelta(days=400)

        result = self._poll(
            db_session,
            monkeypatch,
            thread.id,
            [
                self._gmail_message(
                    "own-1",
                    "Jordan Candidate <candidate@example.com>",
                    long_ago,
                    "Sent from my own client, long before TalentPing.",
                )
            ],
        )

        assert result["new_sent"] == 1
        assert result["new_replies"] == 0
        stored = (
            db_session.query(Email)
            .filter_by(gmail_message_id="own-1")
            .one()
        )
        assert stored.direction == EmailDirection.SENT
        assert stored.status == EmailStatus.SENT
        # Gmail's timestamp, not the moment we polled — otherwise every old email
        # arrives dated today and the history is a lie.
        assert stored.sent_at.date() == long_ago.date()

    def test_a_backfilled_thread_lists_with_its_real_date(
        self, ready, db_session, monkeypatch
    ):
        from datetime import UTC, datetime, timedelta

        _campaign(ready)
        thread = _thread_for(db_session)
        long_ago = datetime.now(UTC) - timedelta(days=400)
        self._poll(
            db_session,
            monkeypatch,
            thread.id,
            [
                self._gmail_message(
                    "own-2", "candidate@example.com", long_ago, "An old application."
                )
            ],
        )

        row = ready.get("/api/v1/inbox?direction=sent").json()["threads"][0]
        assert row["outbound_count"] == 1
        assert row["last_outbound_at"].startswith(long_ago.strftime("%Y-%m-%d"))

    def test_polling_twice_does_not_duplicate_a_message(
        self, ready, db_session, monkeypatch
    ):
        from datetime import UTC, datetime

        _campaign(ready)
        # Read the id up front: the first poll closes the session and detaches
        # every instance it was holding.
        thread_id = _thread_for(db_session).id
        messages = [
            self._gmail_message(
                "own-3", "candidate@example.com", datetime.now(UTC), "One send."
            )
        ]

        first = self._poll(db_session, monkeypatch, thread_id, messages)
        second = self._poll(db_session, monkeypatch, thread_id, messages)

        assert (first["new_sent"], second["new_sent"]) == (1, 0)
        assert db_session.query(Email).filter_by(gmail_message_id="own-3").count() == 1

    def test_an_inbound_reply_keeps_gmails_timestamp(
        self, ready, db_session, monkeypatch
    ):
        from datetime import UTC, datetime, timedelta

        _campaign(ready)
        thread = _thread_for(db_session)
        last_month = datetime.now(UTC) - timedelta(days=30)

        result = self._poll(
            db_session,
            monkeypatch,
            thread.id,
            [
                self._gmail_message(
                    "them-1",
                    "Dana Reed <talent@acme.com>",
                    last_month,
                    "We'd love to talk — are you free Thursday?",
                )
            ],
        )

        assert result["new_replies"] == 1
        stored = db_session.query(Email).filter_by(gmail_message_id="them-1").one()
        assert stored.direction == EmailDirection.RECEIVED
        assert stored.sent_at.date() == last_month.date()


class TestInboxAuth:
    def test_inbox_requires_auth(self, client):
        assert client.get("/api/v1/inbox").status_code == 401
        assert client.get("/api/v1/inbox/threads/1").status_code == 401
        assert client.post("/api/v1/inbox/sync").status_code == 401


class TestStatusIsCarriedThrough:
    def test_the_row_reports_the_application_stage(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        application = db_session.get(Application, thread.application_id)
        application.status = ApplicationStatus.INTERVIEW_SCHEDULED
        db_session.commit()
        _reply(db_session, thread)

        row = ready.get("/api/v1/inbox").json()["threads"][0]
        assert row["application_status"] == ApplicationStatus.INTERVIEW_SCHEDULED.value


# --------------------------------------------------------------------------- #
# What a message in a conversation carries                                     #
# --------------------------------------------------------------------------- #


class TestConversationAttachments:
    """The third place a draft appears should say what the other two say.

    Attachments resolve at send time, so a draft has nothing on its own row to
    show. A reviewer reading the reply waiting on a thread needs the same answer
    the Drafts tab and the Recruiter Inbox give: which document is coming, or
    why none is.
    """

    def test_a_draft_names_the_resume_it_would_carry(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)

        body = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()

        draft = next(m for m in body["messages"] if m["is_draft"])
        assert draft["attachments"], "a draft should name the resume it will send"
        assert draft["attachments"][0].endswith(".pdf")
        assert draft["attachment_note"] is None

    def test_a_draft_with_no_renderer_says_why(self, ready, db_session, monkeypatch):
        from app.services import resume_pdf

        _campaign(ready)
        thread = _thread_for(db_session)
        monkeypatch.setattr(resume_pdf, "is_available", lambda: False)

        body = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()

        draft = next(m for m in body["messages"] if m["is_draft"])
        assert draft["attachments"] == []
        assert "PDF rendering is unavailable" in draft["attachment_note"]

    def test_a_sent_message_reports_what_actually_went(self, ready, db_session):
        """Not what would go today — the row is the record of the send."""
        _campaign(ready)
        thread = _thread_for(db_session)
        sent = _mark_outreach_sent(db_session, thread)
        sent.attachment_filename = "the-one-that-went.pdf"
        db_session.commit()

        body = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()

        row = next(m for m in body["messages"] if m["id"] == sent.id)
        assert row["attachments"] == ["the-one-that-went.pdf"]
        # A sent row has no "why not" to report — it already happened.
        assert row["attachment_note"] is None


# --------------------------------------------------------------------------- #
# Opening an attachment                                                        #
# --------------------------------------------------------------------------- #


def _draft_id(client, thread_id):
    """The id of the reply waiting on this conversation."""
    messages = client.get(f"/api/v1/inbox/threads/{thread_id}").json()["messages"]
    return next(m["id"] for m in messages if m["is_draft"])


class TestAttachmentPreview:
    """Naming a file is not showing it.

    Every screen listed attachments and stopped at the filename — there was no
    endpoint serving the bytes at all, so a reviewer about to approve an
    irreversible send could not open the resume it would carry. These cover the
    file coming back, the position it comes back at, and the two ways asking for
    one can legitimately fail.
    """

    def test_a_draft_attachment_comes_back_as_a_readable_pdf(self, ready, db_session):
        _campaign(ready)
        thread = _thread_for(db_session)
        email_id = _draft_id(ready, thread.id)

        response = ready.get(f"/api/v1/inbox/emails/{email_id}/attachments/0")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        # Inline is the difference between previewing a document and downloading
        # one the user then has to go and find.
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.content.startswith(b"%PDF")

    def test_the_file_served_is_the_file_that_was_named(self, ready, db_session):
        """List and bytes come off one resolver, so position 0 is position 0."""
        _campaign(ready)
        thread = _thread_for(db_session)
        messages = ready.get(f"/api/v1/inbox/threads/{thread.id}").json()["messages"]
        draft = next(m for m in messages if m["is_draft"])

        response = ready.get(
            f"/api/v1/inbox/emails/{draft['id']}/attachments/0"
        )

        assert draft["attachments"][0] in response.headers["content-disposition"]

    def test_a_position_carrying_nothing_is_not_found(self, ready, db_session):
        """One resume, no cover letter — position 1 is empty, not broken."""
        _campaign(ready)
        thread = _thread_for(db_session)
        email_id = _draft_id(ready, thread.id)

        assert ready.get(f"/api/v1/inbox/emails/{email_id}/attachments/1").status_code == 404
        assert ready.get(f"/api/v1/inbox/emails/{email_id}/attachments/-1").status_code == 404

    def test_a_resume_that_cannot_be_rendered_says_why(
        self, ready, db_session, monkeypatch
    ):
        """409 with the sentence the list shows — "no renderer" is an answer."""
        from app.services import resume_pdf

        _campaign(ready)
        thread = _thread_for(db_session)
        email_id = _draft_id(ready, thread.id)
        monkeypatch.setattr(resume_pdf, "is_available", lambda: False)

        response = ready.get(f"/api/v1/inbox/emails/{email_id}/attachments/0")

        assert response.status_code == 409
        assert "PDF rendering is unavailable" in response.json()["detail"]

    def test_a_sent_message_serves_the_pdf_that_was_stored(self, ready, db_session):
        """A tailored resume is kept as bytes, so history opens the real file."""
        from datetime import UTC, datetime

        from app.models.job import JobPosting, job_fingerprint
        from app.models.resume import Resume
        from app.models.tailored_resume import TailoredResume

        _campaign(ready)
        thread = _thread_for(db_session)
        sent = _mark_outreach_sent(db_session, thread)
        application = db_session.get(Application, thread.application_id)

        posting = JobPosting(
            user_id=application.user_id,
            title="Backend Engineer",
            company="Acme",
            fingerprint=job_fingerprint("Backend Engineer", "Acme", None),
        )
        db_session.add(posting)
        db_session.flush()
        base = db_session.query(Resume).filter_by(user_id=application.user_id).first()
        db_session.add(
            TailoredResume(
                user_id=application.user_id,
                resume_id=base.id,
                job_posting_id=posting.id,
                pdf_bytes=b"%PDF-1.4 the one that went",
                pdf_filename="jordan-acme.pdf",
                pdf_generated_at=datetime.now(UTC),
            )
        )
        application.job_posting_id = posting.id
        db_session.commit()

        response = ready.get(f"/api/v1/inbox/emails/{sent.id}/attachments/0")

        assert response.status_code == 200
        assert response.content == b"%PDF-1.4 the one that went"
        assert 'filename="jordan-acme.pdf"' in response.headers["content-disposition"]

    def test_a_filename_cannot_inject_a_header(self, ready, db_session):
        """Stored filenames are data, and this one lands in a response header."""
        from app.services.email_attachments import (
            inline_content_disposition as _content_disposition,
        )

        header = _content_disposition('evil".pdf\r\nSet-Cookie: a=b')

        assert "\r" not in header and "\n" not in header
        assert header.count('"') == 2

    def test_another_users_attachment_is_not_found(self, ready, db_session, client):
        _campaign(ready)
        thread = _thread_for(db_session)
        email_id = _draft_id(ready, thread.id)

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
            f"/api/v1/inbox/emails/{email_id}/attachments/0",
            headers={"Authorization": f"Bearer {token}"},
        )

        # Not found rather than forbidden: a 403 would confirm the id exists.
        assert response.status_code == 404

    def test_a_missing_message_is_404(self, ready):
        assert ready.get("/api/v1/inbox/emails/9999/attachments/0").status_code == 404

    def test_an_attachment_needs_a_login(self, client):
        assert client.get("/api/v1/inbox/emails/1/attachments/0").status_code == 401
