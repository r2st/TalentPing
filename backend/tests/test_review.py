"""Reply agent, status auto-detection, bounce handling, and the review queue.

The reply agent drafts responses for the user to approve — nothing it writes is
ever sent unseen. These tests cover the drafting angles, the pipeline status a
reply moves an application to, delivery-bounce handling, and the approve/dismiss
gate.
"""
from __future__ import annotations

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_thread import EmailThread
from app.models.recruiter import DeliveryState, Recruiter
from app.services import recruiter_discovery
from app.services.ai_composer import CandidateContext, compose_reply
from app.services.career_scraper import Contact, ScrapeResult
from app.tasks import inbox_tasks


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


def _application(db):
    """The single application created by a review-mode campaign."""
    return db.query(Application).first()


class TestReplyComposer:
    def test_rejection_gets_a_graceful_thank_you(self):
        cand = CandidateContext(name="Jordan")
        draft = compose_reply(cand, "Unfortunately we're going to pass.", "NOT_INTERESTED")
        assert "thank you" in draft.lower()
        assert "Jordan" in draft
        # A graceful close makes no ask of the recruiter.
        assert "?" not in draft

    def test_offer_reply_is_warm_but_non_committal(self):
        cand = CandidateContext(name="Jordan")
        draft = compose_reply(cand, "We'd like to offer you the role!", "OFFER")
        low = draft.lower()
        assert "writing" in low or "details" in low  # asks to see it in writing

    def test_scheduling_reply_offers_flexibility(self):
        cand = CandidateContext(name="Jordan")
        draft = compose_reply(cand, "When are you free?", "SCHEDULING")
        assert "Jordan" in draft


class TestStatusAutoDetection:
    def _seed_reply(self, db, ready, intent: ReplyIntent, body: str):
        ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme"], "auto_send": False},
        )
        application = _application(db)
        thread = db.query(EmailThread).filter_by(application_id=application.id).first()
        user = application.campaign.user
        inbound = Email(
            thread_id=thread.id,
            direction=EmailDirection.RECEIVED,
            status=EmailStatus.RECEIVED,
            from_address="talent@acme.com",
            body_text=body,
            intent=intent,
        )
        db.add(inbound)
        db.flush()
        inbox_tasks._apply_intent(db, application, thread, user, intent, body)
        db.commit()
        db.refresh(application)
        return application, thread

    def test_scheduling_moves_the_application_to_scheduling(self, ready, db_session):
        application, _ = self._seed_reply(
            db_session, ready, ReplyIntent.SCHEDULING, "Are you free Thursday?"
        )
        assert application.status == ApplicationStatus.SCHEDULING

    def test_offer_moves_the_application_to_offer(self, ready, db_session):
        application, _ = self._seed_reply(
            db_session, ready, ReplyIntent.OFFER, "We'd love to extend an offer."
        )
        assert application.status == ApplicationStatus.OFFER

    def test_rejection_drafts_a_reply_and_marks_not_interested(self, ready, db_session):
        application, thread = self._seed_reply(
            db_session, ready, ReplyIntent.NOT_INTERESTED, "We'll pass, thanks."
        )
        assert application.status == ApplicationStatus.NOT_INTERESTED
        # A graceful thank-you was drafted for review (newest draft on the thread,
        # i.e. the reply — not the original outreach draft).
        draft = (
            db_session.query(Email)
            .filter_by(thread_id=thread.id, status=EmailStatus.DRAFT)
            .order_by(Email.id.desc())
            .first()
        )
        assert draft is not None and "thank" in (draft.body_text or "").lower()

    def test_unsubscribe_opts_the_recruiter_out(self, ready, db_session):
        self._seed_reply(db_session, ready, ReplyIntent.UNSUBSCRIBE, "Please remove me.")
        recruiter = db_session.query(Recruiter).filter_by(email="talent@acme.com").first()
        assert recruiter.opted_out is True


class TestBounceHandling:
    def test_a_bounce_pauses_the_address_and_records_it(
        self, ready, db_session, monkeypatch
    ):
        ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme"], "auto_send": False},
        )
        application = _application(db_session)
        thread = db_session.query(EmailThread).filter_by(
            application_id=application.id
        ).first()
        thread.gmail_thread_id = "t-1"
        db_session.commit()
        thread_id = thread.id  # capture before poll_thread detaches instances

        bounce_msg = {
            "id": "bounce-1",
            "payload": {
                "headers": [
                    {"name": "From", "value": "mailer-daemon@googlemail.com"},
                    {"name": "Subject", "value": "Delivery Status Notification (Failure)"},
                ]
            },
            "snippet": "Address not found. 550 5.1.1 user unknown",
        }
        monkeypatch.setattr(
            inbox_tasks.gmail_service, "list_thread_messages", lambda acct, tid: [bounce_msg]
        )
        monkeypatch.setattr(
            inbox_tasks.gmail_service,
            "extract_plain_text",
            lambda msg: "Address not found. 550 5.1.1 user unknown",
        )
        # poll_thread opens its own SessionLocal — point it at the test session so
        # it sees the in-memory schema and data.
        monkeypatch.setattr(inbox_tasks, "SessionLocal", lambda: db_session)

        inbox_tasks.poll_thread.run(thread_id)

        db_session.expire_all()
        recruiter = db_session.query(Recruiter).filter_by(email="talent@acme.com").first()
        # 550 5.1.1 is permanent: the address is suppressed for good...
        assert recruiter.delivery_state == DeliveryState.HARD_BOUNCED
        # ...but opted_out is untouched. That flag means the human asked us to
        # stop, and spending it on a delivery failure loses the contact forever
        # across every future campaign.
        assert recruiter.opted_out is False

        from app.models.gmail_account import GmailAccount

        account = db_session.query(GmailAccount).first()
        assert account.bounce_count == 1
        # The bounce is not stored as a recruiter "reply".
        replies = (
            db_session.query(Email)
            .filter_by(thread_id=thread_id, direction=EmailDirection.RECEIVED)
            .count()
        )
        assert replies == 0


class TestReviewQueue:
    def test_lists_outreach_drafts_awaiting_approval(self, ready, db_session):
        ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme"], "auto_send": False},
        )
        body = ready.get("/api/v1/review").json()
        assert body["count"] == 1
        item = body["items"][0]
        assert item["kind"] == "outreach"
        assert item["company"] == "Acme"

    def test_approve_queues_the_draft(self, ready, db_session):
        ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme"], "auto_send": False},
        )
        email_id = db_session.query(Email).filter_by(status=EmailStatus.DRAFT).first().id
        resp = ready.post(f"/api/v1/review/emails/{email_id}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == EmailStatus.QUEUED.value

    def test_dismiss_discards_the_draft(self, ready, db_session):
        ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme"], "auto_send": False},
        )
        email_id = db_session.query(Email).filter_by(status=EmailStatus.DRAFT).first().id
        assert ready.delete  # sanity
        resp = ready.post(f"/api/v1/review/emails/{email_id}/dismiss")
        assert resp.status_code == 204
        assert db_session.get(Email, email_id) is None

    def test_a_sent_email_cannot_be_approved(self, ready, db_session):
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        email = db_session.query(Email).first()
        email.status = EmailStatus.SENT
        db_session.commit()
        assert ready.post(f"/api/v1/review/emails/{email.id}/approve").status_code == 409

    def test_review_requires_auth(self, client):
        assert client.get("/api/v1/review").status_code == 401
