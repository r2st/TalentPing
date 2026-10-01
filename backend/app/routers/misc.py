"""Health check and CAN-SPAM unsubscribe endpoints."""
from __future__ import annotations

import logging
from html import escape

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pii import mask_email
from app.core.rate_limit import ip_rate_limit
from app.services import health as health_service
from app.services import llm_router, opt_out
from app.services import unsubscribe as unsubscribe_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["misc"])

# Both routes below are unauthenticated by design, and both do real work per
# call — which is the combination that had no ceiling on it anywhere in this
# module.
#
# `/health` runs the checks it advertises: a `SELECT 1` round trip and a Redis
# connect, each with its own timeout. That is a database connection checked out
# of a pool deliberately sized to the request threadpool (see
# `test_connection_pool`), taken by an anonymous caller, on a route no session
# is needed to reach. Unmetered, a flat flood of `/health` starves every
# authenticated request of the pool it shares — a health endpoint that reports
# the outage it is causing.
#
# The limit is set well above any monitor: `scripts/verify_deploy.py` polls it
# a handful of times per deploy and the uptime check runs on tens of seconds,
# so two a second is orders of magnitude of headroom while still bounding the
# flood.
_health_limit = ip_rate_limit(120, 60, scope="health")

# The opt-out is the more delicate of the two, and the limit is deliberately
# loose because of it. A refused unsubscribe is worse than a served one: the
# route honours *unsigned* links on purpose (see `unsubscribe`), CAN-SPAM makes
# the mechanism's availability the obligation, and a provider that gets a
# non-2xx from a `List-Unsubscribe` URL scores it against the sending mailbox —
# the mailbox this product spends the rest of its effort protecting.
#
# What the ceiling is actually for is the other direction. `_opt_out` writes,
# the address is chosen by the caller, and the match is on address across
# *every* user's recruiter list — so with a list of addresses and no limit, one
# script opts out the whole database in the time it takes to loop. Thirty in
# five minutes is far more opt-outs than this deployment's send volume can
# generate and slow enough that the same script now takes weeks.
#
# Both GET and POST share the scope: RFC 8058 providers POST and humans click
# the link, and they mean the same thing, so metering them apart would make the
# ceiling a ceiling on neither.
_unsubscribe_limit = ip_rate_limit(30, 300, scope="unsubscribe")


@router.get("/health", dependencies=[Depends(_health_limit)])
def health(db: Session = Depends(get_db)) -> dict:
    """Whether the dependencies this deployment cannot work without are answering.

    ``status`` is ``ok`` only when every check in ``checks`` passed, and
    ``degraded`` otherwise, with the failing dependency named. Before this the
    field was a literal — see :mod:`app.services.health` for what that cost.

    **Why a degraded deployment still answers 200.** The verdict is in the
    body, not the status code, because the only consumer that acts on this is
    ``scripts/verify_deploy.py``, which fetches with ``curl -fsS`` — and ``-f``
    suppresses the body of a non-2xx response. Answering 503 would replace a
    report naming the broken dependency with an empty string, so the deploy
    check would still fail, and would no longer be able to say why. There is no
    load balancer in this topology whose rotation a 503 would change.

    ``mailboxes`` is reported on the same terms as the LLM fields below and for
    the same reason — see :func:`app.services.health.mailbox_grants`. It is here
    because a deployment whose grants have all expired sends nothing, receives
    nothing, and passes every check above; that is this product's most frequent
    total outage and the monitor watching for outages could not see it, the
    figures being behind ``/admin/ops``' admin session.

    The LLM fields are a different kind of claim and stay reported rather than
    judged. A tripped breaker is usually self-healing and resets on restart, so
    treating it as a failed deploy would make every deploy look like a fix.
    ``llm_model_errors`` and ``llm_auth_errors`` are the two an operator must
    fix by hand — a configured model id the upstream no longer recognises, and
    a key it will not accept. They are reported separately because they send
    the reader to different settings, and because both are the kind of fault
    the chain is designed to *absorb*: every caller falls back to its template,
    every task reports success, and the only symptom is a product that quietly
    behaves as though it had never been given AI at all. All of these are
    per-process, so this is the API's view and workers keep their own.

    Left out of ``quiet_paths``' logging on purpose: see
    :class:`app.core.logging.RequestContextMiddleware`.
    """
    checks = health_service.run_checks(db)
    return {
        "status": health_service.overall_status(checks),
        "checks": {c.name: c.as_dict() for c in checks},
        "mailboxes": health_service.mailbox_grants(db),
        "llm_providers": llm_router.configured_providers(),
        "llm_breakers_open": llm_router.breaker.snapshot(),
        "llm_model_errors": llm_router.model_errors(),
        "llm_auth_errors": llm_router.auth_errors(),
    }


def _record(request: unsubscribe_service.UnsubscribeRequest, contacts: int) -> None:
    """Write down that an opt-out happened, and whether the link was ours.

    `unsubscribe.verify` has always computed `signed` and nothing has ever read
    it, which made the signature buy nothing operationally: it was compared,
    stored on a dataclass, and dropped. The module's own docstring says the flag
    exists so "a flood of them is visible as the abuse it would be, rather than
    looking like a sudden collapse in message quality" — this is the line that
    makes that true.

    **Unsigned is a warning, and deliberately so.** It is either a link that
    predates the signature — real, and honoured, and getting rarer every week —
    or someone burning a contact they guessed the address of. Both are worth
    seeing, and neither is visible in the opt-out count alone: a sender watching
    unsubscribes climb cannot otherwise tell "our mail got worse" from "someone
    is walking our recipient list".

    Nothing here refuses anything. Refusing is what this endpoint must never do,
    and the reason the obvious defence — a rate limit — is the wrong tool today
    is written up in the RUNBOOK rather than attempted here.

    **The address is masked.** It used to be logged in full, on the reasoning
    that it is already echoed into the confirmation page this same request
    renders — but that page is shown to the person the address belongs to,
    which a log aggregator is not. This is the moment a third party asks not to
    be contacted, and writing their address into a store with a wider access
    list, a longer retention and no delete path is the wrong direction to move
    it in. What the line is actually for still works masked: two opt-outs from
    the same domain are still one bucket, two different mailboxes on it are
    still distinguishable, and the full address is in ``recruiters`` and on the
    request id if support needs to resolve it. See :mod:`app.core.pii`.
    """
    if not request.valid:
        # No address at all. Not a click a human made; not worth a line.
        return
    if request.signed:
        logger.info(
            "unsubscribe: %s opted out via a signed link (%d contact rows)",
            mask_email(request.address),
            contacts,
        )
        return
    logger.warning(
        "unsubscribe: %s opted out via an UNSIGNED link (%d contact rows) — "
        "a legacy link or a forgery; see RUNBOOK",
        mask_email(request.address),
        contacts,
    )


def _opt_out(db: Session, address: str, signature: str | None) -> str:
    """Opt an address out everywhere, and render the confirmation page.

    Deliberately idempotent and deliberately silent about what it found. The
    page says the same thing whether the address was on ten recruiter rows or
    none, because the alternative — "we had no record of you" — turns an
    unsubscribe endpoint into an oracle telling any caller whether a given
    address is in the database.

    The match is equality on the lowercased address, not ``ilike``. It was
    ``ilike`` — which reads as "equals, ignoring case" and is not: the address
    arrives from an unauthenticated query string, ``%`` is a wildcard, and
    ``/unsubscribe?email=%`` therefore opted out every contact of every user and
    cancelled every follow-up behind them. A signature would not have helped,
    because this endpoint honours unsigned links on purpose.
    """
    request = unsubscribe_service.verify(address, signature)
    contacts = 0
    if request.valid:
        # The consent flag plus the work queued behind it — a sequence left
        # SCHEDULED reads on the tracker as mail still coming, and a batch
        # queued hours ahead reads the same way until each one comes due and is
        # written off one at a time. Shared with the `mailto:` route, which is
        # the other half of the `List-Unsubscribe` header this link comes from.
        contacts = opt_out.apply(db, request.address, reason="recipient unsubscribed")
        if contacts:
            db.commit()

    _record(request, contacts)

    # Escaped: the address is echoed straight from the query string, and an
    # unauthenticated public endpoint that reflects its input into HTML is a
    # stored-nowhere-but-still-real XSS against anyone handed the link.
    shown = escape(request.address) or "this address"
    return (
        "<html><body style='font-family:sans-serif;max-width:480px;margin:64px auto'>"
        "<h2>You've been unsubscribed</h2>"
        "<p>You will no longer receive outreach emails at "
        f"<strong>{shown}</strong>.</p>"
        "</body></html>"
    )


@router.get(
    "/unsubscribe",
    response_class=HTMLResponse,
    dependencies=[Depends(_unsubscribe_limit)],
)
def unsubscribe(
    email: str, sig: str | None = None, db: Session = Depends(get_db)
) -> str:
    """CAN-SPAM opt-out: mark every matching recruiter as opted-out immediately.

    Public (no auth) by design — recruiters click this from an email, and match
    is on address across every user's recruiter list, because consent belongs to
    the human and not to whoever happened to add them.

    ``sig`` is the HMAC :mod:`app.services.unsubscribe` puts on the link. It is
    checked but never *required*: links already sitting in mailboxes predate it,
    and refusing a genuine opt-out to punish a missing parameter is the one
    outcome worse than honouring a forged one.
    """
    return _opt_out(db, email, sig)


@router.post(
    "/unsubscribe",
    response_class=HTMLResponse,
    dependencies=[Depends(_unsubscribe_limit)],
)
def unsubscribe_one_click(
    email: str,
    sig: str | None = None,
    # RFC 8058 says the provider POSTs `List-Unsubscribe=One-Click` as a form
    # body. We do not branch on it — the URL already identifies the recipient —
    # but it is declared so the body parses rather than 422ing, and Optional so
    # a hand-rolled POST without it still works.
    List_Unsubscribe: str | None = Form(default=None, alias="List-Unsubscribe"),
    db: Session = Depends(get_db),
) -> str:
    """The one-click opt-out (RFC 8058) that every unsolicited send advertises.

    Gmail's and Yahoo's native "Unsubscribe" buttons POST here rather than
    navigating; without this route they got a 405 against a sender whose headers
    promised otherwise, which reads to the provider as a broken opt-out — the
    exact failure the header exists to rule out. Same effect as the GET, since
    the click means the same thing whichever way the provider delivers it.
    """
    return _opt_out(db, email, sig)
