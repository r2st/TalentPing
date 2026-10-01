""""Where to focus" — the ranked tables, said out loud.

No new data is computed here, so the tests are almost entirely about restraint:
the feature's only real decision is when to say nothing, and a confident
sentence drawn from four applications is worse than an empty list.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.application import ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_event import EmailEvent, EmailEventType
from app.models.email_thread import EmailThread
from app.models.resume import Resume
from app.routers.analytics import MIN_FOCUS_SAMPLE, MIN_FOCUS_TOTAL
from tests.test_analytics import _make_application

OVERVIEW = "/api/v1/analytics/overview"


@pytest.fixture()
def campaign(db_session, current_user, resume) -> Campaign:
    row = Campaign(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend outreach",
        target_industries=["fintech"],
    )
    db_session.add(row)
    db_session.commit()
    return row


def _spread(db, user, campaign, *, industry, replies, total, prefix):
    """*total* applications in *industry*, of which *replies* wrote back."""
    for index in range(total):
        _make_application(
            db,
            user,
            campaign,
            company=f"{prefix} {index}",
            status=(
                ApplicationStatus.REPLIED
                if index < replies
                else ApplicationStatus.NO_RESPONSE
            ),
            industry=industry,
        )


def _focus(auth_client, **params) -> list[dict]:
    return auth_client.get(OVERVIEW, params=params).json()["focus"]


class TestSilence:
    def test_says_how_far_off_it_is_before_the_volume_floor(
        self, auth_client, db_session, current_user, campaign
    ):
        _spread(
            db_session, current_user, campaign,
            industry="fintech", replies=2, total=4, prefix="Small",
        )
        db_session.commit()

        notes = _focus(auth_client)
        assert [n["kind"] for n in notes] == ["volume"]
        assert str(MIN_FOCUS_TOTAL) in notes[0]["text"]

    def test_offers_nothing_when_segments_perform_alike(
        self, auth_client, db_session, current_user, campaign
    ):
        """"They're about the same" needs no bullet point."""
        _spread(
            db_session, current_user, campaign,
            industry="fintech", replies=3, total=10, prefix="Fin",
        )
        _spread(
            db_session, current_user, campaign,
            industry="healthcare", replies=3, total=10, prefix="Health",
        )
        db_session.commit()

        assert _focus(auth_client) == []

    def test_will_not_compare_across_a_thin_segment(
        self, auth_client, db_session, current_user, campaign
    ):
        """One perfect segment of four sends is not advice, however big the gap."""
        _spread(
            db_session, current_user, campaign,
            industry="fintech", replies=MIN_FOCUS_SAMPLE - 1, total=MIN_FOCUS_SAMPLE - 1,
            prefix="Fin",
        )
        _spread(
            db_session, current_user, campaign,
            industry="logistics", replies=0, total=10, prefix="Log",
        )
        db_session.commit()

        assert not [n for n in _focus(auth_client) if n["kind"] == "industry"]

    def test_an_empty_account_gets_no_advice_and_no_error(self, auth_client, resume):
        body = auth_client.get(OVERVIEW).json()
        assert body["focus"] == []


class TestSegmentComparison:
    def test_states_the_multiple_and_both_sample_sizes(
        self, auth_client, db_session, current_user, campaign
    ):
        _spread(
            db_session, current_user, campaign,
            industry="fintech", replies=6, total=12, prefix="Fin",
        )
        _spread(
            db_session, current_user, campaign,
            industry="enterprise", replies=2, total=20, prefix="Ent",
        )
        db_session.commit()

        note = next(n for n in _focus(auth_client) if n["kind"] == "industry")
        assert note["subject"] == "fintech"
        assert "5×" in note["text"]          # 50% against 10%
        assert "6 of 12" in note["text"]
        assert "2 of 20" in note["text"]
        assert note["sample"] == 32

    def test_avoids_dividing_by_a_segment_that_never_replies(
        self, auth_client, db_session, current_user, campaign
    ):
        _spread(
            db_session, current_user, campaign,
            industry="fintech", replies=5, total=10, prefix="Fin",
        )
        _spread(
            db_session, current_user, campaign,
            industry="mining", replies=0, total=8, prefix="Mine",
        )
        db_session.commit()

        note = next(n for n in _focus(auth_client) if n["kind"] == "industry")
        assert "none of 8" in note["text"]
        assert "×" not in note["text"]

    def test_compares_companies_as_well_as_industries(
        self, auth_client, db_session, current_user, campaign
    ):
        for index in range(8):
            _make_application(
                db_session, current_user, campaign,
                company="Good Corp",
                status=(
                    ApplicationStatus.REPLIED if index < 5 else ApplicationStatus.NO_RESPONSE
                ),
                industry="fintech",
            )
        for _ in range(8):
            _make_application(
                db_session, current_user, campaign,
                company="Quiet Corp",
                status=ApplicationStatus.NO_RESPONSE,
                industry="fintech",
            )
        db_session.commit()

        note = next(n for n in _focus(auth_client) if n["kind"] == "company")
        assert note["subject"] == "Good Corp"

    def test_reads_the_worst_segment_even_when_the_table_is_truncated(
        self, auth_client, db_session, current_user, campaign
    ):
        """The bottom of the ranking is what ``limit`` cuts off, and it is half
        of every comparison worth making."""
        for slot in range(4):
            _spread(
                db_session, current_user, campaign,
                industry=f"sector-{slot}", replies=3, total=6, prefix=f"S{slot}",
            )
        _spread(
            db_session, current_user, campaign,
            industry="dead-sector", replies=0, total=9, prefix="Dead",
        )
        db_session.commit()

        note = next(
            n for n in _focus(auth_client, limit=1) if n["kind"] == "industry"
        )
        assert "dead-sector" in note["text"]


class TestResumeAndEngagement:
    def test_names_the_resume_that_is_pulling_replies(
        self, auth_client, db_session, current_user, resume, campaign
    ):
        other = Resume(
            user_id=current_user.id,
            filename="v2.pdf",
            raw_text="Second version.",
            parsed_with="heuristic",
        )
        db_session.add(other)
        db_session.flush()
        second = Campaign(user_id=current_user.id, resume_id=other.id, name="V2 run")
        db_session.add(second)
        db_session.flush()

        for index in range(8):
            _make_application(
                db_session, current_user, campaign,
                company=f"A{index}",
                status=(
                    ApplicationStatus.REPLIED if index < 5 else ApplicationStatus.NO_RESPONSE
                ),
                industry="fintech",
            )
        for index in range(8):
            _make_application(
                db_session, current_user, second,
                company=f"B{index}",
                status=ApplicationStatus.NO_RESPONSE,
                industry="fintech",
            )
        db_session.commit()

        note = next(n for n in _focus(auth_client) if n["kind"] == "resume")
        assert note["subject"] == resume.display_label
        assert "send the first one more" in note["text"]

    def test_blames_the_body_when_mail_is_opened_and_unanswered(
        self, auth_client, db_session, current_user, campaign
    ):
        """A 0% reply rate at a 60% open rate is a different problem from a 0%
        reply rate at a 5% open rate, with the opposite fix."""
        _seed_tracked(db_session, current_user, campaign, tracked=10, opened=6)
        db_session.commit()

        note = next(n for n in _focus(auth_client) if n["kind"] == "engagement")
        assert note["subject"] == "message body"
        assert "6 of 10" in note["text"]

    def test_blames_the_subject_when_nothing_is_opened(
        self, auth_client, db_session, current_user, campaign
    ):
        _seed_tracked(db_session, current_user, campaign, tracked=12, opened=1)
        db_session.commit()

        note = next(n for n in _focus(auth_client) if n["kind"] == "engagement")
        assert note["subject"] == "subject line"

    def test_offers_at_most_three_sentences(
        self, auth_client, db_session, current_user, campaign
    ):
        _spread(
            db_session, current_user, campaign,
            industry="fintech", replies=6, total=8, prefix="Fin",
        )
        _spread(
            db_session, current_user, campaign,
            industry="mining", replies=0, total=8, prefix="Mine",
        )
        _seed_tracked(db_session, current_user, campaign, tracked=10, opened=0)
        db_session.commit()

        assert len(_focus(auth_client)) <= 3


def _seed_tracked(db, user, campaign, *, tracked: int, opened: int):
    """*tracked* sent emails carrying a pixel, *opened* of which registered."""
    for index in range(tracked):
        application = _make_application(
            db, user, campaign,
            company=f"Tracked {index}",
            status=ApplicationStatus.NO_RESPONSE,
            industry="fintech",
        )
        thread = EmailThread(application_id=application.id, subject="Hello")
        db.add(thread)
        db.flush()
        email = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.SENT,
            to_address="talent@example.com",
            subject="Hello",
            body_text="Outreach.",
            tracking_token=f"tok-{application.id}",
        )
        db.add(email)
        db.flush()
        if index < opened:
            db.add(
                EmailEvent(
                    user_id=user.id,
                    email_id=email.id,
                    event_type=EmailEventType.OPEN,
                    is_prefetch=False,
                    occurred_at=datetime.now(UTC),
                )
            )
