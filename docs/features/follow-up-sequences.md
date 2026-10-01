# Follow-up Sequences — the day-3 nudge and the day-7 close

**Status:** Implemented
**Last updated:** 2026-07-27
**Depends on:** follow-up scheduling (shipped), Celery Beat (shipped),
send-time optimization ([`send-time-optimization.md`](send-time-optimization.md)),
inbox polling (shipped)

---

## 1. Overview

### 1.1 What already existed

Follow-ups are the one feature on this list that was already half-built. Before
this change the codebase had:

* `models/follow_up.py` — a `FollowUp` row per step, with `scheduled_at`,
  `status` (`SCHEDULED` / `SENT` / `CANCELLED` / `FAILED`) and a `template`
  describing the angle.
* `services/follow_up_service.py` — `schedule_for_application()` writing the
  whole sequence up front, `compose_follow_up()` writing the body at *send*
  time, and `cancel_for_application()`.
* `tasks/follow_up_tasks.process_due_follow_ups` — a Beat sweep every
  `follow_up_scan_interval_seconds` (15 min) that turns due rows into queued
  emails and hands them to the same throttled sender the initial outreach uses.

So the infrastructure was real. What was wrong was the **schedule** and the
**stop condition**.

### 1.2 The two defects this feature fixes

**(a) The cadence was not day 3 / day 7.** Offsets were computed from a single
`follow_up_interval_days` knob with a widening multiplier:

```python
offset_days = interval * step + (step - 1) * (interval // 2)
```

At the shipped defaults (`interval=4`, `count=2`) that produces **day 4 and day
10** — then `optimal_send_time()` pushed each one to the next Tue–Thu 06:00–10:00
slot, so a campaign started on a Thursday saw its "day 4" nudge land on day 5 and
its "day 10" close land on day 12. The interval knob could not express "3 then 7"
at all: no integer `interval` yields that pair under that formula.

**(b) Stop-on-reply was inferred from application status, not from mail.**
`prepare_follow_up_email()` cancelled when `application.status` was in
`REPLIED_STATUSES` (`REPLIED`, `INTERESTED`, `SCHEDULING`). Status is set by
`inbox_tasks._apply_intent` from the *classified intent* of the reply — and the
intent map has no entry for `ReplyIntent.QUESTION`, `OUT_OF_OFFICE` or `OTHER`.
A recruiter who wrote back "Who is this? What role?" left the application on
`OUTREACH_SENT`, and the sequence kept nudging someone who had already answered.
That is the single worst thing an outreach product can do.

### 1.3 What this feature adds

```
initial outreach sent
      │  schedule_for_application()
      ▼
  FollowUp(step=1, scheduled_at = send_slot(sent_at + 3d))   ← nudge
  FollowUp(step=2, scheduled_at = send_slot(sent_at + 7d))   ← final
      │
      │  process_due_follow_ups  (Beat, every 15 min)
      ▼
  for each due row:
      ├── recruiter opted out / hard-bounced  ──► CANCELLED
      ├── application in a terminal status    ──► CANCELLED
      ├── ANY inbound email on the thread     ──► CANCELLED  ← new
      ├── application status says "replied"   ──► CANCELLED
      └── otherwise ──► compose → Email(QUEUED) → throttled sender
```

Three concrete changes:

1. **An explicit day-offset sequence.** `settings.follow_up_step_days` defaults
   to `"3,7"`. A campaign may override it per-row via the new
   `Campaign.follow_up_step_days` JSON column. `follow_up_interval_days` is kept
   for backward compatibility and is used only when neither is set.
2. **Reply detection reads the mailbox, not the status column.** A new
   `thread_has_reply()` check cancels the remaining sequence the moment any
   `EmailDirection.RECEIVED` row exists on the application's thread, regardless
   of how the reply was classified.
3. **Scheduling delegates to the send-time optimizer** (feature 5), so day 3
   means "09:00–11:00 in the recruiter's local weekday morning, three days out"
   rather than "the next Tue–Thu 06:00 UTC".

### 1.4 Scope

**In scope:** step offsets, reply-driven cancellation, integration with the
send-time optimizer, migration for the new column, tests.

**Out of scope:** changing the *copy* of follow-ups (`compose_follow_up` and its
four templates are unchanged), and multi-branch sequences that fork on recipient
behaviour. A future step could pick `OPENED_NO_REPLY` from real open data now
that [`email-tracking.md`](email-tracking.md) records it — `choose_template()`
already has the branch, it just guesses today.

---

## 2. User stories

**US-1 — The persistent candidate.** A recruiter ignores the first email. Three
days later they get one short nudge that adds a new piece of context; four days
after that, a graceful close. Then silence. Nothing else is ever sent.

**US-2 — The recruiter who replies late.** They answer on day 5 with a question.
The day-7 close is cancelled before it is composed, and the thread joins the
normal reply machinery.

**US-3 — The out-of-office.** An auto-responder counts as a reply for the
purpose of *not nudging again this week*. This is deliberate: an OOO means the
person is not reading mail, and a nudge into an empty inbox is wasted send
budget against a warm-up cap.

---

## 3. Data model

No new tables. One new column:

| Table | Column | Type | Meaning |
|---|---|---|---|
| `campaigns` | `follow_up_step_days` | `JSON` nullable | Day offsets from the initial send, e.g. `[3, 7]`. Null → `settings.follow_up_step_days`. |

`follow_up_count` continues to cap the sequence length (`offsets[:count]`), and
`follow_up_interval_days` remains the fallback generator for campaigns created
before this change that explicitly set it.

## 4. Scheduling

```python
def sequence_offsets(campaign) -> list[int]:
    """Day offsets from the initial send, one per follow-up step."""
    raw = campaign.follow_up_step_days or parse_step_days(settings.follow_up_step_days)
    offsets = sorted({int(d) for d in raw if int(d) > 0})
    return offsets[: max(0, campaign.follow_up_count)]
```

`schedule_for_application()` then writes one row per offset, each
`scheduled_at = send_time.next_slot(sent_at + timedelta(days=offset), tz)` where
`tz` is the recipient timezone resolved by the send-time optimizer. Idempotency
is unchanged: an application that already has any `FollowUp` row gets none added,
so a retried send cannot double the sequence.

Templates follow the existing rule — the last step is `FINAL`, everything before
it is `NO_RESPONSE`, and `choose_template()` may upgrade a step to
`PARTIAL_RESPONSE` or `OPENED_NO_REPLY` at send time.

## 5. Stopping

`prepare_follow_up_email()` gains one check, ordered *before* the status check
because it is strictly more reliable:

```python
if stop_on_reply and thread_has_reply(db, application.id):
    follow_up.status = FollowUpStatus.CANCELLED
    follow_up.note = "recruiter replied"
    return None, follow_up.note
```

`thread_has_reply()` is a single `EXISTS` over `emails` joined to
`email_threads`, filtered to `direction == RECEIVED`. Cheap, and correct
regardless of what the classifier decided the reply *meant*.

Cancellation remains a status change, never a delete. "We stopped on day 4
because they replied" is history worth keeping, and the tracker renders it.

Existing cancellation paths are unchanged and still apply: `opted_out`
recruiters, terminal application statuses, a hard-bounced address (see
[`bounce-handling.md`](bounce-handling.md)), and a missing thread.

## 6. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `follow_up_step_days` | `"3,7"` | Comma-separated day offsets for the default sequence. |
| `follow_up_scan_interval_seconds` | `900` | Beat sweep cadence (unchanged). |

## 7. Testing

`tests/test_follow_ups.py` is extended with:

* the default sequence lands at day 3 and day 7 from the send, not day 4/10;
* a campaign-level `follow_up_step_days` override is honoured, and clamped by
  `follow_up_count`;
* `follow_up_count=0` or an empty offset list schedules nothing;
* an inbound email on the thread cancels the remaining sequence even when the
  application status was never moved off `OUTREACH_SENT`;
* an inbound email classified `OTHER` (the case that leaked before) cancels;
* `follow_up_stop_on_reply=False` still sends into a replied thread;
* each scheduled slot falls on a weekday inside the 09:00–11:00 recipient-local
  window.

## 8. Risks

* **Day 3 is sooner than the old day 4/day 5.** Slightly more aggressive. It is
  what the brief specifies and it stays within the 24h rolling cap, which is the
  gate that actually protects the mailbox.
* **Treating any inbound message as a reply is blunt.** A bounce is inbound too
  — but bounces are intercepted upstream in `inbox_tasks.poll_thread` and never
  stored as `RECEIVED` rows (see [`bounce-handling.md`](bounce-handling.md)), so
  they cannot be mistaken for a human answering.
