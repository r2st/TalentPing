# Subject Line A/B Testing — generate, split, converge

**Status:** Implemented
**Last updated:** 2026-07-27
**Depends on:** email tracking ([`email-tracking.md`](email-tracking.md)),
OpenRouter free models (shipped), outreach composition (shipped)

---

## 1. Overview

### 1.1 The gap

Subject lines are generated once, per email, inside the body composition call.
`ai_composer.compose_outreach()` asks the model for `Subject: …` on the first
line and falls back to `f"{cand.name} — {role}"`. There is exactly one subject
per message, nobody measures it, and the fallback — which is what every keyless
deployment and every failed LLM call produces — is the same string for every
recruiter that candidate ever contacts.

Since [`email-tracking.md`](email-tracking.md) now records opens, and the subject
line is the single largest lever on whether a cold email is opened at all, the
measurement loop can be closed.

### 1.2 What this feature adds

```
campaign starts
      │
      ▼
ensure_variants(db, campaign, cand)          ← once per campaign, cached
      │  LLM: 3 subject lines, distinct angles
      ▼
  SubjectVariant(label=A/B/C, text=…, sends=0, opens=0, replies=0)
      │
      │  per outreach email
      ▼
assign_variant(db, campaign)
      ├── no winner yet ──► uniform random over active variants
      └── winner decided ──► winner with probability EXPLOIT_SHARE (0.9),
                             otherwise explore
      │
      ▼
  Email.subject = variant.text ; Email.subject_variant_id = variant.id
      │
      ▼  send ──► variant.sends += 1
      ▼  tracking pixel fires ──► variant.opens += 1   (first open only)
      ▼  reply arrives ──► variant.replies += 1
      │
      ▼
maybe_converge(db, campaign)                 ← after every send
      └── every variant has ≥ MIN_SENDS_PER_VARIANT (10)
          and the leader beats the runner-up by ≥ MIN_LIFT (0.10 absolute)
          ──► winner_variant_id set, losers deactivated
```

### 1.3 Scope

**In scope:** LLM variant generation with a deterministic fallback, assignment,
per-variant send/open/reply counters, convergence, a stats endpoint and the UI
that renders it.

**Out of scope:** body A/B testing (one variable at a time, or neither result
means anything), cross-campaign learning (a subject that works for a fintech
backend role tells you little about a design role — and pooling across users
would leak one candidate's data into another's), and full Bayesian bandits. The
90/10 epsilon-greedy split below is the right complexity for samples of this
size.

---

## 2. Why the statistics are deliberately modest

A candidate's campaign is **tens of emails, not thousands**. At n=30 split three
ways, a 10-point open-rate difference is not significant by any honest test.
This feature does not pretend otherwise:

* Convergence needs `MIN_SENDS_PER_VARIANT = 10` **and** a `MIN_LIFT = 0.10`
  absolute gap. Below either, no winner is declared and the split stays uniform.
* Even after converging, `EXPLOIT_SHARE = 0.9` keeps 10% of sends on the other
  variants, so a winner picked from a thin sample can be overturned rather than
  locked in.
* The API returns `confident: false` and the UI captions the panel "early —
  not enough data yet" until the thresholds are met. Same discipline as
  `MIN_SAMPLE` in the analytics module.

The open-rate measurement inherits every bias listed in
[`email-tracking.md`](email-tracking.md) §2 — proxy prefetch, image blocking.
Those biases apply *equally across variants of the same campaign*, which is
precisely why comparing variants is defensible when quoting the absolute number
is not.

---

## 3. Generation

`services/subject_ab_service.py`, using the OpenRouter free tier
(`settings.openrouter_model`, default `openai/gpt-oss-20b:free`) via the shared
`chat_completion` chain:

```
System: You write subject lines for a job seeker's cold email to a recruiter.
        Produce exactly N distinct options, each under 60 characters, each
        taking a DIFFERENT angle: (a) role + proof point, (b) a specific,
        curiosity-opening question, (c) plain and direct with the candidate's
        name. No clickbait, no false urgency, no "Re:" or "Fwd:" fakery, no
        emoji. Never invent facts about the candidate or claim knowledge of an
        open role.
        Return ONLY {"variants": ["...", "...", "..."]}
```

JSON is mandatory, not stylistic: the free reasoning models return `content:
null` with the answer parked in `reasoning`, so the shared
`extract_json_object()` is the only reliable way to get clean text out (see the
module docstring on `openrouter_client`, and the `talentping-openrouter-free-tier-quirks`
note). Output is additionally filtered through `looks_like_reasoning()`, deduped
case-insensitively, stripped of a leading `Subject:`, and truncated to 120 chars.

**Fallback.** With no API key, a failed chain, or fewer than two usable variants
returned, three deterministic templates are used:

```
A: "{name} — {role}"                          (today's fallback, kept as control)
B: "{role} at {company}?"
C: "Quick question from a {seniority} {role}"
```

The deterministic path is what tests exercise, and what a keyless deployment
gets — the feature degrades to "three templates, still measured", never to
"crash".

---

## 4. Data model

### 4.1 `subject_variants` (new)

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `user_id` | FK `users.id` CASCADE, indexed | scoping key |
| `campaign_id` | FK `campaigns.id` CASCADE, indexed | the experiment's unit |
| `label` | String(2) | `A` / `B` / `C` |
| `text` | String(255) | |
| `is_active` | Boolean default true | false once deactivated by convergence |
| `is_winner` | Boolean default false | |
| `sends` / `opens` / `replies` | Integer default 0 | |
| `generated_with` | String(16) | `llm` or `template`, so the UI can say which |

Unique on `(campaign_id, label)`.

### 4.2 New column on `emails`

`subject_variant_id` → FK `subject_variants.id` SET NULL, indexed. Null means
the message predates the experiment or was composed outside a campaign; those
are excluded from every rate rather than counted as failures.

### 4.3 No winner column on `campaigns`

The obvious denormalization — `campaigns.subject_winner_id` — is deliberately
**not** added. Assignment already loads every arm (it needs them for the
exploring 10%), so the column would save no reads, and it would make `campaigns`
and `subject_variants` circularly FK-dependent — which SQLAlchemy cannot order
for `DROP`, breaking schema teardown in the test suite. `SubjectVariant.is_winner`
is the single source of truth.

---

## 5. Attribution

Counters move in exactly three places, each idempotent:

* **`sends`** — in `send_outreach_email`, after Gmail accepts the message. Not at
  compose time: a draft that is never approved must not count as an impression.
* **`opens`** — in the tracking service, only on an email's **first**
  non-prefetch open. Counting every re-open would let one enthusiastic reader
  win the experiment.
* **`replies`** — in `inbox_tasks.poll_thread`, on the first inbound `RECEIVED`
  row for the thread. Reply rate is the metric that actually matters; open rate
  is the one that converges fast enough to act on. Both are reported.

---

## 6. API and UI

```
GET /api/v1/analytics/subject-variants?campaign_id=…
```

Returns, per campaign, each variant with sends/opens/replies, open and reply
rates, `is_winner`, and a campaign-level `confident` flag. Scoped by joining to
`Campaign.user_id == user.id` — another user's campaign id returns 404, matching
`routers/inbox._owned_thread`.

The Pipeline analytics section renders a compact table: label, subject text,
sends, open rate, reply rate, with the winner marked and the "early" caption
until `confident`.

---

## 7. Testing

`tests/test_subject_ab.py`:

* generation with no API key returns three distinct deterministic variants
  labelled A/B/C, marked `generated_with="template"`;
* a stubbed LLM returning valid JSON produces `llm` variants; duplicates and
  blank entries are dropped; a response with one usable line falls back;
* a response that is chain-of-thought (`looks_like_reasoning`) falls back;
* `ensure_variants` is idempotent — a second call returns the same rows;
* assignment with no winner is uniform across active variants over many draws
  (each label appears; χ²-free, just a coverage assertion with a seeded RNG);
* assignment with a winner picks it ~90% of the time under a seeded RNG;
* `maybe_converge` does nothing below `MIN_SENDS_PER_VARIANT`, does nothing when
  the lift is under `MIN_LIFT`, and crowns + deactivates correctly when both
  thresholds are met;
* a first open bumps `opens` once; a second open does not;
* the stats endpoint is scoped by user and reports `confident: false` early.
