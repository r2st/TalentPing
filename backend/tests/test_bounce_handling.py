"""Bounce classification, suppression, and the per-domain report.

The central assertion running through this file is that **consent and
deliverability are separate axes**. Before this feature every bounce set
``opted_out``, which is the CAN-SPAM flag meaning "this human asked us to stop" —
so a recruiter whose mailbox was full lost the contact permanently, across every
future campaign. A hard bounce now suppresses the address without ever touching
that flag.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_bounce import BounceKind, EmailBounce
from app.models.email_thread import EmailThread
from app.models.recruiter import DeliveryState, Recruiter
from app.services import bounce_service as svc


def _recruiter(db, user, email="talent@acme.com", **overrides) -> Recruiter:
    defaults = dict(user_id=user.id, email=email, company="Acme")
    defaults.update(overrides)
    row = Recruiter(**defaults)
    db.add(row)
    db.commit()
    return row


class TestClassification:
    @pytest.mark.parametrize(
        ("body", "kind"),
        [
            ("550 5.1.1 user unknown", BounceKind.HARD),
            ("Status: 5.1.1", BounceKind.HARD),
            ("452 4.2.2 mailbox is full", BounceKind.SOFT),
            ("Status: 4.4.1 connection timed out", BounceKind.SOFT),
        ],
    )
    def test_enhanced_status_codes_decide(self, body, kind):
        assert svc.classify_bounce("Delivery failure", body).kind is kind

    @pytest.mark.parametrize(
        ("body", "kind"),
        [
            ("550 recipient rejected", BounceKind.HARD),
            ("553 sorry", BounceKind.HARD),
            ("421 service unavailable", BounceKind.SOFT),
            ("450 requested action not taken", BounceKind.SOFT),
        ],
    )
    def test_bare_smtp_codes_are_the_fallback(self, body, kind):
        assert svc.classify_bounce("Failure", body).kind is kind

    def test_a_code_beats_a_contradicting_phrase(self):
        """'Mailbox full' with a 5.2.2 is permanent — that server has decided."""
        verdict = svc.classify_bounce(
            "Undeliverable", "The mailbox is full. Status: 5.2.2"
        )
        assert verdict.kind is BounceKind.HARD
        assert verdict.code == "5.2.2"

    def test_phrases_are_used_when_there_is_no_code(self):
        assert svc.classify_bounce("Failure", "user unknown").kind is BounceKind.HARD
        assert svc.classify_bounce("Failure", "mailbox full").kind is BounceKind.SOFT

    def test_ties_go_soft(self):
        """Wrongly soft costs two sends; wrongly hard loses the contact forever."""
        verdict = svc.classify_bounce(
            "Failure", "The mailbox is full and the user is unknown"
        )
        assert verdict.kind is BounceKind.SOFT

    def test_an_unreadable_notice_is_treated_as_soft(self):
        assert svc.classify_bounce("Failure", "something went wrong").kind is (
            BounceKind.SOFT
        )

    @pytest.mark.parametrize(
        "body",
        [
            "I am out of office until Monday.",
            "Automatic reply: on annual leave",
            "Auto-reply — currently on leave",
        ],
    )
    def test_an_auto_responder_is_not_a_bounce(self, body):
        assert svc.classify_bounce("Away", body).is_bounce is False

    def test_empty_input_is_not_a_bounce(self):
        assert svc.classify_bounce(None, None).is_bounce is False
        assert svc.classify_bounce("", "  ").is_bounce is False

    def test_the_reason_is_captured_for_the_tracker(self):
        verdict = svc.classify_bounce("Undeliverable", "550 5.1.1 user unknown")
        assert "user unknown" in (verdict.reason or "")


class TestRecordBounce:
    def test_a_hard_bounce_suppresses_the_address(self, db_session, current_user):
        rec = _recruiter(db_session, current_user)
        verdict = svc.classify_bounce("Failure", "550 5.1.1 user unknown")
        svc.record_bounce(
            db_session, user_id=current_user.id, recruiter=rec, verdict=verdict
        )
        db_session.commit()

        assert rec.delivery_state == DeliveryState.HARD_BOUNCED
        assert svc.is_suppressed(rec) is True
        row = db_session.query(EmailBounce).one()
        assert row.kind is BounceKind.HARD
        assert row.domain == "acme.com"
        assert row.user_id == current_user.id

    def test_a_hard_bounce_never_touches_opted_out(self, db_session, current_user):
        rec = _recruiter(db_session, current_user)
        svc.record_bounce(
            db_session,
            user_id=current_user.id,
            recruiter=rec,
            verdict=svc.classify_bounce("Failure", "550 5.1.1 user unknown"),
        )
        db_session.commit()
        assert rec.opted_out is False

    def test_a_soft_bounce_is_recoverable(self, db_session, current_user):
        rec = _recruiter(db_session, current_user)
        svc.record_bounce(
            db_session,
            user_id=current_user.id,
            recruiter=rec,
            verdict=svc.classify_bounce("Failure", "452 4.2.2 mailbox full"),
        )
        db_session.commit()

        assert rec.delivery_state == DeliveryState.SOFT_BOUNCED
        assert rec.soft_bounce_count == 1
        assert svc.is_suppressed(rec) is False

    def test_repeated_soft_bounces_escalate(self, db_session, current_user):
        rec = _recruiter(db_session, current_user)
        verdict = svc.classify_bounce("Failure", "452 4.2.2 mailbox full")
        for _ in range(svc.SOFT_BOUNCE_LIMIT):
            svc.record_bounce(
                db_session, user_id=current_user.id, recruiter=rec, verdict=verdict
            )
        db_session.commit()

        assert rec.delivery_state == DeliveryState.HARD_BOUNCED
        assert "soft bounces" in rec.last_bounce_reason

    def test_a_reply_clears_soft_bounces(self, db_session, current_user):
        rec = _recruiter(db_session, current_user)
        svc.record_bounce(
            db_session,
            user_id=current_user.id,
            recruiter=rec,
            verdict=svc.classify_bounce("Failure", "452 4.2.2 mailbox full"),
        )
        svc.clear_soft_bounces(rec)
        db_session.commit()

        assert rec.soft_bounce_count == 0
        assert rec.delivery_state == DeliveryState.OK

    def test_a_reply_does_not_revive_a_hard_bounced_contact(
        self, db_session, current_user
    ):
        rec = _recruiter(db_session, current_user, delivery_state=DeliveryState.HARD_BOUNCED)
        svc.clear_soft_bounces(rec)
        assert rec.delivery_state == DeliveryState.HARD_BOUNCED

    def test_a_non_bounce_records_nothing(self, db_session, current_user):
        rec = _recruiter(db_session, current_user)
        assert (
            svc.record_bounce(
                db_session,
                user_id=current_user.id,
                recruiter=rec,
                verdict=svc.classify_bounce("Away", "I am out of office"),
            )
            is None
        )
        db_session.commit()
        assert db_session.query(EmailBounce).count() == 0


class TestReputationLedger:
    """Only permanent failures may push a mailbox toward the bounce-rate pause."""

    def _bounce_count(self, db_session) -> int:
        from app.models.gmail_account import GmailAccount

        # Re-queried rather than held: poll_thread closes its session, which
        # detaches anything captured beforehand.
        return db_session.query(GmailAccount).one().bounce_count

    def test_a_soft_bounce_does_not_reach_the_mailbox_ledger(
        self, db_session, current_user, connected_gmail, resume, monkeypatch
    ):
        """A full inbox is not this sender's deliverability problem."""
        recruiter, _ = _run_bounce_poll(
            db_session, current_user, resume, monkeypatch, "452 4.2.2 mailbox is full"
        )
        assert self._bounce_count(db_session) == 0
        assert recruiter.delivery_state == DeliveryState.SOFT_BOUNCED

    def test_a_hard_bounce_does(
        self, db_session, current_user, connected_gmail, resume, monkeypatch
    ):
        recruiter, _ = _run_bounce_poll(
            db_session, current_user, resume, monkeypatch, "550 5.1.1 user unknown"
        )
        assert self._bounce_count(db_session) == 1
        assert recruiter.delivery_state == DeliveryState.HARD_BOUNCED

    def test_the_same_notice_is_only_ever_counted_once(
        self, db_session, current_user, connected_gmail, resume, monkeypatch
    ):
        """A DSN sits on the thread forever, so every poll re-reads it.

        The bounce branch stores no ``Email`` row, so ``known_ids`` cannot
        suppress the notice on the next pass — and ``bounce_count`` is a
        lifetime counter with no ledger behind it, so nothing noticed the
        double-count. In production a poll a minute reached 4081 bounces against
        20 real sends: a 20400% rate that paused the mailbox, re-paused it on
        every pass forever, and stopped all outbound mail.
        """
        recruiter, _ = _run_bounce_poll(
            db_session,
            current_user,
            resume,
            monkeypatch,
            "550 5.1.1 user unknown",
            polls=5,
        )
        assert self._bounce_count(db_session) == 1
        assert recruiter.delivery_state == DeliveryState.HARD_BOUNCED
        assert db_session.query(EmailBounce).count() == 1

    def test_a_repeated_soft_notice_does_not_escalate_to_undeliverable(
        self, db_session, current_user, connected_gmail, resume, monkeypatch
    ):
        """One full mailbox re-read four times is not four failures.

        ``SOFT_BOUNCE_LIMIT`` is 3, so re-counting a single notice wrote a live
        address off as permanently dead — one contact reached
        ``soft_bounce_count`` 1958 that way.
        """
        recruiter, _ = _run_bounce_poll(
            db_session,
            current_user,
            resume,
            monkeypatch,
            "452 4.2.2 mailbox is full",
            polls=4,
        )
        assert recruiter.delivery_state == DeliveryState.SOFT_BOUNCED
        assert recruiter.soft_bounce_count == 1
        assert self._bounce_count(db_session) == 0

    def test_a_bounce_is_never_stored_as_a_reply(
        self, db_session, current_user, connected_gmail, resume, monkeypatch
    ):
        """Otherwise stop-on-reply would read a bounce as the recruiter answering."""
        _, thread = _run_bounce_poll(
            db_session, current_user, resume, monkeypatch, "550 5.1.1 user unknown"
        )
        replies = (
            db_session.query(Email)
            .filter_by(thread_id=thread.id, direction=EmailDirection.RECEIVED)
            .count()
        )
        assert replies == 0


def _run_bounce_poll(db_session, user, resume, monkeypatch, body: str, polls: int = 1):
    """Drive poll_thread over a single bounce message and return the recruiter.

    ``polls`` repeats the pass over the *same* message, which is what beat does
    every five minutes for the life of the thread.
    """
    from app.tasks import inbox_tasks

    campaign = Campaign(user_id=user.id, name="c", resume_id=resume.id)
    recruiter = Recruiter(
        user_id=user.id, email=f"t{abs(hash(body)) % 10000}@acme.com", company="Acme"
    )
    db_session.add_all([campaign, recruiter])
    db_session.flush()
    application = Application(
        user_id=user.id,
        campaign_id=campaign.id,
        recruiter_id=recruiter.id,
        status=ApplicationStatus.OUTREACH_SENT,
    )
    db_session.add(application)
    db_session.flush()
    thread = EmailThread(
        application_id=application.id, subject="s", gmail_thread_id=f"t-{body[:6]}"
    )
    db_session.add(thread)
    db_session.commit()

    message = {
        "id": f"bounce-{abs(hash(body)) % 10000}",
        # Gmail stamps every message with this and never changes it, which is
        # what lets a re-read notice be recognised as one it already counted.
        "internalDate": "1753600000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "mailer-daemon@googlemail.com"},
                {"name": "Subject", "value": "Delivery Status Notification (Failure)"},
            ]
        },
        "snippet": body,
    }
    monkeypatch.setattr(
        inbox_tasks.gmail_service, "list_thread_messages", lambda acct, tid: [message]
    )
    monkeypatch.setattr(
        inbox_tasks.gmail_service, "extract_plain_text", lambda msg: body
    )
    monkeypatch.setattr(inbox_tasks, "SessionLocal", lambda: db_session)
    # Capture the ids first: poll_thread closes its session, detaching whatever
    # was held across the call.
    recruiter_id, thread_id = recruiter.id, thread.id
    for _ in range(polls):
        inbox_tasks.poll_thread.run(thread_id)
    db_session.expire_all()
    return db_session.get(Recruiter, recruiter_id), db_session.get(EmailThread, thread_id)


class TestSuppressionInThePipeline:
    def test_a_queued_email_to_a_hard_bounced_address_fails_rather_than_sends(
        self, db_session, current_user, connected_gmail, resume, monkeypatch
    ):
        """The gap between compose and send is hours; the address can go bad in it."""
        from app.tasks import email_tasks

        campaign = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
        recruiter = Recruiter(
            user_id=current_user.id,
            email="dead@acme.com",
            company="Acme",
            delivery_state=DeliveryState.HARD_BOUNCED,
        )
        db_session.add_all([campaign, recruiter])
        db_session.flush()
        application = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=ApplicationStatus.QUEUED,
        )
        db_session.add(application)
        db_session.flush()
        thread = EmailThread(application_id=application.id, subject="s")
        db_session.add(thread)
        db_session.flush()
        email = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.QUEUED,
            to_address=recruiter.email,
            subject="s",
            body_text="b",
        )
        db_session.add(email)
        db_session.commit()
        email_id = email.id

        monkeypatch.setattr(email_tasks, "SessionLocal", lambda: db_session)
        sent: list = []
        monkeypatch.setattr(
            email_tasks.gmail_service,
            "send_email",
            lambda **kw: sent.append(kw),
        )

        result = email_tasks.send_outreach_email.run(email_id)

        assert result["status"] == "failed"
        assert sent == []  # nothing left the mailbox
        db_session.expire_all()
        assert db_session.get(Email, email_id).status == EmailStatus.FAILED

    def test_draft_generation_skips_a_hard_bounced_recruiter(
        self, db_session, current_user, resume
    ):
        from app.services import outreach_service

        campaign = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
        recruiter = Recruiter(
            user_id=current_user.id,
            email="dead@acme.com",
            delivery_state=DeliveryState.HARD_BOUNCED,
        )
        db_session.add_all([campaign, recruiter])
        db_session.commit()

        created, notes = outreach_service.generate_drafts_for_campaign(
            db_session, current_user, campaign, [recruiter.id]
        )
        assert created == []
        assert any("undeliverable" in n for n in notes)

    def test_follow_ups_are_cancelled_for_a_suppressed_address(
        self, db_session, current_user, resume
    ):
        from app.models.follow_up import FollowUp, FollowUpStatus
        from app.services import follow_up_service

        campaign = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
        recruiter = Recruiter(
            user_id=current_user.id,
            email="dead@acme.com",
            delivery_state=DeliveryState.HARD_BOUNCED,
        )
        db_session.add_all([campaign, recruiter])
        db_session.flush()
        application = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=ApplicationStatus.OUTREACH_SENT,
        )
        db_session.add(application)
        db_session.flush()
        follow_up = FollowUp(
            application_id=application.id, step=1, scheduled_at=datetime.now(UTC)
        )
        db_session.add(follow_up)
        db_session.commit()

        email, reason = follow_up_service.prepare_follow_up_email(db_session, follow_up)
        assert email is None
        assert follow_up.status == FollowUpStatus.CANCELLED
        assert "undeliverable" in reason


class TestDomainRates:
    def _sent(self, db, user, resume, address: str, count: int) -> Recruiter:
        campaign = Campaign(user_id=user.id, name="c", resume_id=resume.id)
        recruiter = Recruiter(user_id=user.id, email=address, company="X")
        db.add_all([campaign, recruiter])
        db.flush()
        application = Application(
            user_id=user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=ApplicationStatus.OUTREACH_SENT,
        )
        db.add(application)
        db.flush()
        thread = EmailThread(application_id=application.id, subject="s")
        db.add(thread)
        db.flush()
        for _ in range(count):
            db.add(
                Email(
                    thread_id=thread.id,
                    direction=EmailDirection.SENT,
                    status=EmailStatus.SENT,
                    to_address=address,
                    subject="s",
                    body_text="b",
                    sent_at=datetime.now(UTC),
                )
            )
        db.commit()
        return recruiter

    def test_groups_by_domain_and_ranks_worst_first(
        self, db_session, current_user, resume
    ):
        bad = self._sent(db_session, current_user, resume, "a@bigcorp.com", 4)
        self._sent(db_session, current_user, resume, "b@good.com", 5)
        verdict = svc.classify_bounce("Failure", "550 5.1.1 user unknown")
        for _ in range(3):
            svc.record_bounce(
                db_session, user_id=current_user.id, recruiter=bad, verdict=verdict
            )
        db_session.commit()

        rows = svc.domain_bounce_rates(db_session, current_user.id)
        assert rows[0].domain == "bigcorp.com"
        assert rows[0].sent == 4 and rows[0].hard == 3
        assert rows[0].rate == 0.75
        assert rows[0].alerting is True
        assert rows[1].domain == "good.com" and rows[1].alerting is False

    def test_thin_samples_are_excluded(self, db_session, current_user, resume):
        """One bounce out of one send is a 100% rate and means nothing."""
        rec = self._sent(db_session, current_user, resume, "a@tiny.com", 1)
        svc.record_bounce(
            db_session,
            user_id=current_user.id,
            recruiter=rec,
            verdict=svc.classify_bounce("Failure", "550 5.1.1 user unknown"),
        )
        db_session.commit()

        assert svc.domain_bounce_rates(db_session, current_user.id) == []

    def test_another_users_bounces_are_invisible(
        self, db_session, current_user, resume
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.commit()

        rec = self._sent(db_session, current_user, resume, "a@bigcorp.com", 4)
        svc.record_bounce(
            db_session,
            user_id=current_user.id,
            recruiter=rec,
            verdict=svc.classify_bounce("Failure", "550 5.1.1 user unknown"),
        )
        db_session.commit()

        assert svc.domain_bounce_rates(db_session, other.id) == []


class TestBounceEndpoint:
    def test_reports_totals_and_domains(
        self, auth_client, db_session, current_user, resume
    ):
        rec = TestDomainRates()._sent(
            db_session, current_user, resume, "a@bigcorp.com", 4
        )
        svc.record_bounce(
            db_session,
            user_id=current_user.id,
            recruiter=rec,
            verdict=svc.classify_bounce("Failure", "550 5.1.1 user unknown"),
        )
        svc.record_bounce(
            db_session,
            user_id=current_user.id,
            recruiter=rec,
            verdict=svc.classify_bounce("Failure", "452 4.2.2 mailbox full"),
        )
        db_session.commit()

        body = auth_client.get("/api/v1/analytics/bounces").json()
        assert body["hard_total"] == 1
        assert body["soft_total"] == 1
        assert body["suppressed_contacts"] == 1
        assert body["domains"][0]["domain"] == "bigcorp.com"

    def test_requires_authentication(self, client):
        assert client.get("/api/v1/analytics/bounces").status_code == 401
