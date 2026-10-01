"""Subject-line A/B testing: generation, assignment, attribution, convergence.

Generation is tested through the deterministic path (no API key in the test env)
and through a stubbed provider, because the free-tier models this runs on return
their scratchpad about a third of the time and the fallback is the behaviour that
actually ships.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.models.application import Application, ApplicationStatus
from app.models.campaign import Campaign
from app.models.email import Email, EmailDirection, EmailStatus
from app.models.email_thread import EmailThread
from app.models.recruiter import Recruiter
from app.models.subject_variant import SubjectVariant
from app.services import email_tracking, subject_ab_service as svc
from app.services.ai_composer import CandidateContext, RecruiterContext


@pytest.fixture()
def cand() -> CandidateContext:
    return CandidateContext(
        name="Jordan Candidate",
        headline="Senior Backend Engineer",
        seniority="senior",
        skills=["python", "fastapi"],
        target_roles=["Backend Engineer"],
    )


@pytest.fixture()
def rec() -> RecruiterContext:
    return RecruiterContext(name="Sam", company="Acme")


@pytest.fixture()
def campaign(db_session, current_user, resume) -> Campaign:
    row = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
    db_session.add(row)
    db_session.commit()
    return row


def _variants(db, campaign, *, count=3, **overrides) -> list[SubjectVariant]:
    rows = []
    for i in range(count):
        row = SubjectVariant(
            user_id=campaign.user_id,
            campaign_id=campaign.id,
            label=svc.LABELS[i],
            text=f"Subject {svc.LABELS[i]}",
            **overrides,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


class TestGeneration:
    def test_no_api_key_yields_deterministic_variants(self, cand, rec):
        variants, source = svc.generate_variants(cand, rec)
        assert source == "template"
        assert len(variants) == 3
        assert len(set(variants)) == 3
        # Variant A is the pre-feature subject, deliberately kept as the control.
        assert variants[0] == "Jordan Candidate — Backend Engineer"

    def test_seniority_is_not_repeated_when_the_role_already_carries_it(self, rec):
        """Otherwise: 'a senior Senior Backend Engineer'."""
        cand = CandidateContext(
            name="Jordan",
            seniority="senior",
            target_roles=["Senior Backend Engineer"],
        )
        variants, _ = svc.generate_variants(cand, rec)
        assert "senior Senior" not in " ".join(variants)

    def test_a_stubbed_provider_produces_llm_variants(self, cand, rec, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **kw: '{"variants": ["First one", "Second one", "Third one"]}',
        )
        variants, source = svc.generate_variants(cand, rec)
        assert source == "llm"
        assert variants == ["First one", "Second one", "Third one"]

    def test_duplicates_and_blanks_are_dropped(self, cand, rec, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **kw: '{"variants": ["Same", "same", "  ", null, "Other"]}',
        )
        variants, source = svc.generate_variants(cand, rec)
        assert source == "llm"
        assert variants == ["Same", "Other"]

    def test_a_leading_subject_prefix_is_stripped(self, cand, rec, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **kw: '{"variants": ["Subject: Real one", "Another"]}',
        )
        variants, _ = svc.generate_variants(cand, rec)
        assert variants[0] == "Real one"

    def test_only_one_usable_line_falls_back(self, cand, rec, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(
            svc, "chat_completion", lambda *a, **kw: '{"variants": ["Just one"]}'
        )
        _, source = svc.generate_variants(cand, rec)
        assert source == "template"

    def test_chain_of_thought_output_falls_back(self, cand, rec, monkeypatch):
        """The free reasoning models hand back their scratchpad routinely."""
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **kw: (
                '{"variants": ["We need to write a subject line. The user wants '
                'something short", "Okay, so the candidate is a backend engineer '
                'and we must be brief"]}'
            ),
        )
        _, source = svc.generate_variants(cand, rec)
        assert source == "template"

    def test_a_dead_provider_falls_back(self, cand, rec, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")

        def _boom(*a, **kw):
            raise svc.OpenRouterError("all providers failed")

        monkeypatch.setattr(svc, "chat_completion", _boom)
        _, source = svc.generate_variants(cand, rec)
        assert source == "template"

    def test_unparseable_output_falls_back(self, cand, rec, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(svc, "chat_completion", lambda *a, **kw: "not json at all")
        _, source = svc.generate_variants(cand, rec)
        assert source == "template"

    def test_a_very_long_subject_is_truncated(self, cand, rec, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(
            svc,
            "chat_completion",
            lambda *a, **kw: '{"variants": ["' + "x" * 500 + '", "Other"]}',
        )
        variants, _ = svc.generate_variants(cand, rec)
        assert len(variants[0]) == svc.MAX_SUBJECT_LENGTH


class TestEnsureVariants:
    def test_creates_labelled_rows_once(self, db_session, campaign, cand, rec):
        first = svc.ensure_variants(db_session, campaign, cand, rec)
        db_session.commit()
        assert [v.label for v in first] == ["A", "B", "C"]
        assert all(v.user_id == campaign.user_id for v in first)

        again = svc.ensure_variants(db_session, campaign, cand, rec)
        assert [v.id for v in again] == [v.id for v in first]
        assert db_session.query(SubjectVariant).count() == 3


class TestAssignment:
    def test_every_arm_is_used_when_there_is_no_winner(self, db_session, campaign):
        variants = _variants(db_session, campaign)
        rng = random.Random(42)
        picked = {svc.assign_variant(variants, rng=rng).label for _ in range(60)}
        assert picked == {"A", "B", "C"}

    def test_a_winner_takes_most_but_not_all_of_the_traffic(
        self, db_session, campaign
    ):
        """Keeping 10% exploring is what lets a thin-sample winner be overturned."""
        variants = _variants(db_session, campaign)
        variants[0].is_winner = True
        for v in variants[1:]:
            v.is_active = False
        db_session.commit()

        rng = random.Random(7)
        picks = [svc.assign_variant(variants, rng=rng).label for _ in range(1000)]
        winner_share = picks.count("A") / len(picks)
        assert 0.85 < winner_share < 0.95
        assert set(picks) == {"A", "B", "C"}

    def test_no_variants_assigns_nothing(self):
        assert svc.assign_variant([]) is None

    def test_all_arms_retired_still_assigns(self, db_session, campaign):
        variants = _variants(db_session, campaign, is_active=False)
        db_session.commit()
        assert svc.assign_variant(variants) is not None


class TestAttribution:
    def test_counters_move_independently(self, db_session, campaign):
        variant = _variants(db_session, campaign, count=1)[0]
        svc.record_send(db_session, variant.id)
        svc.record_open(db_session, variant.id)
        svc.record_reply(db_session, variant.id)
        db_session.commit()

        assert (variant.sends, variant.opens, variant.replies) == (1, 1, 1)
        assert variant.open_rate == 1.0
        assert variant.reply_rate == 1.0

    def test_a_null_variant_id_is_a_no_op(self, db_session):
        svc.record_send(db_session, None)  # must not raise

    def test_rates_are_zero_without_sends(self, db_session, campaign):
        variant = _variants(db_session, campaign, count=1)[0]
        assert variant.open_rate == 0.0 and variant.reply_rate == 0.0

    def test_only_the_first_open_is_credited(self, db_session, current_user, resume):
        """One enthusiastic re-reader must not win the experiment."""
        campaign = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
        recruiter = Recruiter(user_id=current_user.id, email="t@acme.com")
        db_session.add_all([campaign, recruiter])
        db_session.flush()
        variant = SubjectVariant(
            user_id=current_user.id, campaign_id=campaign.id, label="A", text="S"
        )
        db_session.add(variant)
        db_session.flush()
        application = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=ApplicationStatus.OUTREACH_SENT,
        )
        db_session.add(application)
        db_session.flush()
        thread = EmailThread(application_id=application.id, subject="S")
        db_session.add(thread)
        db_session.flush()
        email = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.SENT,
            subject="S",
            body_text="b",
            tracking_token="tok-1",
            subject_variant_id=variant.id,
            sent_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        db_session.add(email)
        db_session.commit()

        now = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
        email_tracking.record_open(db_session, email, now=now)
        email_tracking.record_open(
            db_session,
            email,
            now=now + email_tracking.DEDUPE_WINDOW + timedelta(seconds=1),
        )
        db_session.commit()

        assert email.open_count == 2
        assert variant.opens == 1

    def test_a_prefetch_open_is_not_credited(self, db_session, current_user, resume):
        """A proxy fetch fires for every delivered message and scores every arm."""
        campaign = Campaign(user_id=current_user.id, name="c", resume_id=resume.id)
        recruiter = Recruiter(user_id=current_user.id, email="t2@acme.com")
        db_session.add_all([campaign, recruiter])
        db_session.flush()
        variant = SubjectVariant(
            user_id=current_user.id, campaign_id=campaign.id, label="A", text="S"
        )
        db_session.add(variant)
        db_session.flush()
        application = Application(
            user_id=current_user.id,
            campaign_id=campaign.id,
            recruiter_id=recruiter.id,
            status=ApplicationStatus.OUTREACH_SENT,
        )
        db_session.add(application)
        db_session.flush()
        thread = EmailThread(application_id=application.id, subject="S")
        db_session.add(thread)
        db_session.flush()
        email = Email(
            thread_id=thread.id,
            direction=EmailDirection.SENT,
            status=EmailStatus.SENT,
            subject="S",
            body_text="b",
            tracking_token="tok-2",
            subject_variant_id=variant.id,
            sent_at=datetime.now(UTC),
        )
        db_session.add(email)
        db_session.commit()

        email_tracking.record_open(db_session, email)
        db_session.commit()
        assert variant.opens == 0


class TestConvergence:
    def _loaded(self, db, campaign, rates: list[tuple[int, int]]):
        variants = _variants(db, campaign, count=len(rates))
        for variant, (sends, opens) in zip(variants, rates, strict=True):
            variant.sends, variant.opens = sends, opens
        db.commit()
        return variants

    def test_nothing_is_decided_below_the_minimum_sample(self, db_session, campaign):
        self._loaded(db_session, campaign, [(5, 5), (5, 0), (5, 0)])
        assert svc.maybe_converge(db_session, campaign) is None

    def test_nothing_is_decided_without_enough_lift(self, db_session, campaign):
        # 50% vs 45% — inside the noise at this sample size.
        self._loaded(db_session, campaign, [(20, 10), (20, 9), (20, 9)])
        assert svc.maybe_converge(db_session, campaign) is None

    def test_a_clear_leader_wins_and_retires_the_rest(self, db_session, campaign):
        variants = self._loaded(db_session, campaign, [(20, 12), (20, 4), (20, 3)])
        winner = svc.maybe_converge(db_session, campaign)
        db_session.commit()

        assert winner is not None and winner.label == "A"
        assert winner.is_winner is True and winner.is_active is True
        assert all(not v.is_active for v in variants[1:])
        # Retired arms keep their counters — the evidence for the choice.
        assert variants[1].sends == 20

    def test_converging_twice_is_stable(self, db_session, campaign):
        self._loaded(db_session, campaign, [(20, 12), (20, 4), (20, 3)])
        first = svc.maybe_converge(db_session, campaign)
        db_session.commit()
        again = svc.maybe_converge(db_session, campaign)
        assert again.id == first.id

    def test_a_single_arm_never_converges(self, db_session, campaign):
        self._loaded(db_session, campaign, [(50, 40)])
        assert svc.maybe_converge(db_session, campaign) is None


class TestStatsEndpoint:
    def test_reports_the_experiment_and_the_confidence_flag(
        self, auth_client, db_session, campaign
    ):
        variants = _variants(db_session, campaign)
        variants[0].sends, variants[0].opens = 4, 2
        db_session.commit()

        body = auth_client.get("/api/v1/analytics/subject-variants").json()
        assert len(body) == 1
        assert body[0]["campaign_id"] == campaign.id
        assert body[0]["confident"] is False  # thin sample
        assert [v["label"] for v in body[0]["variants"]] == ["A", "B", "C"]
        assert body[0]["variants"][0]["open_rate"] == 0.5

    def test_confident_once_every_arm_has_enough_impressions(
        self, auth_client, db_session, campaign
    ):
        for variant in _variants(db_session, campaign):
            variant.sends = svc.MIN_SENDS_PER_VARIANT
        db_session.commit()

        body = auth_client.get("/api/v1/analytics/subject-variants").json()
        assert body[0]["confident"] is True

    def test_another_users_campaign_is_not_reachable(
        self, auth_client, db_session, current_user
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        theirs = Campaign(user_id=other.id, name="theirs")
        db_session.add(theirs)
        db_session.commit()

        resp = auth_client.get(
            f"/api/v1/analytics/subject-variants?campaign_id={theirs.id}"
        )
        assert resp.status_code == 404

    def test_requires_authentication(self, client):
        assert client.get("/api/v1/analytics/subject-variants").status_code == 401


class TestPipelineIntegration:
    def test_campaign_emails_carry_an_assigned_variant(
        self, auth_client, db_session, connected_gmail, resume, stub_scraper
    ):
        auth_client.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})

        variants = db_session.query(SubjectVariant).all()
        assert len(variants) == 3
        emails = db_session.query(Email).all()
        assert emails
        for email in emails:
            assert email.subject_variant_id in {v.id for v in variants}
            assert email.subject in {v.text for v in variants}

    def test_the_experiment_can_be_switched_off(
        self, auth_client, db_session, connected_gmail, resume, stub_scraper, monkeypatch
    ):
        monkeypatch.setattr(settings, "subject_ab_enabled", False)
        auth_client.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})

        assert db_session.query(SubjectVariant).count() == 0
        email = db_session.query(Email).first()
        assert email.subject_variant_id is None
        assert "Jordan Candidate" in email.subject
