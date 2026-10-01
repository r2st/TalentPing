"""Sender-reputation protection for personal-mailbox outreach.

TalentPing sends from the candidate's *own* Gmail, so their personal deliverability
is on the line. A brand-new inbox that suddenly emails 30 strangers a day looks —
to Gmail's own spam systems and to the receiving servers — like a compromised or
purchased account. So sending is governed here, not by a flat cap:

* **Warm-up ramp** — the allowance grows with the mailbox's age in the system:
  5/day for three days, then 10, then 15, then 20, reaching the configured
  ceiling at day 14. Research §3.2 / roadmap improvement 6, reshaped in
  docs/features/email-warmup.md.
* **Bounce guardrail** — if the bounce rate crosses 5% (once there's enough volume
  to be meaningful), sending is paused for a cool-down window instead of digging
  the reputation hole deeper.
* **Spam-complaint guardrail** — any complaint at all triggers a longer pause; a
  single "marked as spam" is a strong negative signal for a cold-outreach sender.

Everything is computed off the ``GmailAccount`` row (``warmup_started_at``,
``sent_total``, ``bounce_count``, ``complaint_count``, ``paused_until``) plus a
rolling 24h count the caller passes in — no extra state, no background clock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.pii import mask_email
from app.models.gmail_account import GmailAccount

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE: tuple[int, ...] = (5, 10, 15, 20)


def _parse_schedule(raw: str | None) -> tuple[int, ...]:
    """Parse ``"5,10,15,20"`` into a step schedule, defaulting on nonsense.

    A malformed setting must not stop the app booting — it degrades to the
    documented default and says so, which is the same posture every other
    optional setting in this codebase takes.

    **A schedule that ever falls is rejected, not honoured.** The values here are
    read as a ramp — the module's whole promise is that the allowance "grows with
    the mailbox's age" — and nothing downstream re-checks that. So
    ``WARMUP_SCHEDULE=20,10,5`` was accepted verbatim and inverted the mechanism:
    the mailbox opened at 20/day on the day it was coldest and was *throttled to
    5* by the time it had earned trust, then jumped to the full ceiling at
    ``WARMUP_RAMP_DAYS``. That is worse than having no ramp at all, and it is a
    plausible typo rather than an exotic one — the schedule is written
    highest-first in plenty of other tools.

    Equal neighbours are fine: ``5,5,10`` holds a step for twice as long, which
    is a real thing to want. Only a *decrease* is refused.
    """
    try:
        parsed = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except (AttributeError, ValueError):
        parsed = ()
    if not parsed or any(step <= 0 for step in parsed):
        logger.warning("invalid WARMUP_SCHEDULE %r; using 5,10,15,20", raw)
        return DEFAULT_SCHEDULE
    if any(later < earlier for earlier, later in zip(parsed, parsed[1:], strict=False)):
        logger.warning(
            "WARMUP_SCHEDULE %r decreases; a warm-up ramp only ever rises. "
            "Using 5,10,15,20",
            raw,
        )
        return DEFAULT_SCHEDULE
    return parsed


# Step-by-step warm-up ceiling. Index 0 is the first ``WARMUP_STEP_DAYS`` days.
# Past the last entry — and past ``WARMUP_RAMP_DAYS`` — the mailbox is "warm"
# and uses the configured ``daily_send_limit``.
WARMUP_SCHEDULE: tuple[int, ...] = _parse_schedule(settings.warmup_schedule)
WARMUP_STEP_DAYS: int = max(1, settings.warmup_step_days)
# The earliest day the full ceiling applies, even once the schedule is
# exhausted. With 4 steps of 3 days the schedule runs out at day 12; holding to
# day 14 is what makes "ramps up over two weeks" literal rather than an accident
# of how the step arithmetic divides.
WARMUP_RAMP_DAYS: int = max(0, settings.warmup_ramp_days)

# Bounce rate above which sending pauses. Below this many sends we don't have the
# volume to judge, so the guardrail stays off (one bounce out of two is noise).
BOUNCE_RATE_LIMIT = 0.05
MIN_VOLUME_FOR_RATE = 20

# Cool-downs. A hard bounce problem is recoverable; a spam complaint is graver.
BOUNCE_COOLDOWN = timedelta(hours=24)
COMPLAINT_COOLDOWN = timedelta(days=3)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips naive datetimes; the arithmetic here needs tz-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def effective_start(account: GmailAccount) -> datetime | None:
    """Where the ramp's clock actually stands: first send, plus time held.

    Two columns, because two different things write them and only one of them
    is a fact about the address:

    * ``warmup_started_at`` — when this address first sent. Written by
      :func:`record_send`, and re-derived from the ``emails`` table by
      :func:`adopt_send_history` whenever the row is rebuilt.
    * ``warmup_hold_seconds`` — how long it has spent under a reputation hold.
      Written only by :func:`_hold_until`.

    The penalty used to be applied by pushing ``warmup_started_at`` forward,
    which made one column mean both. That is not a stylistic problem: the two
    writers move it in opposite directions, and the restorer runs on a
    schedule. ``reconcile_warmup_ramps`` sweeps every mailbox every six hours
    and resets the clock to the first real send — so a three-day complaint hold
    kept its ramp penalty for at most six hours, then silently lost it, and the
    mailbox emerged from the hold promoted for the days it had spent forbidden
    to send. Exactly the outcome :func:`_hold_until` exists to prevent, undone
    by the function whose docstring promises it "only ever moves the clock
    earlier".

    Keeping them apart makes both true at once: history is restored freely, and
    the penalty rides on top of whatever history says.
    """
    started = _aware(account.warmup_started_at)
    if started is None:
        return None
    return started + timedelta(seconds=max(0, account.warmup_hold_seconds or 0))


def warmup_day(account: GmailAccount, *, now: datetime | None = None) -> int:
    """Days of ramp this mailbox has earned. 0 for one that has never sent.

    Counted from :func:`effective_start`, so time under a hold is not time
    served on the ramp.
    """
    started = effective_start(account)
    if started is None:
        return 0
    return max(0, ((now or datetime.now(UTC)) - started).days)


def limit_for_day(day: int) -> int:
    """The ramp's ceiling on a given day of a mailbox's sending life.

    Split out from :func:`warmup_day_limit` so the ramp can be asked about a day
    that is not *this* mailbox's today. :func:`warmup_progress` needs exactly
    that to say "rising to 10 in two days", and computing it by handing
    ``warmup_day_limit`` a fabricated ``now`` only works for a mailbox whose
    clock has already started — the case where the projection is least needed.
    """
    day = max(0, day)
    step = day // WARMUP_STEP_DAYS

    if step >= len(WARMUP_SCHEDULE):
        # The schedule is exhausted, but the ramp isn't over until day
        # WARMUP_RAMP_DAYS — hold at the last step until then.
        limit = (
            settings.daily_send_limit
            if day >= WARMUP_RAMP_DAYS
            else WARMUP_SCHEDULE[-1]
        )
    else:
        limit = WARMUP_SCHEDULE[step]
    return min(limit, settings.daily_send_limit)


def full_limit_day() -> int:
    """The first day of a mailbox's sending life at which the ramp is over.

    Not ``WARMUP_RAMP_DAYS``. The two agree only while the schedule finishes
    inside that window, which the defaults do — four steps of three days is
    twelve, held at the last rung until fourteen — and which a deployment can
    change with one environment variable. :func:`limit_for_day` holds the last
    step until ``WARMUP_RAMP_DAYS`` *and* honours the schedule for as long as
    the schedule runs, so ``WARMUP_SCHEDULE=5,10,15,20,25,30`` in three-day
    steps is still climbing on day fifteen with a fourteen-day window behind it.

    Reading the completion date off the window alone put it in the *past* for
    exactly those days: ``warmup_progress`` reported ``warming_up: True`` beside
    a ``full_limit_at`` of yesterday, which is the failure that field exists to
    avoid arriving from the other direction. It also overstated the wait
    whenever the schedule reaches the ceiling early — with the ramp's last rung
    equal to ``daily_send_limit`` the mailbox is warm on day nine, and the panel
    promised five more days of throttling that were never going to happen.

    So the day is derived from the ramp itself: the first one whose allowance is
    the configured ceiling. The search is bounded by the later of the window and
    the schedule's own length, past which :func:`limit_for_day` returns the
    ceiling by construction.
    """
    horizon = max(WARMUP_RAMP_DAYS, len(WARMUP_SCHEDULE) * WARMUP_STEP_DAYS)
    for day in range(horizon + 1):
        if limit_for_day(day) >= settings.daily_send_limit:
            return day
    return horizon  # pragma: no cover - limit_for_day tops out inside the horizon


def warmup_day_limit(account: GmailAccount, *, now: datetime | None = None) -> int:
    """The warm-up ceiling for *account* today, before the rolling-count check.

    A mailbox with no recorded warm-up start is treated as brand new (step 0) —
    unknown age is the risky case, so assume the risky answer. Never exceeds the
    configured ``daily_send_limit``: a deployment that caps sending at 4/day gets
    4 on day one, not the schedule's 5.
    """
    return limit_for_day(warmup_day(account, now=now or datetime.now(UTC)))


def is_warming_up(account: GmailAccount, *, now: datetime | None = None) -> bool:
    """True while the mailbox is still below the configured ceiling."""
    return warmup_day_limit(account, now=now) < settings.daily_send_limit


def roll_daily_counter(account: GmailAccount, *, now: datetime | None = None) -> None:
    """Zero ``daily_send_count`` when the UTC calendar day has turned over.

    This column has existed since the first migration and was incremented on
    every send but never reset — a lifetime counter wearing a daily counter's
    name. Nothing enforced against it (the gate is a rolling-24h query over
    ``emails``), so it has been a wrong number on the reputation panel rather
    than a live bug; it is now a right one.

    Rollover is on the calendar day, not a rolling window, because that is the
    unit it is *displayed* in — "6 of 20 sent today". Enforcement deliberately
    stays on the rolling window, which a calendar day cannot replace: 20 sends
    at 23:00 and 20 more at 00:01 is a burst the daily counter would wave
    through.
    """
    now = now or datetime.now(UTC)
    if sent_today(account, now=now) == 0:
        account.daily_send_count = 0
        account.daily_count_reset_at = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )


def sent_today(account: GmailAccount, *, now: datetime | None = None) -> int:
    """What ``daily_send_count`` means *today*, without writing to the row.

    The stored column is only zeroed by :func:`roll_daily_counter`, which only
    ever runs from :func:`record_send`. So a mailbox whose last send was
    yesterday keeps yesterday's number in the column until it sends again — and
    the reputation panel read that column straight, telling a user who had sent
    nothing all day that they had already used "17 of 20". The panel is a read;
    it must not have to write a row to be right, and it must not mutate one it
    was only asked to describe.

    Same rule as the roll: the count belongs to the UTC calendar day recorded in
    ``daily_count_reset_at``, and a marker from an earlier day (or none at all,
    which is every row predating that column) describes a day that is over.
    """
    now = now or datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    reset_at = _aware(account.daily_count_reset_at)
    if reset_at is None or reset_at < day_start:
        return 0
    return account.daily_send_count or 0


def bounce_rate(account: GmailAccount) -> float:
    """Lifetime hard-bounce rate for the mailbox (0 when nothing has sent)."""
    total = account.sent_total or 0
    if total <= 0:
        return 0.0
    return (account.bounce_count or 0) / total


@dataclass
class SendDecision:
    """Whether an outreach may go out now, and why not if it can't."""

    allowed: bool
    reason: str | None = None
    # How long to wait before retrying, when the block is temporary.
    retry_after: timedelta | None = None
    day_limit: int = 0


# Never ask a caller to come back sooner than this. A held message that retries
# every few seconds is a busy-loop against the broker and the database, and
# nothing the rolling window does moves that fast.
MIN_RETRY_AFTER = timedelta(minutes=5)
# ...nor later than this, when the wait is computed rather than known. Keeps a
# clock skew or a stray future ``sent_at`` from parking a message for days.
MAX_COMPUTED_RETRY_AFTER = timedelta(hours=24)


def _capacity_frees_in(
    oldest_in_window: datetime | None, now: datetime
) -> timedelta:
    """How long until the rolling 24h window drops one send, bounded.

    The cap is a *rolling* window, so capacity does not return on the hour — it
    returns exactly 24h after the oldest send still inside it. Retrying on a flat
    one-hour tick instead meant a held message woke up, re-took its row lock,
    re-ran the count, and was refused again, as many as twenty-four times before
    its budget ran out. Production watched that happen to fifty-five queued
    replies once an hour for ten days.

    Falls back to an hour when the caller has no oldest send to offer, which is
    the behaviour every caller had before this existed.
    """
    oldest = _aware(oldest_in_window)
    if oldest is None:
        return timedelta(hours=1)
    wait = (oldest + timedelta(hours=24)) - now
    if wait < MIN_RETRY_AFTER:
        return MIN_RETRY_AFTER
    return min(wait, MAX_COMPUTED_RETRY_AFTER)


def evaluate(
    account: GmailAccount | None,
    sent_last_24h: int,
    *,
    now: datetime | None = None,
    oldest_in_window: datetime | None = None,
) -> SendDecision:
    """Decide whether *account* may send one more outreach right now.

    Ordered by severity: no mailbox → complaint pause → bounce pause → warm-up /
    daily cap. ``sent_last_24h`` is the caller's authoritative rolling count (it
    already joins across threads), so this function stays free of DB access and
    is trivial to unit-test.

    ``oldest_in_window`` is the ``sent_at`` of the oldest send still inside that
    rolling window. Optional, and only ever used to make ``retry_after`` truthful
    when the daily cap is what blocks — see :func:`_capacity_frees_in`.
    """
    now = now or datetime.now(UTC)

    if account is None:
        return SendDecision(False, "No connected Gmail account")

    paused_until = _aware(account.paused_until)
    if paused_until is not None and paused_until > now:
        return SendDecision(
            False,
            account.pause_reason or "Sending is paused to protect your reputation",
            # Floored like every other computed wait here. A message reaching
            # the gate in the last seconds of a three-day hold would otherwise
            # be told to come back in under a second, and a held campaign is
            # dozens of messages arriving at the same deadline together — which
            # is a spin against the broker and the row locks, not a retry.
            # `email_tasks` applies its own floor on top; this makes the number
            # this function *returns* honest for every caller, not just the one
            # that remembered.
            retry_after=max(paused_until - now, MIN_RETRY_AFTER),
        )

    day_limit = warmup_day_limit(account, now=now)
    if sent_last_24h >= day_limit:
        # Which of the two ceilings this is decides the sentence, because the
        # two have different answers. A warm-up cap lifts on its own and the
        # only useful advice is to wait; the configured cap never lifts, and a
        # user told to wait for a warm-up that finished a fortnight ago waits
        # forever instead of raising `DAILY_SEND_LIMIT`. This string is not
        # internal — it is `blocked_reason` on the autopilot preflight and the
        # note on every message `email_tasks` parks for review.
        cap = (
            f"{day_limit}/day during warm-up"
            if is_warming_up(account, now=now)
            else f"{day_limit}/day"
        )
        return SendDecision(
            False,
            f"Daily send limit reached ({cap})",
            retry_after=_capacity_frees_in(oldest_in_window, now),
            day_limit=day_limit,
        )

    return SendDecision(True, day_limit=day_limit)


def record_send(account: GmailAccount, *, now: datetime | None = None) -> None:
    """Book a successful send: bump the counters and start the warm-up clock.

    The warm-up clock starts on the *first* real send, not at connect time — a
    mailbox that connected weeks ago but never sent is still cold.

    The daily counter is rolled *before* the increment, so a send on a new day
    reads 1 rather than yesterday's total plus one.
    """
    now = now or datetime.now(UTC)
    account.sent_total = (account.sent_total or 0) + 1
    roll_daily_counter(account, now=now)
    account.daily_send_count = (account.daily_send_count or 0) + 1
    account.last_used_at = now
    if account.warmup_started_at is None:
        account.warmup_started_at = now


def adopt_send_history(
    account: GmailAccount,
    first_sent_at: datetime | None,
    sent_count: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Age the ramp to match sending this address has already done.

    The ramp's whole input is ``warmup_started_at``, and that column lives only
    on the ``GmailAccount`` row — which does not survive disconnecting and
    reconnecting a mailbox. ``google_sub`` matching keeps a *re-consent* on the
    same row, but a user who removes the mailbox and adds it back gets a new row
    with a null clock, and so does everyone on a deployment whose Google OAuth
    client is replaced. The address is unchanged, its reputation with the
    receiving servers is unchanged, and every message it ever sent is still
    sitting in ``emails`` — but the ramp reads it as brand new and drops it to
    5/day.

    That is not a cosmetic downgrade. The rolling-24h gate is checked against
    this ceiling on every send, so a mailbox with weeks of history and a backlog
    behind it goes to five messages a day and stays there: production spent ten
    days at exactly 5/day with fifty-three queued replies that could never
    drain, because the ramp believed a forty-five-day-old mailbox was one day
    old.

    So the clock is seeded from what the address actually did. Two rules keep
    that from becoming a way *around* the ramp:

    * **It only ever moves the clock earlier.** Never later, and never onto an
      account that already has an earlier start. Re-running this is a no-op, and
      a mailbox cannot be made *colder* by reconnecting either.
    * **The history has to be real sends from this address.** The caller
      resolves that (see :func:`app.services.gmail_accounts.observed_send_history`);
      a brand-new address has no rows and lands here with ``None``, which is
      declined. Nothing about connecting a mailbox can manufacture warmth.

    ``sent_total`` is carried across for the same reason: it is the denominator
    of :func:`bounce_rate`, and a reconnect that resets it to zero also disarms
    the bounce guardrail until twenty fresh sends rebuild ``MIN_VOLUME_FOR_RATE``.

    **Restoring history does not release a hold.** This writes the *first send*;
    the ramp penalty a bounce or complaint imposed lives in
    ``warmup_hold_seconds`` and is untouched here, so the mailbox comes back
    with its real age and still owes the days it spent paused. While the two
    shared one column that was not true — see :func:`effective_start`.

    Returns True when something changed, so the caller knows whether to log it.
    """
    if first_sent_at is None:
        return False
    now = now or datetime.now(UTC)
    started = first_sent_at if first_sent_at.tzinfo else first_sent_at.replace(tzinfo=UTC)
    if started > now:
        # A clock in the future would read as day 0 forever. Nothing legitimate
        # produces one; a bad backfill or a skewed host does.
        return False

    changed = False
    current = _aware(account.warmup_started_at)
    if current is None or started < current:
        account.warmup_started_at = started
        changed = True
    if sent_count > (account.sent_total or 0):
        account.sent_total = sent_count
        changed = True
    return changed


def _hold_until(
    account: GmailAccount, until: datetime, reason: str, *, now: datetime
) -> bool:
    """Pause the mailbox until *until* — but never *un*-pause it.

    A pause is a floor, not an assignment. The two guardrails here fire off
    independent events and carry very different cool-downs, so whichever writes
    second used to win outright: a spam complaint pauses for three days, and any
    hard bounce arriving inside that window rewrote ``paused_until`` to 24 hours
    and the reason with it. Delivery notices are exactly what arrives next — a
    campaign that earned a complaint is a campaign still trickling out to dead
    addresses — so the graver hold was routinely cut to a third of its length by
    the lesser one, and the reputation panel stopped saying a complaint had ever
    happened.

    Taking the later of the two is the same rule
    :func:`app.services.send_policy.pause_auto_send` already applies to its own
    pause column, for the same reason: nothing that means "hold back" may be
    allowed to shorten a hold that is already in force.

    **A hold also stops the ramp's clock.** The ramp is derived entirely from the
    calendar distance to the start of sending, so a mailbox paused on day 2 came
    back on day 5 and was promoted twice for the three days it spent forbidden to
    send — it earned trust by being silent, which is exactly backwards. A pause
    is the system's judgement that this address is in trouble; letting it emerge
    at triple the allowance it was refused at is the worst available reading of
    that. So the hold is banked in ``warmup_hold_seconds``, which
    :func:`effective_start` adds to the first send, and the mailbox resumes on
    the step it was stopped on.

    Banked in its own column rather than by pushing ``warmup_started_at``
    forward, which is how this was first written and did not survive contact
    with :func:`adopt_send_history` — see :func:`effective_start` for what the
    six-hourly reconcile did to it.

    Only while it is *still warming up*. Pushing the clock on a mailbox that has
    finished the ramp would put it back onto one — a 15-day-old mailbox held for
    three days would re-enter at 20/day — and the ramp is a starting procedure,
    not a punishment to be re-served. A mailbox with real trouble past day 14 is
    held by the pause itself and by the bounce rate that produced it.

    Only the *newly added* time counts, so a bounce arriving inside a complaint's
    three-day hold does not charge the ramp for days it was already serving.

    Returns True when this call moved the deadline out.
    """
    current = _aware(account.paused_until)
    if current is not None and current >= until:
        return False

    if account.warmup_started_at is not None and is_warming_up(account, now=now):
        already_held = max(current - now, timedelta(0)) if current is not None else timedelta(0)
        added = (until - now) - already_held
        if added > timedelta(0):
            # The effective start may land in the future for a mailbox younger
            # than its own hold. `warmup_day` floors at 0, which is the coldest
            # step and the right place for a brand-new address that has already
            # earned a complaint. It converges back on its own: the bank only
            # ever grows by time actually spent held, which cannot outrun the
            # clock it is charged against.
            account.warmup_hold_seconds = (account.warmup_hold_seconds or 0) + int(
                added.total_seconds()
            )

    account.paused_until = until
    account.pause_reason = reason
    return True


def record_bounce(account: GmailAccount, *, now: datetime | None = None) -> bool:
    """Record a hard bounce and pause the mailbox if the rate is now unsafe.

    Returns True when this bounce tripped a cool-down. An existing pause that
    already runs longer is left alone — see :func:`_hold_until` — so the answer
    is "the guardrail fired", not "the deadline moved".
    """
    now = now or datetime.now(UTC)
    account.bounce_count = (account.bounce_count or 0) + 1
    enough_volume = (account.sent_total or 0) >= MIN_VOLUME_FOR_RATE
    if enough_volume and bounce_rate(account) > BOUNCE_RATE_LIMIT:
        _hold_until(
            account,
            now + BOUNCE_COOLDOWN,
            f"Paused 24h — bounce rate {bounce_rate(account):.0%} exceeds "
            f"{BOUNCE_RATE_LIMIT:.0%}",
            now=now,
        )
        logger.warning(
            "mailbox %s paused for high bounce rate", mask_email(account.email)
        )
        return True
    return False


def record_complaint(account: GmailAccount, *, now: datetime | None = None) -> None:
    """Record a spam complaint and pause the mailbox for a longer cool-down."""
    now = now or datetime.now(UTC)
    account.complaint_count = (account.complaint_count or 0) + 1
    _hold_until(
        account,
        now + COMPLAINT_COOLDOWN,
        "Paused 3 days — a recipient reported outreach as spam",
        now=now,
    )
    logger.warning("mailbox %s paused for a spam complaint", mask_email(account.email))


def warmup_progress(account: GmailAccount, *, now: datetime | None = None) -> dict:
    """Where the mailbox is on the ramp, for the setup and pipeline panels.

    A user who doesn't know about the ramp reads a throttled campaign as a broken
    product, so this exists to turn an invisible limit into an explained one:
    "10 sends a day, rising to 15 in 2 days".

    The projection is stated for a mailbox that has never sent, too. It used to
    be blank there — the walk forward moved ``now``, and ``warmup_day`` answers
    0 for a null clock whatever ``now`` is, so no future day ever read higher
    than today and ``next_limit`` stayed ``None``. That is every freshly
    connected mailbox, which is to say the exact reader this function was
    written for: the setup panel showed them the bare number, "5 a day", with no
    indication it was the *first* rung of anything. ``clock_started`` marks the
    difference, because a projection off a clock that has not started is
    conditional — it is what happens once they send, not a countdown already
    running — and the panel has to be able to say so.
    """
    now = now or datetime.now(UTC)
    day = warmup_day(account, now=now)
    limit = limit_for_day(day)
    warming = limit < settings.daily_send_limit
    step = day // WARMUP_STEP_DAYS

    next_limit: int | None = None
    next_in: int | None = None
    if warming:
        # Walk forward to the first day the allowance actually changes; the last
        # schedule step is held to WARMUP_RAMP_DAYS, so "next step" and "next
        # increase" are not the same day.
        horizon = max(WARMUP_RAMP_DAYS, len(WARMUP_SCHEDULE) * WARMUP_STEP_DAYS)
        for ahead in range(1, horizon + 1):
            future = limit_for_day(day + ahead)
            if future > limit:
                next_limit, next_in = future, ahead
                break

    # The effective start, not the raw column: a mailbox that has been held owes
    # the ramp that time, so the date it finishes moves out with the hold. The
    # panel would otherwise keep promising a completion date that has already
    # passed while the allowance sat still.
    started = effective_start(account)
    full_at = (
        (started + timedelta(days=full_limit_day()))
        if (started is not None and warming)
        else None
    )
    return {
        "warming_up": warming,
        # False until the first real send. Everything below is still true, but
        # the day counts are a forecast rather than a position.
        "clock_started": started is not None,
        "day": day,
        "step": step,
        "day_limit": limit,
        "next_limit": next_limit,
        "next_increase_in_days": next_in,
        "full_limit_at": full_at,
        "sent_today": sent_today(account, now=now),
    }


def status_summary(account: GmailAccount, *, now: datetime | None = None) -> dict:
    """A UI-friendly view of where the mailbox stands on reputation."""
    now = now or datetime.now(UTC)
    paused_until = _aware(account.paused_until)
    progress = warmup_progress(account, now=now)
    return {
        "email": account.email,
        "day_limit": progress["day_limit"],
        "warming_up": progress["warming_up"],
        "sent_total": account.sent_total or 0,
        "bounce_count": account.bounce_count or 0,
        "bounce_rate": round(bounce_rate(account), 3),
        "complaint_count": account.complaint_count or 0,
        "paused": bool(paused_until and paused_until > now),
        "paused_until": paused_until if (paused_until and paused_until > now) else None,
        "pause_reason": account.pause_reason if (paused_until and paused_until > now) else None,
        "warmup": progress,
    }


__all__ = [
    "BOUNCE_RATE_LIMIT",
    "DEFAULT_SCHEDULE",
    "MAX_COMPUTED_RETRY_AFTER",
    "MIN_RETRY_AFTER",
    "WARMUP_RAMP_DAYS",
    "WARMUP_SCHEDULE",
    "WARMUP_STEP_DAYS",
    "SendDecision",
    "bounce_rate",
    "effective_start",
    "evaluate",
    "full_limit_day",
    "is_warming_up",
    "limit_for_day",
    "record_bounce",
    "record_complaint",
    "record_send",
    "roll_daily_counter",
    "sent_today",
    "status_summary",
    "warmup_day",
    "warmup_day_limit",
    "warmup_progress",
]
