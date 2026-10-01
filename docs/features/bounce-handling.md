# Bounce Handling — hard, soft, and what to do about each

**Status:** Implemented
**Last updated:** 2026-07-27
**Depends on:** inbox polling (shipped), reputation service (shipped),
inbound scanner (shipped)

---

## 1. Overview

### 1.1 What already existed, and what it got wrong

`services/reply_classifier.looks_like_bounce()` already detected delivery-failure
notices from the sender and a phrase list, and two call sites acted on it —
`tasks/inbox_tasks.poll_thread` and `services/inbound_scanner`. The handling was:

```python
if looks_like_bounce(from_addr, subject, body):
    reputation_service.record_bounce(user.primary_gmail)
    recruiter.opted_out = True                       # ← every bounce
    cancel_for_application(db, application.id, "outreach bounced")
```

Three problems, all caused by the same thing — **there is only one kind of
bounce in this code**:

1. **A full inbox permanently blacklists a good recruiter.** "Mailbox full" and
   "over quota" are in the phrase list's neighbourhood and are *temporary*.
   Setting `opted_out = True` is the CAN-SPAM opt-out flag: it is how the product
   records "this human asked never to be contacted again", and it is checked
   before every send and every draft. Burning it on a recruiter who was on
   holiday with a full mailbox loses that contact forever, across every future
   campaign.
2. **Soft bounces poison the reputation ledger.** `record_bounce` feeds
   `bounce_count`, which feeds `bounce_rate`, which pauses the whole mailbox for
   24h above 5%. Mail-server hiccups and greylisting should not pause sending.
3. **There is no per-domain view.** If every address at `bigcorp.com` bounces
   because the domain rejects all external mail, nothing notices, and the
   autopilot keeps discovering and mailing addresses there.

### 1.2 What this feature adds

```
inbound message
      │
      ▼
classify_bounce(subject, body, from_addr)
      │
      ├── NONE ──► not a bounce, continue to reply classification
      │
      ├── HARD  (5.x.x, "user unknown", "address not found", "domain not found")
      │      ├─ Recruiter.delivery_state = HARD_BOUNCED  (never send again)
      │      ├─ reputation_service.record_bounce()       (counts toward the 5% gate)
      │      ├─ cancel remaining follow-ups
      │      └─ EmailBounce row
      │
      └── SOFT  (4.x.x, "mailbox full", "over quota", "try again later",
                 "temporarily deferred", "greylisted")
             ├─ Recruiter.soft_bounce_count += 1
             ├─ NOT counted against mailbox reputation
             ├─ cancel remaining follow-ups for this application
             ├─ EmailBounce row
             └─ at SOFT_BOUNCE_LIMIT (3) ──► escalate to HARD_BOUNCED
```

Plus a suppression check in the send pipeline and at draft generation, and a
per-domain bounce-rate report.

### 1.3 Scope

**In scope:** bounce classification, a `EmailBounce` audit table, recruiter
delivery state, suppression at send and at compose, per-domain rates, and the
reputation-ledger split between hard and soft.

**Out of scope:** SMTP-level webhook ingestion (there is no ESP here — mail goes
out through the user's own Gmail, and the only bounce signal available is the
DSN that arrives back in their inbox), and complaint handling, which
`reputation_service.record_complaint` already covers.

---

## 2. Classification

Bounces are DSNs (RFC 3464). The reliable signal is the **enhanced status code**
in the body: `5.x.x` is permanent, `4.x.x` is transient. `services/bounce_service.py`
looks for that first and falls back to phrase matching:

```python
_STATUS_RE = re.compile(r"\b([45])\.\d{1,3}\.\d{1,3}\b")
_SMTP_RE   = re.compile(r"\b([45])\d{2}[ -]")     # bare "550 " / "452 "
```

| Kind | Codes | Phrases (excerpt) |
|---|---|---|
| `HARD` | `5.1.1`, `5.1.10`, `5.4.1`, `550`, `551`, `553` | "user unknown", "no such user", "address not found", "recipient address rejected", "domain not found", "does not exist" |
| `SOFT` | `4.2.2`, `4.3.1`, `4.4.x`, `421`, `450`, `452` | "mailbox full", "over quota", "insufficient storage", "try again later", "temporarily deferred", "greylist", "throttled", "rate limit" |

Precedence rules, in order:

1. An explicit enhanced status code wins over every phrase. A message saying
   "mailbox full" that carries `5.2.2` is a *hard* bounce — that server has
   decided the mailbox is permanently over quota.
2. Between phrases, **soft wins ties**. A DSN matching both lists is treated as
   soft, because the cost of being wrong is asymmetric: a soft call on a hard
   bounce costs two more sends and gets escalated at the third; a hard call on a
   soft bounce loses a real recruiter contact permanently.
3. `looks_like_bounce()` remains the gate. `classify_bounce()` is only consulted
   for messages that already look like DSNs, so a recruiter writing "your last
   email was rejected by our ATS" is never parsed as a bounce.

An out-of-office is explicitly **not** a bounce — it is handled by the reply
classifier as `ReplyIntent.OUT_OF_OFFICE`, and the phrase list excludes
"out of office" / "on leave" / "annual leave".

---

## 3. Data model

### 3.1 `email_bounces` (new)

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `user_id` | FK `users.id` CASCADE, indexed | scoping key |
| `recruiter_id` | FK `recruiters.id` SET NULL, indexed | |
| `email_id` | FK `emails.id` SET NULL | the outbound message that bounced, when known |
| `address` | String(320), indexed | normalized lowercase |
| `domain` | String(255), indexed | everything after `@`; the per-domain report groups on this |
| `kind` | enum `HARD` / `SOFT` | |
| `code` | String(16) nullable | the matched status code, for auditing a misclassification |
| `reason` | Text nullable | first 500 chars of the DSN |
| `occurred_at` | DateTime(tz), indexed | |

### 3.2 New columns on `recruiters`

| Column | Type | Meaning |
|---|---|---|
| `delivery_state` | enum `OK` / `SOFT_BOUNCED` / `HARD_BOUNCED`, default `OK` | |
| `soft_bounce_count` | Integer default 0 | reset to 0 on any successful delivery signal (a reply) |
| `last_bounce_at` | DateTime(tz) nullable | |
| `last_bounce_reason` | String(500) nullable | shown in the tracker so "why did this stop?" has an answer |

**`opted_out` is left alone.** It keeps its single meaning: a human asked not to
be contacted. Deliverability is now a separate axis, which is what makes it safe
to suppress a hard bounce forever without lying about consent.

---

## 4. Suppression

`bounce_service.is_suppressed(recruiter) -> bool` is `delivery_state ==
HARD_BOUNCED`. It is checked in three places:

1. **`outreach_service.generate_drafts_for_campaign`** — skipped with a note, the
   same way `opted_out` recruiters are, so no email is even composed.
2. **`auto_apply_service`** — the autopilot's contact selection.
3. **`tasks/email_tasks.send_outreach_email`** — the backstop. A queued email to
   an address that hard-bounced *after* it was queued is marked `FAILED` with a
   reason rather than sent. This is the one that actually matters: the gap
   between compose and send is hours by design.

Follow-ups are covered by `prepare_follow_up_email`, which cancels on the same
check.

---

## 5. Per-domain bounce rates

```python
def domain_bounce_rates(db, user_id, *, min_sent=3, limit=20) -> list[DomainBounceRate]
```

Sent counts come from `emails` joined through `email_threads → applications →
recruiters`, grouped on the recruiter's email domain; bounce counts come from
`email_bounces`. Domains under `min_sent` are excluded — one bounce out of one
send is a 100% rate and means nothing, the same `MIN_SAMPLE` reasoning the
analytics module already applies to response rates.

Exposed at `GET /analytics/bounces`, returning overall hard/soft totals plus the
worst domains. A domain above `DOMAIN_BOUNCE_ALERT` (50%) with enough volume is
flagged so the UI can say "bigcorp.com rejects mail from outside — 6 of 7
addresses bounced" instead of leaving the user to notice.

Nothing is auto-blocked at the domain level. That is deliberate: a domain-wide
suppression built from a handful of bad addresses would silently exclude the
user's best target company. The report informs; the human decides.

---

## 6. Reputation ledger

`reputation_service.record_bounce()` is now called **only for hard bounces**.
Soft bounces are recorded in `email_bounces` and on the recruiter, but do not
feed `GmailAccount.bounce_count` and cannot trip the 24h pause. The 5% guardrail
and `MIN_VOLUME_FOR_RATE` are otherwise unchanged.

`status_summary()` gains `soft_bounce_count` so the UI can show both.

---

## 7. Testing

`tests/test_bounce_handling.py`:

* `5.1.1` → HARD, `4.2.2` → SOFT, bare `550` → HARD, bare `452` → SOFT;
* "mailbox full" → SOFT; "mailbox full" carrying `5.2.2` → HARD (code wins);
* a message matching both phrase lists → SOFT (ties go soft);
* an out-of-office auto-reply → not a bounce at all;
* a recruiter quoting "delivery failed" in prose → not a bounce
  (`looks_like_bounce` gate);
* a hard bounce sets `HARD_BOUNCED`, writes an `EmailBounce`, cancels follow-ups
  and calls `record_bounce` once;
* a hard bounce does **not** set `opted_out`;
* a soft bounce leaves `delivery_state` recoverable, does not touch
  `GmailAccount.bounce_count`, and escalates to `HARD_BOUNCED` on the third;
* a queued email to a hard-bounced address is marked `FAILED` rather than sent;
* draft generation skips a hard-bounced recruiter with a note;
* `domain_bounce_rates` groups correctly, excludes thin samples, and is scoped to
  one user (another user's bounces are invisible).
