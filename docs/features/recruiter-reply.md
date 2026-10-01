# Recruiter Reply — detecting and answering inbound recruiter mail

**Status:** Proposed (design only — nothing implemented)
**Author:** Scout / engineering
**Last updated:** 2026-07-26
**Depends on:** Gmail OAuth (shipped), profiles (shipped), reply agent (shipped),
reputation gate (shipped)

---

## 1. Overview

### 1.1 The gap

Everything TalentPing does with email today is **outbound-first**. The pipeline
is: campaign → recruiter → outreach → thread → reply → classify → draft. Every
step hangs off an `EmailThread`, and every `EmailThread` hangs off an
`Application` we created.

That means the product is structurally blind to a recruiter who writes *first*.
The blind spot is visible in the code:

* `inbox_tasks.poll_all_inboxes` iterates `EmailThread` rows — conversations we
  started. A message in a thread we never sent into is never fetched.
* `inbox_tasks.ingest_push_notification` is even more explicit. When a push
  notification names a Gmail thread with no local row, it does exactly this:

  ```python
  # A thread we have no row for is mail this product didn't send and
  # isn't tracking — someone else's conversation in the same mailbox.
  if thread is None:
      continue
  ```

That comment was correct when it was written. It is now the feature gap: for a
candidate with a decent resume, *unsolicited* recruiter mail is a meaningful
share of real opportunity, and TalentPing throws all of it on the floor.

### 1.2 What this feature adds

A second ingestion path that reads the **whole mailbox**, not just our threads:

```
Gmail — all mail in the window, read or unread
      │  scan_all_recruiter_inboxes  (Celery Beat, every minute; also on push)
      ▼
  fetch messages not already tracked
      │
      ▼
  classify  ──► not a recruiter ──► record as NOT_RECRUITER, stop
      │
      ▼ recruiter/hiring-manager about an opportunity
  parse the opportunity out of the body (role, company, location, comp)
      │
      ▼
  match against the user's active Profiles  (deterministic fit scoring)
      │
      ├── confidence ≥ 90  ──► auto-reply      (opt-in, default OFF — see §9)
      ├── 70 ≤ confidence < 90 ──► draft for review  (lands in /review)
      └── confidence < 70  ──► flag only, no draft, no send
      │
      ▼
  RecruiterEmail row + "Recruiter Inbox" tab in the UI
```

### 1.3 Scope

**In scope**

* Periodic full-inbox polling for unread mail, per connected Gmail account.
* LLM classification of each message: recruiter opportunity or not.
* Deterministic profile matching against the user's active profiles.
* Context-aware reply generation grounded in the matched profile's resume.
* Three-band confidence routing (auto / draft / flag).
* `RecruiterEmail` persistence and a Recruiter Inbox view.

**Out of scope**

* Calendar integration — a reply may *offer* availability windows the user has
  written down, but it does not book anything.
* Replying to job alerts, newsletters, or ATS status mail. They are classified
  and recorded so the counts are honest, then ignored.
* Multi-turn autonomous negotiation. Once the candidate answers once, the
  conversation joins the existing thread machinery and behaves like every other
  thread: classified, drafted, reviewed.
* LinkedIn InMail. No LinkedIn scraping — that invariant stands.

### 1.4 Two grounding notes worth reading before the rest

**(a) There is no `businessId` in this codebase.** The brief asks for
businessId/user scoping; TalentPing has no tenant or business concept. The
equivalent — and the one every existing model uses — is `user_id` with
`ondelete="CASCADE"` and an index, plus ownership enforced in the router by
joining to `user.id` (see `routers/review._owned_draft`,
`routers/inbox._owned_thread`). Everything below uses that pattern. If a
business/tenant layer is ever added, `RecruiterEmail` inherits it the same way
every other table will.

**(b) The confidence bands are new, not existing.** The brief describes
"the AI confidence routing pattern from the codebase: ≥90% auto-reply, 70–89%
draft, <70% flag". There is no single constant expressing that today. The
closest existing precedents, which the bands below are modelled on, are:

| Precedent | Where | Shape |
|---|---|---|
| `min_fit_score` (default 70) | `models/autopilot.py:66` | below the bar → don't act |
| `autopilot_min_llm_fit_score` (50) | `core/config.py:141` | a second, softer LLM gate |
| `UNEVALUATED_CAP` (55.0) | `services/fit_scorer.py` | "unknown" is capped below "good", never neutral-good |
| three-source answer routing | `services/form_answers.py` | bank / resume / LLM / **unanswered → stop** |

So the 90/70 bands are consistent with house style — act above a high bar,
degrade to human review in the middle, refuse to guess at the bottom — but they
are being *introduced* here as `settings.recruiter_reply_auto_threshold` and
`settings.recruiter_reply_draft_threshold`, not reused.

---

## 2. User stories

**US-1 — Nothing gets missed.**
As a candidate with a public resume, when a recruiter emails me out of the blue,
I want TalentPing to notice within minutes so the message doesn't sit unread for
three days under a pile of job alerts.

**US-2 — Only real opportunities.**
As a candidate whose inbox is 80% LinkedIn alerts and Greenhouse
auto-acknowledgements, I want only genuine human outreach surfaced, and I want
the rest recorded rather than silently dropped so I can trust the filter.

**US-3 — The reply argues the right case.**
As a candidate with a "Backend Engineer, remote or Berlin" profile and a "Tech
Lead, Berlin only" profile, when a recruiter writes about a staff backend role in
Berlin, I want the reply to draw on the right resume — not whichever one happens
to be the default.

**US-4 — Nothing embarrassing goes out.**
As a candidate, I want to see and approve anything sent in my name by default. If
I later decide I trust it, I want to opt into automatic replies for the clearest
matches only, and I want to be able to switch that off in one click.

**US-5 — Ambiguity reaches me, not the recruiter.**
As a candidate, when TalentPing isn't confident which of my profiles a message
fits (or whether it fits at all), I want it flagged for me — not answered with a
generic hedge that costs me the opportunity.

**US-6 — One place to look.**
As a candidate, I want detected recruiter mail, its classification, the profile
it matched, and the reply that went out (or is waiting) in one list, with a
snippet of what they actually wrote.

**US-7 — My reputation is protected.**
As a candidate, I want automatic replies to obey the same warm-up, throttle and
bounce-pause rules as everything else the product sends from my mailbox.

**US-8 — I can correct it.**
As a candidate, when the wrong profile was matched, I want to switch it and
regenerate the reply rather than rewrite it from scratch.

---

## 3. Data model

### 3.1 New model: `RecruiterEmail`

`backend/app/models/recruiter_email.py`. One row per detected inbound message.
Follows house conventions: `Base, TimestampMixin`, `SAEnum(..., native_enum=False)`,
`user_id` first, cascade on delete.

```python
class RecruiterEmailKind(str, enum.Enum):
    RECRUITER_OUTREACH = "RECRUITER_OUTREACH"  # a human, about a specific role
    HIRING_MANAGER     = "HIRING_MANAGER"      # ditto, from the hiring side
    JOB_ALERT          = "JOB_ALERT"           # LinkedIn/Indeed digest, no human
    ATS_AUTOMATED      = "ATS_AUTOMATED"       # "we received your application"
    NOT_RECRUITER      = "NOT_RECRUITER"       # everything else
    UNKNOWN            = "UNKNOWN"             # classifier failed; never actioned


class ReplyRoute(str, enum.Enum):
    """Which confidence band the message fell into."""
    AUTO  = "AUTO"    # >= auto threshold: reply generated and sent
    DRAFT = "DRAFT"   # >= draft threshold: reply generated, awaits approval
    FLAG  = "FLAG"    # below both: recorded, no reply written


class RecruiterEmailStatus(str, enum.Enum):
    DETECTED     = "DETECTED"       # stored, not yet classified
    CLASSIFIED   = "CLASSIFIED"     # classified, no reply warranted
    FLAGGED      = "FLAGGED"        # needs a human look
    DRAFTED      = "DRAFTED"        # a reply is waiting in /review
    REPLY_QUEUED = "REPLY_QUEUED"   # auto-reply approved, with the sender
    REPLIED      = "REPLIED"        # reply confirmed sent
    IGNORED      = "IGNORED"        # user dismissed it
    FAILED       = "FAILED"         # pipeline error; see last_error
```

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | FK `users.id` CASCADE, indexed, **not null** | the scoping column; every query filters on it |
| `gmail_account_id` | FK `gmail_accounts.id` CASCADE, indexed | which connected mailbox it landed in — a user may have several |
| `gmail_message_id` | String(255), indexed, not null | idempotency key |
| `gmail_thread_id` | String(255), indexed | so a later message on the same thread is recognised |
| `from_address` | String(320), not null | |
| `from_name` | String(200) | |
| `subject` | String(998) | |
| `body_text` | Text | plain text, extracted by `gmail_service.extract_plain_text` |
| `snippet` | String(500) | first 240 chars, for the list view without loading bodies |
| `received_at` | DateTime(tz) | Gmail's `internalDate`, **not** poll time — same rule as `inbox_tasks._message_time` |
| `kind` | `RecruiterEmailKind` | classifier verdict |
| `classification_confidence` | Float 0–1 | the model's own confidence in `kind` |
| `classified_by` | String(60) | provider+model that served it, or `"rules"` on fallback |
| `extracted` | JSON | `{role_title, company, location, remote, salary_text, seniority}` — best effort, all nullable |
| `matched_profile_id` | FK `profiles.id` SET NULL, indexed | null when nothing matched |
| `match_score` | Float 0–100 | deterministic fit score of the winning profile |
| `match_reason` | Text | one line, UI only |
| `route_confidence` | Float 0–100 | the combined number the bands are applied to (§5.4) |
| `route` | `ReplyRoute` | which band fired |
| `status` | `RecruiterEmailStatus` | lifecycle |
| `reply_email_id` | FK `emails.id` SET NULL | the generated `Email` row (draft or sent) |
| `application_id` | FK `applications.id` SET NULL | set once we engage (§3.2) |
| `flag_reason` | Text | why a human is needed — shown verbatim in the UI |
| `last_error` | Text | truncated to 500, same as `GmailWatch.last_error` |
| `read_at` | DateTime(tz) | when the user opened it in the Recruiter Inbox |
| `created_at` / `updated_at` | from `TimestampMixin` | |

**Constraints**

```python
__table_args__ = (
    UniqueConstraint("user_id", "gmail_message_id", name="uq_recruiter_email_user_msg"),
    Index("ix_recruiter_emails_user_status", "user_id", "status"),
)
```

The unique constraint is what makes the poller safely re-runnable: a message
seen twice is an upsert no-op, exactly as `poll_thread` uses `known_ids` today.
`route`, `route_confidence` and `status` are deliberately separate — "which band
fired", "how sure were we", and "where is it now" are three questions the UI and
the tests each ask independently.

### 3.2 Reusing the existing thread machinery

`Email.thread_id` is not nullable, `EmailThread.application_id` is not nullable,
and `Application.campaign_id` is not nullable. `routers/review` enforces
ownership by joining `Email → EmailThread → Application.user_id`. So a reply that
must be reviewable and sendable **has to** materialise that chain.

When (and only when) a message routes to `AUTO` or `DRAFT`:

1. **Recruiter** — `get_or_create` on `(user_id, from_address)`. The
   `uq_recruiter_user_email` constraint already exists. `source="inbound"`,
   `confidence=1.0` (they wrote to us — the address is verified by definition).
   If `opted_out` is set, stop: we do not write back to someone who asked us not
   to, even if they just wrote to us. Flag instead.
2. **Campaign** — one lazily-created row per user, `name="Inbound recruiter
   replies"`, `auto_send=False`, `follow_up_enabled=False`. Chosen over making
   `Application.campaign_id` nullable because analytics, the tracker and the
   pipeline all assume a campaign exists; a synthetic campaign is a smaller blast
   radius than a nullable FK. Follow-ups are off because chasing a recruiter who
   contacted *us* is the wrong posture.
3. **Application** — `status=REPLIED`, `profile_id=matched_profile_id`, linked
   to the campaign and recruiter.
4. **EmailThread** — `gmail_thread_id` from the message, so the *existing*
   `poll_thread` picks the conversation up from here on. This is the handoff: the
   new path handles first contact, and the shipped path handles everything after.
5. **Email** — the inbound message stored as `direction=RECEIVED`,
   `status=RECEIVED`, plus the generated reply as `direction=SENT` with
   `status=DRAFT` (review band) or `QUEUED` (auto band).

Messages that route to `FLAG` create **none** of the above. A flag is a row in
`recruiter_emails` and nothing else; if the user dismisses it, one delete is the
whole cleanup.

### 3.3 Migration

`backend/alembic/versions/<rev>_recruiter_reply.py`, following the naming of
`c4d8e2f10a37_v2_smart_apply.py`. Creates `recruiter_emails`, its unique
constraint and its two indexes. Adds no columns to existing tables — the feature
is purely additive, which is what makes the rollback in §10 a one-liner.

---

## 4. API endpoints

All under `settings.api_v1_prefix`, in a new `backend/app/routers/recruiter_inbox.py`,
registered in `main.create_app`. Every handler takes
`user: User = Depends(get_current_user)` and filters on `RecruiterEmail.user_id
== user.id`; a row belonging to someone else returns **404, not 403** — matching
`_owned_thread` and `_owned_draft`, which refuse to confirm that another user's
id exists.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/recruiter-inbox` | list + headline counts; filters below |
| `GET` | `/recruiter-inbox/{id}` | one detected email, full body, match detail, reply |
| `POST` | `/recruiter-inbox/scan` | poll now (mirrors `POST /inbox/sync`) |
| `POST` | `/recruiter-inbox/{id}/read` | mark read |
| `POST` | `/recruiter-inbox/{id}/rematch` | re-run matching, optionally forcing `profile_id` |
| `POST` | `/recruiter-inbox/{id}/generate-reply` | write a reply for a flagged item the user wants answered |
| `POST` | `/recruiter-inbox/{id}/dismiss` | `status=IGNORED`; never re-surfaced |

**`GET /recruiter-inbox` query params:** `kind`, `route`, `status`, `unread`,
`profile_id`, `q`. Counts are computed over everything *before* filters are
applied, so the filter chips keep showing what they would reveal — the same rule
`routers/inbox.inbox` follows and states.

**Response shape** (`schemas/recruiter_inbox.py`):

```python
class RecruiterInboxCounts(BaseModel):
    detected: int
    unread: int
    flagged: int        # needs a human
    drafted: int        # waiting in /review
    replied: int
    by_kind: dict[str, int]

class RecruiterEmailRow(BaseModel):
    id: int
    from_address: str
    from_name: str | None
    subject: str | None
    snippet: str | None
    received_at: datetime
    kind: RecruiterEmailKind
    classification_confidence: float
    route: ReplyRoute | None
    route_confidence: float | None
    status: RecruiterEmailStatus
    matched_profile_id: int | None
    matched_profile_name: str | None
    match_score: float | None
    match_reason: str | None
    flag_reason: str | None
    reply_email_id: int | None      # → /review/emails/{id}/approve
    application_id: int | None      # → /inbox/threads/... once engaged
    read_at: datetime | None
```

**Deliberately not added:** approve/dismiss endpoints for the reply itself.
Those are `POST /review/emails/{id}/approve|dismiss` and `PATCH
/tracker/emails/{id}`, which already exist and already handle throttling and
reputation. `reply_email_id` is the pointer. The Inbox page learned this lesson
once — its docstring notes that two places to act on one draft meant "two chances
to send the same thing twice" — and this feature must not reintroduce it.

---

## 5. Service architecture

Five new modules, each with one job, each pure enough to test without a network.

```
tasks/recruiter_reply_tasks.py      Beat entrypoints, DB transactions
  └─ services/inbound_scanner.py    Gmail → candidate messages (dedup, filters)
  └─ services/recruiter_classifier.py  message → kind + confidence + extracted
  └─ services/inbound_matcher.py    extracted + profiles → best profile + score
  └─ services/inbound_reply.py      profile + message → reply body
  └─ services/reply_routing.py      confidence → AUTO | DRAFT | FLAG
```

### 5.1 `inbound_scanner` — finding candidate messages

Gmail's `users.messages.list` with a query string. `gmail_service` has
`list_thread_messages` and `get_message` but no list/search helper, so one is
added:

```python
def list_messages(account, query: str, *, max_results: int = 50) -> list[dict]
```

**Scope check: no re-consent needed.** `google_oauth.SCOPES` already includes
`https://www.googleapis.com/auth/gmail.readonly`, so `messages.list` over the
whole mailbox works with tokens users have already granted. Nothing about this
feature triggers a new OAuth screen.

The query, built by `inbound_scanner.build_query()`:
`-in:chats -in:trash -in:spam -from:me newer_than:7d`. The 7-day window bounds
the very first scan for a user with a 10,000-message backlog; steady state sees
only what arrived since.

It started life as `is:unread in:inbox newer_than:7d -category:promotions
-category:social`. Every one of those four filters has since been removed,
each after it was caught losing real opportunities:

* `is:unread` — a candidate who opened a recruiter's note on their phone before
  the beat tick made it permanently invisible. Re-reading costs nothing, because
  `known_ids` is what makes a rescan a no-op.
* `-category:promotions` and `-category:social` — agency recruiters mail from
  platforms Gmail files under both. The deterministic pre-filter in
  `recruiter_classifier` is a far better judge of a digest than the tab Gmail
  guessed, and costs no model call to run.
* `in:inbox` — now off by default (`RECRUITER_SCAN_INCLUDE_ARCHIVED=true`). A
  Gmail filter that labels recruiter mail and skips the inbox is a common setup
  for exactly this product's users, and every message it touched was invisible.

`RECRUITER_SCAN_QUERY` overrides the whole thing for a deployment that needs to.

**Listing pages.** `gmail_service.list_messages` follows `nextPageToken` up to
`_LIST_CEILING` (2000). Gmail caps a single page at 500 and says nothing about
it, and because it returns newest-first, an unpaginated read truncated at the
*old* end of the window: those ids were never listed, so they were never skipped,
never deferred and never counted — just gone.

Then, in order:

1. Drop messages whose `gmail_message_id` already exists for this user.
2. Drop messages whose `gmail_thread_id` matches an existing `EmailThread` —
   that is *our* outreach and belongs to `poll_thread`, not here. This is the
   line that keeps the two ingestion paths from double-handling a message.
3. Drop mail from the user's own addresses (`user.gmail_accounts` + `user.email`),
   same set `poll_thread` builds. Matched **exactly**, not as a substring —
   `"bob@acme.com" in "bob@acme.com.mx"` is true, and a recruiter at a domain
   that merely started with the user's own was being discarded as the user's own
   mail.
4. Drop bounces via the existing `reply_classifier.looks_like_bounce`, and book
   them through `reputation_service.record_bounce` — a bounce is reputation data
   wherever it is found.
5. Cap at `settings.recruiter_scan_max_per_run` (default 40) per account per run.
   The cap is **logged and returned**, never silent: the UI says "18 checked, 40
   left for the next scan", exactly as `POST /inbox/sync` reports
   `threads_skipped`.

Everything that survives also carries its `Reply-To`, when the sender set one
that differs from the `From` (see §5.2).

**Accounting.** Every listed id leaves through exactly one counter, and
`ScanResult.as_dict()` — logged once per scan and returned by `POST
/recruiter-inbox/scan` — carries all of them plus `listed` and the `query` that
produced them. The question this feature actually gets asked is never "what did
it find?" but "why did it not find *that* one?", and only a scan where nothing
leaves silently can answer it.

### 5.2 `recruiter_classifier` — is this a recruiter?

Two stages, cheap first.

**Stage 1 — deterministic pre-filter.** Sender-domain and header rules that need
no model: `noreply@`/`no-reply@` senders, `List-Unsubscribe` present with a known
job-board domain, `jobalerts-noreply@linkedin.com`, Greenhouse/Lever/Workday
notification addresses. These classify straight to `JOB_ALERT` or `ATS_AUTOMATED`
with confidence 0.95 and never reach the model. On a real inbox this is most of
the volume, and it is the difference between the feature costing 4 model calls a
day and 40.

**The `Reply-To` escape hatch.** The `noreply@` rule had a large blind spot. Gem,
Loxo, Bullhorn and every in-house ATS mail merge send from an unmonitored address
and put the actual recruiter in `Reply-To` — real, personal outreach that the
pre-filter was condemning on the strength of an address the recruiter never
chose. A `Reply-To` pointing somewhere answerable now suppresses that rule and
the message goes on to be read properly. (A `Reply-To` that is *itself* a
no-reply is still a no-reply.)

`RecruiterEmail.reply_to_address` stores it beside `from_address` rather than
replacing it, because they answer different questions: the `From` is who wrote
and is what the inbox shows; `reply_address` is where an answer goes and is what
the reply pipeline and the `Recruiter` contact row key on. Filing the contact
under the `noreply@` would collapse every recruiter sharing a platform into one
row, and addressing the reply there is worse than not replying — the product
reports it as answered.

**Stage 2 — LLM.** Only for what survives. Prompt in §6.1. Returns JSON.

**Fallback.** On `OpenRouterError` (i.e. *every* provider in the chain failed),
fall back to keyword rules and return `UNKNOWN` when they are inconclusive —
mirroring `reply_classifier.classify_reply`, which returns `OTHER` rather than
guessing an actionable intent. **`UNKNOWN` never routes to `AUTO` or `DRAFT`.**
A degraded classifier can lose us speed; it must not be able to send mail.

This is not a rare path — production spent days with every provider rate-limited
at once — so the fallback *grades* rather than gates. One recruiter phrase is
enough to draft; more buys more confidence, up to a ceiling that still cannot
auto-send.

Its two hint lists are **weighed against each other**, not short-circuited.
Marketing used to be checked first and win on a single phrase, so one line of
platform boilerplate outvoted every job word in the message — the same shape of
bug as the unsubscribe footer, and just as expensive. Marketing now has to
*outweigh*: a sales blast says "shop now" and "sale ends" and "40% off" and at
most brushes one recruiter phrase, while an agency email is the reverse. A tie
goes to drafting, which is the bias the whole module is built on — a false
positive costs the candidate a draft they delete, a false negative costs them a
job they never heard about. Platform preheaders ("view this email in your
browser") are stripped before either list reads the text, alongside the bulk-mail
footer.

### 5.3 `inbound_matcher` — which profile?

This reuses the shipped, deterministic machinery rather than asking a model to
pick:

```python
parsed  = jd_parser.parse_job_input(description=synthetic_jd, use_llm=False)
targets = profile_service.active_targets(db, user)
scores  = {t.profile_id: fit_scorer.score_fit(t.resume, parsed, t.targeting) for t in targets}
```

`synthetic_jd` is assembled from the classifier's `extracted` fields plus the
message body — the recruiter's email *is* a job description, just an informal
one. `use_llm=False` because the classifier has already done the extraction and a
second parse would pay twice for the same text.

This keeps the V2 invariant intact: **fit scoring stays deterministic.** A
candidate who sees 78 today sees 78 tomorrow, `match_score` is comparable with
every other score in the product, and the same "why did it drop?" question has
the same answerable form.

Two rules carried over from `profile_service`:

* **Ties go to the default profile.** Profiles that share a resume score
  identically all the time.
* **A user with no profiles still works** — `active_targets` falls back to
  autopilot preferences plus the default resume as one anonymous target, and
  `matched_profile_id` stays null while `match_score` is still real.

**Location is binding.** `settings.autopilot_enforce_locations` already gates
automatic outreach on the profile's `location_preferences`. The same gate applies
here: an on-site role in a city the profile does not list can be *drafted or
flagged*, but never auto-replied. A candidate auto-accepting interest in a city
they will not move to is the exact failure the location gate was built to stop.

### 5.4 `reply_routing` — the three bands

One pure function, no I/O, trivially unit-tested:

```python
@dataclass(frozen=True)
class RouteDecision:
    route: ReplyRoute
    confidence: float          # 0-100
    reason: str                # shown to the user verbatim

def decide(
    kind, classification_confidence, match_score,
    *, has_profile: bool, location_ok: bool, auto_enabled: bool,
) -> RouteDecision
```

`confidence` is the combined number the thresholds compare against:

```
confidence = 100 * classification_confidence * (match_score / 100)
```

Multiplicative, not averaged, and that is the point. A message we are 95% sure is
a recruiter, matching a profile at 40, is not a 67 — it is a confident read of a
bad fit, and it must not clear a 70 bar by averaging. Multiplying gives 38, which
flags. Both factors have to be strong for the product to act.

| Condition | Route |
|---|---|
| `kind` not in {`RECRUITER_OUTREACH`, `HIRING_MANAGER`} | no route; `status=CLASSIFIED` |
| `kind == UNKNOWN` | `FLAG` — "couldn't classify this one confidently" |
| no active profile matched | `FLAG` — "no profile fits this role" |
| `confidence < 70` | `FLAG` |
| `70 ≤ confidence < 90` | `DRAFT` |
| `confidence ≥ 90` **and** `auto_enabled` **and** `location_ok` | `AUTO` |
| `confidence ≥ 90` and either gate fails | `DRAFT` (never silently downgraded to nothing) |

`reason` is always populated, including on the happy path. "Matched *Backend
Engineer* at 92 — auto-replied" and "Matched *Backend Engineer* at 78 — drafted
for your review" are the same sentence with the decision swapped, which is what
makes the UI honest without a second explanation system.

### 5.5 `inbound_reply` — writing the reply

Reuses `reply_agent`'s guardrails rather than starting fresh. In particular
`_invents_specifics` and `_states_a_figure` — the checks that reject a draft
naming a salary, a date or a company detail that appears nowhere in the thread or
the resume. A model that invents "I'm available Tuesday at 3pm" in an auto-sent
reply is the worst outcome this feature can produce, and those two functions
already exist to prevent it.

Grounding for the reply, all of it from the matched profile:

* the profile's resume text (the only source of claims about the candidate);
* `target_roles`, `skills`, `location_preferences`, `remote_only`;
* `salary_min` / `salary_max`, used **only** to state a range if the recruiter
  asked — never volunteered;
* the candidate's name from `User.full_name`.

Output is a `ReplyDraft` (the existing dataclass): `body`, `template`, `note`.
`template` is `ReplyTemplate.INTERESTED` for the common case,
`ReplyTemplate.QUESTION` when the recruiter asked something specific.

**Post-generation gates, in order.** Any failure downgrades the route to `DRAFT`
(or `FLAG` if no usable body was produced) — it never sends and never silently
drops:

1. `looks_like_reasoning(body)` — free-tier reasoning models park the answer in
   `reasoning` with a null `content`, and the result reads "We need to write a
   reply. The user wants…". Shipping that to a recruiter is unrecoverable.
2. `_invents_specifics` / `_states_a_figure`.
3. Length sanity: 40–2000 characters.
4. Length sanity is the last gate. **No CAN-SPAM footer is appended** — unlike
   cold outreach. The recruiter wrote to us, so there is no list to leave, and a
   one-click opt-out on a reply to someone's own message reads as bulk mail.
   `email_tasks.send_outreach_email` asks
   `recruiter_reply_service.is_inbound_reply` and passes `unsubscribe_url=None`,
   which suppresses both the footer and the `List-Unsubscribe` headers.

### 5.6 Sending

Auto-replies go through **exactly** the existing send path — `Email` row set to
`QUEUED`, then `email_tasks.send_outreach_email`. That is not a convenience; it
is how the reputation invariant holds. `reputation_service.evaluate` gates every
send on warm-up day limits, the rolling 24h count, complaint pauses and bounce
pauses. An auto-reply that bypassed it would be the one send in the product that
can burn a user's mailbox.

If `evaluate` denies the send, the reply stays `QUEUED` and the sender retries on
its own schedule. The `RecruiterEmail` row sits at `REPLY_QUEUED` and the UI says
so.

---

## 6. LLM prompts (high level)

All calls go through `services.openrouter_client.chat_completion`, which
delegates to the `llm_router` chain: **OpenRouter free tier → Gemini → Groq →
Cerebras → `OpenRouterError`**. No Anthropic API, no SDK, plain `httpx`. Each
call site owns a deterministic fallback for the case where the whole chain fails.

Three provider facts shape every prompt below:

* **Always ask for JSON.** The free tier's reasoning models return `content:
  null` with the answer in `reasoning`; `extract_json_object` finds the object
  inside a scratchpad, so JSON is the only reliably parseable shape.
* **`temperature=0.0`** for classification and extraction; `0.6` for the reply,
  which is the only place voice matters.
* **Low `max_tokens`** on the classifier (~200). It is the highest-volume call in
  the feature.

### 6.1 Classification + extraction (one call)

*System:* You are triaging a candidate's inbox. Decide whether this email is a
real person contacting them about a specific job opportunity. Job-board digests,
automated ATS acknowledgements, newsletters and sales mail are **not** recruiter
outreach. Return JSON only.

*Schema:*

```json
{
  "kind": "RECRUITER_OUTREACH|HIRING_MANAGER|JOB_ALERT|ATS_AUTOMATED|NOT_RECRUITER",
  "confidence": 0.0,
  "role_title": null, "company": null, "location": null,
  "remote": null, "salary_text": null, "seniority": null,
  "asks": [], "reason": ""
}
```

*Rules in the prompt:* return `null` for anything the email does not state — do
not infer the company from the sender's domain, do not guess seniority from tone.
`asks` lists direct questions the recruiter posed, which is what §6.2 answers.
Confidence below 0.5 means "not sure", not "probably not".

Combining classification and extraction into one call is deliberate: the model
has already read the message, and a second call to extract fields it just parsed
doubles the cost of the highest-volume path for no accuracy gain.

*Fallback:* keyword rules → `UNKNOWN`, which never routes to a reply.

### 6.2 Reply generation

*System:* You are writing a reply on behalf of a candidate to a recruiter who
contacted them. Warm, specific, brief — under 180 words. Ground every claim about
the candidate in the resume provided. **Never invent** an availability slot, a
salary figure, a notice period, a visa status, or an experience the resume does
not contain. If the recruiter asked something the resume cannot answer, ask them
a question back instead of guessing. Return JSON only.

*User content:* the recruiter's message; the matched profile's name, target
roles, skills, locations, remote preference; the resume text; the extracted
`asks`; whether the candidate has stated a salary band.

*Schema:* `{"subject": "", "body": "", "note": "", "questions_asked": []}`

*The reply should:* acknowledge the specific role by name; state interest with one
concrete, resume-backed reason; answer the recruiter's questions where the resume
supports it; ask about anything material that is missing (comp band, remote
policy, team, stage); offer availability **only in the general terms the profile
supports** ("this week or next" — never a specific slot, because no calendar is
connected).

*Fallback:* a deterministic template built from the profile fields — the same
pattern `ai_composer` and `fit_scorer` use. A templated reply is a worse reply;
a reasoning-model scratchpad sent to a recruiter is a disaster. The template
route is always marked `DRAFT`, never `AUTO`: if no model could write it, no
model's confidence justifies sending it unseen.

### 6.3 What is *not* an LLM decision

Worth stating because it is the architectural line: the model classifies and
writes. It does **not** pick the profile (deterministic fit scoring), does **not**
set the confidence bands (arithmetic on two numbers), and does **not** decide
whether to send (routing + reputation gate). A model failure degrades quality;
it cannot change who gets emailed.

---

## 7. Celery tasks

Added to `celery_app.include` and `beat_schedule` in `tasks/celery_app.py`.

| Task | Trigger | Does |
|---|---|---|
| `scan_all_recruiter_inboxes` | Beat, every `recruiter_scan_interval_seconds` (default 60) | fan-out: one `scan_mailbox` per eligible `GmailAccount` |
| `scan_mailbox(account_id)` | fan-out / `POST /recruiter-inbox/scan` | fetch, filter, persist `DETECTED` rows, enqueue processing |
| `process_recruiter_email(recruiter_email_id)` | per detected row | classify → match → route → generate → persist |

```python
"scan-recruiter-inboxes": {
    "task": "app.tasks.recruiter_reply_tasks.scan_all_recruiter_inboxes",
    "schedule": float(settings.recruiter_scan_interval_seconds),
    "options": {"expires": float(settings.recruiter_scan_interval_seconds)},
},
```

**Why one minute.** This started at 15, on the reasoning that inbound mail is
"urgent to the hour, not to the minute". That was the wrong frame: the thing
being optimised is how long a recruiter waits for an answer, and a quarter of an
hour of it was spent waiting for a tick.

The quota arithmetic makes it cheap. A scan is one `messages.list` (5 units) plus
one `messages.get` per *new* message (5 each), so a tick that finds nothing costs
5 units. Steady state for a busy candidate is a handful of new messages an hour:
roughly 7k units/day/mailbox against a 1.2M/day project quota, and nowhere near
the 250 units/user/second ceiling. The expensive stage — classification — is
driven by new messages, not by ticks, so scanning fifteen times more often does
not cost fifteen times more.

**Not stacking up.** Three callers now aim at one mailbox: beat, Gmail push, and
the "scan now" button. `recruiter_reply_tasks.recently_scanned()` is the single
gate in front of all of them — a mailbox read inside
`RECRUITER_SCAN_MIN_GAP_SECONDS` (default 45) is left alone. Without it, a scan
slower than the beat interval has its successor queued behind it and a slow
mailbox becomes a queue that never drains. The button passes `force=True`: the
user asked, so the gate is for callers with no opinion about *when*. Beat ticks
also carry `expires`, so a stale scan is dropped rather than worked through — the
newer scan sees everything it would have.

**Eligibility for `scan_all_recruiter_inboxes`.** Only accounts where the user
has enabled the feature (`RecruiterReplyPreference.enabled`), the account has a
valid token, and the mailbox is not reputation-paused. Everything else is skipped
with a counted reason, returned in the task result dict — every existing task in
this codebase returns a small dict, which is what makes flower/logs readable.

**Separation of scan and process.** One row per task for processing, not a loop,
because processing makes 1–2 model calls and a slow provider must not hold the
scan's DB session open. It also means one poisonous message fails one task.

**Push integration.** `ingest_push_notification` used to `continue` on unknown
threads — "mail this product didn't send" — which is precisely the shape of a
recruiter writing in for the first time, the one message this product most wants
to see. That branch now counts the thread as untracked and calls
`_scan_for_inbound(account)`, which enqueues `scan_mailbox`. Push is
*complementary*, never exclusive — the same principle `_push_covers` already
encodes — and the hook is best-effort in every direction: the feature may be off,
the broker may be down, and the minute-cadence beat scan covers the same ground
regardless. It only makes inbound faster when push is working.

**Idempotency.** Every task is safe to replay: `scan_mailbox` dedups on
`(user_id, gmail_message_id)`, and `process_recruiter_email` is a no-op on any
row not in `DETECTED`. `task_acks_late=True` is already set globally, so a killed
worker replays rather than drops.

---

## 8. Frontend

**Stack, as it actually is:** React + Vite, plain `.jsx` (not TypeScript),
Tailwind, `react-router-dom`, Vitest + Testing Library. All API calls go through
`lib/api.js`.

### 8.1 Placement — a third Inbox tab, not a fifth nav item

`App.jsx` documents four nav destinations, and `/review` is a redirect to
`/inbox?tab=drafts` with the comment "Review was a second list of the same drafts
the inbox already held". Adding a fifth destination would repeat the mistake that
consolidation fixed.

So: **`/inbox?tab=recruiters`**, a third tab beside Conversations and Drafts,
with `/recruiter-inbox` redirecting to it for linkability.

```jsx
const items = [
  ["replies",   "Conversations", counts?.threads],
  ["recruiters","Recruiter Inbox", counts?.recruiters],   // new
  ["drafts",    "Drafts",        counts?.drafts],
];
```

### 8.2 Components

`frontend/src/pages/Inbox.jsx` gains a `RecruiterInboxView`, following the
existing `RepliesView` / `DraftsView` structure:

| Component | Role |
|---|---|
| `RecruiterInboxView` | header + counts, tabs, toolbar, list/detail split |
| `RecruiterFilters` | `FilterChip` row: All · Needs you · Drafted · Auto-replied · Not a recruiter; plus a profile `<select>` and the search input |
| `RecruiterEmailList` | rows: sender, subject, snippet, `formatWhen(received_at)`, unread dot, badges |
| `RecruiterEmailDetail` | full body, classification, match panel, reply panel |
| `MatchPanel` | matched profile name, `<FitScore />` (existing component) for `match_score`, `match_reason`, and a profile picker that calls `rematch` |
| `RouteBadge` | `auto-replied` / `drafted` / `needs you`, with `route_confidence` in the mono `[10px]` style used for counts |

The reply panel **reuses `DraftReply`** verbatim — the same Approve & send / Edit
/ Discard controls, hitting the same `/review` endpoints, including the existing
confirm dialog ("It will go out from your inbox to X. This can't be recalled.").
One draft, one code path, whichever tab you are looking at. The dialog names
`reply_to_address || from_address`, so it says the address that will actually
receive the answer.

**`Attachments`** sits above the draft body in `DraftReply` and in the Drafts
tab's `DraftCard`. A draft appears in three places — the Recruiter Inbox detail,
the Drafts tab, and inline in a Conversations thread — and all three now say the
same thing, fed by `reply_attachments` (`RecruiterEmailDetail`), `attachments`
(`ReviewItem`) and `attachments` (`InboxMessage`) respectively.

Attachments are resolved at *send* time by design — a draft can wait days in the
review queue and what should go out is the resume as it stands on the day, not as
it stood when the draft was written. The cost was that a draft carried no
filename anywhere the UI could reach, which made "the resume is queued" and
"there is no resume" identical on screen. `email_attachments.plan_for_email()`
runs the same resolvers **without rendering** (a review screen is read far more
often than a send happens), so the reviewer sees the document that is coming, or
the sentence explaining why none is. On a sent reply the recorded
`attachment_filename` wins over the plan: re-resolving would answer with today's
resume rather than the one that actually went.

### 8.3 `lib/api.js`

```js
// ---- recruiter inbox: inbound recruiter mail ----
recruiterInbox: (params = {}) => request(`/recruiter-inbox${qs(params)}`),
recruiterEmail: (id) => request(`/recruiter-inbox/${id}`),
scanRecruiterInbox: () => request("/recruiter-inbox/scan", { method: "POST" }),
markRecruiterEmailRead: (id) => request(`/recruiter-inbox/${id}/read`, { method: "POST" }),
rematchRecruiterEmail: (id, body) => request(`/recruiter-inbox/${id}/rematch`, { method: "POST", body }),
generateRecruiterReply: (id) => request(`/recruiter-inbox/${id}/generate-reply`, { method: "POST" }),
dismissRecruiterEmail: (id) => request(`/recruiter-inbox/${id}/dismiss`, { method: "POST" }),
```

`lib/constants.js` gains `RECRUITER_KIND_STYLE`, `REPLY_ROUTE_STYLE` and
`RECRUITER_STATUS_STYLE` as `[label, tailwindClasses]` pairs, matching
`REPLY_INTENT_STYLE`.

### 8.4 Setup page

Feature toggles live in `pages/Setup.jsx` (the autopilot's own switch is on the
pipeline header; its *fields* are in setup). A "Recruiter Inbox" section with:

* **Watch my inbox for recruiter emails** — master switch, default **off**.
* **Reply automatically to the clearest matches** — the `AUTO` band, default
  **off**, disabled unless the master switch is on, with copy that names the
  threshold: "Only when we're over 90% sure. Everything else waits for you."
* A read-only line: "Last scan: 12 minutes ago · 3 detected this week."

### 8.5 Empty and error states

Three genuinely different empties, following the `EmptyState` precedent of
distinguishing "filters too narrow" from "nothing exists":

* **Feature off** — what it does, and a link to Setup.
* **On, nothing found yet** — "Watching your inbox. Nothing from a recruiter
  yet." Plus a "Check now" button (`scanRecruiterInbox`).
* **Filtered to nothing** — "Widen the filters", with Clear.

Errors use `ErrorBanner` + `useToast`, and a failed first load renders the reason
with a Try again button rather than an eternal skeleton — the pattern `RepliesView`
already implements.

---

## 9. Safety: the one place this design conflicts with a shipped invariant

**Flagging this explicitly, because it is load-bearing.**

The requested ≥90% band auto-sends a reply. The codebase currently holds the
opposite as a hard rule, in three places:

* `routers/review` docstring: reply drafts are "always reviewed — sending an AI
  reply unseen is the one thing we never do".
* `models/autopilot.py:74–76`: "Replies are always drafted for review regardless
  — this only governs the initial outreach."
* The V2 autopilot design invariants: reply drafts never auto-send.

This design implements the ≥90% band as asked, and contains the change as
follows. If any of these are unacceptable, the band should be dropped and ≥90%
should simply mean "drafted, and sorted to the top":

1. **Default off.** `settings.recruiter_reply_auto_enabled = False`, plus a
   per-user `RecruiterReplyPreference.auto_reply_enabled` that is also off. Both
   must be true. No existing user's behaviour changes on deploy.
2. **Narrow scope.** It applies *only* to a first reply to unsolicited inbound
   mail — the case where the alternative is often no reply at all. The existing
   thread pipeline is untouched: replies on conversations we started are still
   always drafted.
3. **Every gate still applies.** Reputation, warm-up, throttle, opt-out,
   location, the invention checks, the reasoning-leak check. Any failure
   downgrades to `DRAFT`.
4. **Never for the fallback template**, and never for `UNKNOWN` classification.
5. **Visible and reversible.** The sent reply appears in the Recruiter Inbox and
   in the normal conversation thread, labelled `auto-replied` with its
   confidence. One switch in Setup turns it off.
6. **A first-week ceiling.** `settings.recruiter_auto_reply_daily_limit`
   (default 3). A misclassification storm costs the user three emails, not
   thirty.

If the invariant is judged absolute, delete `ReplyRoute.AUTO` from
`reply_routing.decide` and everything else in this document stands unchanged —
which is why the routing decision is one pure function.

### 9.1 New settings

```python
# --- Recruiter reply (inbound) ---
recruiter_reply_enabled: bool = False              # master kill switch
recruiter_scan_interval_seconds: int = 60
recruiter_scan_min_gap_seconds: int = 45           # debounce across all callers
recruiter_scan_max_per_run: int = 40
recruiter_scan_window_days: int = 7
recruiter_scan_include_archived: bool = True       # read All Mail, not just the inbox
recruiter_scan_query: str = ""                     # full override; "" builds from the above
recruiter_reply_auto_enabled: bool = False         # the auto band; see §9
recruiter_reply_auto_threshold: float = 65.0
recruiter_reply_draft_threshold: float = 0.0
recruiter_auto_reply_daily_limit: int = 20
recruiter_classifier_model: str = "openai/gpt-oss-20b:free"
```

Defaults follow house style: the feature ships inert and is switched on
deliberately, as `form_apply_enabled` and `linkedin_easy_apply_enabled` do.

---

## 10. Test plan

`backend/tests/test_recruiter_reply.py` (+ `test_recruiter_inbox_api.py`),
pytest, in-memory SQLite, no network. **Tests must run against
`backend/.venv`** — the system Python's FastAPI is too old and `conftest` fails
to collect.

### 10.1 Scanner

* Dedups on `(user_id, gmail_message_id)` — the same message twice is one row.
* Skips messages whose `gmail_thread_id` matches an existing `EmailThread`
  (the outbound path owns those).
* Skips mail from the user's own addresses, including a secondary connected
  Gmail that differs from the login email.
* A bounce is booked via `reputation_service.record_bounce` and creates no
  `RecruiterEmail`.
* Respects `recruiter_scan_max_per_run` and *reports* what it skipped.
* A `GmailNotConfigured` account is skipped without failing the run.
* The query reads past the inbox and past every category tab, and still never
  reads spam or trash. Narrowable back to `in:inbox`, overridable outright.
* Listing follows `nextPageToken` rather than truncating at Gmail's silent
  500-per-page cap — and asserts the cursor is carried between pages.
* A lookalike domain (`candidate@gmail.com.mx` against `candidate@gmail.com`) is
  not mistaken for the user's own mail.
* Every listed id leaves through exactly one counter: the test sums the reported
  skips plus detected plus deferred and asserts it equals `listed`.
* `Reply-To` is captured when it differs from the `From`, and *not* stored when
  it merely repeats it.
* The scan cadence: beat at 60s with a matching `expires`; a mailbox read inside
  the min-gap is refused for automatic callers and never refused for `force`;
  a push about an untracked thread wakes the scanner, and is a no-op with the
  feature off.

### 10.2 Classifier

* LinkedIn job-alert fixture → `JOB_ALERT` via the pre-filter, **zero model
  calls** (assert the stub was never invoked).
* Greenhouse acknowledgement → `ATS_AUTOMATED`, no model call.
* Human recruiter fixture → `RECRUITER_OUTREACH` with the role and company
  extracted.
* Model returns malformed JSON → `UNKNOWN`, no exception.
* No provider configured (the default in `conftest`) → rule fallback, and
  `UNKNOWN` never routes to `AUTO` or `DRAFT`.
* A `noreply@` sender with a human `Reply-To` is read rather than condemned; one
  whose `Reply-To` is *also* a no-reply still is. The reply and the `Recruiter`
  contact both land on the human address.
* The rule fallback survives a platform preheader ("view this email in your
  browser") on a real recruiter email, and still files an actual sales blast as
  `NOT_RECRUITER` — the two cases the weighing rule has to separate.

### 10.3 Matching

* Two profiles, one clearly right: the right one wins and `match_score` is the
  winner's.
* Identical profiles → the default wins (the tie rule).
* A user with no profiles → no crash, `matched_profile_id is None`, a real
  score from the fallback target.
* An inactive profile is never matched.
* Same input twice → identical score (determinism, asserted directly).

### 10.4 Routing — the table in §5.4 as parametrised cases

* 95% classification × 95 match → 90.25 → `AUTO` (with auto enabled).
* 95% × 95 with `auto_enabled=False` → `DRAFT`.
* 95% × 95 with `location_ok=False` → `DRAFT`.
* 95% × 80 → 76 → `DRAFT`.
* 95% × 40 → 38 → `FLAG`. **Explicitly asserts this is not 67.5** — the averaging
  bug the multiplicative rule exists to prevent.
* No profile → `FLAG` with a populated `flag_reason`.
* Every branch returns a non-empty `reason`.

### 10.5 Reply generation

* Grounded in the matched profile's resume, not the default resume — the
  multi-profile regression this feature is most likely to reintroduce.
* A draft naming a salary absent from the message and the resume is rejected
  (`_states_a_figure`) and the route downgrades to `DRAFT`.
* A reasoning-scratchpad response (`"We need to write a reply…"`) is caught by
  `looks_like_reasoning` and never sent.
* Total provider failure → deterministic template, and the route is `DRAFT`
  even if confidence was 95.
* The CAN-SPAM footer is **absent**, in the body and in the headers, on the auto
  band, on the reviewed band and on the tracked HTML alternative. The paired
  test sends ordinary cold outreach through the same task and asserts the footer
  *is* there, so the two can't drift into each other.

### 10.6 Persistence and the thread handoff

* An `AUTO`/`DRAFT` route creates recruiter + campaign + application + thread +
  two `Email` rows, with `thread.gmail_thread_id` set so `poll_thread` takes over.
* The synthetic campaign is created once and reused for the second inbound
  email.
* A `FLAG` route creates **only** the `RecruiterEmail` row.
* A recruiter with `opted_out=True` is never replied to — flagged instead.

### 10.7 API and scoping

* User A cannot read, rematch, or dismiss user B's `RecruiterEmail` → **404**.
* Counts are computed pre-filter (assert a filtered response still reports the
  unfiltered totals).
* `rematch` with an explicit `profile_id` belonging to another user → 404.
* `generate-reply` on a `FLAG` row produces a `DRAFT`, never a send.
* `scan` with `CELERY_ENABLED=false` runs inline and returns real counts (the
  `POST /inbox/sync` precedent), including the query it ran.
* A drafted reply's detail names the resume it will carry; with no renderer it
  names the reason instead; a message with no reply names neither.
* The detail exposes `reply_to_address` separately from `from_address`.

**Attachment plan** (`tests/test_email_attachments.py::TestAttachmentPlan`):

* The plan names the resume that will travel, and **agrees with what
  `files_for_email` actually attaches** — a preview that disagrees with the send
  is worse than no preview.
* It renders nothing (asserted by making `render_pdf` raise).
* No resume on file, and a missing renderer, each produce a sentence rather than
  silence.
* A broken lookup degrades to empty instead of breaking the page.

### 10.8 Sending

* An `AUTO` reply is `QUEUED` and dispatched through `send_outreach_email` — not
  sent by any new code path.
* A reputation-paused mailbox leaves it `QUEUED` and the row at `REPLY_QUEUED`;
  nothing is sent.
* `recruiter_auto_reply_daily_limit` caps auto-replies per user per day; the
  fourth downgrades to `DRAFT`.

### 10.9 Frontend (Vitest + RTL)

`frontend/src/pages/Inbox.test.jsx` extensions:

* The Recruiter Inbox tab renders rows with kind, route and match badges.
* A flagged row offers "Write a reply" and no send control.
* A drafted row renders `DraftReply` and approving calls
  `/review/emails/{id}/approve`.
* The profile picker calls `rematch` and re-renders the new score.
* All three empty states render for their conditions.
* A failed load shows `ErrorBanner` + Try again, not a permanent skeleton.
* A drafted reply names the resume it will carry; with none, it shows the
  server's sentence instead of an empty row; with nothing to say, the row is
  absent entirely.
* The confirm dialog names the `Reply-To` when the sender set one.
* The Drafts tab card names its attachments too — approving is irreversible, so
  the files should be visible before, not discovered afterwards on the sent row.

### 10.10 Coverage bar

Every band in §5.4, every fallback path in §5.2/§5.5, and the ownership boundary
are non-negotiable. The existing suite's shape — `test_inbox.py`,
`test_reply_agent.py`, `test_reputation.py` — is the model.

---

## 11. Rollout plan

### Phase 0 — Foundations (no behaviour change)

* `RecruiterEmail` model + Alembic migration.
* `gmail_service.list_messages`.
* `reply_routing.decide` + its unit tests.
* `settings` block, everything defaulted off.

**Ships dark.** Nothing runs; the migration is additive. Merge risk ≈ zero.

### Phase 1 — Detection only (internal)

* Scanner + classifier + matcher + the Beat task.
* No reply generation at all: every route is recorded, none produces an `Email`.
* Enabled for the developer's own account via `recruiter_reply_enabled`.

**Exit criteria, measured over ≥100 real messages:**

* Classification precision ≥ 95% on `RECRUITER_OUTREACH` (a false positive here
  becomes an email later — precision matters more than recall).
* Pre-filter catches ≥ 80% of automated mail with no model call.
* Median model cost per scan under one call per two messages.
* Zero double-handling: no message appears in both `recruiter_emails` and a
  `poll_thread`-ingested `Email`.

### Phase 2 — Draft mode (beta users)

* Reply generation on; `AUTO` band **hard-disabled** in config.
* Recruiter Inbox tab and Setup toggle ship.
* Everything ≥70 becomes a draft in `/review`.

**Exit criteria:**

* ≥ 70% of drafts approved with no edit or a trivial one.
* Zero drafts containing an invented figure, date, or claim (manual read of
  every draft in the beta — this is a small enough N to actually do it).
* Zero reasoning-scratchpad leaks.
* No reputation incident: no bounce-rate or complaint change attributable to the
  feature.

### Phase 3 — Auto-reply (opt-in), and push

* `AUTO` band available, defaulted off per user (§9).
* First cohort: users who approved ≥20 drafts unedited in phase 2 — evidence,
  not enthusiasm, as the qualifier.
* `recruiter_auto_reply_daily_limit` starts at 1, raised to 3 after two clean
  weeks.
* Push integration: `ingest_push_notification`'s unknown-thread branch enqueues
  `scan_mailbox`. Polling stays on.

**Exit criteria:** zero auto-replies the user later calls a mistake, over two
weeks. Any single incident reverts the cohort to draft mode while it is
understood.

### Phase 4 — General availability

* Feature toggle on by default for new users, in **draft mode**. Auto-reply
  remains opt-in indefinitely.
* Dashboard metrics: detected / replied / reply-rate, alongside outbound.

### Rollback

Additive by design. `recruiter_reply_enabled=False` stops all of it within one
Beat interval, with no data loss and no effect on any other pipeline. The
migration can be reverted independently since nothing else references
`recruiter_emails`.

---

## 12. Open questions

1. **Does the invariant in §9 hold?** This is the one decision that changes the
   shape of the feature rather than its details.
2. **Multiple connected mailboxes.** A user with two Gmail accounts gets both
   scanned; should the reply always go from the mailbox that received it? (This
   design says yes — replying from a different address to a recruiter who wrote
   to one is confusing and hurts deliverability.)
3. **Retention.** `NOT_RECRUITER` rows store a subject and a snippet of mail that
   is, by definition, none of the product's business. Proposal: keep the
   classification and drop `body_text`/`snippet` for anything classified
   `NOT_RECRUITER` after 30 days.
4. **First-scan backlog.** `recruiter_scan_window_days = 7` bounds it. Should a
   new user get an optional deeper one-time backfill (30–90 days)?
5. **Follow-ups.** If a recruiter never answers our reply, do we chase? This
   design says no (`follow_up_enabled=False` on the synthetic campaign) — they
   contacted us; chasing inverts the dynamic. Worth revisiting with data.
