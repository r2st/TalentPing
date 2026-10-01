# TalentPing — Design Document

**Status:** MVP (v0.1)
**Last updated:** 2026-07-22

---

## 1. Product summary

TalentPing is an AI-powered job-application automation platform. Instead of the
"spray and pray" auto-apply model that dominates the market (and that recruiters
increasingly auto-reject), TalentPing does **quality-targeted recruiter outreach
over email**:

1. The candidate builds a profile (resume + skills + preferences).
2. The candidate imports a database of recruiters/contacts.
3. For each recruiter, an LLM composes a genuinely personalized cold email using
   the candidate profile and the recruiter's context.
4. Emails are sent from the candidate's own Gmail, with warm-up and throttling.
5. Incoming replies are monitored, classified by intent, and an AI reply is
   drafted for the candidate to review/send.
6. A campaign dashboard tracks every outreach through its lifecycle.

The core insight from the [research doc](../ai-job-application-system-research.md):
LLM-personalized recruiter email outreach is the single most underserved gap in
the market — only LoopCV does it, and only with static templates.

## 2. Goals & non-goals (MVP)

### In scope (MVP)
- JWT-based user auth (register / login).
- Candidate profile: resume upload + PDF parsing, skills, experience, job
  preferences, target roles/industries.
- Recruiter database: add / bulk-import recruiters (email, company, role, industry).
- AI email composer: generate personalized outreach per recruiter.
- Email sending: Gmail API (OAuth2) with warm-up/throttling, CAN-SPAM footer.
- Inbox + auto-reply: poll Gmail, classify reply intent, draft an AI reply.
- Application tracker: dashboard of all outreach + status + replies.
- Campaign management: group outreach by target role/industry, schedule sends.

### Out of scope (MVP; see research §5.2–5.3)
- Job-board scraping / matching engine (schema stubbed with `pgvector`, not wired).
- Resume tailoring per JD.
- Stripe billing.
- Calendar / Cal.com scheduling.
- Browser extension, mobile app, negotiation agent.

## 3. Architecture

```
                     ┌────────────────────────────────────────┐
  React + Vite SPA   │  FastAPI (async)                        │
  (Tailwind)  ─────► │   /auth  /profile  /recruiters          │
        JWT          │   /campaigns  /emails  /applications     │
                     └───────────┬─────────────────┬───────────┘
                                 │                 │
                     ┌───────────▼──────┐   ┌──────▼────────────┐
                     │ PostgreSQL 16    │   │ Redis             │
                     │ + pgvector       │   │ (cache + broker)  │
                     └──────────────────┘   └──────┬────────────┘
                                                   │
                            ┌──────────────────────▼───────────────────┐
                            │ Celery worker + beat                       │
                            │  • send_outreach_email (throttled)         │
                            │  • poll_inbox → classify → draft reply     │
                            │  • warm-up scheduler                       │
                            └──────────────┬─────────────────────────────┘
                                           │
                      ┌────────────────────┼────────────────────┐
                      │                    │                     │
                ┌─────▼─────┐        ┌─────▼──────┐       ┌──────▼──────┐
                │ Gmail API │        │ OpenRouter │       │  PDF parse  │
                │ (OAuth2)  │        │ (free LLM) │       │  (pypdf)    │
                └───────────┘        └────────────┘       └─────────────┘
```

### Request flow: sending an outreach email
1. `POST /campaigns/{id}/generate` — for each target recruiter, the **AI composer
   service** builds a prompt (candidate profile + recruiter context) and calls
   OpenRouter, producing a draft `Email` row (status `DRAFT`).
2. The user reviews drafts in the dashboard and approves a campaign send.
3. `POST /campaigns/{id}/send` enqueues one Celery `send_outreach_email` task per
   approved email, each scheduled with a randomized throttle interval and subject
   to the per-mailbox daily limit and warm-up curve.
4. The task renders MIME (with CAN-SPAM footer), sends via Gmail API, records the
   `gmail_message_id`/`thread_id`, and advances the `Application` status to
   `OUTREACH_SENT`.

### Request flow: handling a reply
1. `poll_inbox` (Celery beat, every N minutes) lists new messages on tracked
   Gmail threads.
2. Each inbound message is stored as an `Email(direction=RECEIVED)`.
3. The **reply-classifier service** calls OpenRouter with a structured-output
   prompt → one of `INTERESTED / NOT_INTERESTED / SCHEDULING / QUESTION /
   OUT_OF_OFFICE / UNSUBSCRIBE / OTHER`.
4. Intent updates the `Application` status; for actionable intents the AI composer
   drafts a reply (`Email(direction=SENT, status=DRAFT)`) for user review.
5. `UNSUBSCRIBE` immediately removes the contact from all active sequences.

## 4. Data model

Core tables (see `backend/app/models/`):

| Table | Purpose |
|-------|---------|
| `users` | Job seekers. Auth credentials, Gmail refresh token ref. |
| `candidate_profiles` | 1:1 with user. Resume text, skills, experience, preferences, target roles/industries. |
| `recruiters` | Contacts: name, email, company, role/title, industry, source, opt-out flag. |
| `campaigns` | Named outreach campaign targeting role(s)/industry(ies), schedule, status. |
| `applications` | Central pipeline entity: user × recruiter × campaign, lifecycle status. |
| `email_threads` | Gmail thread wrapper (thread id, subject, message count). |
| `emails` | Individual messages (direction, subject, body, intent, sentiment, gmail ids). |

`applications.status` state machine:

```
QUEUED → OUTREACH_SENT → REPLIED → INTERESTED → SCHEDULING → INTERVIEW_SCHEDULED
                                 ↘ NOT_INTERESTED / NO_RESPONSE / UNSUBSCRIBED
```

Job/company/vector-matching tables from the research doc are intentionally
deferred; the `candidate_profiles.embedding` and future `jobs.description_embedding`
columns are the `pgvector` seam for v1.1.

## 5. AI layer

- **Provider:** OpenRouter, **free-tier models only** (default
  `openai/gpt-oss-20b:free`). We never call Anthropic directly. The service is a
  thin OpenAI-compatible client pointed at `OPENROUTER_BASE_URL`, so the model is
  swappable via env.
- **Composer prompt** follows research §3.3: candidate role/experience + recruiter
  name/company/specialization, < 130 words, one low-friction CTA, peer-to-peer tone.
- **Classifier** uses a constrained-output prompt and parses to an enum; on any
  ambiguity it falls back to `OTHER` (never guesses an actionable intent).
- All AI calls are isolated behind `services/ai_composer.py` and
  `services/reply_classifier.py` so they can be mocked in tests (no network in CI).

## 6. Email & deliverability

- **Send/receive:** Gmail API with OAuth2 (`gmail.send`, `gmail.readonly`).
- **Throttling:** per-mailbox `DAILY_SEND_LIMIT` (default 30) with randomized
  `MIN/MAX_SEND_INTERVAL_SECONDS` between sends — never bursts.
- **Warm-up:** week-over-week ramp (20–30 → 40–60 → 70–100 → 100–150) enforced by
  the sending scheduler.
- **CAN-SPAM:** every outbound email carries a physical postal address and a
  working unsubscribe link; unsubscribes are honored immediately.

## 7. Security

- Passwords hashed with bcrypt (`passlib`).
- JWT access tokens (HS256), short-lived, verified on every protected route.
- All secrets (JWT secret, OpenRouter key, Gmail client secret/token) come from
  env / the gitignored `keys/` dir — never committed.
- OAuth tokens are per-user and stored server-side; the frontend never sees them.
- CORS restricted to configured frontend origins.

## 8. Testing

- `pytest` with an in-memory SQLite DB and dependency overrides — no Postgres,
  Redis, Gmail, or OpenRouter needed in CI.
- AI and Gmail services are mocked; tests cover auth, profile CRUD, recruiter
  CRUD/import, campaign generation, and the classifier's parsing logic.

## 9. Roadmap (post-MVP)

1. Job-board aggregation + `pgvector` semantic matching + resume tailoring.
2. Stripe billing (Free / Starter / Pro / Unlimited per research §6.2).
3. Gmail push notifications (replace polling) + Cal.com scheduling links.
4. Analytics dashboard (open/reply/conversion) and GDPR data-subject portal.
