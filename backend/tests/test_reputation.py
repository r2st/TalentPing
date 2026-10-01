"""Sender-reputation guardrails: warm-up ramp, daily caps, bounce/complaint pauses.

Pure unit tests over the service — no DB, no network. GmailAccount rows are built
in memory just to carry the counters the service reads.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.models.gmail_account import GmailAccount
from app.services import reputation_service as rep

NOW = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)


def _account(**overrides) -> GmailAccount:
    defaults = dict(
        email="candidate@gmail.com",
        google_sub="sub",
        refresh_token_encrypted="x",
        status="connected",
        sent_total=0,
        bounce_count=0,
        complaint_count=0,
    )
    defaults.update(overrides)
    return GmailAccount(**defaults)


class TestWarmupRamp:
    """5 -> 10 -> 15 -> 20 -> ceiling, in three-day steps, over two weeks."""

    def test_brand_new_mailbox_starts_at_five_a_day(self):
        acct = _account(warmup_started_at=None)
        assert rep.warmup_day_limit(acct, now=NOW) == 5

    def test_ramp_grows_step_by_step(self):
        expectations = {0: 5, 2: 5, 3: 10, 5: 10, 6: 15, 9: 20, 14: settings.daily_send_limit}
        for days, limit in expectations.items():
            acct = _account(warmup_started_at=NOW - timedelta(days=days))
            assert rep.warmup_day_limit(acct, now=NOW) == limit, days

    def test_the_last_step_is_held_until_the_ramp_is_over(self):
        """The schedule runs out at day 12; the ceiling still waits for day 14."""
        acct = _account(warmup_started_at=NOW - timedelta(days=12))
        assert rep.warmup_day_limit(acct, now=NOW) == 20
        assert rep.warmup_day_limit(acct, now=NOW + timedelta(days=2)) == (
            settings.daily_send_limit
        )

    def test_warm_mailbox_uses_configured_ceiling(self):
        acct = _account(warmup_started_at=NOW - timedelta(days=40))
        assert rep.warmup_day_limit(acct, now=NOW) == settings.daily_send_limit

    def test_ramp_never_exceeds_configured_ceiling(self, monkeypatch):
        """A deployment capping sends at 4/day gets 4 on day one, not the schedule's 5."""
        monkeypatch.setattr(settings, "daily_send_limit", 4)
        for days in (0, 9, 21):
            acct = _account(warmup_started_at=NOW - timedelta(days=days))
            assert rep.warmup_day_limit(acct, now=NOW) == 4, days

    def test_a_malformed_schedule_falls_back_to_the_default(self):
        assert rep._parse_schedule("not,a,schedule") == (5, 10, 15, 20)
        assert rep._parse_schedule("") == (5, 10, 15, 20)
        assert rep._parse_schedule("5,-3,10") == (5, 10, 15, 20)
        assert rep._parse_schedule("2, 4 ,6") == (2, 4, 6)

    def test_is_warming_up_flips_at_the_ceiling(self):
        assert rep.is_warming_up(_account(warmup_started_at=NOW), now=NOW) is True
        old = _account(warmup_started_at=NOW - timedelta(days=30))
        assert rep.is_warming_up(old, now=NOW) is False


class TestDailyCounter:
    def test_a_new_utc_day_zeroes_the_count(self):
        acct = _account(
            daily_send_count=9, daily_count_reset_at=NOW - timedelta(days=1)
        )
        rep.roll_daily_counter(acct, now=NOW)
        assert acct.daily_send_count == 0
        assert acct.daily_count_reset_at == NOW.replace(hour=0, minute=0, second=0, microsecond=0)

    def test_the_same_day_leaves_the_count_alone(self):
        day_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        acct = _account(daily_send_count=9, daily_count_reset_at=day_start)
        rep.roll_daily_counter(acct, now=NOW)
        assert acct.daily_send_count == 9

    def test_a_null_reset_marker_rolls_immediately(self):
        """Every row predating this feature has a null marker and a stale count."""
        acct = _account(daily_send_count=412, daily_count_reset_at=None)
        rep.roll_daily_counter(acct, now=NOW)
        assert acct.daily_send_count == 0

    def test_record_send_rolls_before_incrementing(self):
        acct = _account(
            daily_send_count=17, daily_count_reset_at=NOW - timedelta(days=1)
        )
        rep.record_send(acct, now=NOW)
        assert acct.daily_send_count == 1  # not 18

    def test_record_send_increments_within_a_day(self):
        day_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        acct = _account(daily_send_count=2, daily_count_reset_at=day_start)
        rep.record_send(acct, now=NOW)
        assert acct.daily_send_count == 3


class TestWarmupProgress:
    def test_reports_the_next_increase_mid_ramp(self):
        acct = _account(warmup_started_at=NOW - timedelta(days=4))
        progress = rep.warmup_progress(acct, now=NOW)
        assert progress["warming_up"] is True
        assert progress["day"] == 4
        assert progress["day_limit"] == 10
        assert progress["next_limit"] == 15
        assert progress["next_increase_in_days"] == 2  # day 6

    def test_a_warm_mailbox_reports_no_next_step(self):
        acct = _account(warmup_started_at=NOW - timedelta(days=30))
        progress = rep.warmup_progress(acct, now=NOW)
        assert progress["warming_up"] is False
        assert progress["next_limit"] is None
        assert progress["full_limit_at"] is None


class TestEvaluate:
    def test_allows_a_fresh_mailbox_under_the_cap(self):
        decision = rep.evaluate(_account(), sent_last_24h=4, now=NOW)
        assert decision.allowed and decision.day_limit == 5

    def test_blocks_once_the_daily_cap_is_hit(self):
        decision = rep.evaluate(_account(), sent_last_24h=5, now=NOW)
        assert not decision.allowed
        assert "Daily send limit" in decision.reason
        assert decision.retry_after == timedelta(hours=1)

    def test_no_account_is_never_allowed(self):
        assert rep.evaluate(None, sent_last_24h=0, now=NOW).allowed is False

    def test_an_active_pause_blocks_even_under_the_cap(self):
        acct = _account(paused_until=NOW + timedelta(hours=5), pause_reason="cooling down")
        decision = rep.evaluate(acct, sent_last_24h=0, now=NOW)
        assert not decision.allowed
        assert decision.reason == "cooling down"
        assert decision.retry_after == timedelta(hours=5)

    def test_an_expired_pause_is_ignored(self):
        acct = _account(paused_until=NOW - timedelta(hours=1))
        assert rep.evaluate(acct, sent_last_24h=0, now=NOW).allowed is True


class TestBounceGuardrail:
    def test_a_single_bounce_at_low_volume_does_not_pause(self):
        acct = _account(sent_total=5, bounce_count=0)
        tripped = rep.record_bounce(acct, now=NOW)
        assert tripped is False and acct.paused_until is None

    def test_high_bounce_rate_at_volume_pauses_the_mailbox(self):
        # 20 sent, already 1 bounce; a second pushes the rate to 10% (> 5%).
        acct = _account(sent_total=20, bounce_count=1)
        tripped = rep.record_bounce(acct, now=NOW)
        assert tripped is True
        assert acct.paused_until == NOW + rep.BOUNCE_COOLDOWN
        assert "bounce rate" in acct.pause_reason.lower()

    def test_evaluate_respects_a_bounce_pause(self):
        acct = _account(sent_total=20, bounce_count=1)
        rep.record_bounce(acct, now=NOW)
        assert rep.evaluate(acct, sent_last_24h=0, now=NOW).allowed is False


class TestComplaintGuardrail:
    def test_a_single_complaint_pauses_for_three_days(self):
        acct = _account(sent_total=50)
        rep.record_complaint(acct, now=NOW)
        assert acct.complaint_count == 1
        assert acct.paused_until == NOW + rep.COMPLAINT_COOLDOWN
        assert rep.evaluate(acct, sent_last_24h=0, now=NOW).allowed is False


class TestRecordSend:
    def test_first_send_starts_the_warmup_clock(self):
        acct = _account(warmup_started_at=None, sent_total=0)
        rep.record_send(acct, now=NOW)
        assert acct.sent_total == 1
        assert acct.warmup_started_at == NOW

    def test_later_sends_do_not_reset_the_clock(self):
        started = NOW - timedelta(days=3)
        acct = _account(warmup_started_at=started, sent_total=9)
        rep.record_send(acct, now=NOW)
        assert acct.sent_total == 10
        assert acct.warmup_started_at == started


def test_status_summary_reports_the_headline_numbers():
    acct = _account(
        warmup_started_at=NOW - timedelta(days=7), sent_total=40, bounce_count=1
    )
    summary = rep.status_summary(acct, now=NOW)
    assert summary["day_limit"] == 15  # day 7 -> step 2
    assert summary["warming_up"] is True
    assert summary["bounce_rate"] == round(1 / 40, 3)
    assert summary["paused"] is False
    # The warm-up block the setup panel reads, embedded rather than a new route.
    assert summary["warmup"]["day"] == 7
    assert summary["warmup"]["next_limit"] == 20
