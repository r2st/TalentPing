"""A recruiter we already answered, writing again.

The bug: a recruiter who starts a *new* thread rather than hitting reply flows
through the whole inbound pipeline a second time as though it were first contact
— classified, matched, routed, and eligible to be auto-replied to again. That is
extremely common when the outreach came from a platform (Gem, Loxo, Bullhorn),
where every send is its own thread.

The same-thread case was already safe by accident: once we answer, the thread has
an ``EmailThread`` row, so the scanner skips it and ``poll_thread`` — which drafts
and never auto-sends — owns it. What was missing there is *visibility*: the row
still read REPLIED and nothing told the candidate their recruiter had come back.

Both cases end in the same place: escalated to the user, on a flag that sits
alongside ``status`` rather than replacing it. A message that was correctly
REPLIED last week is still correctly REPLIED; what changed is that it now wants a
human.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.recruiter_email import (
    RecruiterEmail,
    RecruiterEmailKind,
    RecruiterEmailStatus,
    ReplyRoute,
)
from app.services import recruiter_follow_up, recruiter_reply_service
from tests.test_recruiter_reply import gmail_message


def make_row(
    db,
    user,
    account,
    *,
    message_id="m-prior",
    address="alex@northwind.com",
    reply_to=None,
    status=RecruiterEmailStatus.REPLIED,
    received_at=None,
) -> RecruiterEmail:
    row = RecruiterEmail(
        user_id=user.id,
        gmail_account_id=account.id,
        gmail_message_id=message_id,
        gmail_thread_id=f"t-{message_id}",
        from_address=address,
        reply_to_address=reply_to,
        subject="Senior Backend Engineer",
        body_text="Are you open to a chat?",
        kind=RecruiterEmailKind.RECRUITER_OUTREACH,
        classification_confidence=0.9,
        route=ReplyRoute.DRAFT,
        status=status,
        received_at=received_at or (datetime.now(UTC) - timedelta(days=3)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Finding the earlier conversation                                             #
# --------------------------------------------------------------------------- #


class TestFindingPriorEngagement:
    def test_a_second_message_from_an_answered_sender_is_found(
        self, db_session, current_user, connected_gmail
    ):
        prior = make_row(db_session, current_user, connected_gmail)
        follow_up = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-follow",
            status=RecruiterEmailStatus.DETECTED,
        )

        found = recruiter_follow_up.find_prior_engagement(db_session, follow_up)

        assert found is not None
        assert found.id == prior.id

    def test_a_first_message_from_a_stranger_finds_nothing(
        self, db_session, current_user, connected_gmail
    ):
        row = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-first",
            status=RecruiterEmailStatus.DETECTED,
        )

        assert recruiter_follow_up.find_prior_engagement(db_session, row) is None

    @pytest.mark.parametrize(
        "status",
        [
            RecruiterEmailStatus.DRAFTED,
            RecruiterEmailStatus.FLAGGED,
            RecruiterEmailStatus.CLASSIFIED,
            RecruiterEmailStatus.IGNORED,
        ],
    )
    def test_a_message_we_never_actually_answered_does_not_count(
        self, db_session, current_user, connected_gmail, status
    ):
        """A draft nobody approved is not a conversation.

        Treating it as one would escalate a recruiter's second message because we
        once wrote something that was never sent.
        """
        make_row(db_session, current_user, connected_gmail, status=status)
        follow_up = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-follow",
            status=RecruiterEmailStatus.DETECTED,
        )

        assert recruiter_follow_up.find_prior_engagement(db_session, follow_up) is None

    @pytest.mark.parametrize(
        "status",
        [RecruiterEmailStatus.REPLIED, RecruiterEmailStatus.REPLY_QUEUED],
    )
    def test_a_queued_reply_counts_as_engagement(
        self, db_session, current_user, connected_gmail, status
    ):
        """A reply on the sender's desk in sixty seconds is a reply."""
        make_row(db_session, current_user, connected_gmail, status=status)
        follow_up = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-follow",
            status=RecruiterEmailStatus.DETECTED,
        )

        assert recruiter_follow_up.find_prior_engagement(db_session, follow_up)

    def test_it_keys_on_the_reply_address_not_the_from(
        self, db_session, current_user, connected_gmail
    ):
        """Platform outreach comes from noreply@ and names the human in Reply-To.

        Keying on the From would make every recruiter on the same platform one
        person — so answering one would escalate everybody else's first message.
        """
        make_row(
            db_session,
            current_user,
            connected_gmail,
            address="noreply@gem.com",
            reply_to="alex@northwind.com",
        )
        # A *different* recruiter, same platform. Must not match.
        other = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-other",
            address="noreply@gem.com",
            reply_to="sam@initech.com",
            status=RecruiterEmailStatus.DETECTED,
        )
        # The same recruiter. Must match.
        same = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-same",
            address="noreply@gem.com",
            reply_to="alex@northwind.com",
            status=RecruiterEmailStatus.DETECTED,
        )

        assert recruiter_follow_up.find_prior_engagement(db_session, other) is None
        assert recruiter_follow_up.find_prior_engagement(db_session, same) is not None

    def test_a_row_is_never_its_own_predecessor(
        self, db_session, current_user, connected_gmail
    ):
        """A replayed task must not escalate a message against itself."""
        row = make_row(db_session, current_user, connected_gmail)

        assert recruiter_follow_up.find_prior_engagement(db_session, row) is None

    def test_another_users_conversation_is_invisible(
        self, db_session, current_user, connected_gmail
    ):
        from app.models.user import User

        other_user = User(
            email="other@example.com", hashed_password="x", full_name="Other"
        )
        db_session.add(other_user)
        db_session.commit()

        theirs = RecruiterEmail(
            user_id=other_user.id,
            gmail_message_id="m-theirs",
            from_address="alex@northwind.com",
            kind=RecruiterEmailKind.RECRUITER_OUTREACH,
            status=RecruiterEmailStatus.REPLIED,
        )
        db_session.add(theirs)
        db_session.commit()

        mine = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-mine",
            status=RecruiterEmailStatus.DETECTED,
        )

        assert recruiter_follow_up.find_prior_engagement(db_session, mine) is None


# --------------------------------------------------------------------------- #
# Escalating                                                                   #
# --------------------------------------------------------------------------- #


class TestEscalating:
    def test_the_follow_up_is_flagged_and_the_prior_is_counted(
        self, db_session, current_user, connected_gmail
    ):
        prior = make_row(db_session, current_user, connected_gmail)
        follow_up = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-follow",
            status=RecruiterEmailStatus.DETECTED,
        )

        reason = recruiter_follow_up.escalate(db_session, follow_up, prior)
        db_session.commit()

        assert follow_up.escalated is True
        assert follow_up.previous_recruiter_email_id == prior.id
        assert "alex@northwind.com" in reason
        assert prior.follow_up_count == 1
        assert prior.last_follow_up_at is not None

    def test_the_reason_names_the_date_of_the_earlier_reply(
        self, db_session, current_user, connected_gmail
    ):
        prior = make_row(
            db_session,
            current_user,
            connected_gmail,
            received_at=datetime(2026, 7, 12, tzinfo=UTC),
        )
        follow_up = make_row(
            db_session,
            current_user,
            connected_gmail,
            message_id="m-follow",
            status=RecruiterEmailStatus.DETECTED,
        )

        reason = recruiter_follow_up.escalate(db_session, follow_up, prior)

        assert "12 July" in reason


# --------------------------------------------------------------------------- #
# In the pipeline                                                              #
# --------------------------------------------------------------------------- #


class TestThePipelineNeverAnswersTwice:
    def _detect(self, db, user, account, mailbox, message_id):
        mailbox[message_id] = gmail_message(
            message_id, thread_id=f"t-{message_id}"
        )
        _, created = recruiter_reply_service.record_scan(db, user, account)
        db.commit()
        return [row for row in created if row.gmail_message_id == message_id][0]

    def test_a_new_thread_from_an_answered_recruiter_escalates(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        """The real bug: a fresh thread was first contact all over again."""
        make_row(db_session, current_user, connected_gmail)

        row = self._detect(
            db_session, current_user, connected_gmail, stub_gmail, "m-follow"
        )
        outcome = recruiter_reply_service.process(db_session, row)
        db_session.commit()

        assert outcome["status"] == "flagged_follow_up"
        assert row.escalated is True
        assert row.route is ReplyRoute.FLAG
        assert row.status is RecruiterEmailStatus.FLAGGED
        # And critically: nothing was written to send.
        assert row.reply_email_id is None

    def test_a_first_message_is_answered_as_normal(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        row = self._detect(
            db_session, current_user, connected_gmail, stub_gmail, "m-first"
        )
        recruiter_reply_service.process(db_session, row)
        db_session.commit()

        assert row.escalated is False
        assert row.status is RecruiterEmailStatus.DRAFTED

    def test_a_prior_we_only_flagged_does_not_escalate_the_next_one(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        """We never spoke, so there is nothing for them to be following up on."""
        make_row(
            db_session,
            current_user,
            connected_gmail,
            status=RecruiterEmailStatus.FLAGGED,
        )

        row = self._detect(
            db_session, current_user, connected_gmail, stub_gmail, "m-second"
        )
        recruiter_reply_service.process(db_session, row)
        db_session.commit()

        assert row.escalated is False
        assert row.status is RecruiterEmailStatus.DRAFTED


class TestSameThreadFollowUps:
    def test_a_reply_on_our_thread_escalates_the_inbound_row(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        """poll_thread already drafts rather than sends; this makes it *visible*."""
        stub_gmail["m1"] = gmail_message("m1")
        _, created = recruiter_reply_service.record_scan(
            db_session, current_user, connected_gmail
        )
        db_session.commit()
        row = created[0]
        recruiter_reply_service.process(db_session, row)
        db_session.commit()
        assert row.application_id is not None

        recruiter_follow_up.note_thread_follow_up(db_session, row.application_id)
        db_session.commit()

        assert row.escalated is True
        assert row.follow_up_count == 1
        assert "replied to the answer we sent" in row.escalation_reason

    def test_the_audit_trail_of_the_original_decision_is_untouched(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        """kind/route/status record a decision made at a moment in time."""
        stub_gmail["m1"] = gmail_message("m1")
        _, created = recruiter_reply_service.record_scan(
            db_session, current_user, connected_gmail
        )
        db_session.commit()
        row = created[0]
        recruiter_reply_service.process(db_session, row)
        db_session.commit()
        before = (row.kind, row.route, row.status)

        recruiter_follow_up.note_thread_follow_up(db_session, row.application_id)
        db_session.commit()

        assert (row.kind, row.route, row.status) == before

    def test_repeated_follow_ups_accumulate(
        self, db_session, current_user, connected_gmail, stub_gmail, profiles
    ):
        stub_gmail["m1"] = gmail_message("m1")
        _, created = recruiter_reply_service.record_scan(
            db_session, current_user, connected_gmail
        )
        db_session.commit()
        row = created[0]
        recruiter_reply_service.process(db_session, row)
        db_session.commit()

        recruiter_follow_up.note_thread_follow_up(db_session, row.application_id)
        recruiter_follow_up.note_thread_follow_up(db_session, row.application_id)
        db_session.commit()

        assert row.follow_up_count == 2

    def test_a_cold_outreach_thread_escalates_nothing(self, db_session):
        """Most threads have no RecruiterEmail behind them, and that is fine."""
        assert recruiter_follow_up.note_thread_follow_up(db_session, 4242) is None
        assert recruiter_follow_up.note_thread_follow_up(db_session, None) is None


# --------------------------------------------------------------------------- #
# What the user sees                                                           #
# --------------------------------------------------------------------------- #


class TestTheInboxSurfacesEscalations:
    def test_an_escalated_row_appears_in_needs_you(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        """Even though its status still reads REPLIED — which is the whole point."""
        row = make_row(db_session, current_user, connected_gmail)
        row.escalated = True
        row.escalation_reason = "They wrote again."
        db_session.commit()

        body = auth_client.get(
            "/api/v1/recruiter-inbox", params={"view": "needs_you"}
        ).json()

        assert [e["id"] for e in body["emails"]] == [row.id]
        assert body["counts"]["needs_you"] == 1
        assert body["counts"]["escalated"] == 1

    def test_the_row_carries_its_reason(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        row = make_row(db_session, current_user, connected_gmail)
        row.escalated = True
        row.escalation_reason = "They wrote again."
        row.follow_up_count = 2
        db_session.commit()

        body = auth_client.get(f"/api/v1/recruiter-inbox/{row.id}").json()

        assert body["escalated"] is True
        assert body["escalation_reason"] == "They wrote again."
        assert body["follow_up_count"] == 2

    def test_an_ordinary_replied_row_is_not_in_needs_you(
        self, auth_client, db_session, current_user, connected_gmail
    ):
        make_row(db_session, current_user, connected_gmail)

        body = auth_client.get(
            "/api/v1/recruiter-inbox", params={"view": "needs_you"}
        ).json()

        assert body["emails"] == []
        assert body["counts"]["needs_you"] == 0
