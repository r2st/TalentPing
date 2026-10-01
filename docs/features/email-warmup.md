# Email Warm-up — 5 a day, then 10, 15, 20, then the ceiling

**Status:** Implemented
**Last updated:** 2026-07-27
**Depends on:** reputation service (shipped), send pipeline (shipped)

---

## 1. Overview

### 1.1 What already existed, and what was wrong with it

`services/reputation_service.py` already ramps sending by mailbox age:

```python
WARMUP_SCHEDULE: tuple[int, ...] = (3, 5, 10, 15)   # per WEEK
week_index = (now - warmup_started_at).days // 7
```

That is 3/day for the whole first week, 5/day for the second, 10 for the third,
15 for the fourth, and the configured ceiling from day 28. Two problems:

1. **It is not the specified ramp, and it is a month long.** The brief asks for
   5 → 10 → 15 → 20 → max **over two weeks**. The shipped schedule reaches 20/day
   never (it goes 15 → ceiling) and reaches the ceiling on day 28.
2. **`GmailAccount.daily_send_count` is written but never reset.** The column and
   its companion `daily_count_reset_at` exist; `send_outreach_email` increments
   the counter on every send and nothing ever rolls it over. It is a lifetime
   counter wearing a daily counter's name. Nothing reads it for enforcement —
   the real gate is a rolling-24h `COUNT(*)` over `emails` — so it has been a
   silently-wrong number on the reputation panel rather than a live bug, but it
   is exactly the sort of thing that becomes a bug the first time someone trusts
   it.

### 1.2 What this feature adds

```
first successful send
      │  record_send() stamps warmup_started_at   (unchanged)
      ▼
  day 0-2   →  5/day
  day 3-5   → 10/day
  day 6-8   → 15/day
  day 9-13  → 20/day
  day 14+   →  settings.daily_send_limit  (30)
      │
      │  every send attempt
      ▼
evaluate(account, sent_last_24h)
      ├── mailbox paused (bounce/complaint cool-down) ──► retry later
      ├── sent_last_24h >= warmup_day_limit()         ──► retry later
      └── allowed
      │
      ▼  on success
  roll_daily_counter(account, now)   ← new: resets at local midnight UTC
  daily_send_count += 1
  sent_total += 1
```

### 1.3 Scope

**In scope:** the new ramp shape, a configurable schedule, a working daily
counter with rollover, `warmup_progress()` for the UI, and enforcement in the
send pipeline.

**Out of scope:** automated warm-up *traffic* — the mailbox-network trick where a
tool sends mail to itself and marks it read to fake engagement. That inflates
reputation with fake signal, violates Gmail's terms, and this product sends from
the candidate's real personal mailbox where the downside is their actual email
account. The ramp here is a throttle on genuine sends, nothing else.

---

## 2. The ramp

```python
WARMUP_SCHEDULE: tuple[int, ...] = (5, 10, 15, 20)   # per STEP
WARMUP_STEP_DAYS: int = 3
```

`warmup_day_limit()` becomes:

```python
day_index = (now - warmup_started_at).days
step = day_index // WARMUP_STEP_DAYS
limit = settings.daily_send_limit if step >= len(SCHEDULE) else SCHEDULE[step]
return min(limit, settings.daily_send_limit)
```

With the defaults that is days 0–2 at 5, 3–5 at 10, 6–8 at 15, 9–11 at 20 —
and days 12–13 also at 20, because step 4 falls past the end of the schedule and
takes the ceiling from day 12. To make "over two weeks" literal, the last step is
held: a `WARMUP_HOLD_DAYS = 14` floor means the ceiling is not reached before day
14 even though the schedule is exhausted at day 12.

Both invariants from the old implementation are preserved:

* **A mailbox with no `warmup_started_at` is treated as brand new** (step 0,
  5/day). Unknown age is the risky case; assume the risky answer.
* **The ramp never exceeds `settings.daily_send_limit`.** A deployment that sets
  the ceiling to 4 gets 4 on day one, not 5.

The schedule is configurable — `warmup_schedule="5,10,15,20"`,
`warmup_step_days=3`, `warmup_ramp_days=14` — parsed and validated once at
import, falling back to the defaults on a malformed value rather than refusing
to boot.

### 2.1 Why a day-based ramp is safer than the week-based one it replaces

It is *more* aggressive early (5/day rather than 3/day in week 1) and reaches
full volume in half the time. That is acceptable because the ramp is not the only
protection and never was: the bounce guardrail (5% over 20+ sends → 24h pause),
the complaint guardrail (any complaint → 3-day pause), the randomized 90–600 s
inter-send spacing, business-hours scheduling
([`send-time-optimization.md`](send-time-optimization.md)) and hard-bounce
suppression ([`bounce-handling.md`](bounce-handling.md)) all still apply. 5/day
from a real personal Gmail with SPF/DKIM/DMARC alignment is well inside normal
human behaviour.

---

## 3. The daily counter

```python
def roll_daily_counter(account, *, now=None) -> None:
    """Zero the daily count when the UTC day has turned over."""
```

Called from `record_send()` immediately before the increment, so the counter is
correct at the moment it is read. Rollover is on the **UTC calendar day**, not a
rolling 24h window, because that is the unit the number is presented in ("6 of
20 sent today"). `daily_count_reset_at` stores the start of the day the counter
belongs to; a null value (every existing row) is treated as "roll now".

**Enforcement still uses the rolling-24h query**, not this counter. That is
deliberate and worth being explicit about: a calendar-day counter lets a mailbox
send 20 at 23:00 and 20 more at 00:01. The rolling window closes that hole, and
`_sent_in_last_24h()` in `email_tasks` is the authoritative input to
`evaluate()`. The daily counter exists to be *displayed*, and it now displays
something true.

---

## 4. Surfacing progress

`warmup_progress(account)` returns what the setup and pipeline panels need:

```json
{
  "warming_up": true,
  "day": 4,
  "step": 1,
  "day_limit": 10,
  "next_limit": 15,
  "next_increase_in_days": 2,
  "full_limit_at": "2026-08-10T00:00:00Z",
  "sent_today": 6
}
```

`status_summary()` — already rendered by `GET /autopilot/reputation` — embeds
this, so the existing endpoint gains the fields without a new route. The Setup
page shows "Your mailbox is warming up: 10 sends a day, rising to 15 in 2 days",
which turns an invisible throttle into an explained one. A user who does not know
about the ramp reads a stalled campaign as a broken product.

---

## 5. Enforcement path

Unchanged in structure, which is the point — the gate already existed and was
already in the right place:

`send_outreach_email` → `reputation_service.evaluate(account, _sent_in_last_24h(...))`
→ on refusal, `self.retry(countdown=max(300, retry_after))` leaving the email
`QUEUED`. A warm-up refusal delays a send; it never drops one. With
`max_retries=24` and a 1h retry a blocked email survives a full day of waiting,
which is longer than any warm-up hold.

---

## 6. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `warmup_schedule` | `"5,10,15,20"` | Per-step daily ceilings. |
| `warmup_step_days` | `3` | Days per step. |
| `warmup_ramp_days` | `14` | Earliest day the configured ceiling applies. |
| `daily_send_limit` | `30` | The ceiling. Caps every step. |

---

## 7. Testing

`tests/test_reputation.py` is extended (and its week-based expectations updated):

* day 0 → 5, day 3 → 10, day 6 → 15, day 9 → 20, day 14 → `daily_send_limit`;
* day 12 is still 20, not the ceiling — the `warmup_ramp_days` hold;
* a mailbox with no `warmup_started_at` gets 5;
* the ramp never exceeds `daily_send_limit`, including when the ceiling is below
  the first step;
* a malformed `warmup_schedule` falls back to the defaults instead of raising;
* `evaluate` blocks at exactly the step limit and allows one below it;
* the pause guardrails still take precedence over the ramp;
* `roll_daily_counter` zeroes the count on a new UTC day, leaves it alone within
  the same day, and treats a null `daily_count_reset_at` as "roll now";
* `record_send` rolls before incrementing, so a send on a new day reads 1;
* `warmup_progress` reports the right `next_limit` and `next_increase_in_days`
  mid-ramp, and `warming_up: false` past the ramp;
* an integration test through `send_outreach_email` confirms the sixth send on
  day 1 is retried and left `QUEUED` rather than sent or failed.
