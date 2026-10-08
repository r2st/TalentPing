"""Application configuration, loaded from environment / .env.

All settings are typed and validated by pydantic-settings. Nothing sensitive is
hard-coded — secrets come from the environment or the gitignored ``keys/`` dir.
"""
from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _declared_environment(info: ValidationInfo) -> str:
    """Which environment *these settings* are for.

    Read off the ``environment`` field being validated alongside, and not from
    ``os.getenv`` — which is what every production guard below used to consult,
    and which is a different question with a different answer.

    ``model_config`` lists ``env_file``, and pydantic-settings reads that file
    itself: it never exports what it finds into ``os.environ``. So a deployment
    whose ``.env`` says ``ENVIRONMENT=production`` — the spelling the env_file
    support exists for, and the one a hand-started worker, an ``alembic`` run or
    a one-off script gets — produced ``settings.environment == "production"``
    while every guard read ``development`` from the process env and waved the
    settings through. All five at once: ``DEBUG=true`` serving tracebacks to
    the internet, the shipped placeholder ``JWT_SECRET`` letting anyone forge a
    token for any account, ``BCRYPT_ROUNDS=4``, a credentialed CORS wildcard,
    and an empty ``TOKEN_ENCRYPTION_KEY`` storing Gmail refresh tokens in the
    clear.

    Every one of those docstrings argues that the mistake is invisible from a
    working production box and must therefore be caught at startup. Reading the
    wrong source is what made the check invisible too.

    ``environment`` is declared ahead of every field guarded by it, so it is
    always in ``info.data`` by the time these run. The fallback covers only the
    case where it failed its own validation.
    """
    declared = (info.data or {}).get("environment")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return os.getenv("ENVIRONMENT", "development")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- App ----
    # The product's own name. Titles the OpenAPI document and the root
    # response, and is not the agent's name — see `agent_name` below, which is
    # what the candidate actually reads.
    app_name: str = "DoAide AutoApply"
    # What the product calls its agent, everywhere it speaks to the candidate:
    # prompts introduce it by this name, and the UI labels its output with it.
    agent_name: str = "Scout"
    environment: str = "development"
    # Handed straight to FastAPI, where it decides whether an unhandled
    # exception renders as a generic 500 or as the full traceback. See
    # `_debug_off_outside_development` for why that is a startup-refusal
    # offence rather than a preference.
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    backend_cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # ---- Logging ----
    # Default resolved per-environment by `log_format` below: JSON where a
    # shipper reads it, text where a human does.
    log_level: str = "INFO"
    log_format: str = ""

    # ---- Rate limiting ----
    # How many reverse proxies sit in front of this app. 0 means trust nothing
    # and use the socket peer; production behind Caddy is 1. See
    # `core/rate_limit.client_address` for why this counts from the right —
    # a wrong value here is the difference between limiting the caller and
    # letting the caller pick their own bucket.
    trusted_proxy_hops: int = 0
    # The credential surface. Deliberately tighter than the LLM limits: these
    # are guarding against guessing and signup abuse, not quota exhaustion, and
    # a human logging in never approaches them.
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 300
    register_rate_limit: int = 5
    register_rate_window_seconds: int = 3600
    # Changing a password requires the current one, so this endpoint is a
    # password oracle for anyone holding a stolen token. Metered per user
    # rather than per address: the caller is authenticated, and the account is
    # what needs protecting.
    password_change_rate_limit: int = 5
    password_change_rate_window_seconds: int = 3600
    # Write endpoints (profile/board/notification/follow-up/review mutations).
    # Per user, generous enough that no real workflow hits it — the purpose is
    # to cap a runaway client or a stolen token churning writes.
    write_rate_limit: int = 60
    write_rate_window_seconds: int = 60
    # Public tracking endpoints (open pixel + click redirect). Per IP, because
    # the caller is a mail client with no session. Generous: a single email
    # with ten tracked links opens them all at once, but a sustained flood of
    # forged tokens is the abuse this bounds.
    tracking_rate_limit: int = 120
    tracking_rate_window_seconds: int = 60

    # ---- Security / JWT ----
    # The HMAC key every access token is signed with. The shipped value is a
    # placeholder and `_jwt_secret_not_placeholder` refuses to start production
    # on it — it is in the public repository, so leaving it in place lets
    # anyone mint a token for any account. Rotating it invalidates every
    # outstanding session at once, which is the intended emergency lever.
    jwt_secret: str = "change-me-to-a-long-random-string"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440
    # bcrypt's work factor. Every doubling costs an attacker twice as much and
    # costs a login twice as long, so it is the one security parameter that has
    # to be raised as hardware gets faster. It is settable mainly so the test
    # suite can drop it: at the default cost a hash is ~0.2s, and a suite that
    # registers a user per test spends its entire runtime inside bcrypt.
    # `_bcrypt_rounds_strong_enough` below refuses a lowered value outside
    # development, so this cannot leak into a deployment.
    bcrypt_rounds: int = 12

    # ---- Database ----
    # SQLAlchemy URL for the application database. The driver matters: this app
    # is tested on `postgresql+psycopg` (psycopg 3), and the pool settings below
    # are sized for a Postgres server. The default names the local development
    # cluster; see docs/api/ENVIRONMENT.md for the deployed form.
    database_url: str = "postgresql+psycopg://talentping:talentping@localhost:5432/talentping"

    # How many Postgres connections one process may hold. The default was
    # SQLAlchemy's own — 5 plus 10 overflow, a ceiling of 15 — against a server
    # that runs 40 request handlers at once, because that is Starlette's
    # threadpool size and every non-async route goes through it. The 16th
    # concurrent request therefore waited `pool_timeout` seconds and then got a
    # 500 reading "QueuePool limit of size 5 overflow 10 reached".
    #
    # A slow route makes that arrive far sooner than 40 users would suggest. The
    # session is opened by `get_db` before the handler runs and held until the
    # response, so an endpoint that calls a model holds its connection for the
    # whole call — up to 60 seconds per provider in the fallback chain. Fifteen
    # of those and nothing else in the process can reach the database: not the
    # login form, not the tracking pixel, not `/health`.
    #
    # A ceiling of 40, which is exactly the threadpool size — `app.main` ties
    # the two together, so the two halves of one limit can no longer drift
    # apart. Affordable against Postgres' default `max_connections` of 100: a
    # pool is a ceiling rather than a reservation, and the Celery prefork
    # children that share these settings run one task apiece and so only ever
    # open one connection each.
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    # Recycled well inside any idle-connection reaper between here and Postgres.
    # `pool_pre_ping` already catches a dead connection; this avoids paying for
    # the discovery on a connection that was always going to be stale.
    db_pool_recycle_seconds: int = 1800

    # ---- Redis / Celery ----
    # Three logical databases on one Redis: /0 for application caches, /1 for
    # the Celery broker, /2 for results. Kept apart so that flushing a cache
    # cannot drop queued work, and so the queue depth on /1 means what it says.
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    # When False, campaign work runs inline in the request instead of being
    # handed to a worker. Intended for single-process deployments and tests;
    # production runs workers and leaves this on.
    celery_enabled: bool = True

    # ---- AI (OpenRouter) ----
    # These ids are not forever. OpenRouter retired the ``:free`` variant of
    # gpt-oss-20b and Groq decommissions models on a published schedule, and
    # when that happens the provider answers 404 for a model that worked the
    # day before. The chain now names that fault instead of absorbing it —
    # see llm_router.ModelNotFound — but the fix is still to edit these.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-oss-20b"
    openrouter_classifier_model: str = "openai/gpt-oss-20b"
    openrouter_app_url: str = "https://job.doaide.com"
    openrouter_app_title: str = "DoAide AutoApply"

    # ---- AI fallback providers -------------------------------------------
    # Tried in order after OpenRouter. All three speak the OpenAI chat
    # completions dialect, so one client handles the lot; a provider with no
    # key configured is simply skipped.
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_model: str = "gemini-flash-latest"

    # Groq: second in the fallback chain after Gemini. Blank disables it, and
    # the chain simply moves on to Cerebras.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-120b"

    # Cerebras: last resort in the chain. Blank disables it, which leaves the
    # deployment with no provider behind Groq — `llm_is_configured()` reports
    # the chain as a whole, so an empty chain is a visible state, not a silent
    # fall back to templates.
    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    cerebras_model: str = "llama-3.3-70b"

    # Circuit breaker: after this many consecutive failures a provider is
    # skipped entirely for the cool-down, so one dead upstream costs a timeout
    # once rather than on every request.
    llm_breaker_threshold: int = 3
    llm_breaker_cooldown_seconds: int = 300

    # Rate limiting is not failure. A free-tier 429 means "too fast", so it gets
    # retried in-place and pauses the provider for seconds rather than counting
    # toward the breaker's cool-down. See app.services.llm_router.
    llm_rate_limit_retries: int = 2
    llm_rate_limit_backoff_seconds: float = 2.0
    llm_rate_limit_max_wait_seconds: float = 20.0
    # How long a throttled provider is skipped for when it did not send a
    # ``Retry-After``. Distinct from the backoff above, which is the gap between
    # two attempts inside one call: reusing that 2s as the pause meant a
    # provider that was out of quota for the day was re-tried every couple of
    # seconds, which is what kept every free tier exhausted. Doubles per
    # consecutive throttle up to the ceiling; a success resets it.
    llm_rate_limit_pause_seconds: float = 60.0
    llm_rate_limit_pause_max_seconds: float = 900.0
    # Floor between two calls to the same provider, so a burst of inbound mail
    # does not manufacture the 429s it then has to recover from.
    llm_min_interval_seconds: float = 1.1
    # Ceiling on one whole trip through the chain, wall-clock. The per-call
    # ``timeout`` bounds a single HTTP request; nothing bounded the product of
    # it with four providers and three rate-limit attempts each, so an upstream
    # that held the connection open and *then* said 429 cost 12 requests and
    # twelve minutes inside one caller — with the API's LLM routes being sync
    # handlers, that is a threadpool slot and a database session held for the
    # whole of it. Whichever is larger of this and the caller's own timeout
    # wins, so a caller asking for a long single attempt still gets one.
    llm_total_deadline_seconds: float = 120.0

    # ---- Frontend ----
    # Where the OAuth popup posts its result back to, and where the callback
    # bounces the browser after a successful connect.
    frontend_url: str = "http://localhost:5173"

    # ---- Platform administration ----
    # Comma-separated addresses that are always administrators. Reconciled onto
    # ``users.role`` at login, so it is a *bootstrap*, not a parallel authority:
    # the column stays the only thing an authorization check reads.
    #
    # It exists because the alternative first admin is an operator running SQL
    # on production — the exact "SSH onto the box to change a value" loop the
    # credentials screen is meant to close, reintroduced at the step before it.
    # Removing an address here does not demote the account; that is a deliberate
    # write through the admin API, so a typo in an env var cannot silently strip
    # the last administrator and lock the deployment out of its own settings.
    admin_emails: str = ""

    # ---- Gmail OAuth (per-user, authorization-code flow) ----
    # The OAuth client this deployment asks for Gmail consent as. Both halves
    # come from one Google Cloud project, and the redirect URI below has to be
    # registered on that same client or the callback is refused before any code
    # is issued. See docs/GOOGLE-OAUTH-SETUP.md.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/api/v1/gmail/callback"
    # Fernet key protecting OAuth tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = ""
    # Is this deployment's Google consent screen still unpublished?
    #
    # A Google OAuth client in "Testing" hands out refresh tokens that Google
    # expires **seven days** after issue, whatever the user does with them. The
    # mailbox then stops sending and stops fetching replies, and the product
    # goes quiet in a way that looks exactly like "no recruiter wrote back".
    #
    # This is not a feature switch — nothing behaves differently with it on. It
    # is a fact about the deployment that the app cannot discover for itself:
    # a token that is going to lapse on Friday is indistinguishable, right up
    # until it does, from one that will last a year. Setting it lets the product
    # warn *before* the week is up and name the fix (publish the consent screen)
    # instead of only reporting the wreckage afterwards.
    google_oauth_testing_mode: bool = False

    # ---- Gmail push notifications (Cloud Pub/Sub) ----
    # The Google Cloud project the topic lives in. Not read by the app — the
    # topic string below is fully qualified — but it is what
    # scripts/setup_gmail_pubsub.sh provisions against, and having both in one
    # file is what stops the script and the deployment disagreeing about which
    # project owns the subscription.
    gmail_pubsub_project: str = "project-ea8b53f8-7b58-4580-ac0"
    # The topic Gmail publishes mailbox changes to, as
    # "projects/<project>/topics/<topic>". Empty means push is off and every
    # mailbox is polled, which is the pre-push behaviour and always correct —
    # just slower. Registration is skipped entirely rather than half-configured.
    gmail_pubsub_topic: str = ""
    # Shared secret Pub/Sub appends to the webhook URL as ?token=…. The webhook
    # is necessarily unauthenticated (Pub/Sub has no user session), so this is
    # what stops anyone POSTing to it. Empty accepts anything — acceptable for
    # local development, and a deployment setting the topic should set this too.
    gmail_pubsub_token: str = ""
    # How often the beat task re-registers watches approaching their 7-day
    # expiry. Twice a day leaves plenty of room inside the 2-day renewal margin.
    gmail_watch_renew_interval_seconds: int = 43200
    # How long a thread keeps being polled after its last message.
    #
    # The five-minute sweep enqueued one `poll_thread` per thread that had ever
    # been given a Gmail id, with no upper bound of any kind — so the work per
    # tick was the account's entire history, and it only ever grew. A user a
    # year in re-fetched every conversation they had ever had, twelve times an
    # hour, forever, for the sake of a reply that was not coming to any of them.
    #
    # Ninety days is deliberately generous against how the poller is the *only*
    # route for mail on our own threads: `inbound_scanner` skips anything on a
    # thread we started (`skipped_own_thread`), so a bound set too tight would
    # not degrade detection, it would end it. A recruiter answering a cold email
    # after three months is rare enough to trade for a sweep whose cost is a
    # function of recent activity rather than of account age.
    #
    # 0 disables the bound and restores polling everything, for a deployment
    # that would rather pay than choose.
    inbox_poll_max_age_days: int = 90

    # How long a live conversation may sit on the candidate's last word before
    # the inbox calls it quiet. Only ever a *label* — nothing is sent, cancelled
    # or reclassified on this timer, so the cost of it being slightly wrong is a
    # badge the user disagrees with.
    #
    # A week rather than a working-days count: the number the user is really
    # asking is "did they say they'd get back to me last week", and a recruiter
    # who is on holiday is exactly the conversation worth surfacing.
    inbox_quiet_after_days: int = 7

    # ---- Sending / warm-up ----
    # The ceiling on outbound mail per mailbox per day, and the spacing either
    # side of each send. The limit is what the warm-up ramp climbs *towards*,
    # so lowering it lowers every step of the ramp; the interval bounds keep a
    # burst from looking automated to the receiving provider.
    daily_send_limit: int = 30
    min_send_interval_seconds: int = 90
    max_send_interval_seconds: int = 600
    # Per-step daily ceilings for a new mailbox, and how many days each step
    # lasts. 5/10/15/20 over three-day steps reaches the configured ceiling in
    # two weeks (see docs/features/email-warmup.md). Every step is capped by
    # ``daily_send_limit``, so lowering the ceiling lowers the whole ramp.
    warmup_schedule: str = "5,10,15,20"
    warmup_step_days: int = 3
    # The earliest day the full ceiling applies, even once the schedule above is
    # exhausted. Keeps "over two weeks" literal rather than an accident of how
    # the step arithmetic divides.
    warmup_ramp_days: int = 14

    # ---- Send-time optimization ----
    # Schedule outreach for the recipient's local business morning instead of
    # whenever the campaign happened to start. Off restores pure interval
    # spacing (see docs/features/send-time-optimization.md).
    send_time_optimization_enabled: bool = True
    # Fallback zone when nothing about the recipient resolves. An invalid name
    # degrades to UTC with a warning rather than failing startup.
    default_send_timezone: str = "UTC"
    send_window_start_hour: int = 9
    send_window_end_hour: int = 11

    # ---- Open / click tracking ----
    # Embeds a 1x1 pixel and wraps links in outbound outreach. Off sends plain
    # text with neither (see docs/features/email-tracking.md).
    email_tracking_enabled: bool = True
    # Public base URL the pixel and click links point at. Must be reachable by
    # the *recipient's* mail client, so it is the deployed API origin — not
    # localhost, in production.
    tracking_base_url: str = "http://localhost:8000/api/v1"

    # ---- Subject-line A/B testing ----
    # Generate several subject lines per campaign and converge on what gets
    # opened (see docs/features/subject-line-ab.md).
    subject_ab_enabled: bool = True
    subject_ab_variants: int = 3

    # ---- Weekly digest ----
    # The one email the product sends *to* its user. Swept hourly rather than
    # weekly so a worker that is down at the scheduled hour costs nobody their
    # digest; each user's row decides whether it is actually due.
    digest_scan_interval_seconds: int = 3600

    # ---- In-app notifications ----
    # How often state is re-read and turned into notices. Fifteen minutes is
    # set by the one condition that is genuinely urgent — a recruiter who wrote
    # and is waiting — and everything else the sweep looks at is measured in
    # days. The sweep is stateless and deduplicated by key, so a tick that is
    # late or doubled costs nothing; see `app/services/notification_sweep.py`.
    notification_sweep_interval_seconds: int = 900

    # ---- Feature-usage analytics ----
    # Internal only: one append-only row per deliberate act, read by
    # `GET /admin/usage` and by nothing else. No third party is involved and
    # nothing leaves the deployment — see `app/models/feature_event.py`.
    # Off writes no rows at all; the report then reads an empty table and says
    # so, rather than the screen disappearing.
    usage_analytics_enabled: bool = True
    # How long a usage row is kept. Half a year covers every window the report
    # offers plus a year-over-year glance at the edges of it; past that the rows
    # only slow down questions about the last month. `prune_usage_events` is
    # the only thing that deletes them.
    usage_analytics_retention_days: int = 180

    # ---- CAN-SPAM ----
    # The postal address and unsubscribe endpoint stamped into the footer of
    # every outbound message. CAN-SPAM requires both to be real: the shipped
    # address is an example and a deployment that mails strangers on it is
    # non-compliant. The value contains commas and is *not* a list — quote it
    # in `.env` or the comma-separated readers upstream will split it.
    compliance_physical_address: str = "123 Example St, Suite 100, San Francisco, CA 94105"
    compliance_unsubscribe_base_url: str = "http://localhost:8000/api/v1/unsubscribe"

    # ---- Job discovery ----
    # Google Jobs via SerpApi — the legal route to Indeed/LinkedIn/Workday
    # listings (research §3.3: never scrape those platforms directly). Optional:
    # without it, discovery still runs on the keyless public boards.
    serpapi_api_key: str = ""
    # How often the beat task sweeps for saved searches that are due.
    job_scan_interval_seconds: int = 1800
    # How often the beat task sweeps for follow-ups that have come due.
    follow_up_scan_interval_seconds: int = 900
    # Day offsets from the initial send for the default follow-up sequence: a
    # nudge on day 3 and a final touch on day 7. A campaign can override this
    # per-row (``Campaign.follow_up_step_days``); ``follow_up_count`` caps how
    # many of these are used. See docs/features/follow-up-sequences.md.
    follow_up_step_days: str = "3,7"
    # How often the autopilot runs the full auto-apply loop per active user.
    # Hourly by default — the warm-up ramp caps daily volume regardless.
    autopilot_scan_interval_seconds: int = 3600

    # ---- ATS board APIs (the company's own board, not an aggregator's copy) ----
    # Read the public JSON boards target companies publish (Greenhouse, Lever,
    # Ashby, Workable, SmartRecruiters). Free, keyless, complete for that
    # employer, and the only source that gives an exact posting date rather than
    # "when a crawler noticed". On by default; see services/ats_boards.py.
    ats_board_discovery_enabled: bool = True
    # Employers whose board is read per scan. Generous, because a board whose
    # token is already known is one request for that company's whole pipeline.
    ats_board_companies_per_scan: int = 8
    # Postings taken from any single board. A large employer can have hundreds
    # open; the scan's own filter and threshold do the selecting after this.
    ats_board_jobs_per_company: int = 40
    # Employers per scan whose token may be *guessed*. Metered separately from
    # the read budget above because discovery costs up to one request per
    # platform per spelling, where a known token costs one. Negative results are
    # cached, so over a few scans every employer in the feed gets its one probe.
    ats_board_probe_limit: int = 2
    # A board token is a stable fact, but companies do migrate ATS vendors —
    # long enough to be free in practice, short enough that a dead token heals.
    ats_board_cache_ttl_days: int = 14
    # An *error* row is not the same claim and must not share that lifetime. It
    # says a vendor was unreachable, throttling, or serving a WAF page when we
    # last looked, and none of those is a fact about the employer. Cached only
    # long enough that one sweep's failure does not become the next sweep's
    # identical failure; short enough that a transient outage heals the same day.
    ats_board_error_ttl_hours: float = 6.0

    # ---- Scout re-ranking (LLM quality pass over the deterministic score) ----
    # The deterministic fit score is the filter; Scout re-ranks only the best of
    # what survives it. One LLM call per scan covers this many postings — enough
    # to reorder the shortlist a candidate will actually read, cheap enough for
    # the free tier.
    llm_rerank_enabled: bool = True
    llm_rerank_top_n: int = 8
    # Scout's veto over auto-apply. A posting Scout scored below this is not
    # emailed about, however well it did on keywords — the whole point of asking
    # for a second opinion is to act on it when it disagrees. Postings Scout
    # never saw (no provider, re-rank off, outside the top N) are unaffected:
    # absence of a verdict is not a bad verdict.
    autopilot_min_llm_fit_score: int = 50
    # Floor on title relevance for an *automatic* application, on the 0-1 scale
    # in fit_scorer.title_relevance. 0.5 admits the same role and adjacent ones
    # (0.65) while rejecting a loose word overlap (0.3) and an unrelated title.
    # The last gate before an email is composed, and deliberately independent of
    # the fit score so a scoring regression can't reopen the hole on its own.
    autopilot_min_role_relevance: float = 0.5
    # Treat the candidate's stated locations as binding for *automatic* outreach:
    # a role outside them, and not marked remote, is not emailed about. The fit
    # score already weighs location, but it is a weighted average — a strong
    # showing on skills and salary carries a role in the wrong country over the
    # line, and "we applied on your behalf to a job you cannot take" is a worse
    # outcome than a missed application. Off restores the pre-gate behaviour, in
    # which locations only ever nudged the score.
    autopilot_enforce_locations: bool = True

    # ---- Cross-board deduplication ----
    # Normalized-title similarity at or above this counts as the same role when
    # the company matches and the locations are compatible. 0.86 keeps
    # "Sr. Software Engineer" ≈ "Senior Software Engineer" while holding
    # "Backend Engineer" and "Frontend Engineer" apart.
    dedup_title_threshold: float = 0.86

    # ---- Ghost-job detection ----
    # The default a new saved search gets for ``max_ghost_risk``. Postings
    # scoring above it are screened out at ingest and reported as ghosts in the
    # run result, never dropped silently. See :mod:`app.services.ghost_job` for
    # why the bar sits this high.
    ghost_default_max_risk: int = 85

    # ---- Market intelligence ----
    # Benchmarks are a deterministic model, so they only go stale when the
    # mapping table itself changes; a month keeps rows refreshed after a deploy.
    salary_benchmark_ttl_days: int = 30
    # Company research is external fact-shaped, so it refreshes weekly.
    company_profile_ttl_days: int = 7
    # Companies researched per scan, so one sweep can't fan out into hundreds of
    # lookups. The rest are filled in lazily when the candidate opens the card.
    company_research_per_scan: int = 5

    # ---- Form auto-apply (Playwright browser agent) ----
    # The browser agent fills (and, when asked, submits) an ATS application
    # form. Submitting is always opt-in per run; this switch is the server-wide
    # off button.
    form_apply_enabled: bool = True
    form_apply_headless: bool = True
    form_apply_timeout_ms: int = 45000
    # Attempts per run, for the transient failures only (see browser_runner).
    form_apply_max_attempts: int = 3
    # Ceiling on *submitted* form applications per user per rolling 24h. Filling
    # a form costs the candidate nothing and isn't counted.
    form_apply_daily_limit: int = 40
    # Where step screenshots are written. Mounted as a volume in Docker so the
    # audit trail survives a redeploy.
    form_apply_artifact_dir: str = "./data/form_apply"
    form_apply_screenshots: bool = True
    # How long those artifacts are kept. They are personal data — a screenshot
    # of a part-filled application shows the candidate's address and phone, and
    # the ``.txt`` written for an upload control is their whole career history —
    # and until ``app.tasks.artifact_tasks`` landed nothing removed them at all,
    # so the directory grew for the lifetime of the deployment. Thirty days is
    # long enough that "what did the agent actually submit?" is still
    # answerable, which is the only reason to keep them past the run.
    form_apply_artifact_retention_days: int = 30
    # Wall-clock ceiling on one run, whatever it is doing.
    #
    # ``form_apply_timeout_ms`` bounds a single navigation and ``Form``'s own
    # timeout bounds a single control; neither bounds the *run*. A Workday
    # wizard is eight steps, each of which can fill a dozen controls at five
    # seconds apiece and ask an LLM about the rest, so the per-operation
    # timeouts multiply into something with no useful ceiling at all — and a
    # run that never finishes holds a browser, a worker slot and a row stuck at
    # RUNNING until the sweep comes past half an hour later.
    #
    # Five minutes is well above a real application (the slowest observed
    # Workday run is a little over two) and well below the point where a wedged
    # run is indistinguishable from a stopped worker.
    form_apply_deadline_seconds: int = 300

    # ---- LinkedIn ----
    # Both of these are off by default, deliberately. LinkedIn's User Agreement
    # prohibits automated access, and the account at risk is the candidate's own
    # — so turning either on is a deployment's informed decision, not a default.
    #
    # Job source: LinkedIn's public *guest* listing endpoint, listing fields
    # only. Never member profiles, never anything behind the login.
    linkedin_job_source_enabled: bool = False
    # Easy Apply automation, which needs the candidate's stored credentials.
    linkedin_easy_apply_enabled: bool = False
    # Rolling-24h ceiling. 25 is the line below which LinkedIn's own anti-
    # automation heuristics leave a normal account alone.
    linkedin_easy_apply_daily_limit: int = 25
    # Minimum gap between two Easy Applies. Bursting is what gets noticed.
    linkedin_min_seconds_between_applies: int = 45

    # ---- Recruiter reply (inbound mail detection + auto-reply) ----
    # The whole feature's off switch. Off by default: this is the first thing in
    # the product that reads a user's *entire* mailbox rather than the threads it
    # started, so it is switched on deliberately per deployment and then per user.
    recruiter_reply_enabled: bool = False
    # How often the beat task sweeps watched mailboxes — now a *fallback*
    # cadence rather than the primary one. Gmail push tells us the moment mail
    # arrives (see services/gmail_push.py), so a mailbox with a healthy,
    # delivering subscription is detected in seconds and this tick skips it
    # entirely. Five minutes is what a mailbox waits when push is off, failed,
    # or has gone quiet.
    #
    # This was 60 when beat was the only route, which meant sixty scans an hour
    # per mailbox that mostly found nothing. The cost of one scan is still small
    # — one `messages.list` (5 quota units) plus one `messages.get` per *new*
    # message — but "small times nothing useful" is still waste, and the latency
    # it was buying is now bought by push instead.
    recruiter_scan_interval_seconds: int = 300
    # How long a push subscription's silence is still trusted. Past this, the
    # beat sweep scans the mailbox even though its watch claims to be active.
    #
    # This is the whole safety margin on making push primary. A watch can sit at
    # status="active" with a future expires_at while Google has quietly stopped
    # publishing — renewal keeps the row looking healthy either way, so "active"
    # is not evidence of delivery. Fifteen minutes of silence is enough to stop
    # believing it and go back to polling, which is the behaviour this feature
    # must always degrade to rather than to silence.
    recruiter_push_trust_seconds: int = 900
    # Floor on the gap between two scans of the same mailbox, whatever asked for
    # them. Beat, Gmail push and the "scan now" button all reach the same task,
    # and at a one-minute cadence a slow scan would otherwise have its successor
    # queued behind it before it finished. See recruiter_reply_tasks.
    recruiter_scan_min_gap_seconds: int = 45
    # Messages examined per mailbox per scan. Bounds the model spend of one run;
    # whatever is left is reported, never silently dropped, and picked up next
    # time — which at a one-minute cadence is a minute away, not a quarter hour.
    recruiter_scan_max_per_run: int = 40
    # How far back the Gmail query looks. Bounds the very first scan for a user
    # with a five-figure backlog; steady state only ever sees new mail anyway.
    recruiter_scan_window_days: int = 7
    # Whether to read past the inbox. Gmail filters routinely archive or label
    # mail without it ever touching the inbox, and a recruiter's note caught by
    # one of those rules was invisible to this scanner. Reading All Mail costs
    # nothing extra — the same window, the same dedup — and it is the difference
    # between "we scan your inbox" and "we scan your mail".
    recruiter_scan_include_archived: bool = True
    # Full override of the Gmail search, for a deployment that needs one. Empty
    # means "build it from the settings above" (see inbound_scanner.build_query).
    recruiter_scan_query: str = ""
    # Replying without the user reading it first. Off by default, and gated again
    # per user. Everywhere else in the product an AI reply is always reviewed (see
    # routers/review), so this is the one deliberate exception and it stays opt-in
    # at both levels.
    recruiter_reply_auto_enabled: bool = False
    # The auto bar, compared against classification x match (see reply_routing).
    # 90 was unreachable: it needed a 0.95 read of a 95-point fit, which in
    # practice never co-occur, so the auto band never once fired in production.
    #
    # 65 is chosen for a second reason as well. The keyword fallback the
    # classifier uses when every LLM provider is down is capped at 0.6
    # confidence, and 0.6 x 100 = 60 — below this bar at a *perfect* match. So an
    # LLM outage can still draft (see below) but can never send unread, without
    # that rule having to be written down anywhere else.
    recruiter_reply_auto_threshold: float = 65.0
    # The floor below which an identified recruiter gets no reply at all. Zero:
    # a real person who wrote about a job gets an answer, and a weak fit is
    # something for the reply to ask about rather than a reason for silence.
    # Raise it to go back to staying quiet below a given fit.
    recruiter_reply_draft_threshold: float = 0.0
    # Ceiling on unreviewed replies per user per rolling 24h. Sized to cover a
    # heavy inbound day rather than to ration replies — three meant a candidate
    # with real inbound traffic had the feature switch itself off before lunch.
    # The reputation gate, the warm-up ramp and the daily send limit all still
    # sit in front of every one of these.
    recruiter_auto_reply_daily_limit: int = 20
    recruiter_classifier_model: str = "openai/gpt-oss-20b"

    # ---- Reading again what no model could read the first time ----
    # A message is classified once, on arrival. On the free tiers the provider
    # chain answers roughly a third of the time, so most arrivals were read by
    # the keyword fallback, scored far below the auto bar by construction, and
    # then never asked about again — a rate limit lasting seconds became a draft
    # lasting forever. This sweep asks again. See
    # ``recruiter_reply_service.retry_degraded``.
    recruiter_retry_degraded_enabled: bool = True
    recruiter_retry_interval_seconds: int = 600
    # Rows per sweep. Small on purpose: each one costs a model call, and the
    # thing that produced this backlog was too many calls at once. At ten every
    # ten minutes the sweep clears a 450-row backlog in about eight hours while
    # staying well inside the per-minute windows that new mail also needs.
    recruiter_retry_batch_size: int = 10
    # Stop the sweep after this many rows in a row come back still unreadable.
    # Two is enough to tell a single unparseable message from a chain that is
    # down, and stopping then is what keeps a dead chain from costing a full
    # batch of provider timeouts every ten minutes.
    recruiter_retry_abort_after: int = 2
    # Messages older than this are left as they are. A re-read cannot make a
    # three-month-old note worth acting on, and sweeping them forever would
    # starve the recent mail that the ordering exists to reach.
    recruiter_retry_max_age_days: int = 30

    # ---- Catching up on mail that arrived while detection was down ----
    # The scan window above is a week, and it is the only thing that decides
    # what the pipeline can still see. So any outage longer than that — a dead
    # OAuth grant, a stopped worker, a Google client rotation — does not merely
    # delay the mail it covers, it makes it **permanently invisible**: the next
    # healthy scan looks back seven days, the messages are older than that, and
    # nothing ever looks further. Production lost ten days of recruiter mail
    # exactly this way in August 2026.
    #
    # This sweep is the thing that looks further. See app/tasks/backlog_tasks.py.
    recruiter_backlog_enabled: bool = True
    # How far back a catch-up scan reads. Thirty days rather than the week the
    # ordinary scan uses: it has to outlast an outage nobody noticed, and the
    # cost of the extra range is one wider `messages.list` — bodies are fetched
    # only for messages that have never been seen, and after the first pass
    # there are none.
    recruiter_backlog_window_days: int = 30
    # How often the catch-up runs. Deliberately slow: it is a safety net, not a
    # detector, and everything it finds that arrived *recently* was already
    # found by the five-minute scan.
    recruiter_backlog_interval_seconds: int = 21600
    # Messages handed to the classifier per user per run. This is the rate limit
    # that matters — the scan half is cheap and idempotent, and what must never
    # happen is a decade of backlog turning into a burst of model calls and a
    # burst of replies. Whatever is left stays DETECTED and is picked up by the
    # next run, so a large backlog drains over days rather than in one blast.
    recruiter_backlog_batch_size: int = 20
    # Messages *detected* per mailbox per run, which is a different and much
    # larger budget than the one above. Detection costs one Gmail `messages.get`
    # apiece and no model call, and a message that is detected but not yet
    # classified is already visible in the Recruiter Inbox — so finding the
    # backlog fast and answering it slowly is exactly the right shape. Five times
    # the live cap: at the live 40 a month of undetected mail on a busy mailbox
    # would take days merely to *see*. Whatever is left over is reported as
    # `deferred` and picked up by the next run.
    recruiter_backlog_scan_max_per_run: int = 200
    # Seconds between two of those, so a batch does not fire its model calls
    # simultaneously. Wider than the live path's ten: new mail is worth more
    # than backlog and should not have to queue behind it.
    recruiter_backlog_spacing_seconds: int = 30

    # ---- Learning from the user's own verdicts ----
    # Whether approving or discarding a draft feeds back into how confidently
    # the next message from that sender is read. On by default because it cannot
    # send anything: the boost is clamped, and an upward adjustment can never on
    # its own move a message from DRAFT into AUTO (see services/classifier_feedback).
    recruiter_feedback_enabled: bool = True
    # Ceilings on what history may do to a classification, in the classifier's
    # own 0..1 units. Asymmetric on purpose, and by a factor of three: a reply
    # sent in the candidate's name that should not have been is far worse than a
    # draft they never see, so rejections move the number further and faster
    # than approvals do.
    recruiter_feedback_max_boost: float = 0.10
    recruiter_feedback_max_penalty: float = 0.30

    # ---- Smart resume selection ----
    # Pick the resume that argues best for *this* role rather than taking the
    # matched profile's document by default. Safe to leave on: it only changes
    # which PDF travels, never whether anything is sent.
    recruiter_smart_resume_enabled: bool = True
    # How far ahead a challenger has to score before it displaces the resume the
    # profile/default resolution would have picked. A margin rather than a plain
    # ">" because two resumes for the same person share most of their content,
    # so their scores cluster — and a 0.4-point difference is not a reason to
    # send a different document than the user's own settings imply.
    recruiter_resume_switch_margin: float = 5.0

    # ---- Career-page scraping / recruiter discovery ----
    # How the crawler identifies itself, and how long it waits. The UA names
    # the bot and links a page explaining it, which is what lets a site owner
    # allow or refuse us deliberately rather than by fingerprint.
    scraper_user_agent: str = (
        "Mozilla/5.0 (compatible; DoAideAutoApplyBot/1.0; +https://job.doaide.com/bot)"
    )
    scraper_timeout_seconds: float = 12.0
    # How long a cached company scrape stays fresh before we re-crawl.
    recruiter_cache_ttl_days: int = 30
    # A crawl that ended in `status="error"` is cached too — otherwise a site
    # that reliably times out is re-crawled by every campaign that names it —
    # but it must not be trusted for a month. It records that a domain was
    # unreachable *once*, and this cache is deployment-wide, so one bad
    # afternoon would otherwise deny that employer's contacts to every user
    # until September.
    recruiter_cache_error_ttl_hours: float = 12.0
    # Cap on contacts auto-imported per company so one careers page can't flood a campaign.
    max_contacts_per_company: int = 5

    # ---- Dead letters ----
    # How long a *resolved* failure is kept — one an administrator replayed or
    # consciously ignored. The decision has been made; what remains is the audit
    # trail, and a month of it is what anyone would look back over.
    #
    # An open failure is kept three times as long (see
    # `tasks.dead_letter_tasks.prune_dead_letters`), because the two are not the
    # same kind of row: nobody has looked at an open one, so deleting it on the
    # same clock would quietly discard the record of a bug nobody triaged. Three
    # months without a single recurrence is the point at which it stops being
    # "what is broken now".
    dead_letter_retention_days: int = 30

    @field_validator("access_token_expire_minutes", "daily_send_limit")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("bcrypt_rounds")
    @classmethod
    def _bcrypt_rounds_strong_enough(cls, v: int, info: ValidationInfo) -> int:
        """Refuse a work factor that only looks like it is hashing.

        The suite lowers this to keep a couple of thousand registrations from
        costing fifteen minutes, which means the weak value lives in a checked-in
        conftest and travels with the repository. Nothing about a deployment
        running at cost 4 would ever look wrong — logins would succeed, and be
        fast — so the mistake is caught here rather than discovered from a
        cracked dump. bcrypt's own floor is 4; 12 is the default and the
        minimum outside development.
        """
        if not 4 <= v <= 31:
            raise ValueError("BCRYPT_ROUNDS must be between 4 and 31")
        if _declared_environment(info) != "development" and v < 12:
            raise ValueError(
                f"BCRYPT_ROUNDS must be at least 12 outside development (got {v}). "
                "A lower cost is a test-suite speedup, not a deployment setting."
            )
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_not_default(cls, v: str, info: ValidationInfo) -> str:
        if (
            _declared_environment(info) != "development"
            and v == "change-me-to-a-long-random-string"
        ):
            raise ValueError(
                "JWT_SECRET must be set to a strong random value in production. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        return v

    @field_validator("token_encryption_key")
    @classmethod
    def _token_key_not_empty(cls, v: str, info: ValidationInfo) -> str:
        if _declared_environment(info) != "development" and not v:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY must be set in production. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        return v

    @field_validator("gmail_pubsub_token")
    @classmethod
    def _pubsub_token_set_when_push_is_on(cls, v: str, info: ValidationInfo) -> str:
        """Refuse a configured push topic with no shared secret behind it.

        ``POST /gmail/webhook`` cannot be authenticated the usual way — Pub/Sub
        carries no user session — so ``gmail_pubsub_token`` is the only thing
        standing in front of it, and an empty token makes
        :func:`app.services.gmail_push.token_is_valid` return True for every
        caller. The handler then decodes whatever it was posted, looks up the
        mailbox by the address in the payload, and enqueues an inbox fetch for
        it.

        That is an unauthenticated trigger for Gmail API work against any
        connected mailbox whose address the caller can guess, which for this
        product is the user's own email address. Free to send, billed to us in
        Gmail quota, and repeatable.

        Keyed on the topic rather than on the environment, because the topic is
        what decides whether the webhook is reachable in practice: no topic
        means Google was never told to publish and registration is skipped, so
        an empty token there is the documented local-development case and stays
        allowed. Setting the topic is the act that opens the endpoint, and it is
        the same act that has to close it.

        ``gmail_pubsub_topic`` is declared above this field, so it is already in
        ``info.data`` by the time this runs.
        """
        topic = (info.data or {}).get("gmail_pubsub_topic") or ""
        if topic.strip() and not v.strip():
            raise ValueError(
                "GMAIL_PUBSUB_TOKEN must be set whenever GMAIL_PUBSUB_TOPIC is. "
                "The webhook is public by necessity and the token is its only "
                "gate; empty accepts anyone. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))" '
                "and append it to the push endpoint URL as ?token=…"
            )
        return v

    @field_validator("google_client_id")
    @classmethod
    def _google_client_id_is_a_client_id(cls, v: str) -> str:
        """Refuse a value that cannot be a Google OAuth client ID.

        Every real one ends in ``.apps.googleusercontent.com``. The failure this
        catches is transcription: the two credentials are copied out of the same
        console panel one after the other, and swapping them, truncating the ID
        at a line break, or pasting the project id instead all produce a string
        that is *set* — so ``is_configured()`` is True, the wizard offers the
        connect button, and the mistake first appears as Google's own
        ``invalid_client`` screen with nothing in the server log, because the
        browser never reaches the callback.

        Empty stays legal: Gmail is an optional integration and the app is
        designed to boot without it, telling the user so in step 1 of the
        wizard. This checks the shape of a value that is present, not that one
        is present.
        """
        v = v.strip()
        if v and not v.endswith(".apps.googleusercontent.com"):
            raise ValueError(
                "GOOGLE_CLIENT_ID does not look like a Google OAuth client ID — "
                "it must end with '.apps.googleusercontent.com'. Check it was not "
                "swapped with GOOGLE_CLIENT_SECRET or truncated on paste."
            )
        return v

    @field_validator("google_client_secret")
    @classmethod
    def _google_credentials_come_as_a_pair(cls, v: str, info: ValidationInfo) -> str:
        """Refuse half a credential, which is worse than none.

        ``google_oauth.is_configured()`` requires both, so exactly one of them
        reads to the app as "Gmail is not set up" — the same state as a fresh
        deployment. The operator who just pasted a client ID into ``.env`` and
        restarted has no way to tell the difference: the wizard says "Gmail
        sign-in isn't configured on this server yet", which is what it said
        before, so the natural conclusion is that the restart did not take.

        Named at startup instead, where the half-finished edit is still the
        thing that just happened.
        """
        v = v.strip()
        client_id = (info.data or {}).get("google_client_id") or ""
        if v and not client_id:
            raise ValueError(
                "GOOGLE_CLIENT_SECRET is set but GOOGLE_CLIENT_ID is not. Gmail "
                "stays switched off until both are present, indistinguishably "
                "from not being configured at all."
            )
        if client_id and not v:
            raise ValueError(
                "GOOGLE_CLIENT_ID is set but GOOGLE_CLIENT_SECRET is not. Gmail "
                "stays switched off until both are present, indistinguishably "
                "from not being configured at all."
            )
        return v

    @field_validator("google_oauth_redirect_uri")
    @classmethod
    def _redirect_uri_is_one_google_will_accept(
        cls, v: str, info: ValidationInfo
    ) -> str:
        """Refuse a callback URI Google will not register on a web client.

        Google's rule is about the *host*, not the deployment: a "Web
        application" client may only hold an ``https`` redirect URI, with
        loopback (``localhost`` / ``127.0.0.1`` / ``[::1]``) as the single
        exception that may be plain http. ``http://job.doaide.com/...``
        is therefore not a laxer setting than the https one — it is one that
        cannot be registered, so it fails as ``redirect_uri_mismatch`` at
        Google's own screen, before the browser reaches
        ``/api/v1/gmail/callback``. That is the failure this codebase documents
        as leaving no server-side trace at all: the silence is the fingerprint.

        Keyed on the host rather than on ``environment`` deliberately. An
        environment-keyed version refuses the loopback default whenever
        ``ENVIRONMENT=production`` is declared over a developer's ``.env`` —
        which several tests in this suite do to exercise unrelated guards, and
        which is a property of the harness rather than of any real deployment.
        A rule that only ever refuses a URI Google itself would refuse cannot be
        tripped that way.

        Checked only when a client ID is present: a deployment running without
        Gmail never uses this value, and refusing to boot over an unused default
        would punish a deployment for a setting it does not have.
        """
        v = v.strip()
        if not ((info.data or {}).get("google_client_id") or ""):
            return v
        parsed = urlsplit(v)
        if parsed.scheme == "https":
            return v
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme == "http" and loopback:
            return v
        raise ValueError(
            f"GOOGLE_OAUTH_REDIRECT_URI must be https (got {v!r}). Google accepts "
            "a plain-http redirect URI on a web client only for localhost, so this "
            "one cannot be registered: the connect flow dies at Google with "
            "redirect_uri_mismatch and never reaches the callback, leaving nothing "
            "in the server log."
        )

    @field_validator("debug")
    @classmethod
    def _debug_off_outside_development(cls, v: bool, info: ValidationInfo) -> bool:
        """Refuse to serve tracebacks to the internet.

        ``debug`` is passed to ``FastAPI(debug=...)``, and Starlette's debug
        handler answers any unhandled exception with the full traceback —
        frames, file paths, and the source line of each. A database that
        refuses a connection returns fifteen kilobytes describing the inside of
        the process to whoever asked; anything carrying a secret in a local
        variable returns that too.

        It is caught here for the same reason the JWT secret is: ``.env.example``
        ships ``DEBUG=true``, deployments are made by copying it, and nothing
        about a working production box would ever reveal the mistake. The
        failure mode is silent until the first 500, and then it is a disclosure
        rather than an outage.
        """
        env = _declared_environment(info)
        if v and env != "development":
            raise ValueError(
                f"DEBUG must be false when ENVIRONMENT={env!r} — FastAPI's debug "
                "handler returns full tracebacks, including file paths and source, "
                "in the response body of any unhandled error. Set DEBUG=false."
            )
        return v

    @field_validator("trusted_proxy_hops")
    @classmethod
    def _hops_not_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("TRUSTED_PROXY_HOPS cannot be negative")
        return v

    @field_validator(
        "login_rate_limit",
        "register_rate_limit",
        "password_change_rate_limit",
        "write_rate_limit",
        "tracking_rate_limit",
    )
    @classmethod
    def _rate_limit_not_negative(cls, v: int, info: ValidationInfo) -> int:
        """Zero is allowed and means closed; below zero means nothing.

        Zero is a real lever — it is how the credential surface gets shut during
        an incident without a code change — and the limiter answers it with a
        clean 429. A negative is a typo, and one that used to reach the store
        and be treated as the same thing, so it is refused here where the fix is
        obvious rather than at 3am in a stack trace.
        """
        if v < 0:
            raise ValueError(f"{info.field_name.upper()} cannot be negative")
        return v

    @field_validator(
        "login_rate_window_seconds",
        "register_rate_window_seconds",
        "password_change_rate_window_seconds",
        "write_rate_window_seconds",
        "tracking_rate_window_seconds",
    )
    @classmethod
    def _rate_window_is_positive(cls, v: int, info: ValidationInfo) -> int:
        """A window of zero seconds is not a tight limit; it is no limit.

        Every hit is measured against `now - window`, so with a window of zero
        nothing recorded is ever inside it and the budget reads as untouched on
        every request. `LOGIN_RATE_WINDOW_SECONDS=0` therefore switched off the
        limit on the login route while still reporting a limit of ten in the API
        reference, in the 429 message it would never send, and to anybody
        reading the setting. The same shape as `DEBUG=true` in production: a
        value that looks like configuration and is actually a hole, invisible on
        a box that otherwise works. Refused at startup, where it is one line to
        fix.
        """
        if v <= 0:
            raise ValueError(
                f"{info.field_name.upper()} must be at least 1 second; 0 disables "
                "the limit entirely rather than tightening it"
            )
        return v

    @field_validator("backend_cors_origins")
    @classmethod
    def _no_credentialed_wildcard(cls, v: str, info: ValidationInfo) -> str:
        """Refuse ``*`` outside development, because it does not mean what it looks like.

        The app sends ``allow_credentials=True``. Starlette handles the illegal
        wildcard-plus-credentials combination by echoing the *requesting*
        origin back with ``Access-Control-Allow-Credentials: true`` — so a
        setting that reads like "allow any origin, unauthenticated" actually
        grants every site on the internet credentialed access, and does it
        silently. Caught at startup rather than in a pen test.
        """
        origins = [o.strip() for o in v.split(",") if o.strip()]
        if "*" in origins and _declared_environment(info) != "development":
            raise ValueError(
                "BACKEND_CORS_ORIGINS cannot be '*' when credentials are allowed — "
                "Starlette echoes the caller's origin back, so this grants every "
                "site credentialed access. List the frontend origins explicitly."
            )
        return v

    @property
    def bootstrap_admin_emails(self) -> set[str]:
        """Addresses that are promoted to administrator on sign-in.

        Lower-cased to match ``users.email``, which is stored normalized — an
        operator who writes their address with a capital letter should still
        end up an admin rather than silently not.
        """
        return {
            a.strip().lower() for a in self.admin_emails.split(",") if a.strip()
        }

    @property
    def cors_origins(self) -> list[str]:
        """Origins the browser may make credentialed requests from.

        Trailing slashes are stripped: an ``Origin`` header is a scheme, host
        and port and never carries a path, so ``https://app.example.com/`` in
        the env var matches nothing and fails as a CORS error with no clue as
        to why. Silently accepting the more natural spelling is worth more than
        being strict about it.
        """
        return [
            o.strip().rstrip("/")
            for o in self.backend_cors_origins.split(",")
            if o.strip()
        ]

    @property
    def resolved_log_format(self) -> str:
        """``json`` unless a human is reading it, or unless told otherwise.

        Explicit ``LOG_FORMAT`` always wins; the environment only decides the
        default, so development stays readable without anyone configuring it
        and production stays parseable without anyone remembering to.
        """
        if self.log_format:
            return self.log_format.strip().lower()
        return "text" if self.environment == "development" else "json"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (env is read once per process)."""
    return Settings()


settings = get_settings()
