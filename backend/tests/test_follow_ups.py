"""Follow-up automation: scheduling, timing, template choice, and stop-on-reply."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_thread import EmailThread
from app.models.follow_up import FollowUp, FollowUpStatus, FollowUpTemplate
from app.models.recruiter import Recruiter
from app.services import follow_up_service as svc
from app.services import send_time
from app.services.ai_composer import CandidateContext, RecruiterContext


@pytest.fixture()
def outreach(db_session, current_user, resume):
    """A campaign with one application, one thread, and one sent outreach email."""
    campaign = Campaign(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Test campaign",
        target_companies=["Northwind"],
        follow_up_enabled=True,
        follow_up_count=2,
        follow_up_interval_days=4,
        follow_up_stop_on_reply=True,
    )
    recruiter = Recruiter(
        user_id=current_user.id,
        email="talent@northwind.example",
        name="Sam Recruiter",
        company="Northwind Labs",
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

    thread = EmailThread(application_id=application.id, subject="Jordan Candidate — Backend")
    db_session.add(thread)
    db_session.flush()
    db_session.add(
        Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.SENT,
            to_address=recruiter.email,
            subject=thread.subject,
            body_text="Hello there.",
            sent_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    return {
        "campaign": campaign,
        "application": application,
        "recruiter": recruiter,
        "thread": thread,
    }


class TestOptimalSendTime:
    """Timing now delegates to send_time; jitter is off so the slot is exact."""

    def test_lands_on_a_weekday_business_morning(self):
        # A Saturday afternoon.
        saturday = datetime(2026, 7, 25, 15, 0, tzinfo=UTC)
        slot = send_time.next_slot(saturday, jitter=False)

        assert slot.weekday() in (0, 1, 2, 3, 4)
        assert 9 <= slot.hour < 11

    def test_never_moves_a_time_backwards(self):
        friday = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        assert svc.optimal_send_time(friday) > friday

    def test_keeps_a_time_already_in_the_window(self):
        tuesday = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)  # a Tuesday
        assert send_time.next_slot(tuesday, jitter=False) == tuesday

    def test_moves_an_early_morning_to_the_window_start(self):
        tuesday_dawn = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)
        assert send_time.next_slot(tuesday_dawn, jitter=False).hour == 9


class TestSequenceOffsets:
    """The cadence the brief asks for: a nudge on day 3, a final touch on day 7."""

    def test_the_default_sequence_is_day_three_and_day_seven(self, outreach):
        assert svc.sequence_offsets(outreach["campaign"]) == [3, 7]

    def test_a_campaign_can_override_the_offsets(self, db_session, outreach):
        outreach["campaign"].follow_up_step_days = [2, 5]
        outreach["campaign"].follow_up_count = 2
        db_session.commit()
        assert svc.sequence_offsets(outreach["campaign"]) == [2, 5]

    def test_the_count_caps_the_sequence(self, db_session, outreach):
        outreach["campaign"].follow_up_count = 1
        db_session.commit()
        assert svc.sequence_offsets(outreach["campaign"]) == [3]

    def test_offsets_are_sorted_and_deduplicated(self, db_session, outreach):
        """Two steps landing on the same day would be one email sent twice."""
        outreach["campaign"].follow_up_step_days = [7, 3, 7, 0, -2]
        outreach["campaign"].follow_up_count = 9
        db_session.commit()
        assert svc.sequence_offsets(outreach["campaign"]) == [3, 7]

    def test_a_malformed_setting_falls_back(self):
        assert svc.parse_step_days("nonsense") == [3, 7]
        assert svc.parse_step_days("") == [3, 7]
        assert svc.parse_step_days("3, 7 ,14") == [3, 7, 14]
        assert svc.parse_step_days("0,-1") == [3, 7]


class TestScheduling:
    def test_the_steps_land_on_day_three_and_day_seven(self, db_session, outreach):
        sent_at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)  # Tuesday morning
        created = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"], sent_at=sent_at
        )
        db_session.commit()

        # Read before the commit expires them — SQLite drops tzinfo on round-trip.
        first, second = (f.scheduled_at.replace(tzinfo=UTC) for f in created)
        # Day 3 is a Friday; day 7 is the following Tuesday. Both are already
        # weekdays, so the business-hours move doesn't shift the day.
        assert (first.date() - sent_at.date()).days == 3
        assert (second.date() - sent_at.date()).days == 7

    def test_a_step_landing_on_a_weekend_moves_to_the_next_weekday(
        self, db_session, outreach
    ):
        sent_at = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)  # Wednesday
        created = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"], sent_at=sent_at
        )
        db_session.commit()

        first = created[0].scheduled_at.replace(tzinfo=UTC)
        assert first.weekday() in send_time.WEEKDAYS  # day 3 would be Saturday

    def test_every_step_falls_inside_the_business_window(self, db_session, outreach):
        created = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"]
        )
        db_session.commit()
        for follow_up in created:
            due = follow_up.scheduled_at.replace(tzinfo=UTC)
            assert due.weekday() in send_time.WEEKDAYS
            assert 9 <= due.hour < 11

    def test_creates_one_row_per_step(self, db_session, outreach):
        created = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"]
        )
        db_session.commit()

        assert len(created) == 2
        assert [f.step for f in created] == [1, 2]
        assert all(f.status == FollowUpStatus.SCHEDULED for f in created)

    def test_intervals_widen_across_the_sequence(self, db_session, outreach):
        """Day 3 then day 7: the gap grows, so chasing doesn't read as a machine."""
        sent_at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)  # a Tuesday morning
        created = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"], sent_at=sent_at
        )
        db_session.commit()

        # Read the values before the commit expires them — SQLite drops tzinfo
        # on round-trip, and these are tz-aware in memory.
        first, second = (f.scheduled_at.replace(tzinfo=UTC) for f in created)
        assert (second - first) > (first - sent_at)

    def test_the_last_step_uses_the_final_template(self, db_session, outreach):
        created = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"]
        )
        assert created[-1].template == FollowUpTemplate.FINAL

    def test_is_idempotent(self, db_session, outreach):
        """A retried send must not double the sequence."""
        svc.schedule_for_application(db_session, outreach["application"], outreach["campaign"])
        db_session.commit()
        again = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"]
        )
        db_session.commit()

        assert again == []
        assert db_session.query(FollowUp).count() == 2

    def test_disabled_campaigns_schedule_nothing(self, db_session, outreach):
        outreach["campaign"].follow_up_enabled = False
        db_session.commit()
        assert (
            svc.schedule_for_application(
                db_session, outreach["application"], outreach["campaign"]
            )
            == []
        )

    def test_zero_count_schedules_nothing(self, db_session, outreach):
        outreach["campaign"].follow_up_count = 0
        db_session.commit()
        assert (
            svc.schedule_for_application(
                db_session, outreach["application"], outreach["campaign"]
            )
            == []
        )

    def test_count_cannot_invent_steps_the_sequence_does_not_have(
        self, db_session, outreach
    ):
        """A high count is bounded by the offset list, not padded out to it."""
        outreach["campaign"].follow_up_count = 99
        db_session.commit()
        created = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"]
        )
        assert len(created) == 2  # the default sequence is day 3 and day 7

    def test_a_long_explicit_sequence_is_capped_at_five(self, db_session, outreach):
        outreach["campaign"].follow_up_count = 99
        outreach["campaign"].follow_up_step_days = [2, 4, 6, 8, 10, 12, 14]
        db_session.commit()
        created = svc.schedule_for_application(
            db_session, outreach["application"], outreach["campaign"]
        )
        assert len(created) == 5


class TestCancellation:
    def test_cancels_pending_steps_with_a_reason(self, db_session, outreach):
        svc.schedule_for_application(db_session, outreach["application"], outreach["campaign"])
        db_session.commit()

        cancelled = svc.cancel_for_application(
            db_session, outreach["application"].id, "recruiter replied"
        )
        db_session.commit()

        assert cancelled == 2
        rows = db_session.query(FollowUp).all()
        assert all(f.status == FollowUpStatus.CANCELLED for f in rows)
        assert all(f.note == "recruiter replied" for f in rows)

    def test_cancelling_keeps_the_history(self, db_session, outreach):
        """Cancellation is a status change, never a delete."""
        svc.schedule_for_application(db_session, outreach["application"], outreach["campaign"])
        db_session.commit()
        svc.cancel_for_application(db_session, outreach["application"].id, "replied")
        db_session.commit()
        assert db_session.query(FollowUp).count() == 2


class TestDueSweep:
    def test_finds_only_what_is_due(self, db_session, outreach):
        now = datetime.now(UTC)
        db_session.add_all(
            [
                FollowUp(
                    application_id=outreach["application"].id,
                    step=1,
                    scheduled_at=now - timedelta(hours=1),
                ),
                FollowUp(
                    application_id=outreach["application"].id,
                    step=2,
                    scheduled_at=now + timedelta(days=3),
                ),
            ]
        )
        db_session.commit()

        due = svc.due_follow_ups(db_session, now=now)
        assert len(due) == 1
        assert due[0].step == 1

    def test_ignores_already_actioned_rows(self, db_session, outreach):
        now = datetime.now(UTC)
        db_session.add(
            FollowUp(
                application_id=outreach["application"].id,
                step=1,
                scheduled_at=now - timedelta(days=1),
                status=FollowUpStatus.SENT,
            )
        )
        db_session.commit()
        assert svc.due_follow_ups(db_session, now=now) == []


class TestPrepareFollowUpEmail:
    def _due(self, db_session, outreach, **kwargs):
        follow_up = FollowUp(
            application_id=outreach["application"].id,
            step=kwargs.pop("step", 1),
            scheduled_at=datetime.now(UTC) - timedelta(hours=1),
            **kwargs,
        )
        db_session.add(follow_up)
        db_session.commit()
        return follow_up

    def test_queues_an_email_on_the_existing_thread(self, db_session, outreach):
        follow_up = self._due(db_session, outreach)
        email, skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()

        assert skip is None
        assert email is not None
        assert email.thread_id == outreach["thread"].id
        assert email.status == EmailStatus.QUEUED
        assert email.to_address == "talent@northwind.example"
        assert email.subject.startswith("Re: ")
        assert follow_up.status == FollowUpStatus.SENT
        assert follow_up.email_id == email.id

    def test_advances_the_application_to_follow_up(self, db_session, outreach):
        follow_up = self._due(db_session, outreach)
        svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()
        assert outreach["application"].status == ApplicationStatus.FOLLOW_UP

    def test_parks_a_draft_when_the_campaign_wants_review(self, db_session, outreach):
        outreach["campaign"].auto_send = False
        db_session.commit()

        follow_up = self._due(db_session, outreach)
        email, _skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()
        assert email.status == EmailStatus.DRAFT

    def test_stops_when_the_recruiter_replied(self, db_session, outreach):
        outreach["application"].status = ApplicationStatus.INTERESTED
        db_session.commit()

        follow_up = self._due(db_session, outreach)
        email, skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()

        assert email is None
        assert skip == "recruiter replied"
        assert follow_up.status == FollowUpStatus.CANCELLED

    def test_keeps_going_when_stop_on_reply_is_off(self, db_session, outreach):
        outreach["application"].status = ApplicationStatus.REPLIED
        outreach["campaign"].follow_up_stop_on_reply = False
        db_session.commit()

        follow_up = self._due(db_session, outreach)
        email, _skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()
        assert email is not None

    def _reply(self, db_session, outreach, intent=None):
        db_session.add(
            Email(
                thread_id=outreach["thread"].id,
                direction=EmailDirection.RECEIVED,
                status=EmailStatus.RECEIVED,
                from_address="talent@northwind.example",
                subject="Re: hello",
                body_text="Who is this? What role?",
                intent=intent,
                sent_at=datetime.now(UTC),
            )
        )
        db_session.commit()

    def test_an_inbound_email_stops_the_sequence_whatever_the_status_says(
        self, db_session, outreach
    ):
        """The defect this fixes: the intent map has no entry for QUESTION,
        OUT_OF_OFFICE or OTHER, so a recruiter who wrote back "Who is this?"
        left the application on OUTREACH_SENT and kept getting nudged."""
        self._reply(db_session, outreach, intent=ReplyIntent.QUESTION)
        assert outreach["application"].status == ApplicationStatus.OUTREACH_SENT

        follow_up = self._due(db_session, outreach)
        email, skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()

        assert email is None
        assert skip == "recruiter replied"
        assert follow_up.status == FollowUpStatus.CANCELLED

    @pytest.mark.parametrize(
        "intent",
        [ReplyIntent.OTHER, ReplyIntent.OUT_OF_OFFICE, ReplyIntent.QUESTION, None],
    )
    def test_every_unmapped_intent_still_stops_the_sequence(
        self, db_session, outreach, intent
    ):
        self._reply(db_session, outreach, intent=intent)
        follow_up = self._due(db_session, outreach)
        email, _skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()
        assert email is None

    def test_an_inbound_email_does_not_stop_it_when_stop_on_reply_is_off(
        self, db_session, outreach
    ):
        outreach["campaign"].follow_up_stop_on_reply = False
        db_session.commit()
        self._reply(db_session, outreach)

        follow_up = self._due(db_session, outreach)
        email, _skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()
        assert email is not None

    def test_our_own_outbound_mail_is_not_a_reply(self, db_session, outreach):
        """Mail we sent comes back on the thread; it must not cancel anything."""
        db_session.add(
            Email(
                thread_id=outreach["thread"].id,
                direction=EmailDirection.SENT,
                status=EmailStatus.SENT,
                subject="another one",
                body_text="b",
                sent_at=datetime.now(UTC),
            )
        )
        db_session.commit()

        follow_up = self._due(db_session, outreach)
        email, _skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()
        assert email is not None

    def test_thread_has_reply_is_scoped_to_the_application(
        self, db_session, outreach, current_user
    ):
        """Another application's reply must not silence this one's sequence."""
        other_recruiter = Recruiter(
            user_id=current_user.id, email="other@northwind.example", company="Other"
        )
        db_session.add(other_recruiter)
        db_session.flush()
        other_app = Application(
            user_id=current_user.id,
            campaign_id=outreach["campaign"].id,
            recruiter_id=other_recruiter.id,
            status=ApplicationStatus.OUTREACH_SENT,
        )
        db_session.add(other_app)
        db_session.flush()
        other_thread = EmailThread(application_id=other_app.id, subject="other")
        db_session.add(other_thread)
        db_session.flush()
        db_session.add(
            Email(
                thread_id=other_thread.id,
                direction=EmailDirection.RECEIVED,
                status=EmailStatus.RECEIVED,
                subject="Re: other",
                body_text="hi",
                sent_at=datetime.now(UTC),
            )
        )
        db_session.commit()

        assert svc.thread_has_reply(db_session, other_app.id) is True
        assert svc.thread_has_reply(db_session, outreach["application"].id) is False

    def test_never_contacts_an_opted_out_recruiter(self, db_session, outreach):
        """CAN-SPAM: an opt-out is absolute, whatever the sequence says."""
        outreach["recruiter"].opted_out = True
        db_session.commit()

        follow_up = self._due(db_session, outreach)
        email, skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()

        assert email is None
        assert skip == "recruiter opted out"

    @pytest.mark.parametrize(
        "status",
        [
            ApplicationStatus.NOT_INTERESTED,
            ApplicationStatus.UNSUBSCRIBED,
            ApplicationStatus.CLOSED,
            ApplicationStatus.INTERVIEW_SCHEDULED,
        ],
    )
    def test_stops_on_terminal_statuses(self, db_session, outreach, status):
        outreach["application"].status = status
        db_session.commit()

        follow_up = self._due(db_session, outreach)
        email, skip = svc.prepare_follow_up_email(db_session, follow_up)
        db_session.commit()

        assert email is None
        assert skip is not None
        assert follow_up.status == FollowUpStatus.CANCELLED


class TestTemplateChoice:
    def _follow_up(self, db_session, outreach, step=1, template=FollowUpTemplate.NO_RESPONSE):
        follow_up = FollowUp(
            application_id=outreach["application"].id,
            step=step,
            scheduled_at=datetime.now(UTC),
            template=template,
        )
        db_session.add(follow_up)
        db_session.commit()
        return follow_up

    def test_silence_gets_the_no_response_angle(self, db_session, outreach):
        follow_up = self._follow_up(db_session, outreach)
        assert (
            svc.choose_template(db_session, outreach["application"], follow_up)
            == FollowUpTemplate.NO_RESPONSE
        )

    def test_a_reply_without_a_decision_gets_the_partial_angle(self, db_session, outreach):
        outreach["application"].status = ApplicationStatus.REPLIED
        db_session.commit()

        follow_up = self._follow_up(db_session, outreach)
        assert (
            svc.choose_template(db_session, outreach["application"], follow_up)
            == FollowUpTemplate.PARTIAL_RESPONSE
        )

    def test_a_later_step_assumes_they_have_seen_it(self, db_session, outreach):
        follow_up = self._follow_up(db_session, outreach, step=2)
        assert (
            svc.choose_template(db_session, outreach["application"], follow_up)
            == FollowUpTemplate.OPENED_NO_REPLY
        )

    def test_the_final_step_keeps_its_angle(self, db_session, outreach):
        follow_up = self._follow_up(db_session, outreach, step=2, template=FollowUpTemplate.FINAL)
        assert (
            svc.choose_template(db_session, outreach["application"], follow_up)
            == FollowUpTemplate.FINAL
        )


class TestComposition:
    @pytest.mark.parametrize("template", list(FollowUpTemplate))
    def test_every_template_produces_a_usable_email(self, template):
        cand = CandidateContext(name="Jordan", target_roles=["Backend Engineer"])
        rec = RecruiterContext(name="Sam", company="Northwind Labs")

        composed = svc.compose_follow_up(
            template, cand, rec, step=1, thread_subject="Jordan — Backend"
        )

        assert composed.subject == "Re: Jordan — Backend"
        assert "Jordan" in composed.body
        assert composed.generated_with == "template"  # no API key in tests

    def test_falls_back_when_the_llm_leaks_its_scratchpad(self, monkeypatch):
        monkeypatch.setattr(svc.settings, "openrouter_api_key", "test-key", raising=False)
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **kw: '{"body": "We need to write a follow-up. The user wants a nudge."}',
        )
        composed = svc.compose_follow_up(
            FollowUpTemplate.NO_RESPONSE,
            CandidateContext(name="Jordan"),
            RecruiterContext(company="Northwind"),
            step=1,
            thread_subject="Hello",
        )
        assert composed.generated_with == "template"
        assert "We need to" not in composed.body

    def test_uses_good_llm_output(self, monkeypatch):
        monkeypatch.setattr(svc.settings, "openrouter_api_key", "test-key", raising=False)
        monkeypatch.setattr(
            svc, "chat_completion", lambda *a, **kw: '{"body": "Quick nudge — still hiring?"}'
        )
        composed = svc.compose_follow_up(
            FollowUpTemplate.NO_RESPONSE,
            CandidateContext(name="Jordan"),
            RecruiterContext(company="Northwind"),
            step=1,
            thread_subject="Hello",
        )
        assert composed.generated_with == "llm"
        assert composed.body == "Quick nudge — still hiring?"


class TestCampaignConfiguration:
    def test_create_accepts_follow_up_settings(self, auth_client, connected_gmail, resume):
        resp = auth_client.post(
            "/api/v1/campaigns",
            json={
                "target_companies": ["Northwind"],
                "follow_up_enabled": True,
                "follow_up_count": 3,
                "follow_up_interval_days": 5,
                "follow_up_stop_on_reply": False,
            },
        )
        assert resp.status_code == 201, resp.text
        campaign = resp.json()["campaign"]

        assert campaign["follow_up_count"] == 3
        assert campaign["follow_up_interval_days"] == 5
        assert campaign["follow_up_stop_on_reply"] is False

    def test_defaults_follow_the_research(self, auth_client, connected_gmail, resume):
        campaign = auth_client.post(
            "/api/v1/campaigns", json={"target_companies": ["Northwind"]}
        ).json()["campaign"]

        assert campaign["follow_up_enabled"] is True
        assert campaign["follow_up_count"] == 2
        assert campaign["follow_up_interval_days"] == 4
        assert campaign["follow_up_stop_on_reply"] is True

    def test_rejects_an_absurd_sequence_length(self, auth_client, connected_gmail, resume):
        resp = auth_client.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Northwind"], "follow_up_count": 50},
        )
        assert resp.status_code == 422

    def test_zero_count_disables_the_sequence(self, auth_client, connected_gmail, resume):
        campaign = auth_client.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Northwind"], "follow_up_count": 0},
        ).json()["campaign"]
        assert campaign["follow_up_enabled"] is False
