"""Learning from what the user did with a draft.

The signal was always there — approve, discard the draft, dismiss the message —
and nothing recorded it, so a user who rejected eleven drafts from the same
sending platform got a twelfth.

The single most important test in this file is
``test_a_boost_can_never_promote_a_draft_to_auto``. Everything else here is
arithmetic; that one is the reason the arithmetic is safe to run at all. Without
it, "approve four drafts from this recruiter" becomes a way to teach the product
to send unread mail to that recruiter, and the user doing the approving has no
idea that is what they are agreeing to.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.models.email import Email, EmailStatus
from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailKind,
    RecruiterEmailStatus,
    ReplyRoute,
)
from app.models.reply_feedback import (
    ClassifierPrior,
    FeedbackSignal,
    PriorScope,
    ReplyFeedback,
)
from app.services import classifier_feedback, recruiter_reply_service
from tests.test_recruiter_reply import gmail_message


@pytest.fixture()
def detected(db_session, current_user, connected_gmail) -> RecruiterEmail:
    """One classified inbound message, as the pipeline would have left it."""
    row = RecruiterEmail(
        user_id=current_user.id,
        gmail_account_id=connected_gmail.id,
        gmail_message_id="m-detected",
        gmail_thread_id="t-detected",
        from_address="alex@northwind.com",
        subject="Senior Backend Engineer",
        body_text="Are you open to a chat?",
        kind=RecruiterEmailKind.RECRUITER_OUTREACH,
        classification_confidence=0.8,
        status=RecruiterEmailStatus.DRAFTED,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _approve(db, row, times=1):
    for _ in range(times):
        classifier_feedback.record(
            db, row, FeedbackSignal.APPROVED, source="review_approve"
        )
    db.commit()


def _reject(db, row, times=1):
    for _ in range(times):
        classifier_feedback.record(
            db, row, FeedbackSignal.REJECTED, source="review_dismiss"
        )
    db.commit()


# --------------------------------------------------------------------------- #
# Recording                                                                    #
# --------------------------------------------------------------------------- #


class TestRecording:
    def test_an_approval_writes_an_audit_row(self, db_session, detected):
        _approve(db_session, detected)

        row = db_session.query(ReplyFeedback).one()
        assert row.signal is FeedbackSignal.APPROVED
        assert row.sender_address == "alex@northwind.com"
        assert row.sender_domain == "northwind.com"

    def test_the_audit_row_freezes_what_the_classifier_thought(
        self, db_session, detected
    ):
        """Re-running the classifier later gives a different reading.

        The point of the row is a disagreement with one *specific* reading, so
        the reading is copied rather than joined.
        """
        _approve(db_session, detected)
        detected.classification_confidence = 0.1
        db_session.commit()

        row = db_session.query(ReplyFeedback).one()
        assert row.classification_confidence == 0.8
        assert row.kind is RecruiterEmailKind.RECRUITER_OUTREACH

    def test_both_scopes_are_rolled_up(self, db_session, detected):
        _approve(db_session, detected)

        scopes = {p.scope: p for p in db_session.query(ClassifierPrior)}
        assert scopes[PriorScope.ADDRESS].value == "alex@northwind.com"
        assert scopes[PriorScope.ADDRESS].approvals == 1
        assert scopes[PriorScope.DOMAIN].value == "northwind.com"
        assert scopes[PriorScope.DOMAIN].approvals == 1

    def test_counters_accumulate_rather_than_duplicating_rows(
        self, db_session, detected
    ):
        _approve(db_session, detected, times=3)

        prior = db_session.query(ClassifierPrior).filter_by(
            scope=PriorScope.ADDRESS
        ).one()
        assert prior.approvals == 3
        assert prior.last_signal_at is not None

    def test_the_reply_to_address_is_what_keys_it(
        self, db_session, current_user, connected_gmail
    ):
        """Platform mail comes from noreply@ and names a human in Reply-To.

        Keying on the From would file every recruiter using the same platform
        under one prior — so one rejection would silence all of them.
        """
        row = RecruiterEmail(
            user_id=current_user.id,
            gmail_account_id=connected_gmail.id,
            gmail_message_id="m-platform",
            from_address="noreply@gem.com",
            reply_to_address="alex@northwind.com",
            kind=RecruiterEmailKind.RECRUITER_OUTREACH,
            classification_confidence=0.8,
            status=RecruiterEmailStatus.DRAFTED,
        )
        db_session.add(row)
        db_session.commit()

        _reject(db_session, row)

        audit = db_session.query(ReplyFeedback).one()
        assert audit.sender_address == "alex@northwind.com"

    def test_feedback_off_records_nothing(self, db_session, detected, monkeypatch):
        monkeypatch.setattr(settings, "recruiter_feedback_enabled", False)

        result = classifier_feedback.record(
            db_session, detected, FeedbackSignal.APPROVED, source="review_approve"
        )

        assert result is None
        assert db_session.query(ReplyFeedback).count() == 0
        assert db_session.query(ClassifierPrior).count() == 0


# --------------------------------------------------------------------------- #
# The arithmetic                                                               #
# --------------------------------------------------------------------------- #


class TestAdjustment:
    def test_no_history_is_no_adjustment(self, db_session, current_user):
        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "stranger@example.com"
        )

        assert found.is_zero
        assert found.reason is None

    def test_an_approval_nudges_upward(self, db_session, current_user, detected):
        _approve(db_session, detected)

        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "alex@northwind.com"
        )

        assert found.delta == pytest.approx(classifier_feedback.APPROVAL_STEP)
        assert "approved" in (found.reason or "")

    def test_a_rejection_moves_further_than_an_approval(
        self, db_session, current_user, detected
    ):
        """Asymmetric by a factor of three, deliberately.

        A reply sent in the candidate's name that should not have been is far
        worse than a draft they never see.
        """
        assert classifier_feedback.REJECTION_STEP > classifier_feedback.APPROVAL_STEP
        _reject(db_session, detected)

        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "alex@northwind.com"
        )

        assert found.delta == pytest.approx(-classifier_feedback.REJECTION_STEP)

    def test_the_boost_is_capped(self, db_session, current_user, detected):
        _approve(db_session, detected, times=20)

        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "alex@northwind.com"
        )

        assert found.delta == pytest.approx(settings.recruiter_feedback_max_boost)

    def test_the_penalty_is_capped(self, db_session, current_user, detected):
        _reject(db_session, detected, times=20)

        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "alex@northwind.com"
        )

        assert found.delta == pytest.approx(-settings.recruiter_feedback_max_penalty)

    def test_approvals_and_rejections_net_off(self, db_session, current_user, detected):
        _approve(db_session, detected, times=2)
        _reject(db_session, detected, times=1)

        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "alex@northwind.com"
        )

        # 2 * 0.05 - 1 * 0.10 == 0
        assert found.delta == pytest.approx(0.0)

    def test_the_address_prior_replaces_the_domain_prior(
        self, db_session, current_user
    ):
        """Summing would credit one judgement twice — every signal writes both."""
        db_session.add_all(
            [
                ClassifierPrior(
                    user_id=current_user.id,
                    scope=PriorScope.ADDRESS,
                    value="alex@northwind.com",
                    approvals=2,
                ),
                ClassifierPrior(
                    user_id=current_user.id,
                    scope=PriorScope.DOMAIN,
                    value="northwind.com",
                    rejections=3,
                ),
            ]
        )
        db_session.commit()

        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "alex@northwind.com"
        )

        # The address's +0.10, not the domain's -0.30, and not their sum.
        assert found.delta == pytest.approx(0.10)

    def test_the_domain_prior_applies_to_a_new_colleague(
        self, db_session, current_user
    ):
        """A whole sending platform you keep rejecting is evidence about the next one."""
        db_session.add(
            ClassifierPrior(
                user_id=current_user.id,
                scope=PriorScope.DOMAIN,
                value="northwind.com",
                rejections=2,
            )
        )
        db_session.commit()

        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "someone-else@northwind.com"
        )

        assert found.delta == pytest.approx(-0.20)

    def test_priors_are_per_user(self, db_session, current_user, detected):
        """One candidate's opinion of an agency is not evidence about anyone else's mail."""
        _reject(db_session, detected, times=3)

        other = classifier_feedback.adjustment_for(
            db_session, current_user.id + 999, "alex@northwind.com"
        )

        assert other.is_zero

    def test_feedback_off_reads_as_no_history(
        self, db_session, current_user, detected, monkeypatch
    ):
        _reject(db_session, detected, times=3)
        monkeypatch.setattr(settings, "recruiter_feedback_enabled", False)

        found = classifier_feedback.adjustment_for(
            db_session, current_user.id, "alex@northwind.com"
        )

        assert found.is_zero

    @pytest.mark.parametrize(
        ("confidence", "delta", "expected"),
        [
            (0.8, 0.1, 0.9),
            (0.8, -0.3, 0.5),
            (0.95, 0.1, 1.0),   # clamped at the top of the classifier's range
            (0.05, -0.3, 0.0),  # and at the bottom
        ],
    )
    def test_adjusted_confidence_stays_in_range(self, confidence, delta, expected):
        adjustment = classifier_feedback.Adjustment(delta=delta)

        assert classifier_feedback.adjusted_confidence(
            confidence, adjustment
        ) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# The safety clamp                                                             #
# --------------------------------------------------------------------------- #


class TestTheAutoClamp:
    """Feedback may always make the product quieter. Never louder past the bar."""

    def _decide(self, confidence, delta, score=100.0):
        return recruiter_reply_service._route_with_feedback(
            RecruiterEmailKind.RECRUITER_OUTREACH,
            confidence,
            classifier_feedback.Adjustment(delta=delta, reason="you approved before"),
            score,
            has_profile=True,
            location_ok=True,
            profile_label="Backend",
            auto_enabled=True,
        )

    def test_a_boost_can_never_promote_a_draft_to_auto(self):
        """The whole reason the arithmetic above is safe to run.

        Raw 0.6 x 100 = 60, under the 65 bar. Boosted to 0.7 it would clear it —
        and must not, because the user approving those drafts was agreeing that
        the *classification* was right, not that unread sends were welcome.
        """
        decision = self._decide(confidence=0.6, delta=0.10)

        assert decision.route is ReplyRoute.DRAFT

    def test_a_message_already_over_the_bar_still_auto_replies(self):
        """The clamp holds back promotions; it does not veto the auto band."""
        decision = self._decide(confidence=0.9, delta=0.05)

        assert decision.route is ReplyRoute.AUTO

    def test_a_penalty_may_demote_an_auto_to_a_draft(self):
        """Downward is unrestricted — that is the direction that costs nothing."""
        decision = self._decide(confidence=0.7, delta=-0.30)

        assert decision.route is ReplyRoute.DRAFT

    def test_no_adjustment_leaves_the_decision_untouched(self):
        decision = self._decide(confidence=0.9, delta=0.0)

        assert decision.route is ReplyRoute.AUTO
        assert "approved before" not in decision.reason


# --------------------------------------------------------------------------- #
# The pipeline, end to end                                                     #
# --------------------------------------------------------------------------- #


class TestThePipelineReadsHistory:
    def _detect(self, db, user, account, mailbox, message_id="m1"):
        mailbox[message_id] = gmail_message(message_id)
        _, created = recruiter_reply_service.record_scan(db, user, account)
        db.commit()
        return created[0]

    def test_the_adjustment_is_recorded_beside_the_classifier_reading(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles, detected
    ):
        """Both numbers, so the audit trail still shows what the model thought."""
        _reject(db_session, detected, times=2)

        row = self._detect(db_session, current_user, connected_gmail, stub_gmail)
        recruiter_reply_service.process(db_session, row)
        db_session.commit()

        assert row.confidence_adjustment == pytest.approx(-0.20)
        # The classifier's own reading is untouched by what the user did.
        assert row.classification_confidence > 0

    def test_no_history_leaves_the_adjustment_at_zero(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        row = self._detect(db_session, current_user, connected_gmail, stub_gmail)
        recruiter_reply_service.process(db_session, row)
        db_session.commit()

        assert row.confidence_adjustment == 0.0


# --------------------------------------------------------------------------- #
# The endpoints that produce the signal                                        #
# --------------------------------------------------------------------------- #


class TestTheEndpoints:
    def _drafted(self, db, user, account, mailbox, profiles):
        mailbox["m1"] = gmail_message("m1")
        _, created = recruiter_reply_service.record_scan(db, user, account)
        db.commit()
        row = created[0]
        recruiter_reply_service.process(db, row)
        db.commit()
        return row

    def test_approving_a_reply_records_an_approval(
        self, auth_client, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        row = self._drafted(
            db_session, current_user, connected_gmail, stub_gmail, profiles
        )

        resp = auth_client.post(f"/api/v1/review/emails/{row.reply_email_id}/approve")

        assert resp.status_code == 200
        feedback = db_session.query(ReplyFeedback).one()
        assert feedback.signal is FeedbackSignal.APPROVED
        assert feedback.source == "review_approve"

    def test_discarding_a_draft_records_a_rejection(
        self, auth_client, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        row = self._drafted(
            db_session, current_user, connected_gmail, stub_gmail, profiles
        )

        resp = auth_client.post(f"/api/v1/review/emails/{row.reply_email_id}/dismiss")

        assert resp.status_code == 204
        feedback = db_session.query(ReplyFeedback).one()
        assert feedback.signal is FeedbackSignal.REJECTED
        assert feedback.source == "review_dismiss"

    def test_dismissing_a_message_records_a_rejection(
        self, auth_client, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        row = self._drafted(
            db_session, current_user, connected_gmail, stub_gmail, profiles
        )

        resp = auth_client.post(f"/api/v1/recruiter-inbox/{row.id}/dismiss")

        assert resp.status_code == 204
        feedback = db_session.query(ReplyFeedback).one()
        assert feedback.signal is FeedbackSignal.REJECTED
        assert feedback.source == "inbox_dismiss"

    def test_approving_cold_outreach_records_nothing(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        """Approving a cold email says nothing about whether a stranger was a recruiter."""
        from app.models.application import Application
        from app.models.campaign import Campaign
        from app.models.email import EmailDirection
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
            status=EmailStatus.DRAFT,
            to_address="alex@northwind.com",
            subject="Hello",
            body_text="Hi",
        )
        db_session.add(email)
        db_session.commit()

        resp = auth_client.post(f"/api/v1/review/emails/{email.id}/approve")

        assert resp.status_code == 200
        assert db_session.query(ReplyFeedback).count() == 0

    def test_a_broken_feedback_write_does_not_break_the_approval(
        self,
        auth_client,
        db_session,
        current_user,
        connected_gmail,
        stub_gmail,
        profiles,
        monkeypatch,
    ):
        """A missed lesson is much cheaper than a reply that doesn't go out."""
        row = self._drafted(
            db_session, current_user, connected_gmail, stub_gmail, profiles
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("prior table on fire")

        monkeypatch.setattr(classifier_feedback, "record_for_email", _boom)

        resp = auth_client.post(f"/api/v1/review/emails/{row.reply_email_id}/approve")

        assert resp.status_code == 200
        assert (
            db_session.get(Email, row.reply_email_id).status is EmailStatus.QUEUED
        )
