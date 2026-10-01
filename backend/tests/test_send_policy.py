"""Auto-send policy: the trial ramp, the pause, the daily ceiling, batch approve.

The invariant every test here defends is that a *block is never a drop*. When the
policy says no, the outreach still exists as a DRAFT the user can send by hand —
so the failure mode of this whole feature is "you have mail to review", never
"your application vanished" and never "it sent something you hadn't seen".
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.application import Application
from app.models.autopilot import AutopilotPreference
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.services import recruiter_discovery, send_policy
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


def _pref(client, **fields) -> dict:
    """Save autopilot preferences and return the stored row as JSON."""
    response = client.put("/api/v1/autopilot", json=fields)
    assert response.status_code == 200, response.text
    return response.json()


def _outbound(db) -> list[Email]:
    return (
        db.query(Email)
        .filter(Email.direction == EmailDirection.SENT)
        .order_by(Email.id)
        .all()
    )


def _reply_draft(db, application: Application | None = None) -> Email:
    """Turn a thread into a conversation and park a reply draft on it.

    Inbound mail on the thread is what makes the draft a *reply* rather than
    outreach — the same test the queue and the trial both use. Callers that also
    care about a pending outreach draft must pass a *different* application:
    inbound mail relabels everything on its thread, pending drafts included.
    """
    application = application or db.query(Application).first()
    thread = db.query(EmailThread).filter_by(application_id=application.id).first()
    db.add(
        Email(
            thread_id=thread.id,
            direction=EmailDirection.RECEIVED,
            status=EmailStatus.RECEIVED,
            from_address="talent@acme.com",
            body_text="Are you free Thursday?",
        )
    )
    reply = Email(
        thread_id=thread.id,
        direction=EmailDirection.SENT,
        status=EmailStatus.DRAFT,
        to_address="talent@acme.com",
        subject="Re: hello",
        body_text="Thursday works.",
    )
    db.add(reply)
    db.commit()
    return reply


def _run_campaign(client, company: str = "Acme") -> dict:
    response = client.post(
        "/api/v1/campaigns", json={"target_companies": [company], "auto_send": True}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# The decision function                                                        #
# --------------------------------------------------------------------------- #


class TestEvaluate:
    def _bare(self, db_session, user_id: int, **fields) -> AutopilotPreference:
        pref = AutopilotPreference(user_id=user_id, **fields)
        db_session.add(pref)
        db_session.commit()
        return pref

    def test_no_preference_row_means_review_mode(self, db_session, current_user):
        """Never having configured anything is not consent to send."""
        decision = send_policy.evaluate(db_session, current_user, None)
        assert decision.enabled is False
        assert decision.code == send_policy.REASON_REVIEW_MODE

    def test_auto_send_off_is_review_mode(self, db_session, current_user):
        pref = self._bare(db_session, current_user.id, auto_send=False)
        assert send_policy.evaluate(db_session, current_user, pref).enabled is False

    def test_auto_send_on_with_no_trial_sends_immediately(self, db_session, current_user):
        pref = self._bare(db_session, current_user.id, auto_send=True)
        decision = send_policy.evaluate(db_session, current_user, pref)
        assert decision.enabled is True
        assert decision.code is None

    def test_an_unfinished_trial_holds_sending(self, db_session, current_user):
        pref = self._bare(
            db_session, current_user.id, auto_send=True, auto_send_trial_approvals=3
        )
        decision = send_policy.evaluate(db_session, current_user, pref)
        assert decision.enabled is False
        assert decision.code == send_policy.REASON_TRIAL
        # The reason has to carry the arithmetic — "approve 3 more" is the whole
        # instruction, and a bare "trial in progress" leaves the user stuck.
        assert "3 more" in (decision.reason or "")
        assert decision.approvals_needed == 3
        assert decision.approvals_done == 0

    def test_a_finished_trial_graduates(self, db_session, current_user):
        pref = self._bare(
            db_session,
            current_user.id,
            auto_send=True,
            auto_send_trial_approvals=2,
            auto_send_approved_count=2,
        )
        assert send_policy.evaluate(db_session, current_user, pref).enabled is True

    def test_extra_approvals_do_not_un_graduate(self, db_session, current_user):
        """Over-shooting the trial keeps auto-send on, not off by one."""
        pref = self._bare(
            db_session,
            current_user.id,
            auto_send=True,
            auto_send_trial_approvals=2,
            auto_send_approved_count=9,
        )
        assert send_policy.evaluate(db_session, current_user, pref).enabled is True

    def test_a_pause_outranks_a_finished_trial(self, db_session, current_user):
        pref = self._bare(
            db_session,
            current_user.id,
            auto_send=True,
            auto_send_paused_until=datetime.now(UTC) + timedelta(hours=2),
        )
        decision = send_policy.evaluate(db_session, current_user, pref)
        assert decision.enabled is False
        assert decision.code == send_policy.REASON_PAUSED

    def test_an_expired_pause_stops_blocking(self, db_session, current_user):
        pref = self._bare(
            db_session,
            current_user.id,
            auto_send=True,
            auto_send_paused_until=datetime.now(UTC) - timedelta(minutes=1),
        )
        assert send_policy.evaluate(db_session, current_user, pref).enabled is True

    def test_a_naive_pause_timestamp_is_still_honoured(self, db_session, current_user):
        """SQLite hands back naive datetimes; a pause must not evaporate on read."""
        pref = self._bare(db_session, current_user.id, auto_send=True)
        pref.auto_send_paused_until = (
            datetime.now(UTC) + timedelta(hours=3)
        ).replace(tzinfo=None)
        db_session.commit()
        assert send_policy.evaluate(db_session, current_user, pref).code == (
            send_policy.REASON_PAUSED
        )

    def test_review_mode_beats_a_pause_in_the_reason(self, db_session, current_user):
        """Off is off. The user should not be told to resume a pause instead."""
        pref = self._bare(
            db_session,
            current_user.id,
            auto_send=False,
            auto_send_paused_until=datetime.now(UTC) + timedelta(hours=2),
        )
        assert send_policy.evaluate(db_session, current_user, pref).code == (
            send_policy.REASON_REVIEW_MODE
        )


class TestCampaignIntent:
    """``intent`` — the campaign's own auto_send flag, folded into the decision.

    It is an extra requirement, never a substitute for the user's switch. The two
    asymmetries below are the whole design: a campaign may not out-vote someone
    who turned auto-send off, but a *missing* preference row is "unconfigured"
    rather than "off" and must not strand a campaign the user just launched.
    """

    def _bare(self, db_session, user_id: int, **fields) -> AutopilotPreference:
        pref = AutopilotPreference(user_id=user_id, **fields)
        db_session.add(pref)
        db_session.commit()
        return pref

    def test_a_campaign_that_does_not_ask_stays_in_review(self, db_session, current_user):
        pref = self._bare(db_session, current_user.id, auto_send=True)
        decision = send_policy.evaluate(db_session, current_user, pref, intent=False)
        assert decision.enabled is False
        assert decision.code == send_policy.REASON_REVIEW_MODE

    def test_a_campaign_cannot_override_an_explicit_off(self, db_session, current_user):
        """The settings toggle is the user speaking; a campaign flag is not a veto
        override. Getting this backwards sends mail someone switched off."""
        pref = self._bare(db_session, current_user.id, auto_send=False)
        decision = send_policy.evaluate(db_session, current_user, pref, intent=True)
        assert decision.enabled is False
        assert decision.code == send_policy.REASON_REVIEW_MODE

    def test_a_campaign_sends_for_a_user_who_never_configured_autopilot(
        self, db_session, current_user
    ):
        """The regression this fixes: no row meant review mode for everything, so
        follow-ups on an explicitly auto-send campaign silently became drafts."""
        decision = send_policy.evaluate(db_session, current_user, None, intent=True)
        assert decision.enabled is True
        assert decision.code is None

    def test_no_row_and_no_intent_is_still_review_mode(self, db_session, current_user):
        """Without a campaign flag the row is the only consent on record."""
        assert send_policy.evaluate(db_session, current_user, None).enabled is False

    def test_intent_does_not_skip_the_trial(self, db_session, current_user):
        pref = self._bare(
            db_session, current_user.id, auto_send=True, auto_send_trial_approvals=3
        )
        decision = send_policy.evaluate(db_session, current_user, pref, intent=True)
        assert decision.enabled is False
        assert decision.code == send_policy.REASON_TRIAL

    def test_intent_does_not_skip_a_pause(self, db_session, current_user):
        """A pause has to reach outreach the user already asked to automate —
        otherwise it is not a pause the user can trust."""
        pref = self._bare(
            db_session,
            current_user.id,
            auto_send=True,
            auto_send_paused_until=datetime.now(UTC) + timedelta(hours=2),
        )
        decision = send_policy.evaluate(db_session, current_user, pref, intent=True)
        assert decision.enabled is False
        assert decision.code == send_policy.REASON_PAUSED


class TestPauseAndResume:
    def test_pause_extends_rather_than_shortens(self, db_session, current_user):
        """"Pause" must never be the verb that made something send sooner."""
        pref = AutopilotPreference(user_id=current_user.id, auto_send=True)
        db_session.add(pref)
        db_session.commit()

        now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
        send_policy.pause(pref, 72, now=now)
        far = pref.auto_send_paused_until
        send_policy.pause(pref, 1, now=now)
        assert pref.auto_send_paused_until == far

    def test_pause_is_clamped_to_the_maximum(self, db_session, current_user):
        pref = AutopilotPreference(user_id=current_user.id, auto_send=True)
        db_session.add(pref)
        db_session.commit()
        now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
        send_policy.pause(pref, 10**6, now=now)
        assert pref.auto_send_paused_until == now + timedelta(
            hours=send_policy.MAX_PAUSE_HOURS
        )

    def test_resume_clears_the_hold(self, db_session, current_user):
        pref = AutopilotPreference(
            user_id=current_user.id,
            auto_send=True,
            auto_send_paused_until=datetime.now(UTC) + timedelta(days=1),
        )
        db_session.add(pref)
        db_session.commit()
        send_policy.resume(pref)
        assert pref.auto_send_paused_until is None
        assert send_policy.evaluate(db_session, current_user, pref).enabled is True


# --------------------------------------------------------------------------- #
# The daily ceiling                                                            #
# --------------------------------------------------------------------------- #


class TestDailyCeiling:
    def _sent(self, db, current_user, *, auto_sent: bool, when: datetime) -> None:
        """Book one SENT outreach for the user, at *when*."""
        application = db.query(Application).first()
        thread = db.query(EmailThread).filter_by(
            application_id=application.id
        ).first()
        db.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.SENT,
                status=EmailStatus.SENT,
                to_address="someone@acme.com",
                subject="hi",
                sent_at=when,
                auto_sent=auto_sent,
            )
        )
        db.commit()

    def test_no_limit_means_no_ceiling_check(self, ready, db_session, current_user):
        _pref(ready, auto_send=True)
        _run_campaign(ready)
        pref = db_session.query(AutopilotPreference).filter_by(user_id=current_user.id).one()
        assert pref.auto_send_daily_limit is None
        assert send_policy.evaluate(db_session, current_user, pref).enabled is True

    def test_reaching_the_ceiling_parks_the_next_email(self, ready, db_session, current_user):
        _pref(ready, auto_send=True, auto_send_daily_limit=1)
        _run_campaign(ready, "Acme")
        pref = db_session.query(AutopilotPreference).filter_by(user_id=current_user.id).one()

        self._sent(db_session, current_user, auto_sent=True, when=datetime.now(UTC))
        decision = send_policy.evaluate(db_session, current_user, pref)
        assert decision.enabled is False
        assert decision.code == send_policy.REASON_DAILY_LIMIT

        # And the next campaign's mail waits for the user rather than vanishing.
        _run_campaign(ready, "Northwind")
        parked = [e for e in _outbound(db_session) if e.status == EmailStatus.DRAFT]
        assert parked, "an email blocked by the ceiling must survive as a draft"

    def test_approved_sends_do_not_eat_the_agents_allowance(
        self, ready, db_session, current_user
    ):
        """The ceiling bounds what the agent does unsupervised — nothing else.

        A user who reads and approves ten emails has not used up the budget for
        unattended sending; if anything they have earned more of it.
        """
        _pref(ready, auto_send=True, auto_send_daily_limit=1)
        _run_campaign(ready)
        pref = db_session.query(AutopilotPreference).filter_by(user_id=current_user.id).one()

        self._sent(db_session, current_user, auto_sent=False, when=datetime.now(UTC))
        assert send_policy.evaluate(db_session, current_user, pref).enabled is True

    def test_the_window_rolls(self, ready, db_session, current_user):
        _pref(ready, auto_send=True, auto_send_daily_limit=1)
        _run_campaign(ready)
        pref = db_session.query(AutopilotPreference).filter_by(user_id=current_user.id).one()

        self._sent(
            db_session,
            current_user,
            auto_sent=True,
            when=datetime.now(UTC) - timedelta(hours=25),
        )
        assert send_policy.evaluate(db_session, current_user, pref).enabled is True


# --------------------------------------------------------------------------- #
# End to end through the pipeline                                              #
# --------------------------------------------------------------------------- #


class TestPipelineHonoursThePolicy:
    def test_a_trial_parks_campaign_outreach_as_drafts(self, ready, db_session):
        """auto_send=True on the campaign is not enough while a trial is running."""
        _pref(ready, auto_send=True, auto_send_trial_approvals=2)
        _run_campaign(ready)

        emails = _outbound(db_session)
        assert emails
        assert all(e.status == EmailStatus.DRAFT for e in emails)
        # Not marked auto-sent: a human is about to read it.
        assert all(e.auto_sent is False for e in emails)

    def test_no_trial_queues_campaign_outreach_and_marks_it_auto_sent(
        self, ready, db_session
    ):
        _pref(ready, auto_send=True, auto_send_trial_approvals=0)
        _run_campaign(ready)

        emails = _outbound(db_session)
        assert emails
        assert all(e.status == EmailStatus.QUEUED for e in emails)
        assert all(e.auto_sent is True for e in emails)

    def test_a_pause_parks_outreach_the_user_already_asked_to_auto_send(
        self, ready, db_session
    ):
        """A pause has to reach campaigns launched before it was set."""
        _pref(ready, auto_send=True)
        assert ready.post("/api/v1/autopilot/auto-send/pause", json={"hours": 6}).is_success
        _run_campaign(ready)

        emails = _outbound(db_session)
        assert emails
        assert all(e.status == EmailStatus.DRAFT for e in emails)

    def test_the_campaign_says_why_nothing_sent(self, ready):
        """A pile of drafts under an auto-send campaign must explain itself."""
        _pref(ready, auto_send=True, auto_send_trial_approvals=2)
        body = _run_campaign(ready)
        assert body["queued"] is False
        assert "approve" in (body["detail"] or "").lower()

    def test_a_paused_sweep_does_not_promote_parked_drafts(self, ready, db_session):
        """Resuming a campaign must not send what the pause held back.

        The dispatch sweep used to pick up DRAFT rows as well as QUEUED ones, which
        would have made "pause" mean "delay until the next campaign action".
        """
        _pref(ready, auto_send=True)
        ready.post("/api/v1/autopilot/auto-send/pause", json={"hours": 6})
        campaign = _run_campaign(ready)["campaign"]

        ready.post(f"/api/v1/campaigns/{campaign['id']}/pause")
        ready.post(f"/api/v1/campaigns/{campaign['id']}/resume")

        emails = _outbound(db_session)
        assert emails
        assert all(e.status == EmailStatus.DRAFT for e in emails)


# --------------------------------------------------------------------------- #
# Approving, one at a time and in bulk                                         #
# --------------------------------------------------------------------------- #


class TestApprovalGraduatesTheTrial:
    def test_approving_counts_toward_the_trial(self, ready, db_session, current_user):
        _pref(ready, auto_send=True, auto_send_trial_approvals=1)
        _run_campaign(ready)
        draft = _outbound(db_session)[0]

        assert ready.post(f"/api/v1/review/emails/{draft.id}/approve").is_success

        pref = db_session.query(AutopilotPreference).filter_by(user_id=current_user.id).one()
        assert pref.auto_send_approved_count == 1
        # Graduated: the next campaign sends without asking.
        _run_campaign(ready, "Northwind")
        fresh = [e for e in _outbound(db_session) if e.id != draft.id]
        assert fresh and all(e.status == EmailStatus.QUEUED for e in fresh)

    def test_approving_clears_the_auto_sent_flag(self, ready, db_session):
        """A draft the policy parked, then a human approved, is a reviewed send."""
        _pref(ready, auto_send=True, auto_send_daily_limit=5)
        _run_campaign(ready)
        queued = _outbound(db_session)[0]
        assert queued.auto_sent is True

        # Park it back to a draft the way a pause would, then approve it.
        queued.status = EmailStatus.DRAFT
        db_session.commit()
        assert ready.post(f"/api/v1/review/emails/{queued.id}/approve").is_success

        db_session.refresh(queued)
        assert queued.auto_sent is False

    def test_dismissing_does_not_count_as_approval(self, ready, db_session, current_user):
        _pref(ready, auto_send=True, auto_send_trial_approvals=2)
        _run_campaign(ready)
        draft = _outbound(db_session)[0]

        assert ready.post(f"/api/v1/review/emails/{draft.id}/dismiss").status_code == 204

        pref = db_session.query(AutopilotPreference).filter_by(user_id=current_user.id).one()
        assert pref.auto_send_approved_count == 0

    def test_approving_a_reply_does_not_count_toward_the_trial(
        self, ready, db_session, current_user
    ):
        """The trial asks "have you read what we write to strangers?". Answering a
        recruiter who wrote to *you* does not answer it — and counting it let a
        busy inbox graduate the trial without the user ever seeing a cold email."""
        _pref(ready, auto_send=True, auto_send_trial_approvals=2)
        _run_campaign(ready)
        reply = _reply_draft(db_session)

        assert ready.post(f"/api/v1/review/emails/{reply.id}/approve").is_success

        pref = db_session.query(AutopilotPreference).filter_by(user_id=current_user.id).one()
        assert pref.auto_send_approved_count == 0
        assert (
            send_policy.evaluate(db_session, current_user, pref).code
            == send_policy.REASON_TRIAL
        )

    def test_a_batch_counts_only_the_outreach_in_it(self, ready, db_session, current_user):
        _pref(ready, auto_send=True, auto_send_trial_approvals=3)
        _run_campaign(ready)
        _run_campaign(ready, "Northwind")

        # Northwind's outreach already went out and they wrote back, so that
        # thread holds a reply draft. Acme's cold email is still pending.
        second = db_session.query(Application).order_by(Application.id.desc()).first()
        for email in _outbound(db_session):
            if email.thread.application_id == second.id:
                email.status = EmailStatus.SENT
        db_session.commit()
        reply = _reply_draft(db_session, second)

        outreach = [
            e
            for e in _outbound(db_session)
            if e.status == EmailStatus.DRAFT and e.id != reply.id
        ]
        assert len(outreach) == 1

        response = ready.post(
            "/api/v1/review/approve-batch",
            json={"email_ids": [reply.id, *[e.id for e in outreach]]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["approved"] == len(outreach) + 1

        pref = db_session.query(AutopilotPreference).filter_by(user_id=current_user.id).one()
        assert pref.auto_send_approved_count == len(outreach)

    def test_the_queue_reports_why_drafts_are_waiting(self, ready):
        _pref(ready, auto_send=True, auto_send_trial_approvals=2)
        _run_campaign(ready)

        body = ready.get("/api/v1/review").json()
        assert body["auto_send"]["sending_now"] is False
        assert body["auto_send"]["blocked_reason_code"] == send_policy.REASON_TRIAL
        assert body["auto_send"]["trial_remaining"] == 2


class TestBatchApprove:
    def _drafts(self, ready, db_session, companies=("Acme", "Northwind")):
        _pref(ready, auto_send=False)
        for company in companies:
            ready.post(
                "/api/v1/campaigns",
                json={"target_companies": [company], "auto_send": False},
            )
        return [e for e in _outbound(db_session) if e.status == EmailStatus.DRAFT]

    def test_explicit_ids_are_approved_together(self, ready, db_session):
        drafts = self._drafts(ready, db_session)
        assert len(drafts) >= 2

        response = ready.post(
            "/api/v1/review/approve-batch",
            json={"email_ids": [d.id for d in drafts]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["approved"] == len(drafts)

        for draft in drafts:
            db_session.refresh(draft)
            assert draft.status == EmailStatus.QUEUED

    def test_scope_outreach_takes_the_whole_queue(self, ready, db_session):
        drafts = self._drafts(ready, db_session)
        response = ready.post(
            "/api/v1/review/approve-batch", json={"scope": "outreach"}
        )
        assert response.json()["approved"] == len(drafts)

    def test_scope_outreach_leaves_reply_drafts_alone(self, ready, db_session):
        """"Approve all my outreach" must not send an AI reply to a human unread."""
        self._drafts(ready, db_session, companies=("Acme",))
        application = db_session.query(Application).first()
        thread = db_session.query(EmailThread).filter_by(
            application_id=application.id
        ).first()
        # An inbound message makes this thread a conversation...
        db_session.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.RECEIVED,
                status=EmailStatus.RECEIVED,
                from_address="talent@acme.com",
                body_text="Are you free Thursday?",
            )
        )
        # ...and this draft a reply to a person who wrote in.
        reply = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.DRAFT,
            to_address="talent@acme.com",
            subject="Re: hello",
            body_text="Thursday works.",
        )
        db_session.add(reply)
        db_session.commit()

        ready.post("/api/v1/review/approve-batch", json={"scope": "outreach"})

        db_session.refresh(reply)
        assert reply.status == EmailStatus.DRAFT

    def test_scope_all_does_include_replies(self, ready, db_session):
        """The explicit opt-in exists; it just isn't what "approve all" means."""
        self._drafts(ready, db_session, companies=("Acme",))
        application = db_session.query(Application).first()
        thread = db_session.query(EmailThread).filter_by(
            application_id=application.id
        ).first()
        db_session.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.RECEIVED,
                status=EmailStatus.RECEIVED,
                from_address="talent@acme.com",
                body_text="Are you free Thursday?",
            )
        )
        reply = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.DRAFT,
            to_address="talent@acme.com",
            subject="Re: hello",
            body_text="Thursday works.",
        )
        db_session.add(reply)
        db_session.commit()

        ready.post("/api/v1/review/approve-batch", json={"scope": "all"})

        db_session.refresh(reply)
        assert reply.status == EmailStatus.QUEUED

    def test_a_stale_id_is_skipped_not_fatal(self, ready, db_session):
        """A queue can go stale between the render and the click."""
        drafts = self._drafts(ready, db_session)
        response = ready.post(
            "/api/v1/review/approve-batch",
            json={"email_ids": [drafts[0].id, 999_999]},
        )
        body = response.json()
        assert body["approved"] == 1
        assert "999999" in body["skipped"]

    def test_a_batch_can_finish_a_trial(self, ready, db_session, current_user):
        _pref(ready, auto_send=True, auto_send_trial_approvals=2)
        ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme", "Northwind"], "auto_send": True},
        )
        drafts = [e for e in _outbound(db_session) if e.status == EmailStatus.DRAFT]
        assert len(drafts) >= 2

        body = ready.post(
            "/api/v1/review/approve-batch",
            json={"email_ids": [d.id for d in drafts[:2]]},
        ).json()
        assert body["approved"] == 2
        assert body["trial_completed"] is True
        assert body["auto_send"]["sending_now"] is True

    def test_neither_ids_nor_scope_is_a_422(self, ready):
        assert ready.post("/api/v1/review/approve-batch", json={}).status_code == 422

    def test_another_users_draft_is_not_approvable(self, ready, db_session):
        """Batch approve must not become a way to send from someone else's queue.

        The batch endpoint reports unapprovable ids instead of failing, so without
        the ownership join this would look like a success to the caller.
        """
        drafts = self._drafts(ready, db_session)

        ready.post(
            "/api/v1/auth/register",
            json={"email": "stranger@example.com", "password": "supersecret123"},
        )
        token = ready.post(
            "/api/v1/auth/login",
            data={"username": "stranger@example.com", "password": "supersecret123"},
        ).json()["access_token"]

        response = ready.post(
            "/api/v1/review/approve-batch",
            json={"email_ids": [drafts[0].id]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.json()["approved"] == 0
        db_session.refresh(drafts[0])
        assert drafts[0].status == EmailStatus.DRAFT

    def test_scope_cannot_reach_another_users_queue(self, ready, db_session):
        """And neither can "approve everything" — scope is per-user by construction."""
        drafts = self._drafts(ready, db_session)

        ready.post(
            "/api/v1/auth/register",
            json={"email": "stranger2@example.com", "password": "supersecret123"},
        )
        token = ready.post(
            "/api/v1/auth/login",
            data={"username": "stranger2@example.com", "password": "supersecret123"},
        ).json()["access_token"]

        response = ready.post(
            "/api/v1/review/approve-batch",
            json={"scope": "all"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.json()["approved"] == 0
        db_session.refresh(drafts[0])
        assert drafts[0].status == EmailStatus.DRAFT


# --------------------------------------------------------------------------- #
# The settings surface                                                         #
# --------------------------------------------------------------------------- #


class TestAutoSendEndpoints:
    def test_status_reports_the_toggle_and_the_live_answer_separately(self, ready):
        _pref(ready, auto_send=True, auto_send_trial_approvals=3)
        body = ready.get("/api/v1/autopilot/auto-send").json()
        assert body["auto_send"] is True      # what the user asked for
        assert body["sending_now"] is False   # what is happening
        assert body["trial_remaining"] == 3

    def test_pause_then_resume_round_trips(self, ready):
        _pref(ready, auto_send=True)
        paused = ready.post(
            "/api/v1/autopilot/auto-send/pause", json={"hours": 12}
        ).json()
        assert paused["paused"] is True
        assert paused["sending_now"] is False
        assert paused["paused_until"] is not None

        resumed = ready.post("/api/v1/autopilot/auto-send/resume").json()
        assert resumed["paused"] is False
        assert resumed["sending_now"] is True

    def test_pause_defaults_to_a_day(self, ready):
        _pref(ready, auto_send=True)
        body = ready.post("/api/v1/autopilot/auto-send/pause").json()
        until = datetime.fromisoformat(body["paused_until"])
        assert timedelta(hours=23) < until - datetime.now(UTC) <= timedelta(hours=24)

    def test_resume_is_idempotent(self, ready):
        _pref(ready, auto_send=True)
        assert ready.post("/api/v1/autopilot/auto-send/resume").is_success
        assert ready.post("/api/v1/autopilot/auto-send/resume").is_success

    def test_resume_does_not_override_an_unfinished_trial(self, ready):
        """Resuming clears the pause and nothing else."""
        _pref(ready, auto_send=True, auto_send_trial_approvals=2)
        ready.post("/api/v1/autopilot/auto-send/pause", json={"hours": 5})
        body = ready.post("/api/v1/autopilot/auto-send/resume").json()
        assert body["paused"] is False
        assert body["sending_now"] is False
        assert body["blocked_reason_code"] == send_policy.REASON_TRIAL

    def test_pausing_auto_send_leaves_autopilot_active(self, ready):
        """The point of the pause is not having to tear the pipeline down."""
        _pref(ready, auto_send=True, is_active=True)
        ready.post("/api/v1/autopilot/auto-send/pause", json={"hours": 3})
        assert ready.get("/api/v1/autopilot").json()["is_active"] is True

    def test_the_knobs_round_trip_through_the_preferences_endpoint(self, ready):
        saved = _pref(
            ready,
            auto_send=True,
            auto_send_trial_approvals=4,
            auto_send_daily_limit=7,
        )
        assert saved["auto_send_trial_approvals"] == 4
        assert saved["auto_send_daily_limit"] == 7

    def test_a_null_daily_limit_clears_the_ceiling(self, ready):
        _pref(ready, auto_send_daily_limit=7)
        assert _pref(ready, auto_send_daily_limit=None)["auto_send_daily_limit"] is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"auto_send_trial_approvals": -1},
            {"auto_send_trial_approvals": 99},
            {"auto_send_daily_limit": 0},
            {"auto_send_daily_limit": 500},
        ],
    )
    def test_out_of_range_knobs_are_refused(self, ready, payload):
        assert ready.put("/api/v1/autopilot", json=payload).status_code == 422
