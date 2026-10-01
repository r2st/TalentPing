# Recruiter Email Improvements V2 — six changes to inbound handling

**Status:** Design + implementation (this doc ships with the code)
**Author:** Scout / engineering
**Last updated:** 2026-07-28
**Depends on:** Recruiter Reply (shipped, `docs/features/recruiter-reply.md`),
Gmail push (partially shipped), profiles (shipped), fit scoring (shipped),
email attachments (shipped)

---

## 0. What this is

`docs/features/recruiter-reply.md` describes the inbound pipeline that shipped:
scan the mailbox, classify, match a profile, route into one of three confidence
bands, write a reply. It works. Six things about it are wrong or missing, and
this document covers all six as one change set because four of them touch the
same rows.

| # | Improvement | Shape of the work |
|---|---|---|
| 1 | Gmail push notifications (Pub/Sub) | Mostly built — finish the wiring, make beat a *fallback* |
| 2 | Thread-aware replies | Two new columns and two arguments the sender wasn't passing |
| 3 | Learn from approvals/rejections | New feedback table + rolled-up per-sender priors |
| 4 | Recruiter follow-up detection | New escalation flag; never answer the same recruiter twice unread |
| 5 | Stats / analytics view | New scan-run table (the numbers were being logged and thrown away) + endpoint + UI |
| 6 | Smart resume selection | New selector service, wired into the attachment resolver |

A running theme: **four of the six exist because a number that was computed was
never persisted.** The scan result is logged and dropped, so "emails scanned"
cannot be answered. The classifier's verdict is stored but the user's disagreement
with it is not, so nothing can learn. The recruiter's `Message-ID` is read and
discarded, so the reply cannot thread. Fixing those is mostly a matter of writing
down what the code already knows.

---

## 1. Gmail push notifications (Pub/Sub)

### 1.1 What already exists

More than the ask assumes. `app/services/gmail_push.py`, `app/models/gmail_watch.py`,
`POST /api/v1/gmail/webhook`, `POST/DELETE /api/v1/gmail/watch`,
`inbox_tasks.ingest_push_notification` and the `renew-gmail-watches` beat task all
shipped with the cover-letter release (`e2b8f4a19c63`). The webhook decodes the
Pub/Sub envelope, resolves the mailbox, records the notification, and enqueues a
history walk; the walk polls tracked threads and calls `_scan_for_inbound` when it
finds a thread we have no row for — which is exactly the shape of a recruiter
writing in for the first time.

What is *not* done:

1. **The beat poll is not a fallback.** `recruiter_scan_interval_seconds`
   defaults to **60**, and `scan_all_recruiter_inboxes` enqueues a scan for every
   watched mailbox on every tick regardless of whether push is delivering. With
   push working that is sixty scans an hour that find nothing, per mailbox.
2. **`push_is_healthy()` has no caller.** It was written to answer "can this
   mailbox's push be trusted right now?" and nothing asks.
3. **No Pub/Sub project is actually provisioned**, and no setup path is written
   down.

### 1.2 The change

**Beat drops to 300s and becomes a genuine fallback.**
`recruiter_scan_interval_seconds` 60 → **300**. `scan_all_recruiter_inboxes` gains
one new skip, counted and named like every other:

```python
if push_covers(db, account):        # healthy watch + a notification recently
    skipped_push_covered += 1
    continue
```

`push_covers` is deliberately two conditions, not one:

* `gmail_push.push_is_healthy(watch)` — there is an active watch that has not
  passed its expiry. This is the part that says push is *registered*.
* the watch was notified within `recruiter_push_trust_seconds` (default **900**),
  **or** never notified but registered less than that long ago. This is the part
  that says push is *delivering*.

The second condition is the whole point. A watch can sit at `status="active"`
with a future `expires_at` while Google has quietly stopped publishing — renewal
keeps the row looking healthy either way. Trusting `status` alone would mean a
silently-dead subscription turns the mailbox off rather than falling back to
polling, which is the exact failure this feature is supposed to make impossible.
Fifteen minutes of silence on a mailbox that push claims to cover is enough to
stop believing it, and the 5-minute beat picks it straight back up.

So the cadence a candidate actually experiences:

| Push state | Detection latency |
|---|---|
| Delivering | seconds (webhook → `_scan_for_inbound` → debounced scan) |
| Registered but silent | ≤ 5 min, from the tick after the 15-minute trust window lapses |
| Not configured / failed / stopped | ≤ 5 min, every tick |

**`GET /gmail/status` reports the truth.** `GmailStatus` gains `push_healthy`,
computed with `push_is_healthy` — giving that function its first caller and the
Setup page a line that distinguishes "push is on" from "push is working".

**Provisioning.** `scripts/setup_gmail_pubsub.sh` against project
`metal-cascade-500017-k3`:

```
topic          projects/metal-cascade-500017-k3/topics/talentping-gmail
publisher      gmail-api-push@system.gserviceaccount.com  (roles/pubsub.publisher)
subscription   talentping-gmail-push  (push)
endpoint       https://talentping.aiknol.com/api/v1/gmail/webhook?token=<secret>
```

The script generates the shared token if absent, writes it to
`keys/gmail_pubsub_token.txt` (gitignored, per the project convention), and prints
the two env lines for `talentping_production.env`. It is idempotent — re-running
it against an existing topic/subscription updates rather than fails — because the
one thing worse than no push is a half-provisioned topic nobody wants to touch.

`gmail_pubsub_project` is added to settings so the topic string and the script
agree on one place.

### 1.3 What is deliberately *not* changed

The webhook stays incurious and always answers 204, including on a bad token or
an unparseable body. Pub/Sub retries any non-2xx with backoff, so returning an
error for a payload that will never parse turns one malformed message into an
indefinite retry loop — and the mailbox it would have covered is polled anyway.

---

## 2. Thread-aware replies

### 2.1 The bug

`recruiter_reply_service._write_reply` sets `EmailThread.gmail_thread_id` to the
recruiter's thread, and `email_tasks.send_outreach_email` passes
`thread_id=thread.gmail_thread_id` to Gmail. So the reply *does* land in the right
Gmail thread — for Gmail users.

`gmail_service.send_email` also accepts `in_reply_to` and `references` and builds
the headers correctly. **Nothing passes them.** And nothing could: the recruiter's
RFC822 `Message-ID` is read by `inbound_scanner._headers()` and then dropped on the
floor, because `CandidateMessage` has no field for it.

Gmail's `threadId` is a Gmail-side convenience. Outlook, Apple Mail, Thunderbird,
Superhuman and every ATS that ingests mail thread on `In-Reply-To`/`References`.
A recruiter reading in Outlook currently sees the reply as a new, unrelated
message that happens to share a subject line — which is precisely the "don't start
a new email thread" failure the ask names.

### 2.2 The change

Capture and carry the headers.

**Scanner** — `CandidateMessage` gains `rfc_message_id` and `rfc_references`, read
from the `message-id` and `references` headers.

**`RecruiterEmail`** — gains `rfc_message_id` (String 998, the RFC 5322 limit) and
`rfc_references` (Text).

**`Email`** — gains `in_reply_to` (String 998) and `email_references` (Text). The
column is *not* called `references`: it is a reserved word in both SQLite and
Postgres and would need quoting everywhere forever.

**`_write_reply`** sets both on the reply row, following RFC 5322 §3.6.4:

```
In-Reply-To:  <the message we are answering>
References:   <their References, if any> <their Message-ID>
```

**The sender** passes them through. Unconditional, not inbound-only: an outreach
follow-up on an existing thread should thread too, and `Email.in_reply_to` is
simply null everywhere it does not apply.

The inbound `Email` row (the recruiter's own message, stored so the conversation
reads from its real start) also carries its `rfc_message_id`, so a future
follow-up in the same thread can chain off it without going back to Gmail.

### 2.3 Test that pins it

The one that would have caught this: build a `RecruiterEmail` with a known
`Message-ID`, run `_write_reply`, send through a fake Gmail service, and assert
the MIME the service received carries `In-Reply-To: <that id>` and a `References`
ending in it. Header-level, not argument-level — the assertion should survive a
refactor of how the arguments get there.

---

## 3. Learn from feedback

### 3.1 What signal exists

Every drafted reply ends one of three ways, and all three are already API calls:

| Action | Endpoint | Means |
|---|---|---|
| Approve | `POST /review/emails/{id}/approve` | the classification was right |
| Discard the draft | `POST /review/emails/{id}/dismiss` | the classification was wrong, or the reply was |
| Dismiss the message | `POST /recruiter-inbox/{id}/dismiss` | this was not worth answering |

None of them is recorded as a judgement. The row's `kind` and
`classification_confidence` are frozen at classify time and nothing ever revisits
them.

### 3.2 The design

Two tables, for the reason the codebase already splits `Email.open_count` from
`email_events`: one is the audit trail, the other is the number read on the hot
path.

**`reply_feedback`** — one immutable row per signal. `user_id`,
`recruiter_email_id`, `signal` (`APPROVED` | `REJECTED`), `kind` and
`classification_confidence` *as they were at the time*, `sender_address`,
`sender_domain`, `source` (which endpoint). This is what answers "why did the
product get more confident about this sender?" six months later.

**`classifier_priors`** — the rolled-up counters actually read during
classification. Keyed `(user_id, scope_type, scope_value)` where `scope_type` is
`address` or `domain`. Holds `approvals`, `rejections`, `last_signal_at`.

Two scopes because they answer different questions. A specific recruiter you have
approved three drafts for is strong evidence about *them*. A domain you have
rejected eleven messages from is evidence about a whole sending platform. Address
beats domain when both exist.

### 3.3 How the adjustment applies

`classifier_feedback.adjustment_for(db, user_id, address)` returns a delta in
`[-max_penalty, +max_boost]`, defaults `[-0.30, +0.10]`:

```
+0.05 per approval, capped at +0.10   (two approvals is all the credit on offer)
-0.10 per rejection, capped at -0.30
address prior wins over domain prior; otherwise they sum, still clamped
```

The asymmetry is the point. A wrong send is far worse than a missed draft, so
rejections move the number three times as far and three times as fast.

The adjusted confidence is what `reply_routing.decide` sees. The row records
**both**: `classification_confidence` stays the classifier's own reading, and a
new `confidence_adjustment` column records what feedback did to it, so the audit
trail still shows what the model thought.

### 3.4 The invariant that keeps this safe

> **An upward adjustment can never move a message from DRAFT into AUTO.**

Enforced in `recruiter_reply_service.process`, not by convention: the route is
computed twice — once on the raw confidence, once adjusted — and if the raw
confidence would not have cleared `recruiter_reply_auto_threshold`, the result is
clamped to `DRAFT`.

Without that rule, "approve four drafts from this recruiter" becomes a way to
teach the product to send unread mail to that recruiter, and the user performing
those approvals has no idea that is what they are doing. Downward adjustments are
unrestricted — feedback may always make the product quieter, never louder past
the bar. This mirrors the existing rule that a *templated* reply is never sent
unreviewed however confident the routing was.

### 3.5 Scope: what this is not

This is not model fine-tuning and not an embedding index. It is a per-user,
per-sender counter with a bounded effect, chosen because it is auditable, needs no
new infrastructure, works from the very first signal, and cannot fail in an
interesting way. "Similar patterns" is read as "the same sender or the same
sending domain" — the similarity measure a user would actually predict.

---

## 4. Recruiter follow-up detection

### 4.1 The two shapes of a follow-up

A recruiter who got an auto-reply and writes again does it one of two ways, and
they arrive through completely different code paths:

**(a) Same Gmail thread.** They hit reply. Once we answered, that thread has an
`EmailThread` row, so `inbound_scanner` skips it as `skipped_own_thread` and
`inbox_tasks.poll_thread` owns it. `poll_thread` drafts for review and never
auto-sends — so the "don't auto-reply twice" property already holds here by
accident. What is missing is *visibility*: the Recruiter Inbox row still reads
`REPLIED` and nothing tells the user their recruiter came back.

**(b) New thread.** They start a fresh message — extremely common when the
original outreach came from a platform (Gem, Loxo, Bullhorn), where each send is
its own thread. This one flows through the full inbound pipeline again as if it
were first contact, gets classified, matched, routed, and **can be auto-replied to
a second time.** That is the real bug.

### 4.2 The change

`RecruiterEmail` gains four columns:

```
escalated              bool     needs a human because we already engaged
escalation_reason      text     in the words the UI shows
follow_up_count        int      how many times they have come back
last_follow_up_at      datetime
previous_recruiter_email_id      the row we already replied to
```

**Path (b) — detection in `process()`**, before routing. Look for an earlier
`RecruiterEmail` for this user, same `reply_address` (the address a reply reaches,
not the `From` — for platform mail those differ, and keying on `From` would file
every recruiter on the platform as one person), in a status that means we actually
answered: `REPLY_QUEUED`, `REPLIED`, or `DRAFTED` with the draft since sent. If one
exists, the route is forced to `FLAG`, `escalated` is set, and the reason names the
date:

> "You replied to alex@acme.com on 12 July and they've written again. This one's
> for you rather than an automatic answer."

**Path (a) — a hook in `poll_thread`.** When an inbound message lands on a thread
whose application is linked to a `RecruiterEmail`, bump `follow_up_count`, set
`last_follow_up_at`, and set `escalated`. The row's `kind`/`route`/`status` are
left exactly as they were — they are the audit trail of a decision that was made
and does not change retroactively. Escalation is a separate axis, which is why it
is a flag rather than a new status.

**The Inbox surfaces it.** `_NEEDS_YOU` becomes "flagged, failed, **or
escalated**", so an escalated message appears in the "Needs you" view even though
its status still reads `REPLIED`. The counts follow.

### 4.3 Why not just re-reply

Because the second message is, by construction, the one an automatic answer is
worst at. It is a response to something we said — it may be a scheduling request,
a rejection, a question about salary, or a follow-up to a reply the candidate never
read. Answering it needs the transcript, and the whole reason the inbound path
exists as a separate pipeline is that first contact *has* no transcript. Handing
it to the user is not a degradation; it is the correct destination.

---

## 5. Dashboard — stats and trends

### 5.1 The missing denominator

`ScanResult` already carries everything the ask names — listed, examined,
detected, `skipped_known`, `skipped_own_thread`, `skipped_from_self`,
`skipped_bounce`, `skipped_opt_out`, `skipped_unfetchable`, `deferred`, the query,
any error — and `inbound_scanner.scan` logs the whole dict once per scan and then
drops it. "Emails scanned" is not answerable from the database at all. Neither is
"skipped, and why".

### 5.2 New table: `recruiter_scan_runs`

One row per scan, written by `recruiter_reply_service.record_scan` — the single
funnel every caller (beat, push, the button) already goes through. Carries the
whole `ScanResult` plus `user_id`, `gmail_account_id`, `trigger`
(`beat` | `push` | `manual`), and `created_at`.

The `trigger` field earns its place immediately: it is how you answer "is push
actually doing the work, or is beat still finding everything?" — the operational
question feature 1 creates.

### 5.3 Endpoint

```
GET /api/v1/recruiter-inbox/stats?days=30&bucket=day|week
```

```jsonc
{
  "totals": {
    "scans": 412, "listed": 9120, "examined": 388,
    "detected": 71, "classified_recruiter": 34,
    "replied": 22, "auto_sent": 4, "drafts_pending": 6,
    "flagged": 9, "escalated": 2, "dismissed": 5
  },
  "skipped": [                       // ranked, each with the user-facing reason
    {"reason": "already_seen", "label": "Already checked", "count": 8_602},
    {"reason": "own_thread",   "label": "Conversations you started", "count": 402}
  ],
  "trend": [                         // one point per bucket, oldest first
    {"period": "2026-07-21", "label": "21 Jul",
     "scanned": 1240, "detected": 11, "recruiter": 6,
     "replied": 4, "auto_sent": 1, "flagged": 2}
  ],
  "bucket": "day", "days": 30,
  "push": {"configured": true, "healthy": true, "notifications": 318,
           "last_notified_at": "2026-07-28T14:02:11Z"}
}
```

Two counting rules, stated because they are the ones a reader will second-guess:

* **Scan counters come from `recruiter_scan_runs`; outcome counters come from
  `recruiter_emails`.** They are different denominators over different time axes —
  a message detected on the 20th and replied to on the 21st is one detection on
  the 20th and one reply on the 21st. Merging them into one "funnel" would imply a
  conversion rate that does not exist.
* **Trend buckets are keyed on the event's own timestamp**, not on scan time —
  the same rule `received_at` follows everywhere else in this feature.

### 5.4 Frontend

A stats panel inside the existing Recruiter Inbox tab (`src/pages/Inbox.jsx`),
not a fifth nav destination. The reasoning from the original doc holds: a separate
page listing the same rows is how you end up with two places to act on one draft.

* `RecruiterStats` — headline tiles (scanned / detected / replied / pending /
  auto-sent / escalated), a "why messages were skipped" list with plain-English
  labels, and a `TrendBars` sparkline.
* `TrendBars` — an inline SVG bar chart. No charting library: the project has
  three runtime dependencies and this is a dozen `<rect>`s.
* Range toggle (7 / 30 / 90 days), which flips the bucket to weekly at 90.

---

## 6. Smart resume selection

### 6.1 What happens today

`email_attachments._base_resume_for` resolves, in order: the matched profile's
resume → the campaign's resume → `is_default desc, id desc`. For an inbound
recruiter reply there is no campaign resume, so it is the matched profile's
document, or the default.

That is already better than "always the default" — the profile match does real
work. But it is a decision made by *intent* ("you matched the backend profile"),
not by *this specific role*. A candidate with three resumes under one profile —
`platform-eng.pdf`, `ml-infra.pdf`, `staff-generalist.pdf` — always gets the same
one, whatever the recruiter is actually hiring for.

### 6.2 The selector

New `app/services/resume_selector.py`. Given the parsed job (from the recruiter's
own email — `Classification.as_job_text` + `parse_job`, exactly what `rematch`
already does) it scores every resume the user owns:

```
score = fit_scorer.score_fit(resume, parsed).overall      # 0..100, the existing scorer
      + filename/label affinity bonus                     # 0..10
```

The bonus is the "parse the filenames" half of the ask: tokens from
`Resume.filename`, `Resume.headline` and `Resume.target_roles` matched against the
role title and the job's required skills, worth up to 10 points. It breaks ties
between resumes the deterministic scorer rates equally — which is the common case,
since two resumes for the same person share most of their content — and it is
capped low enough that a filename can never outvote the content.

The winner must beat the incumbent (the profile/default resume) by
`recruiter_resume_switch_margin` (default **5** points) to displace it. A margin
rather than a plain `>` because these scores are noisy at the top and a
0.4-point difference is not a reason to send a different document than the one
the user's profile setting implies.

The choice and its reason are recorded on the `RecruiterEmail`
(`selected_resume_id`, `resume_choice_reason`) so the review screen can say
*"Attaching ml-infra.pdf — matched this role at 81 vs 64 for your default"*
instead of silently swapping the document.

### 6.3 The rule that is deliberately not relaxed

`email_attachments` documents an invariant worth restating:

> Never a tailoring run for some other job — its header carries that company's
> role title, and sending Acme's resume to Initech is worse than sending a generic
> one.

The ask says "match against available tailored resumes". `TailoredResume` rows in
this codebase are tailored **to a specific posting**, and their rendered PDFs carry
that posting's company in the header. Selecting one for a *different* recruiter's
role would put another company's name on the document the recruiter opens.

So the selector chooses among the user's **base resumes**, and a `TailoredResume`
is still only ever used for the posting it was tailored to — which the existing
`_tailored_for_posting` step, untouched and still first in the chain, already
handles. That covers the intent ("send the resume that argues for *this* role")
without the failure mode. This is the one place this change set narrows the ask,
and it is narrowed on purpose.

### 6.4 Where it runs

At **draft** time, recorded on the row; the sender resolves and renders from
`selected_resume_id` at **send** time. This keeps the existing property that a
draft sitting in review for three days goes out with the resume as edited today,
while making the choice visible while it is still reviewable. `plan_for_email` —
the "what will this carry?" preview — reads the same id, so the review screen and
the send can never disagree.

---

## 7. Data model summary

One migration, `f8d1c4b62a05`, down-revision `d2b6e8f04a71` (current head).

**`recruiter_emails`** — 10 columns:
`rfc_message_id`, `rfc_references` (§2); `confidence_adjustment` (§3);
`escalated`, `escalation_reason`, `follow_up_count`, `last_follow_up_at`,
`previous_recruiter_email_id` (§4); `selected_resume_id`, `resume_choice_reason` (§6).

**`emails`** — 2 columns: `in_reply_to`, `email_references` (§2).

**New tables:** `reply_feedback`, `classifier_priors` (§3); `recruiter_scan_runs` (§5).

Every added column is nullable or has a server default. This migration does not
rewrite a row.

---

## 8. Settings

```python
# Push-first ingestion
gmail_pubsub_project: str = "metal-cascade-500017-k3"
recruiter_scan_interval_seconds: int = 300     # was 60 — beat is now the fallback
recruiter_push_trust_seconds: int = 900        # silence past this and we poll anyway

# Feedback
recruiter_feedback_enabled: bool = True
recruiter_feedback_max_boost: float = 0.10
recruiter_feedback_max_penalty: float = 0.30

# Smart resume selection
recruiter_smart_resume_enabled: bool = True
recruiter_resume_switch_margin: float = 5.0
```

Every new behaviour is switchable, and every switch defaults to the safe reading:
feedback and smart selection are on because neither can send anything (feedback
cannot lift a message into AUTO; selection only changes which PDF travels), and
the two that govern *sending* — `recruiter_reply_enabled`,
`recruiter_reply_auto_enabled` — are untouched and still default off.

---

## 9. Test plan

New tests, by file. The bar is the same as the original feature: every branch that
decides whether something is *sent* gets a test, and every skip gets a test that
asserts the counter, not just the absence.

**`tests/test_gmail_push.py`** (extends) — a healthy, recently-notified watch is
skipped by beat and counted as `skipped_push_covered`; a watch silent past the
trust window is scanned; a failed/stopped/absent watch is scanned; an expired-but-
`active` watch is not trusted; `push_healthy` on `GET /gmail/status`.

**`tests/test_recruiter_threading.py`** (new) — headers captured by the scanner;
`References` chains `their-references + their-message-id`; a message with no
`References` yields a single-id chain; the reply's MIME carries `In-Reply-To`;
`threadId` still goes to Gmail; an outreach email with no `in_reply_to` sends
exactly the MIME it sent before.

**`tests/test_recruiter_feedback.py`** (new) — approve writes `APPROVED`; both
dismiss endpoints write `REJECTED`; priors roll up; address beats domain; the boost
and penalty clamps; **the DRAFT→AUTO clamp**; `confidence_adjustment` is recorded
separately from `classification_confidence`; feedback off is a no-op.

**`tests/test_recruiter_follow_up.py`** (new) — a second message from a replied
address escalates rather than routing; `reply_address` is what keys it, not `From`;
a first message from an unknown address does not escalate; a same-thread follow-up
bumps `follow_up_count` via `poll_thread`; escalated rows appear in "Needs you"
and in the counts; a prior message we *flagged* (never answered) does not escalate
the next one.

**`tests/test_recruiter_stats.py`** (new) — scan runs are written with the right
trigger for beat / push / manual; totals; skip reasons ranked with labels; daily
and weekly buckets; the empty case returns zeros rather than 404; another user's
scans are invisible; `days` bounds are validated.

**`tests/test_resume_selector.py`** (new) — the best-matching resume wins; the
filename bonus breaks a tie; the bonus cannot outvote content; the switch margin
holds the incumbent; a single-resume user is unchanged; a tailored resume for a
*different* posting is never selected; the choice and reason land on the row;
the feature off falls back to the current resolver exactly.

**Frontend, `src/pages/Inbox.test.jsx` + `src/components/RecruiterStats.test.jsx`** —
tiles render from the payload; the skip list shows plain-English reasons; the
range toggle refetches; the empty state; an escalated row shows its banner; the
trend renders one bar per bucket; a failed stats read does not break the tab.

**Count.** Backend baseline 1495, frontend 264. This change set adds tests in every
file above and removes none.

---

## 10. Rollout

1. **Migration + settings** — no behaviour change; every new column unread.
2. **Threading (§2)** — safe to ship alone, and the most immediately visible.
3. **Scan runs + stats (§5)** — write-only first, then the endpoint, then the UI.
4. **Follow-up escalation (§4)** — strictly reduces what gets sent.
5. **Smart resume (§6)** — changes which document travels; behind
   `recruiter_smart_resume_enabled`.
6. **Feedback (§3)** — last, because it is the only one that changes a routing
   input, and it wants the stats view (3) already in place to watch its effect.
7. **Push-first cadence (§1)** — last in production, because dropping beat to
   5 minutes is only safe once `push_covers` has been observed to be honest. The
   rollback is one setting: `RECRUITER_SCAN_INTERVAL_SECONDS=60`.
