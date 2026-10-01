# Send Time Optimization — 9–11am, in the recipient's morning

**Status:** Implemented
**Last updated:** 2026-07-27
**Depends on:** company research (shipped), throttled sender (shipped),
follow-up scheduling ([`follow-up-sequences.md`](follow-up-sequences.md))

---

## 1. Overview

### 1.1 The gap

Two places decide *when* mail goes out, and neither knows anything about the
recipient:

* **`email_tasks.enqueue_campaign_sends`** gives each queued email a cumulative
  random countdown of 90–600 s. A campaign launched at 23:00 trickles out
  through the night. The spacing is right — bursts are what get a mailbox
  flagged — but the *absolute* time is whatever o'clock the user hit start.
* **`follow_up_service.optimal_send_time`** snaps a follow-up to the next
  Tue–Thu 06:00–10:00 slot, in **UTC**, with a comment citing Tuesday-morning
  conversion research. 06:00 UTC is 22:00 the previous day in San Francisco.
  For a Bay Area recruiter the "optimal" slot was reliably the worst one
  available.

So the product had a timing *intent* and no timezone to apply it in.

### 1.2 What this feature adds

`services/send_time.py` — one module that answers "when should this specific
message go out?", used by both schedulers:

```
recruiter
   │
   ▼ resolve_timezone()
   ├── Recruiter.timezone            (cached from a previous resolve)
   ├── CompanyProfile.headquarters   (global research cache, by company name)
   ├── the application's JobPosting.location
   ├── the recruiter's email TLD     (.co.uk → Europe/London, .de → …)
   └── settings.default_send_timezone (UTC)
   │
   ▼ next_slot(after, tz)
   "the next weekday 09:00–11:00 in tz, at or after `after`"
   │
   ▼ returned as UTC
   Celery countdown  /  FollowUp.scheduled_at
```

### 1.3 Scope

**In scope:** timezone resolution from location data, the business-hours slot
calculator, integration into campaign send dispatch and follow-up scheduling,
and a per-recruiter timezone cache.

**Out of scope:** learning each recruiter's *actual* open times from tracking
data (interesting, needs far more data than one candidate produces), geocoding
via a paid API, and honouring public holidays — weekday/weekend is the line
that pays for itself; a per-country holiday calendar is not.

---

## 2. Timezone resolution

No network calls and no new dependency: Python 3.9+ ships `zoneinfo`, and the
IANA database comes with the platform (`tzdata` is already an indirect
dependency). Resolution is a deterministic table lookup over free-text location
strings, tried in this order:

1. **`Recruiter.timezone`** — a cached IANA name from a previous resolve. Set
   once, cheap forever.
2. **`CompanyProfile.headquarters`** for the recruiter's company, looked up by
   `job_dedup.normalize_company`. This is the best source: the research cache is
   global and already populated for any company the product has looked at.
3. **The job posting's `location`**, when the application targets a specific
   posting.
4. **The email TLD.** `talent@acme.co.uk` is very probably UK. Only unambiguous
   country TLDs are mapped; `.com` / `.io` / `.ai` map to nothing.
5. **`settings.default_send_timezone`** (default `UTC`).

The location table maps three kinds of token, longest match first so
"Portland, Oregon" cannot be matched by a bare "or":

| Kind | Examples |
|---|---|
| City | san francisco, new york, austin, seattle, london, berlin, paris, bangalore, singapore, sydney, toronto, tel aviv, … |
| US state / region | california, texas, new york state, pacific, eastern, pst, est, … |
| Country | united kingdom, germany, india, japan, australia, canada, brazil, … |

"Remote" resolves to nothing on its own and falls through to the next source —
a remote role is not in a timezone, but the recruiter reading the mail is, and
their company's HQ is the better guess.

`resolve_timezone()` writes the result back to `Recruiter.timezone` so the
lookup happens once per contact rather than once per message. A resolution that
lands on the default is *not* cached, so a later `CompanyProfile` can still
improve it.

---

## 3. The slot calculator

```python
BUSINESS_START = 9    # 09:00 local
BUSINESS_END   = 11   # exclusive — the window closes at 11:00
WEEKDAYS       = (0, 1, 2, 3, 4)

def next_slot(after: datetime, tz: ZoneInfo, *, jitter: bool = True) -> datetime
```

Rules:

* Converts `after` into `tz`, then finds the first weekday whose 09:00–11:00
  window has not closed.
* If `after` already falls inside the window on a weekday, that moment is used —
  a message ready to go at 09:40 Tuesday is not deferred to Wednesday.
* Otherwise it lands at 09:00 on the next eligible day.
* **Never moves a time backwards.** A follow-up due Saturday goes out Monday,
  not the preceding Friday.
* With `jitter=True` a deterministic-per-call random offset of 0–119 minutes is
  added within the window, so fifty emails scheduled for the same Tuesday do not
  all fire at exactly 09:00:00 — which is both a burst pattern and obviously
  automated.
* Returns UTC. Callers store and schedule in UTC exclusively; the local time
  exists only inside this function.

The search is bounded at 14 iterations; a valid weekday slot is always within
four days, so exhaustion is unreachable and returns `after` unchanged.

---

## 4. Integration

### 4.1 Campaign sends

`enqueue_campaign_sends` computes `countdown = (slot - now).total_seconds()` per
email, where `slot = next_slot(now + cumulative_spacing, tz_for(recruiter))`.
The existing randomized 90–600 s spacing is preserved *inside* the window as the
`cumulative` term, so the throttle and the timing rule compose rather than one
overriding the other: emails still trickle, they just trickle during business
hours in the right country.

A countdown is clamped to `MAX_SEND_DELAY_SECONDS` (7 days) — an unreachable
case that would otherwise let a bad timezone park a task in the broker forever.

When `send_time_optimization_enabled` is `False`, or Celery is disabled (tests,
single-process deployments), behaviour is exactly the pre-feature cumulative
countdown. That switch is what keeps the existing campaign tests meaningful.

### 4.2 Follow-ups

`follow_up_service.optimal_send_time` is reimplemented as a thin wrapper over
`send_time.next_slot`, keeping its name and signature (it is exported and
tested), and gains an optional `tz` argument. `schedule_for_application()` passes
the recruiter's resolved zone, so a day-3 follow-up means 09:00–11:00 local on
the third weekday-adjusted day.

The old Tue–Thu constraint is dropped. It came from submission-timing research
about *job applications*, generalized to follow-ups; restricting nudges to three
days a week interacts badly with an explicit day-3/day-7 cadence — a Thursday
send would push the day-3 nudge to the following Tuesday, five days late.
Weekday mornings keep the defensible part of the finding.

---

## 5. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `send_time_optimization_enabled` | `true` | Master switch; off restores pure interval spacing. |
| `default_send_timezone` | `"UTC"` | Fallback when nothing resolves. |
| `send_window_start_hour` | `9` | Local hour the window opens. |
| `send_window_end_hour` | `11` | Local hour it closes (exclusive). |

An invalid `default_send_timezone` falls back to UTC with a warning rather than
failing app startup.

---

## 6. Testing

`tests/test_send_time.py`:

* "San Francisco, CA" → `America/Los_Angeles`; "London, UK" → `Europe/London`;
  "Bangalore, India" → `Asia/Kolkata`; unknown → default;
* "Portland, Oregon" is not matched by the "or" substring (longest-match);
* "Remote" alone resolves to nothing and falls through to the company profile;
* a `.co.uk` address resolves to `Europe/London`; a `.com` address does not;
* resolution order: cached column beats company profile beats posting beats TLD;
* a resolved zone is cached on the recruiter; a defaulted one is not;
* `next_slot` from a Saturday lands on Monday 09:00–11:00 local;
* from 09:40 on a Tuesday it returns that same moment (no needless deferral);
* from 14:00 Tuesday it returns Wednesday morning;
* from 23:00 UTC Monday for a Los Angeles recruiter it returns Tuesday
  09:00–11:00 **Pacific**, not 09:00 UTC;
* the result is always tz-aware UTC, always a weekday, always inside the window;
* `next_slot` never returns a time before `after`;
* jitter stays inside the window across 200 seeded draws;
* with the feature disabled, `enqueue_campaign_sends` produces the old
  cumulative countdowns.
