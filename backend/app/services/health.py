"""What ``/health`` actually checks, as opposed to what it used to claim.

The endpoint returned ``{"status": "ok"}`` unconditionally. It was reachable
exactly when the process was running, and it said "ok" for every state the
process can be running in — including the ones that matter: Postgres refusing
connections, Redis down so nothing is dispatched and nothing is sent, the pool
exhausted. Every one of those is a total outage of the product, and the check
built to notice them reported healthy throughout, because the only thing it
proved was that a Python process could return a dict.

That is the failure mode ``scripts/verify_deploy.py`` was written against — a
deploy verified by an endpoint that cannot fail is a deploy that is not
verified. Its ``health_verdict`` has always been able to read a ``status`` of
``degraded`` and a named failing dependency; there was simply no code path in
which one was ever produced.

So each dependency is now actually exercised:

``database``
    A round trip, not a pool inspection. ``SELECT 1`` is the smallest statement
    that proves a connection was checked out, handed to Postgres, and answered
    — which is the claim being made. A pool that reports free capacity proves
    only that nobody has tried lately.

``broker``
    Redis, reached through the same Celery connection settings the publishers
    use, so a credential or URL that works here is one that works for them.
    This is the check the product needs most and had least: with the broker
    unreachable every task publish fails, no mail is sent, no mailbox is
    scanned — and the API answers every request normally, because nothing it
    serves synchronously touches the broker at all.

Two properties are deliberate.

**A failing check is a result, not an exception.** Each returns a
:class:`Check`, and the sweep never raises. A health endpoint that 500s when a
dependency is down has converted a precise report into the least informative
signal available, at the exact moment the precision was the point.

**The whole sweep is bounded.** Each check owns a timeout, because the
interesting failure is rarely a refused connection — it is a dependency that
accepts and then never answers. Unbounded, the health check hangs exactly as
hard as the thing it is reporting on, and the monitor times out with nothing to
show for it.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: How long a dependency has to answer before it counts as down.
#:
#: Short on purpose. This runs on a monitor's clock — every few seconds,
#: forever — and the question it asks is "are you answering *now*", to which a
#: slow yes is operationally a no. It also has to stay under whatever timeout
#: the monitor itself uses, or the report never arrives to be read.
CHECK_TIMEOUT_SECONDS = 2.0

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


@dataclass(frozen=True)
class Check:
    """One dependency's verdict, and how long it took to give it.

    ``latency_ms`` is reported for healthy checks too, which is the half of it
    people skip: a database answering in 900ms is not down, and is the single
    most useful thing to have been recording for the week before it goes down.
    """

    name: str
    ok: bool
    detail: str
    latency_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": STATUS_OK if self.ok else "down",
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


def check_database(db: Session) -> Check:
    """Prove a statement can be executed, not merely that a pool object exists."""
    started = time.perf_counter()
    try:
        db.execute(text("SELECT 1")).scalar_one()
    except Exception as exc:  # noqa: BLE001 - reporting the fault *is* the job
        # Logged as well as returned: the response goes to whoever asked, and
        # the traceback goes where tracebacks go. A monitor sees "down"; the
        # person it wakes needs the driver's actual complaint.
        logger.warning("health: database check failed", exc_info=True)
        return Check("database", False, _reason(exc), _elapsed_ms(started))
    return Check("database", True, "SELECT 1", _elapsed_ms(started))


def check_broker(timeout: float = CHECK_TIMEOUT_SECONDS) -> Check:
    """Open a connection to the Celery broker and close it again.

    ``max_retries=0`` matters more than the timeout: kombu's default is to
    retry a failed connection with a growing backoff, so a down broker would
    keep this handler for the better part of a minute and every health request
    would pile up behind it — turning a broker outage into an API outage,
    caused by the thing watching for the broker outage.
    """
    started = time.perf_counter()
    try:
        from app.tasks.celery_app import celery_app

        connection = celery_app.connection()
        try:
            connection.ensure_connection(max_retries=0, timeout=timeout)
        finally:
            # The health check must not accumulate connections against the
            # thing it is checking; at one poll every few seconds that is how
            # a monitor exhausts a broker's client limit by itself.
            connection.release()
    except Exception as exc:  # noqa: BLE001 - reporting the fault *is* the job
        logger.warning("health: broker check failed", exc_info=True)
        return Check("broker", False, _reason(exc), _elapsed_ms(started))
    return Check("broker", True, "connected", _elapsed_ms(started))


def mailbox_grants(db: Session) -> dict[str, Any]:
    """How many mailbox grants this deployment holds, and how many are dead.

    **Reported, never judged.** It is not a :class:`Check` and it cannot make
    the deployment ``degraded``, for the same reason the LLM fields in
    ``/health`` are not checks: a revoked grant is a *user's* OAuth consent
    expiring, which no deploy caused and no rollback fixes. Wired into the
    verdict it would fail ``scripts/verify_deploy.py`` on an unrelated schedule
    and teach whoever runs deploys to ignore the one signal that is supposed to
    stop them.

    It belongs here anyway, because of how this deployment actually breaks. A
    dead grant is not an error anywhere: the scanner translates it into a
    "not configured" outcome, every task reports success, and the product goes
    completely silent while every dependency stays green — the exact shape of
    outage ``/health`` returning a literal ``ok`` was unable to show. The
    numbers were already computed for ``/admin/ops``, which needs an
    administrator's session; the uptime monitor watching this deployment has no
    session and therefore could not see the most common way it stops working.

    Counts only — no addresses. ``/health`` is deliberately unauthenticated,
    which puts its body closer to public than to internal, and a list of the
    mailboxes connected to this deployment is not something to serve there.
    """
    started = time.perf_counter()
    try:
        from app.models.gmail_account import GmailAccount

        rows = db.execute(
            select(GmailAccount.status, func.count(GmailAccount.id)).group_by(
                GmailAccount.status
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 - reporting the fault *is* the job
        logger.warning("health: mailbox grant read failed", exc_info=True)
        # An unreadable count is not a count of zero. Reported as an error
        # rather than as "no mailboxes", which is what a bare `0` would say and
        # is the distinction the rest of this module exists to keep.
        return {"error": _reason(exc), "latency_ms": _elapsed_ms(started)}

    counts = {str(status): int(count) for status, count in rows}
    connected = counts.get("connected", 0)
    revoked = counts.get("revoked", 0)
    return {
        "connected": connected,
        "revoked": revoked,
        "error": counts.get("error", 0),
        # The reading, so a monitor can alert on one boolean rather than
        # encoding the arithmetic. True when this deployment holds grants and
        # none of them works — total inbound and outbound silence, with every
        # dependency answering normally.
        "all_revoked": bool(revoked and not connected),
        "latency_ms": _elapsed_ms(started),
    }


def run_checks(db: Session) -> list[Check]:
    return [check_database(db), check_broker()]


def overall_status(checks: list[Check]) -> str:
    return STATUS_OK if all(c.ok for c in checks) else STATUS_DEGRADED


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


#: Enough of the failure to identify it, not enough to be a leak.
#:
#: Connection errors quote the URL they failed against, and for Redis and
#: Postgres alike that string carries the password:
#: ``Error 111 connecting to redis://:hunter2@localhost:6379/1.`` is a real
#: shape of redis-py error, and it is 60 characters — so truncation is not a
#: defence, because the credential is at the *front*. ``/health`` is the one
#: endpoint deliberately reachable without authentication, which makes its body
#: closer to public than to internal.
#:
#: So the userinfo is removed before the excerpt is taken, and the two limits
#: do different jobs: the redaction is what makes the string safe, the length
#: cap only keeps it a line. The log line beside it — which is not public —
#: keeps the traceback in full.
_REASON_LIMIT = 120

#: ``scheme://`` then anything up to the ``@`` that ends the userinfo. Anchored
#: on ``://`` rather than matching a bare ``user:pass`` so it cannot eat an
#: ordinary colon out of a message like "timeout: 2s".
#:
#: Non-greedy up to the *first* ``@``, and refusing ``/`` inside the userinfo,
#: so a message quoting two URLs redacts each one rather than swallowing
#: everything between the first scheme and the last ``@``.
_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)[^/\s@]*@")


def _reason(exc: Exception) -> str:
    first_line = str(exc).splitlines()[0] if str(exc).strip() else ""
    redacted = _USERINFO_RE.sub(r"\g<scheme>***@", first_line)
    name = type(exc).__name__
    return f"{name}: {redacted[:_REASON_LIMIT]}" if redacted else name
