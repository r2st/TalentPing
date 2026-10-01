"""The autopilot dry run: what it will do, before it does it.

Three properties carry the whole feature, and each has tests here:

* it is **read-only and offline** — no rows written, no crawl, no model call;
* it is **deterministic** — two reads a second apart agree, so a refresh does not
  teach the user that the plan is fiction;
* it **predicts what the live run actually does** — the same gates, the same
  budget arithmetic, the same refusals, because it calls the same functions.

The third is the one that rots silently, so the gate tests below assert that a
posting the plan skips is a posting the pipeline would have skipped, with the same
sentence.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.autopilot import AutopilotPreference
from app.models.job import JobPosting, JobStatus
from app.models.recruiter import DeliveryState, Recruiter
from app.models.recruiter_cache import RecruiterCache
from app.services import autopilot_plan


@pytest.fixture()
def pref(db_session, current_user, resume) -> AutopilotPreference:
    """Autopilot on, with the fixture resume's own targeting."""
    row = AutopilotPreference(
        user_id=current_user.id,
        is_active=True,
        resume_id=resume.id,
        target_roles=["Senior Backend Engineer"],
        locations=["San Francisco", "Remote"],
        min_fit_score=70,
        daily_application_limit=5,
        auto_send=True,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(current_user)
    return row


def _posting(
    db_session,
    current_user,
    *,
    title="Senior Backend Engineer",
    company="Northwind Labs",
    location="San Francisco, CA",
    fit=88.0,
    fingerprint=None,
    **kwargs,
) -> JobPosting:
    row = JobPosting(
        user_id=current_user.id,
        title=title,
        company=company,
        location=location,
        url=f"https://example.com/{(fingerprint or title).lower().replace(' ', '-')}",
        fingerprint=fingerprint or f"fp-{title}-{company}",
        status=JobStatus.NEW,
        fit_score=fit,
        **kwargs,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _known_contact(db_session, current_user, company="Northwind Labs", **kwargs):
    row = Recruiter(
        user_id=current_user.id,
        email=kwargs.pop("email", "talent@northwind.com"),
        name="Alex Recruiter",
        title="Technical Recruiter",
        company=company,
        confidence=0.9,
        **kwargs,
    )
    db_session.add(row)
    db_session.commit()
    return row


# --------------------------------------------------------------------------- #
# Nothing to plan                                                              #
# --------------------------------------------------------------------------- #


class TestNothingToPlan:
    def test_autopilot_off_explains_itself_rather_than_erroring(
        self, db_session, current_user, connected_gmail
    ):
        """The question is asked *before* turning it on, so it must not 409."""
        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.active is False
        assert "off" in plan.blocked_reason
        assert plan.sends == []
        assert plan.summary == plan.blocked_reason

    def test_no_mailbox_is_a_different_answer_from_autopilot_off(
        self, db_session, current_user, pref
    ):
        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.active is False
        assert "mailbox" in plan.blocked_reason

    def test_the_endpoint_does_not_require_autopilot_to_be_on(self, auth_client):
        """Unlike POST /autopilot/run, which refuses. Asking is always allowed."""
        resp = auth_client.get("/api/v1/autopilot/dry-run")
        assert resp.status_code == 200
        assert resp.json()["active"] is False


# --------------------------------------------------------------------------- #
# The plan itself                                                              #
# --------------------------------------------------------------------------- #


class TestThePlan:
    def test_a_matching_posting_becomes_a_planned_send(
        self, db_session, current_user, connected_gmail, pref
    ):
        posting = _posting(db_session, current_user)
        _known_contact(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)

        assert plan.active is True
        assert len(plan.sends) == 1
        send = plan.sends[0]
        assert send.job_posting_id == posting.id
        assert send.company == "Northwind Labs"
        assert send.contact.email == "talent@northwind.com"
        assert send.contact.source == autopilot_plan.SOURCE_KNOWN
        assert send.from_address == "candidate@gmail.com"
        assert send.resume_label == "Senior Backend Engineer"
        assert send.scheduled_at is not None

    def test_every_planned_send_carries_its_reasoning(
        self, db_session, current_user, connected_gmail, pref
    ):
        """"Why this one" is the entire point — a queue length already existed."""
        _posting(db_session, current_user)
        _known_contact(db_session, current_user)

        send = autopilot_plan.build_plan(db_session, current_user).sends[0]

        assert send.reasons, "a planned send with no reasons is just a queue entry"
        assert "88" in send.reasons[0] and "70" in send.reasons[0]
        assert any("Matched under" in r for r in send.reasons)

    def test_scouts_second_opinion_is_quoted_when_it_exists(
        self, db_session, current_user, connected_gmail, pref
    ):
        _posting(
            db_session,
            current_user,
            llm_fit_score=91.0,
            llm_reasoning="Payments background lines up with the team's roadmap.",
        )
        _known_contact(db_session, current_user)

        send = autopilot_plan.build_plan(db_session, current_user).sends[0]
        assert send.llm_fit_score == 91.0
        assert any("Second opinion" in r and "roadmap" in r for r in send.reasons)

    def test_posting_age_is_stated_from_the_boards_own_date(
        self, db_session, current_user, connected_gmail, pref
    ):
        _posting(
            db_session,
            current_user,
            posted_at=datetime.now(UTC) - timedelta(days=3),
        )
        _known_contact(db_session, current_user)

        send = autopilot_plan.build_plan(db_session, current_user).sends[0]
        assert any("3 day(s) ago" in r for r in send.reasons)

    def test_the_cover_letter_setting_is_reported_per_send(
        self, db_session, current_user, connected_gmail, pref
    ):
        _posting(db_session, current_user)
        _known_contact(db_session, current_user)

        send = autopilot_plan.build_plan(db_session, current_user).sends[0]
        assert send.cover_letter == "inline"

        pref.cover_letter_enabled = False
        db_session.commit()
        send = autopilot_plan.build_plan(db_session, current_user).sends[0]
        assert send.cover_letter is None


# --------------------------------------------------------------------------- #
# Contacts: only what is on file                                               #
# --------------------------------------------------------------------------- #


class TestContacts:
    def test_a_company_with_no_contact_says_so_instead_of_inventing_one(
        self, db_session, current_user, connected_gmail, pref
    ):
        """The gap is the information: these are the sends that may not happen."""
        _posting(db_session, current_user)

        send = autopilot_plan.build_plan(db_session, current_user).sends[0]
        assert send.contact.email is None
        assert send.contact.source == autopilot_plan.SOURCE_SEARCH
        assert send.contact.resolved is False
        assert "needs a contact found" in (
            autopilot_plan.build_plan(db_session, current_user).summary
        )

    def test_a_previous_global_crawl_is_reused_without_touching_the_network(
        self, db_session, current_user, connected_gmail, pref
    ):
        db_session.add(
            RecruiterCache(
                company="Northwind Labs",
                domain="northwind.com",
                emails=["careers@northwind.com"],
                contacts=[
                    {
                        "email": "careers@northwind.com",
                        "title": "Talent Team",
                        "confidence": 0.8,
                        "kind": "careers_page",
                    }
                ],
                status="ok",
                scraped_at=datetime.now(UTC),
            )
        )
        db_session.commit()
        _posting(db_session, current_user)

        send = autopilot_plan.build_plan(db_session, current_user).sends[0]
        assert send.contact.email == "careers@northwind.com"
        assert send.contact.source == autopilot_plan.SOURCE_CACHED
        assert send.contact.confidence == 0.8

    def test_the_users_own_contact_beats_the_shared_cache(
        self, db_session, current_user, connected_gmail, pref
    ):
        db_session.add(
            RecruiterCache(
                company="Northwind Labs",
                domain="northwind.com",
                contacts=[{"email": "careers@northwind.com", "confidence": 0.8}],
                status="ok",
                scraped_at=datetime.now(UTC),
            )
        )
        _known_contact(db_session, current_user)
        _posting(db_session, current_user)

        send = autopilot_plan.build_plan(db_session, current_user).sends[0]
        assert send.contact.email == "talent@northwind.com"
        assert send.contact.source == autopilot_plan.SOURCE_KNOWN

    def test_a_hard_bounced_contact_is_reported_not_silently_replaced(
        self, db_session, current_user, connected_gmail, pref
    ):
        """A plan that swapped the recipient would describe an unread send."""
        _known_contact(
            db_session, current_user, delivery_state=DeliveryState.HARD_BOUNCED
        )
        _posting(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.sends == []
        assert len(plan.skipped) == 1
        assert "hard-bounced" in plan.skipped[0].reason

    def test_an_opted_out_contact_stops_the_send(
        self, db_session, current_user, connected_gmail, pref
    ):
        _known_contact(db_session, current_user, opted_out=True)
        _posting(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.sends == []
        assert "asked not to be contacted" in plan.skipped[0].reason

    def test_one_contact_does_not_get_three_of_todays_emails(
        self, db_session, current_user, connected_gmail, pref
    ):
        """Three open roles at one company is one email, as the live run does."""
        _known_contact(db_session, current_user)
        for n in range(3):
            _posting(db_session, current_user, fingerprint=f"fp-{n}", fit=88.0 - n)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert len(plan.sends) == 1
        assert sum(1 for s in plan.skipped if "already getting" in s.reason) == 2


# --------------------------------------------------------------------------- #
# The gates: the plan must agree with the pipeline                             #
# --------------------------------------------------------------------------- #


class TestGatesMatchTheLiveRun:
    def test_an_off_target_role_is_skipped_with_the_gates_own_sentence(
        self, db_session, current_user, connected_gmail, pref, resume
    ):
        from app.services import auto_apply_service, profile_service

        posting = _posting(db_session, current_user, title="Registered Nurse")
        _known_contact(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.sends == []

        target = profile_service.active_targets(db_session, current_user)[0]
        expected = auto_apply_service.relevance_gate(
            target.resume, target.targeting, posting
        )
        assert expected is not None
        assert plan.skipped[0].reason == expected

    def test_the_wrong_city_is_skipped_with_the_gates_own_sentence(
        self, db_session, current_user, connected_gmail, pref
    ):
        from app.services import auto_apply_service, profile_service

        posting = _posting(db_session, current_user, location="Tokyo, Japan")
        _known_contact(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.sends == []

        target = profile_service.active_targets(db_session, current_user)[0]
        expected = auto_apply_service.location_gate(target.targeting, posting)
        assert expected is not None
        assert plan.skipped[0].reason == expected

    def test_a_posting_below_the_threshold_never_reaches_the_plan(
        self, db_session, current_user, connected_gmail, pref
    ):
        """Same query as the live run — `candidate_postings` filters it out."""
        _posting(db_session, current_user, fit=40.0)
        _known_contact(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.sends == []
        assert plan.skipped == []


# --------------------------------------------------------------------------- #
# Budget                                                                       #
# --------------------------------------------------------------------------- #


class TestBudget:
    def test_the_budget_is_decomposed_not_a_single_number(
        self, db_session, current_user, connected_gmail, pref
    ):
        """"You capped it" and "your mailbox is warming up" need different actions."""
        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.daily_application_limit == 5
        assert plan.warmup_day_limit > 0
        assert plan.applied_today == 0
        assert plan.budget == min(plan.send_headroom, 5)

    def test_matches_past_the_budget_become_the_backlog(
        self, db_session, current_user, connected_gmail, pref
    ):
        """The honest answer to "why isn't autopilot doing more?"."""
        pref.daily_application_limit = 2
        db_session.commit()
        for n in range(5):
            _known_contact(
                db_session,
                current_user,
                company=f"Company {n}",
                email=f"talent@company{n}.com",
            )
            _posting(
                db_session,
                current_user,
                company=f"Company {n}",
                fingerprint=f"fp-{n}",
                fit=90.0 - n,
            )

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert len(plan.sends) == 2
        assert plan.backlog == 3
        # Best fit first, the same order the live run applies in.
        assert [s.fit_score for s in plan.sends] == [90.0, 89.0]

    def test_a_spent_budget_says_which_ceiling_spent_it(
        self, db_session, current_user, connected_gmail, pref, resume
    ):
        from app.models.application import Application
        from app.models.campaign import Campaign

        campaign = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
        recruiter = _known_contact(db_session, current_user)
        db_session.add(campaign)
        db_session.commit()
        pref.daily_application_limit = 1
        db_session.add(
            Application(
                user_id=current_user.id,
                campaign_id=campaign.id,
                recruiter_id=recruiter.id,
            )
        )
        db_session.commit()

        _posting(db_session, current_user, company="Other Co")
        plan = autopilot_plan.build_plan(db_session, current_user)

        assert plan.budget == 0
        assert plan.sends == []
        assert plan.backlog == 1
        assert any("budget is spent" in n for n in plan.notes)
        assert "budget is spent" in plan.summary

    def test_limit_trims_the_list_without_shrinking_the_reported_day(
        self, db_session, current_user, connected_gmail, pref
    ):
        for n in range(4):
            _known_contact(
                db_session,
                current_user,
                company=f"Company {n}",
                email=f"talent@company{n}.com",
            )
            _posting(
                db_session, current_user, company=f"Company {n}", fingerprint=f"fp-{n}"
            )

        plan = autopilot_plan.build_plan(db_session, current_user, limit=2)
        assert len(plan.sends) == 2
        assert plan.budget >= 4
        assert any("showing the first 2" in n for n in plan.notes)


# --------------------------------------------------------------------------- #
# Review mode                                                                  #
# --------------------------------------------------------------------------- #


class TestReviewMode:
    def test_review_mode_says_these_are_drafts_not_sends(
        self, db_session, current_user, connected_gmail, pref
    ):
        pref.auto_send = False
        db_session.commit()
        _known_contact(db_session, current_user)
        _posting(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.auto_send is False
        assert plan.sends_unreviewed is False
        assert plan.sends[0].sends_unreviewed is False
        assert "review queue" in " ".join(plan.notes)
        assert "drafts" in plan.summary and "approval" in plan.summary

    def test_a_pause_is_reported_with_the_policys_own_reason(
        self, db_session, current_user, connected_gmail, pref
    ):
        from app.services import send_policy

        send_policy.pause(pref, 6)
        db_session.commit()
        _known_contact(db_session, current_user)
        _posting(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.auto_send is True
        assert plan.sends_unreviewed is False
        assert "paused" in plan.send_policy_reason.lower()

    def test_an_unfinished_trial_holds_the_plan_in_review(
        self, db_session, current_user, connected_gmail, pref
    ):
        pref.auto_send_trial_approvals = 3
        db_session.commit()
        _known_contact(db_session, current_user)
        _posting(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.sends_unreviewed is False
        assert "auto-send takes over" in plan.send_policy_reason


# --------------------------------------------------------------------------- #
# It must not do anything                                                      #
# --------------------------------------------------------------------------- #


class TestSideEffectFree:
    def test_the_plan_writes_nothing(
        self, db_session, current_user, connected_gmail, pref
    ):
        """A dry run that created what it described is a live run, renamed."""
        from app.models.application import Application
        from app.models.email import Email
        from app.models.tailored_resume import TailoredResume

        posting = _posting(db_session, current_user)
        _known_contact(db_session, current_user)

        before = (
            db_session.query(Application).count(),
            db_session.query(Email).count(),
            db_session.query(TailoredResume).count(),
            posting.status,
            posting.applied_at,
            posting.screened_out_at,
            pref.applications_created,
            pref.last_run_at,
        )
        autopilot_plan.build_plan(db_session, current_user)
        db_session.expire_all()
        posting = db_session.get(JobPosting, posting.id)
        after = (
            db_session.query(Application).count(),
            db_session.query(Email).count(),
            db_session.query(TailoredResume).count(),
            posting.status,
            posting.applied_at,
            posting.screened_out_at,
            pref.applications_created,
            pref.last_run_at,
        )
        assert before == after

    def test_a_skipped_posting_is_not_screened_out(
        self, db_session, current_user, connected_gmail, pref
    ):
        """The live run retires a rejected posting. A dry run must not — the user
        has not decided anything yet, and a preview that narrowed tomorrow's feed
        would charge them for looking."""
        posting = _posting(db_session, current_user, title="Registered Nurse")
        _known_contact(db_session, current_user)

        autopilot_plan.build_plan(db_session, current_user)
        db_session.expire_all()
        assert db_session.get(JobPosting, posting.id).screened_out_at is None

    def test_it_does_not_cache_a_resolved_timezone_onto_a_recruiter(
        self, db_session, current_user, connected_gmail, pref
    ):
        recruiter = _known_contact(db_session, current_user)
        assert recruiter.timezone is None
        _posting(db_session, current_user)

        autopilot_plan.build_plan(db_session, current_user)
        db_session.expire_all()
        assert db_session.get(Recruiter, recruiter.id).timezone is None

    def test_it_never_crawls(self, db_session, current_user, connected_gmail, pref, monkeypatch):
        """The live path would crawl a careers page here; the plan must not."""
        from app.services import career_scraper

        def _explode(*args, **kwargs):  # pragma: no cover - the assertion is that this never runs
            raise AssertionError("the dry run must not reach the network")

        monkeypatch.setattr(career_scraper, "get_or_scrape", _explode)
        monkeypatch.setattr(career_scraper, "scrape_company", _explode)
        _posting(db_session, current_user)

        plan = autopilot_plan.build_plan(db_session, current_user)
        assert plan.sends[0].contact.source == autopilot_plan.SOURCE_SEARCH


# --------------------------------------------------------------------------- #
# Determinism                                                                  #
# --------------------------------------------------------------------------- #


class TestDeterminism:
    def test_two_reads_agree_on_the_send_times(
        self, db_session, current_user, connected_gmail, pref
    ):
        """Jitter in a plan means a refresh contradicts the plan it replaces."""
        for n in range(3):
            _known_contact(
                db_session,
                current_user,
                company=f"Company {n}",
                email=f"talent@company{n}.com",
            )
            _posting(
                db_session, current_user, company=f"Company {n}", fingerprint=f"fp-{n}"
            )

        first = autopilot_plan.build_plan(db_session, current_user)
        second = autopilot_plan.build_plan(db_session, current_user)

        assert [s.job_posting_id for s in first.sends] == [
            s.job_posting_id for s in second.sends
        ]
        # Within a minute: the projections are anchored on "now", not identical
        # clocks, and a plan that promised the second would be lying anyway.
        for a, b in zip(first.sends, second.sends, strict=True):
            assert abs((a.scheduled_at - b.scheduled_at).total_seconds()) < 60

    def test_sends_are_spaced_and_land_in_business_hours(
        self, db_session, current_user, connected_gmail, pref
    ):
        from app.core.config import settings

        pref.locations = ["New York"]
        db_session.commit()
        for n in range(3):
            _known_contact(
                db_session,
                current_user,
                company=f"Company {n}",
                email=f"talent@company{n}.com",
            )
            _posting(
                db_session,
                current_user,
                company=f"Company {n}",
                location="New York, NY",
                fingerprint=f"fp-{n}",
            )

        plan = autopilot_plan.build_plan(db_session, current_user)
        times = [s.scheduled_at for s in plan.sends]
        assert times == sorted(times), "a plan that reorders itself is not a schedule"

        zone = plan.sends[0].timezone
        assert zone == "America/New_York"
        for send in plan.sends:
            local = send.scheduled_at.astimezone(
                __import__("zoneinfo").ZoneInfo(send.timezone)
            )
            assert local.weekday() < 5
            assert (
                settings.send_window_start_hour
                <= local.hour
                < settings.send_window_end_hour
            )


# --------------------------------------------------------------------------- #
# The endpoint                                                                 #
# --------------------------------------------------------------------------- #


class TestEndpoint:
    def test_the_payload_carries_the_plan(
        self, auth_client, db_session, current_user, connected_gmail, pref
    ):
        _known_contact(db_session, current_user)
        _posting(db_session, current_user)

        body = auth_client.get("/api/v1/autopilot/dry-run").json()

        assert body["active"] is True
        assert body["window_hours"] == 24
        assert len(body["sends"]) == 1
        send = body["sends"][0]
        assert send["contact"]["email"] == "talent@northwind.com"
        assert send["reasons"]
        assert send["scheduled_at"]
        assert body["summary"]

    def test_limit_is_bounded_by_the_schema(self, auth_client):
        assert auth_client.get("/api/v1/autopilot/dry-run?limit=0").status_code == 422
        assert auth_client.get("/api/v1/autopilot/dry-run?limit=99").status_code == 422

    def test_it_requires_authentication(self, client):
        assert client.get("/api/v1/autopilot/dry-run").status_code == 401
