# TalentPing

**AI-powered job-application automation** — finds recruiter contacts, writes the outreach, sends it from your own Gmail, and tracks the replies.

TalentPing bypasses the ATS "spray and pray" trap entirely: instead of blasting application forms, it composes genuinely personalized cold emails to recruiters, sends them from your own Gmail with warm-up/throttling, then monitors the inbox, classifies each reply's intent, and drafts a suitable response.

> Positioning: **quality-targeted outreach, not volume.** See [`docs/DESIGN.md`](docs/DESIGN.md).

## The whole product, in three steps

Nothing has to be typed in that a resume already says, and there are no contact
lists to build:

1. **Connect email** — one-click Gmail OAuth. Outreach sends as you, so it is
   SPF/DKIM/DMARC aligned and replies come back to your own inbox.
   Setup: [`docs/GOOGLE-OAUTH-SETUP.md`](docs/GOOGLE-OAUTH-SETUP.md).
2. **Upload a resume** — drop in a PDF. Name, location, skills, seniority, job
   history and the roles to pitch you for are all extracted automatically
   (regex/keyword heuristics, then an LLM pass that fills the gaps and degrades
   silently if it fails). Upload several, one per role you're targeting — each
   becomes a **profile** (its own roles, locations, salary floor and level), and
   every job found is scored against all of them. The best-matching profile is
   the one whose resume and cover letter actually go out.
3. **Start outreach** — name some companies, or pick an industry. From there the
   autopilot runs on its own: crawl each company's careers page for recruiting
   contacts, write a personalized email per contact, and trickle the sends out
   over hours.

After that, a tracker shows every message, its status, and any reply with the
classified intent.

### How contacts are found

`services/career_scraper.py` resolves a company name to a domain, verifies the
site really belongs to that company (a responding domain proves nothing —
`linear.io` is parked while the company is at `linear.app`), locates the careers
page, and extracts addresses: `mailto:` links first, then bare addresses in the
page text, then LinkedIn recruiter profiles. `noreply@`, `press@`, freemail and
third-party vendor addresses are filtered out. When a company publishes nothing,
it falls back to conventional role mailboxes (`careers@`, `jobs@`, …) marked at
low confidence so the caller can tell a real find from a guess.

Results are cached **globally** by domain in `recruiter_cache`: the contacts for
a company are the same for every candidate, so the first user to target it pays
the crawl and everyone after reads the row.

## Tech stack

| Layer | Choice |
|-------|--------|
| Backend | Python 3.11+ / FastAPI (async) |
| ORM / DB | SQLAlchemy 2.0 + PostgreSQL 16 + `pgvector` |
| Cache / broker | Redis |
| Task queue | Celery + Celery Beat |
| AI | [OpenRouter](https://openrouter.ai) free-tier models (e.g. `openai/gpt-oss-20b:free`) |
| Email | Gmail API — per-user OAuth 2.0, send + receive |
| Scraping | `requests` + BeautifulSoup |
| Frontend | React + Vite + TailwindCSS |

## Repository layout

```
TalentPing/
  backend/            FastAPI service
    app/
      core/           config, security, database
      models/         SQLAlchemy models
      routers/        API endpoints
      services/       business logic (AI, Gmail, resume parsing, ...)
      tasks/          Celery tasks (sending, inbox polling)
    tests/            pytest suite
    alembic/          DB migrations
  frontend/           React + Vite + Tailwind SPA
  docs/               DESIGN.md and other design docs
  docker-compose.yml  Postgres (pgvector) + Redis + api + worker + beat
  keys/               gitignored — OAuth secrets & tokens
```

## Quick start (Docker)

```bash
cp .env.example .env          # fill in OPENROUTER_API_KEY, JWT_SECRET, etc.
docker compose up --build     # postgres + redis + api + celery worker + beat
# API:  http://localhost:8000  (docs at /docs)
```

Then run migrations (first boot):

```bash
docker compose exec api alembic upgrade head
```

## Local dev (without Docker)

Backend:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example ../.env    # edit values
uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev                   # http://localhost:5173 (proxies /api -> :8000)
```

## Tests

```bash
cd backend
pytest                        # uses an in-memory SQLite DB, no external services
```

## API surface

Deliberately small — most of the product is automation, not endpoints.

| | |
|---|---|
| `POST /auth/register`, `POST /auth/login`, `GET /auth/me` | accounts |
| `GET/POST /resumes`, `PATCH/DELETE /resumes/{id}` | step 1 — upload, parsed on the spot |
| `GET /resumes/suggested-preferences` | step 2 — the search read off *every* resume |
| `GET /gmail/authorize`, `GET /gmail/callback`, `GET /gmail/status`, `DELETE /gmail/accounts/{id}` | step 3 |
| `POST /campaigns` | creates *and* launches the autopilot |
| `POST /campaigns/{id}/pause`, `/resume` | control |
| `GET /onboarding` | which step the user is on |
| `GET /tracker`, `GET /tracker/{id}`, `PATCH /tracker/emails/{id}` | results |
| `GET /recruiters`, `POST /recruiters/discover` | inspect / preview discovery |

## Configuration

All configuration is via environment variables — see [`.env.example`](.env.example). Secrets live in `keys/` (gitignored). Nothing sensitive is ever committed.

Two that matter beyond the obvious:

- `TOKEN_ENCRYPTION_KEY` — Fernet key encrypting Gmail refresh tokens at rest.
  Rotating it forces every user to reconnect.
- `CELERY_ENABLED` — set `false` to run campaign work inline in the request
  instead of on a worker (single-process deployments, tests). Production runs
  workers and leaves this on.

## Compliance

Automated outreach is treated as commercial email: every message includes an opt-out mechanism and a physical postal address (CAN-SPAM), and sending is rate-limited with a warm-up schedule to protect deliverability. See the *Legal & Compliance* section of the design doc.

## License

MIT
