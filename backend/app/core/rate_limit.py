"""Sliding-window rate limiting, keyed by user or by client address.

Two limiters, because the endpoints worth protecting divide in two:

``rate_limit(max, window, scope=...)``
    Per authenticated user. Guards the LLM-backed routes, where a single
    client can exhaust a free-tier quota the whole account shares::

        @router.post(
            "/smart-apply",
            dependencies=[Depends(rate_limit(10, 60, scope="smart-apply"))],
        )

``ip_rate_limit(max, window, scope=...)``
    Per client address, with **no authentication dependency**. This is the
    one that can sit on ``/auth/login`` and ``/auth/register`` — an
    unauthenticated endpoint has no user to key on, which is precisely why
    the per-user limiter could never protect the credential surface. Those
    two routes are where password guessing and account-creation abuse arrive,
    and until this existed they were the only unmetered POSTs in the app.

Both share one bounded store. Keys are ``(scope, identity)`` so a login limit
and a register limit never draw down the same budget — and neither do two
per-user limits belonging to different routers.

**On trusting proxy headers.** Production runs behind Caddy, so
``request.client.host`` is ``127.0.0.1`` for every request — IP limiting there
would put every user on the planet in one bucket, and the fifth failed login
account-wide would lock out the sixth person. The fix is to read
``X-Forwarded-For``, but reading it naively is worse than not limiting at all:
the header is caller-supplied, so an attacker sends a fresh value per request
and buys an unlimited budget. ``trusted_proxy_hops`` resolves this by counting
from the *right*: each proxy appends the peer it actually saw, so with one
trusted hop the rightmost entry is the address Caddy observed and everything
left of it is forgeable. Default 0 — trust nothing until deployment says how
many hops there are. The header may also arrive *repeated* rather than
comma-joined, so the chain is read across every instance of it —
:func:`_forwarded_chain` — because reading only the first is a bypass that
looks exactly like a correct configuration.

The window state is in-process and resets on restart, which is correct for the
single-worker deployment this runs on. Multi-worker wants Redis; the store is
isolated behind ``_hit`` so that swap touches one function.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response, status

from app.core.config import settings
from app.core.deps import get_current_user
from app.models.user import User

logger = logging.getLogger(__name__)

# {(scope, identity): [timestamp, ...]} — a plain dict, not a defaultdict, so
# that merely *reading* a key cannot create one. The old defaultdict grew an
# entry per user forever; nothing ever removed them.
_windows: dict[tuple[str, str], list[float]] = {}

# Sweeping every key on every request is O(users) per call. Instead the sweep
# runs at most this often, which keeps the store bounded by the number of
# distinct callers within one sweep interval rather than for all time.
_SWEEP_INTERVAL_SECONDS = 300.0
_last_sweep = 0.0

# The window each key was last recorded under, so the sweep knows when that key
# is actually dead. Without it the sweep has to guess, and the only number in
# scope to guess with is its own interval — which silently truncates every
# window longer than it. See `_sweep`.
_window_span: dict[tuple[str, str], float] = {}


def _sweep(now: float) -> None:
    """Drop windows whose every hit has aged out.

    Without this the store is a slow leak: one list per address that ever hit
    a limited endpoint, kept for the life of the process. An unauthenticated
    limiter makes that worse than it was — the key space becomes every IP that
    can reach the login form, which is the whole internet.

    A key is dead only once its newest hit has left *its own* window. Bounding
    that by the sweep interval instead — which is what this did first — caps
    every limit at the interval no matter what it was configured to be: with a
    300s sweep, ``REGISTER_RATE_LIMIT=5`` over ``REGISTER_RATE_WINDOW_SECONDS
    =3600`` let a caller spend the budget, wait five minutes for the sweep to
    forget them, and spend it again — twelve times an hour, against a limit
    that reads as five. The eviction has to know the window to be safe, so the
    window is recorded with the key.
    """
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep = now
    dead = [
        key
        for key, hits in _windows.items()
        if not hits or hits[-1] + _window_span.get(key, _SWEEP_INTERVAL_SECONDS) <= now
    ]
    for key in dead:
        del _windows[key]
        _window_span.pop(key, None)


@dataclass(frozen=True)
class Verdict:
    """What the store decided about one request, and the numbers to publish.

    ``retry_after`` alone was enough to build the 429 and nothing else. It could
    not build the headers on a *served* request, because "how many are left" and
    "when does the oldest hit expire" are not derivable from "you are not
    refused" — and those two are what let a client pace itself instead of
    discovering the limit by hitting it.
    """

    allowed: bool
    #: Requests left in the window after this one. Zero on a refusal.
    remaining: int
    #: Seconds until the budget next grows — the oldest live hit leaving the
    #: window. On a refusal that is the earliest moment a retry can succeed.
    reset_after: float

    @property
    def refused(self) -> bool:
        return not self.allowed

    @property
    def retry_after(self) -> float | None:
        """Seconds to wait, or ``None`` when the request was allowed."""
        return None if self.allowed else self.reset_after


def _hit(
    key: tuple[str, str], max_requests: int, window_seconds: float
) -> Verdict:
    """Record a request against *key* and say what the caller may be told.

    A :class:`Verdict` rather than a bare wait, because the headers on a served
    request need the same arithmetic and used to have none of it: every 200 from
    a limited route carried ``RateLimit-Limit`` and nothing to measure it
    against, which is a budget without a balance.
    """
    now = time.monotonic()
    _sweep(now)

    # Guard the arithmetic rather than the callers: `window_seconds` reaches
    # here from settings, and a non-positive one used to fail *open*. With a
    # window of 0 the cutoff is `now`, no recorded hit is ever strictly after
    # it, and the budget therefore looks untouched on every request —
    # `LOGIN_RATE_WINDOW_SECONDS=0` unmetered the login route while still
    # reading, everywhere it is reported, as a limit of ten. A negative one was
    # worse: the cutoff moves into the future and discards hits that had not
    # expired.
    #
    # `config` now refuses both at startup, which is where a typo should be
    # caught. This is the second line of that defence and it fails the other
    # way: no window means nothing ages out, so the limit still binds.
    window = max(float(window_seconds), 0.0)
    cutoff = now - window if window > 0 else float("-inf")
    hits = [t for t in _windows.get(key, ()) if t > cutoff]

    # Recorded on every hit, including a refused one: the sweep needs the span
    # of the key it is deciding about, and a refused caller is exactly the key
    # that must not be forgotten early.
    #
    # A window of zero has no span to record. Writing 0.0 would make the sweep
    # judge the key dead the moment it is written, so the budget the clamp above
    # just made permanent would come back every five minutes anyway — the two
    # halves of the defence cancelling out. Infinity is the honest span for a
    # window nothing ages out of.
    _window_span[key] = window if window > 0 else float("inf")

    if max_requests <= 0:
        # A budget of nothing refuses everything, and it has to refuse it as a
        # 429. The comparison below is `>=`, so zero already refused — and then
        # read `hits[0]` of an empty list to say how long to wait. The result
        # was an IndexError, which FastAPI renders as a 500 with no
        # ``Retry-After``: `LOGIN_RATE_LIMIT=0`, the obvious way to shut the
        # credential surface during an incident, turned every login into a
        # server error instead of a refusal. Nothing ages into a budget of
        # zero, so the wait is the whole window.
        _windows[key] = hits
        return Verdict(allowed=False, remaining=0, reset_after=window)

    if len(hits) >= max_requests:
        # Write the pruned list back even on refusal, so a caller hammering a
        # limit doesn't keep re-pruning the same expired entries every request.
        _windows[key] = hits
        return Verdict(
            allowed=False, remaining=0, reset_after=max(hits[0] + window - now, 0.0)
        )

    hits.append(now)
    _windows[key] = hits
    return Verdict(
        allowed=True,
        remaining=max_requests - len(hits),
        reset_after=max(hits[0] + window - now, 0.0),
    )


def _budget_headers(verdict: Verdict, max_requests: int) -> dict[str, str]:
    """The draft-standard trio, for a served request and a refused one alike.

    Rounded *up*, always to at least one second. Rounding ``RateLimit-Reset``
    down produces a retry that is still inside the window and refused again,
    and a ``0`` reads as "now" to every client that parses it.
    """
    return {
        "RateLimit-Limit": str(max_requests),
        "RateLimit-Remaining": str(verdict.remaining),
        "RateLimit-Reset": str(max(1, int(verdict.reset_after + 0.999))),
    }


def _publish(response: Response | None, verdict: Verdict, max_requests: int) -> None:
    """Put the budget on a *served* response.

    Only the limit was published before, which told a caller the size of a
    budget and nothing about how much of it was left — so the only way to find
    the balance was to spend it and read the 429. That is precisely the request
    a well-behaved client is trying not to make, and on the credential scopes it
    is the request that gets an address refused for the rest of the window.
    """
    if response is None:
        return
    response.headers.update(_budget_headers(verdict, max_requests))


def _refuse(
    verdict: Verdict, max_requests: int, window_seconds: int
) -> HTTPException:
    """Build the 429, with the headers a client can actually act on.

    ``Retry-After`` is the one that matters: a 429 without it leaves a client
    guessing, and the guess is usually "immediately", which is how a rate limit
    turns into a retry storm. It carries the same rounded-up seconds as
    ``RateLimit-Reset`` so the two can never disagree.
    """
    headers = _budget_headers(verdict, max_requests)
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Rate limit exceeded. Max {max_requests} requests per {window_seconds}s.",
        headers={"Retry-After": headers["RateLimit-Reset"], **headers},
    )


# Whether the "a proxy is in front of you and you don't know it" warning has
# already been emitted. The condition is a property of the deployment, not of
# any one request, so a line per call would bury the line it is trying to be
# seen in.
_warned_untrusted_forwarding = False


def _warn_if_evidently_proxied(request: Request) -> None:
    """Say something when a proxy is evidently in front of a deployment
    configured as though there were none.

    ``trusted_proxy_hops`` defaults to 0 and ``.env.example`` ships it that way,
    which is right for a laptop and right for a directly-exposed server. It is
    wrong for this one: production is behind Caddy, so ``request.client.host``
    is ``127.0.0.1`` for every request that ever arrives, and with 0 hops the
    limiter keys every caller on the planet to that single string.

    The limiter goes on working perfectly — it just limits the wrong thing.
    ``auth:login`` becomes a *global* budget of ten attempts per five minutes,
    so ten failed logins from anywhere refuse the login route for every user of
    the deployment until the window rolls, and repeating that costs an attacker
    nothing. A rate limit meant to slow password guessing turns into a denial of
    service against the whole credential surface, and registration goes with it.

    The same shape as ``DEBUG=true`` reaching production — a default that is
    safe locally and harmful deployed, copied in from the example file, invisible
    on a box that otherwise works. It cannot be a startup refusal the way that
    one is, because 0 is a legitimate and correct answer for a deployment with
    nothing in front of it. What separates the two cases is not in the config at
    all: it is whether real requests arrive carrying ``X-Forwarded-For``, which
    is only knowable once they do.

    So this does not change the answer — a header from an untrusted hop is still
    ignored, which is the safe reading — it just stops the misconfiguration
    being silent.

    **It reports the observed hop count, because naming a constant was wrong.**
    This line used to say "1 behind Caddy". That is the answer for a box with
    only Caddy on it, and this deployment has a CDN in front of Caddy — so the
    number is 2, and following the message would have keyed every caller to the
    CDN *edge* they happened to reach. Quieter than one global bucket and just
    as wrong: an attacker picks one edge and refuses login for everyone behind
    it. Nothing in the config can distinguish those two deployments, but the
    request can — each proxy appends one entry, so the entries actually arriving
    *are* the hop count. Reporting it turns a warning that says "you are
    misconfigured" into one that says what to set.
    """
    global _warned_untrusted_forwarding
    # Presence and length are separate questions. A header that is *there* and
    # empty still means something in front set it, and reporting 0 hops says
    # exactly that — so the trigger is whether any instance of the header
    # arrived, not whether the chain it parses to has entries in it.
    if _warned_untrusted_forwarding or not request.headers.getlist("x-forwarded-for"):
        return
    _warned_untrusted_forwarding = True
    # Counted the same way `client_address` counts, so the number this line
    # tells an operator to set is the number that setting will then mean.
    observed = len(_forwarded_chain(request))
    logger.warning(
        "requests carry X-Forwarded-For but trusted_proxy_hops is 0, so every "
        "caller is being rate-limited as one identity (the proxy's). This "
        "request arrived through %d proxy hop(s); set TRUSTED_PROXY_HOPS to "
        "that, counting any CDN in front of the reverse proxy, or the "
        "credential rate limits refuse the whole deployment at once.",
        observed,
        extra={
            "trusted_proxy_hops": settings.trusted_proxy_hops,
            "observed_forwarded_hops": observed,
        },
    )


def _forwarded_chain(request: Request) -> list[str]:
    """Every ``X-Forwarded-For`` entry, in wire order, across *all* instances.

    ``headers.get`` returns the **first** header of a repeated name, and that is
    the whole bypass: ``X-Forwarded-For`` is a list header, so a caller may send
    their own and a proxy may append its entry as a *separate* header line
    rather than merging into the existing one. Both spellings are legal and both
    are in use — Caddy merges, other proxies and CDNs do not, and this
    deployment has a CDN in front of Caddy.

    Read with ``get``, the chain that reached ``client_address`` was then the
    attacker's header alone: ``parts[-hops]`` counted from the right of a list
    the attacker wrote every entry of, which hands out a fresh bucket per
    request and undoes the entire point of counting from the right. The failure
    is silent — the setting looks correct, the code looks correct, and the limit
    simply never binds.

    Joining every instance in order restores the real chain: each hop's entry
    stays to the right of everything written before it, whichever way the hop
    chose to write it.
    """
    return [
        part.strip()
        for header in request.headers.getlist("x-forwarded-for")
        for part in header.split(",")
        if part.strip()
    ]


def client_address(request: Request) -> str:
    """The caller's address, honouring exactly as many proxies as configured.

    Counting from the right is the whole point. Each proxy appends the peer it
    saw, so in ``X-Forwarded-For: <forged>, <forged>, <real>`` behind one
    trusted hop, only the rightmost entry was written by our own proxy about a
    connection it actually accepted. Taking the leftmost — the common version of
    this code — reads the attacker's own string and hands them a fresh bucket
    per request.

    **What "trusted" is actually asserting.** A hop count above 0 is a claim
    that every request arrived *through* those proxies, and nothing in this
    process can check it — it is a property of the firewall in front of the
    origin. On an origin that still answers on 443 from anywhere, an attacker
    connects to the reverse proxy directly with one forged entry, the proxy
    appends its own peer, and the header is exactly as long as the setting
    expects: the entry this function returns is the one the attacker wrote, and
    they get a fresh bucket per request. That is strictly worse than the
    misconfiguration :func:`_warn_if_evidently_proxied` shouts about, because
    nothing shouts about this one. See the note beside ``TRUSTED_PROXY_HOPS`` in
    ``.env.example``: lock the origin to the CDN's ranges first, raise the count
    second.
    """
    hops = settings.trusted_proxy_hops
    if hops <= 0:
        _warn_if_evidently_proxied(request)
    if hops > 0:
        # Every instance of the header, not just the first — see
        # `_forwarded_chain` for why reading one of them is a full bypass.
        parts = _forwarded_chain(request)
        # `hops` entries were appended by our own proxies; the one before them
        # is the address the outermost trusted proxy saw.
        if len(parts) >= hops:
            return parts[-hops]
        # Fewer entries than configured hops means the request did not arrive
        # through the expected chain. Fall through to the socket address rather
        # than trusting a short header — a caller who strips it should not be
        # able to choose which bucket they land in.
        logger.warning(
            "x-forwarded-for shorter than trusted_proxy_hops; using peer address",
            extra={"xff_entries": len(parts), "trusted_proxy_hops": hops},
        )

    return request.client.host if request.client else "unknown"


# A limit may be given as a number or as a zero-argument callable. The callable
# form exists because a router builds its dependency at *import* time, so
# `ip_rate_limit(settings.login_rate_limit, ...)` would freeze whatever the
# setting happened to be when the module was first imported — before any test
# (or any deployment reading a later env) could influence it.
Limit = int | Callable[[], int]


def _value(limit: Limit) -> int:
    return limit() if callable(limit) else limit


def rate_limit(
    max_requests: Limit = 20,
    window_seconds: Limit = 60,
    *,
    scope: str = "user",
) -> Callable:
    """Per-user limit. *max_requests* calls per *window_seconds*, then 429.

    *scope* separates budgets, for the same reason it does on ``ip_rate_limit``.
    Every per-user limiter used to key on ``("user", id)`` regardless of which
    router installed it, so the budgets were one budget: with smart-apply at
    20/60 and interview-prep at 10/60, ten smart-apply calls refused the *first*
    interview-prep call of the minute — and refused it with "Max 10 requests per
    60s", a sentence describing a limit the caller had not spent anything
    against. Distinct scopes are what make the number in that message true.
    """

    # `user` stays the first parameter and `response` carries a default, so the
    # dependency is still callable directly as `limiter(user)`. FastAPI picks
    # `Response` out by annotation rather than by position or default, so it is
    # injected either way — but a bare `response: Response` first would silently
    # rebind a positional `user` onto it, which is exactly how a direct call
    # ends up reading `.id` off a `Depends` object.
    async def _check(
        user: User = Depends(get_current_user), response: Response = None
    ) -> None:
        limit, window = _value(max_requests), _value(window_seconds)
        # get_current_user is also a dependency of the route itself, so
        # FastAPI's per-request dependency cache means this doesn't add a
        # second token decode or DB lookup.
        verdict = _hit((scope, str(user.id)), limit, window)
        if verdict.refused:
            raise _refuse(verdict, limit, window)
        _publish(response, verdict, limit)

    _stamp(_check, scope, max_requests, window_seconds, "user")
    return _check


def ip_rate_limit(
    max_requests: Limit = 10,
    window_seconds: Limit = 60,
    *,
    scope: str = "ip",
) -> Callable:
    """Per-address limit for routes with no authenticated user to key on.

    *scope* separates budgets that should not share one. Login and register are
    different kinds of abuse arriving at different costs, and a single bucket
    would let failed logins lock out registration.
    """

    async def _check(request: Request, response: Response) -> None:
        limit, window = _value(max_requests), _value(window_seconds)
        verdict = _hit((scope, client_address(request)), limit, window)
        if verdict.refused:
            # Worth a log line — repeated refusals on the credential scopes are
            # what password guessing looks like from the server side, and the
            # request id on the record ties it to the access log entry.
            logger.warning(
                "rate limit refused a request",
                extra={
                    "rate_limit_scope": scope,
                    "rate_limit_max": limit,
                    "rate_limit_window": window,
                },
            )
            raise _refuse(verdict, limit, window)
        _publish(response, verdict, limit)

    _stamp(_check, scope, max_requests, window_seconds, "ip")
    return _check


def _stamp(
    dependency: Callable,
    scope: str,
    max_requests: Limit,
    window_seconds: Limit,
    keyed_by: str,
) -> None:
    """Record on the dependency what limit it enforces.

    ``scope_of`` already made "is this route metered?" answerable from the
    assembled app; this makes "metered *how*?" answerable the same way. The
    numbers otherwise live only in the argument list at each call site, so the
    published API reference had to restate them by hand — and a restated number
    is one deploy away from being a lie about a limit callers plan retries
    around.

    The limits are kept in their given form, callable included, and read
    through :func:`limit_of` at the moment somebody asks. Resolving them here
    would freeze the value at import time, which is the exact bug the callable
    form exists to avoid.
    """
    dependency.rate_limit_scope = scope  # type: ignore[attr-defined]
    dependency.rate_limit_max = max_requests  # type: ignore[attr-defined]
    dependency.rate_limit_window = window_seconds  # type: ignore[attr-defined]
    dependency.rate_limit_keyed_by = keyed_by  # type: ignore[attr-defined]


def limit_of(dependency: object) -> dict[str, object] | None:
    """The budget a limiter dependency enforces, resolved now.

    ``None`` when *dependency* is not a limiter. Otherwise the scope, the
    numbers as they stand at this moment, and whether the budget is per user or
    per client address — which is the difference between "you have spent yours"
    and "somebody sharing your egress has".
    """
    scope = scope_of(dependency)
    if scope is None:
        return None
    return {
        "scope": scope,
        "max_requests": _value(getattr(dependency, "rate_limit_max", 0)),
        "window_seconds": _value(getattr(dependency, "rate_limit_window", 0)),
        "keyed_by": getattr(dependency, "rate_limit_keyed_by", "user"),
    }


def scope_of(dependency: object) -> str | None:
    """The scope a limiter dependency guards, or ``None`` if it is not one.

    Both factories stamp the scope onto the function they return, which is what
    makes "is this route metered, and under which budget?" a question that can
    be asked of the assembled application rather than of the source. The test
    that keeps expensive endpoints from arriving unmetered is built on this;
    reading it back out of the closure instead would break the moment either
    factory gained a local variable.
    """
    return getattr(dependency, "rate_limit_scope", None)


def reset() -> None:
    """Forget every window. For tests, and for nothing else."""
    global _last_sweep, _warned_untrusted_forwarding
    _windows.clear()
    _window_span.clear()
    _last_sweep = 0.0
    # Also the once-per-process warning: a test that asserts on it has to be
    # able to reach it, and the autouse fixture that calls this is what makes
    # ordering between such tests irrelevant.
    _warned_untrusted_forwarding = False
