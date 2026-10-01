"""DoAide AutoApply FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.core.apidocs import enrich
from app.core.config import settings
from app.core.database import connection_ceiling
from app.core.errors import ServerErrorEnvelopeMiddleware
from app.core.logging import RequestContextMiddleware, configure_logging
from app.routers import (
    admin,
    analytics,
    auth,
    autopilot,
    board,
    campaigns,
    dashboard,
    digest,
    follow_ups,
    form_apply,
    gmail,
    inbox,
    interview_prep,
    jobs,
    linkedin,
    misc,
    notifications,
    profiles,
    recruiter_inbox,
    recruiters,
    resumes,
    review,
    smart_apply,
    tracker,
    tracking,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Startup work that has to happen inside the running event loop.

    Which is all of it: the threadpool limiter lives in an anyio ``RunVar``, so
    reading it from module scope raises rather than returning a default.

    The shutdown half runs *after* uvicorn has stopped accepting connections and
    every in-flight request has returned — that ordering is what makes disposing
    the pool safe rather than a way to fail the last few requests of a deploy.
    """
    bound_threadpool()
    _hydrate_credentials()
    yield
    _dispose_engine()


def _dispose_engine() -> None:
    """Close pooled database connections on the way down.

    Without this, SIGTERM ends the process with its pool still checked out, and
    Postgres only notices when the TCP connections time out. A restart therefore
    briefly holds two generations of connections at once — the dying process's
    and the new one's — against one ``max_connections``. With three units
    restarting together on every deploy that is the moment the ceiling is
    closest, and the failure it produces (``FATAL: sorry, too many clients``)
    lands on the *new* process, reading as if the deploy broke the database.

    Best-effort, and last: a shutdown that raises here would mask whatever else
    was being torn down, and the process is leaving anyway.
    """
    try:
        from app.core.database import engine

        engine.dispose()
    except Exception:  # noqa: BLE001 - the process is exiting regardless
        logger.warning("database pool not disposed cleanly", exc_info=True)


def _hydrate_credentials() -> None:
    """Apply database credential overrides onto ``settings`` before serving.

    Best-effort on purpose. A database that is not reachable yet, or a schema
    that predates ``app_credentials``, must not stop the API from starting —
    the environment values are already loaded and are a working configuration.
    What is lost in that case is the override, so it is logged at WARNING and
    the beat refresh picks it up on the next tick.
    """
    from app.core.database import SessionLocal
    from app.services import credential_store

    try:
        db = SessionLocal()
        try:
            credential_store.hydrate(db)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - the environment is a valid config
        logger.warning("credential overrides not loaded at startup: %s", exc)


def create_app() -> FastAPI:
    # Before anything else: routers and services acquire their loggers at
    # import time, and a handler installed after that still catches them
    # (they log through the root), but startup messages emitted during import
    # would otherwise be lost to the last-resort handler.
    configure_logging(level=settings.log_level, fmt=settings.resolved_log_format)

    app = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        description="AI-powered recruiter outreach automation.",
        debug=settings.debug,
        lifespan=_lifespan,
    )

    prefix = settings.api_v1_prefix

    # Added first, so it sits *innermost* of the three: an unhandled exception
    # has to become a response before CORS can put its headers on that response.
    # Caught any further out — which is where Starlette's own ServerErrorMiddleware
    # lives — the 500 goes back without `Access-Control-Allow-Origin` and the
    # browser discards it, so the SPA reports a network failure rather than the
    # server error that actually happened.
    app.add_middleware(ServerErrorEnvelopeMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # `allow_headers` is about the *request*; a browser hides every response
        # header outside the CORS-safelisted six unless it is named here. The
        # paging headers are the whole way a client learns its list was cut
        # short, and without this line they arrive and are unreadable — which
        # looks exactly like the truncation being silent again.
        #
        # X-Request-ID joins them for the same reason: the id is only useful if
        # the client can read it back and quote it in a bug report.
        #
        # And the rate-limit four, which had the same problem without anybody
        # noticing: `Retry-After` is not one of the CORS-safelisted six, so the
        # header the limiter takes such care to compute — the whole answer to
        # "when may I try again" — arrived at the browser and was dropped before
        # any code could read it. The web client is the only consumer these
        # routes have, so a header it cannot see is a header that does not
        # exist, and the countdown it should have driven was instead an
        # immediate retry into another refusal.
        expose_headers=[
            "X-Total-Count",
            "X-Has-More",
            "X-Request-ID",
            "Retry-After",
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
        ],
    )

    # Added last, so it sits outermost: the request id must be set before any
    # other middleware can log, and the timing should cover them too.
    app.add_middleware(
        RequestContextMiddleware,
        quiet_paths=(f"{prefix}/health", "/health"),
    )
    app.include_router(misc.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(gmail.router, prefix=prefix)
    app.include_router(resumes.router, prefix=prefix)
    app.include_router(profiles.router, prefix=prefix)
    app.include_router(campaigns.router, prefix=prefix)
    app.include_router(recruiters.router, prefix=prefix)
    app.include_router(tracker.router, prefix=prefix)
    app.include_router(smart_apply.router, prefix=prefix)
    app.include_router(jobs.router, prefix=prefix)
    app.include_router(form_apply.router, prefix=prefix)
    app.include_router(linkedin.router, prefix=prefix)
    app.include_router(dashboard.router, prefix=prefix)
    app.include_router(board.router, prefix=prefix)
    app.include_router(digest.router, prefix=prefix)
    app.include_router(follow_ups.router, prefix=prefix)
    app.include_router(notifications.router, prefix=prefix)
    app.include_router(analytics.router, prefix=prefix)
    app.include_router(autopilot.router, prefix=prefix)
    app.include_router(review.router, prefix=prefix)
    app.include_router(inbox.router, prefix=prefix)
    app.include_router(recruiter_inbox.router, prefix=prefix)
    app.include_router(interview_prep.router, prefix=prefix)
    # Public: recipients' mail clients hit these, so no auth dependency.
    app.include_router(tracking.router, prefix=prefix)

    _static = Path(__file__).resolve().parent / "static"

    @app.get("/", tags=["misc"], summary="Service banner")
    def root() -> dict[str, str]:
        return {"app": settings.app_name, "docs": "/docs", "health": f"{prefix}/health"}

    @app.get("/privacy", tags=["misc"], summary="Privacy policy", response_class=HTMLResponse)
    def privacy_policy() -> HTMLResponse:
        return HTMLResponse((_static / "privacy.html").read_text())

    @app.get("/terms", tags=["misc"], summary="Terms of service", response_class=HTMLResponse)
    def terms_of_service() -> HTMLResponse:
        return HTMLResponse((_static / "terms.html").read_text())

    # The published schema is the one the reference is generated from, and the
    # one /docs renders. Overriding the method rather than post-processing a
    # copy is what keeps those three the same document — a client reading
    # /openapi.json sees the same 401s and 429s as a client reading the
    # checked-in reference. See `app.core.apidocs` for what is derived and how.
    app.openapi = lambda: enrich(app)  # type: ignore[method-assign]

    return app


def bound_threadpool() -> None:
    """Admit no more concurrent handlers than there are connections to serve.

    Every non-async route runs in Starlette's threadpool, whose default is 40
    tokens, and `get_db` opens a session before the handler and holds it until
    the response. The connection pool's ceiling was 15. So the 16th concurrent
    request waited `pool_timeout` seconds and was then answered with a 500
    reading "QueuePool limit of size 5 overflow 10 reached" — and a route that
    calls a model holds its connection for the whole call, up to 60 seconds per
    provider in the chain, so fifteen of those took the login form, the tracking
    pixel and `/health` down with them.

    Raising the pool is half of it. The other half is that a limit expressed in
    handlers and a limit expressed in connections must be the same number, or a
    burst is admitted into a queue it cannot be served out of. Waiting for a
    thread costs nothing and has no deadline; waiting for a connection has both.

    Set in both directions on purpose. The shipped pool ceiling matches the
    threadpool default exactly, so today this changes nothing — but an operator
    who lowers the pool for a smaller database should get fewer concurrent
    handlers as a consequence, rather than the same burst arriving at a pool
    that can no longer serve it.
    """
    try:
        from anyio.to_thread import current_default_thread_limiter

        current_default_thread_limiter().total_tokens = connection_ceiling()
    except Exception:  # noqa: BLE001 - a tuning knob must never fail a boot
        logger.warning("could not bound the request threadpool", exc_info=True)


app = create_app()
