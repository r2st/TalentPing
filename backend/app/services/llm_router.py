"""Multi-provider LLM chain — OpenRouter → Gemini → Groq → Cerebras → template.

Every AI feature in TalentPing (outreach copy, resume tailoring, fit summaries,
reply classification, JD parsing) funnels through :func:`complete`. The point is
that a single flaky upstream must never turn into a silent product failure: a
candidate's outreach email either gets written by *some* model or falls back to
a deterministic template, but it is never empty and never a stack trace.

Three things make that work:

* **One dialect.** All four providers speak OpenAI's ``/chat/completions``, so
  they differ only in base URL, key and model name. A provider with no API key
  configured is skipped rather than attempted-and-failed.
* **A circuit breaker.** After ``llm_breaker_threshold`` consecutive failures a
  provider is skipped for ``llm_breaker_cooldown_seconds``. Without it a dead
  upstream costs a full timeout on *every* request, and the chain's latency is
  the sum of everything broken ahead of the one that works. The first success
  resets the count.
* **A terminal template tier.** When every provider is exhausted the chain
  raises :class:`AllProvidersFailed`. That is deliberate rather than returning
  some generic filler string: each call site already owns a *specific*
  deterministic template (ai_composer's hand-written email, fit_scorer's
  computed summary, reply_classifier's keyword rules), and those beat anything
  generic this module could invent. The chain's job is to tell the caller
  "you're on your own now", which the existing ``except OpenRouterError`` blocks
  already handle.

:func:`app.services.openrouter_client.chat_completion` converts
``AllProvidersFailed`` into ``OpenRouterError`` precisely so those call sites
keep working untouched.

The breaker state is per-process, in-memory. With several workers that means
each learns about a dead provider independently — fine, since the cost of
learning is one timeout and the alternative (shared state in Redis) buys little
for how rarely this fires.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from app.core.config import settings
from app.core.pii import scrub

logger = logging.getLogger(__name__)

# Indirection so tests can take the wall-clock out of the retry path without
# reaching into the shared `time` module — patching `time.sleep` itself lands on
# every other module in the process, which is how a rate-limit test elsewhere in
# the suite came to fail.
_sleep = time.sleep


class LLMError(RuntimeError):
    """A single provider call failed."""


class RateLimited(LLMError):
    """A provider refused this call because we asked too fast.

    Distinct from every other :class:`LLMError` because the remedy is opposite.
    A provider that returns 500, drops the connection or hands back an empty
    completion is *broken*: the useful response is to stop calling it, which is
    what the breaker does. A provider that returns 429 is *working* — it is
    telling us the rate, and the useful response is to wait and ask again.

    Counting the second as the first is what took this whole feature down. Three
    rate-limited calls tripped the breaker, the next provider was rate-limited
    for the same reason (a burst of inbound mail hits all of them at once), and
    within seconds every provider in the chain was marked dead for the full
    five-minute cool-down. Every call for those five minutes failed instantly,
    without a request leaving the box, and every caller fell back to its own
    static template: an identical generic reply to every recruiter, and
    ``UNKNOWN`` for every classification.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ModelNotFound(LLMError):
    """The configured model id does not exist at this provider.

    Distinct from every other :class:`LLMError` because nothing about it is
    transient: the breaker's cool-down exists to stop hammering an upstream that
    might come back, and a model that has been retired is never coming back. The
    remedy is an operator editing one setting, and until they do, every retry is
    a guaranteed-wasted round trip.

    Telling it apart matters far more for *visibility* than for control flow.
    These providers publish decommission dates and act on them, so a model id
    that worked yesterday returns 404 today with no deploy on our side — and the
    chain is designed to absorb exactly that kind of failure. It did: every
    caller fell back to its deterministic template, every Celery task reported
    ``succeeded``, and the only trace was a WARNING among thousands.

    That is how production spent nine days unable to draft a single recruiter
    reply. Groq decommissioned ``llama-3.3-70b-versatile`` on its published
    schedule and OpenRouter retired the ``:free`` suffix from
    ``openai/gpt-oss-20b``, so two of the three configured models 404'd and the
    third was throttled. Because the keyword classifier caps its confidence at
    0.6 and routing multiplies that by the match score against a floor of 70,
    ``0.6 × 100 < 70`` — drafting was not degraded but *arithmetically
    impossible*, and nothing anywhere said so.

    So this is logged at ERROR naming the setting to change, and surfaced on
    ``/health`` under ``llm_model_errors``, because a permanent configuration
    fault should not need a log grep to find.
    """


class AuthenticationFailed(LLMError):
    """The provider rejected our credentials.

    The third member of the "no amount of waiting fixes this" family, alongside
    :class:`ModelNotFound`, and the one that hides best. A retired model id at
    least names itself in the response body; an expired key produces
    ``401 Unauthorized`` and, until this class existed, the chain recorded that
    as ``openrouter request failed: Client error '401 Unauthorized'`` — a
    WARNING indistinguishable from the connection resets and gateway hiccups
    that scroll past all day, and that the breaker is designed to absorb.

    Absorb it the breaker duly did. Every caller fell back to its deterministic
    template, every Celery task reported ``succeeded``, ``/health`` said the
    provider was configured, and the product looked exactly like a deployment
    that had simply been given no AI. This is the same shape of nine-day
    silence :class:`ModelNotFound` was written for, arriving through the one
    door that class does not cover — and it is the likelier of the two, because
    a key is revoked, rotated or run past its trial by a person, on no
    schedule, where a decommission at least gets announced.

    So it is logged at ERROR naming the setting to change and published on
    ``/health`` under ``llm_auth_errors``, which — unlike ``llm_model_errors``
    — tells the operator to look at a *key* rather than at a model id. Those
    are different drawers, and a health check that sends someone to the wrong
    one has not really reported the fault.
    """


class ChainDeadlineReached(LLMError):
    """We ran out of time before this provider was called.

    The other half of :class:`RequestTooLarge`'s argument: a verdict that says
    nothing about the provider must not be recorded against it. This one is
    even further from the provider's fault — it never saw a request. Charging
    it to the breaker would let a run of slow *first* providers mark the
    healthy ones behind them as failing, which is the precise inversion of what
    the chain is ordered for.
    """


class RequestTooLarge(LLMError):
    """The prompt did not fit this provider's context window.

    Unlike every other failure here, this one is *ours*. The provider is
    healthy and answering; we sent it a 40,000-token resume and an 8,192-token
    model. Two consequences follow, and the chain used to get both wrong by
    counting this as an ordinary provider failure.

    **It must not touch the breaker.** The breaker's premise is that recent
    failures predict future ones, which holds for an upstream that is down and
    is precisely false here: the next request, with a normal-sized prompt, will
    succeed. Counting it meant one candidate uploading one oversized CV put a
    failure on all four providers, and three such uploads inside a cool-down
    window opened every breaker in the chain — taking AI away from *every other
    user* for five minutes over a document that was only ever going to fail for
    the one who sent it.

    **The rest of the chain is still worth trying**, which is why this does not
    stop the loop. The providers have wildly different windows — Gemini's is
    over a hundred times the free OpenRouter model's — so the prompt that
    overflows the first tier routinely fits the second. Failing over is not a
    wasted retry here; it is the single case where the fallback chain is doing
    something a retry could not.

    What it does buy is a legible ending: when every window really is too
    small, the caller's fallback fires with "the prompt was too large" in the
    log rather than "request failed", and the person reading it goes and looks
    at the input instead of at the provider's status page.
    """


# Which providers answered "no such model", and what they said. Keyed by
# provider name so a later success can clear it — the point is to report the
# *current* fault, not that one ever happened.
_model_errors: dict[str, str] = {}
_model_errors_lock = threading.Lock()

# The same, for providers that rejected our key. Kept in its own map rather
# than merged with the above because the two send an operator to different
# settings — a model id and an API key — and "which drawer" is the entire value
# of reporting a permanent fault at all.
_auth_errors: dict[str, str] = {}
_auth_errors_lock = threading.Lock()


#: How much of a provider's refusal ``/health`` will publish. The stored string
#: is provider-controlled and unbounded, and ``/health`` is the one endpoint
#: deliberately reachable without authentication — which makes its body closer
#: to public than to internal, exactly as :mod:`app.services.health` says about
#: its own reasons. Some gateways echo the offending request back inside a
#: refusal, and the requests this chain makes carry resume text and message
#: bodies. The full string is still logged at ERROR beside this call, where it
#: is not public.
_MODEL_ERROR_LIMIT = 200


def _record_model_error(provider: str, detail: str) -> None:
    with _model_errors_lock:
        _model_errors[provider] = (scrub(detail) or "")[:_MODEL_ERROR_LIMIT]


def _clear_model_error(provider: str) -> None:
    with _model_errors_lock:
        _model_errors.pop(provider, None)


def _record_auth_error(provider: str, detail: str) -> None:
    with _auth_errors_lock:
        _auth_errors[provider] = (scrub(detail) or "")[:_MODEL_ERROR_LIMIT]


def _clear_auth_error(provider: str) -> None:
    with _auth_errors_lock:
        _auth_errors.pop(provider, None)


def model_errors() -> dict[str, str]:
    """Providers whose configured model id the upstream does not recognise."""
    with _model_errors_lock:
        return dict(_model_errors)


def auth_errors() -> dict[str, str]:
    """Providers that rejected our API key."""
    with _auth_errors_lock:
        return dict(_auth_errors)


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible chat endpoint."""

    name: str
    api_key: str
    base_url: str
    model: str
    # OpenRouter wants attribution headers; nobody else needs extras.
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }


def _providers() -> list[Provider]:
    """The chain, in priority order, skipping anything without a key.

    Read fresh on every call so tests (and a restarted worker picking up new
    env) see key changes without module reloading.
    """
    candidates = [
        Provider(
            name="openrouter",
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.openrouter_model,
            extra_headers={
                "HTTP-Referer": settings.openrouter_app_url,
                "X-Title": settings.openrouter_app_title,
            },
        ),
        Provider(
            name="gemini",
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            model=settings.gemini_model,
        ),
        Provider(
            name="groq",
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            model=settings.groq_model,
        ),
        Provider(
            name="cerebras",
            api_key=settings.cerebras_api_key,
            base_url=settings.cerebras_base_url,
            model=settings.cerebras_model,
        ),
    ]
    return [p for p in candidates if p.api_key]


def configured_providers() -> list[str]:
    """Names of the providers that have a key, in the order they'd be tried."""
    return [p.name for p in _providers()]


def is_configured() -> bool:
    """Whether *any* provider in the chain has a key.

    This is the question every AI feature actually wants before deciding
    between "call the model" and "use my static template", and asking it any
    other way has been wrong since the chain grew past one provider. Each call
    site used to test ``settings.openrouter_api_key`` on its own — which was
    right when OpenRouter was the only provider and became quietly wrong the
    moment Gemini, Groq and Cerebras were added behind :func:`complete`.

    The failure it caused is invisible rather than loud: a deployment holding a
    Gemini key and no OpenRouter key has a working chain, and every feature
    checking the old gate skipped it anyway. Nothing errored and nothing was
    logged — outreach went out as the deterministic template, replies were
    classified by the keyword rules, resumes were parsed by the regex path — so
    the product looked configured and behaved as though it had no AI at all.
    """
    return bool(_providers())


# --------------------------------------------------------------------------- #
# Circuit breaker                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class _BreakerState:
    failures: int = 0
    open_until: float = 0.0
    # Consecutive throttles, counted apart from *failures* because a 429 must
    # never trip the breaker — see record_throttle. This is what the pause
    # escalates on when the provider does not say how long to wait.
    throttles: int = 0


class CircuitBreaker:
    """Per-provider consecutive-failure counter with a cool-down.

    Deliberately trips on *consecutive* failures: an upstream that fails one
    request in ten is degraded, not down, and tripping on a cumulative count
    would eventually take it out of rotation permanently. Only a success clears
    the count, which is what keeps "consecutive" honest.

    The count survives the trip, and that is the half-open behaviour: after the
    cool-down the next request through is a trial, and if it fails the breaker
    re-opens on that one failure rather than starting the climb to *threshold*
    again. Zeroing it there — which this did first — meant a provider that was
    simply gone cost ``threshold`` full timeouts every cool-down, forever. At
    the shipped numbers (threshold 3, cool-down 300s, and the 60s timeout
    ``chat_completion`` passes) that is three minutes of dead waiting every five
    minutes, paid by whichever three requests arrive first, on a provider the
    chain already knows is down. Avoiding exactly that is what the breaker is
    for; it was doing it only until the first cool-down elapsed.
    """

    def __init__(
        self,
        threshold: int,
        cooldown_seconds: float,
        throttle_pause_seconds: float = 60.0,
        throttle_pause_max_seconds: float = 900.0,
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self.throttle_pause = throttle_pause_seconds
        self.throttle_pause_max = throttle_pause_max_seconds
        # How many times the unasked-for pause may double before the ceiling is
        # certain to have been reached anyway. Bounds the exponent rather than
        # the result — see record_throttle.
        self._max_doubling_steps = 64
        self._state: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def is_open(self, name: str, *, now: float | None = None) -> bool:
        """True when *name* should be skipped right now."""
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(name)
            return state is not None and state.open_until > now

    def record_failure(self, name: str, *, now: float | None = None) -> bool:
        """Count a failure; return True if this one opened the breaker.

        The count is not cleared here. Once a provider has reached *threshold*
        it stays at or above it until something succeeds, so every subsequent
        failure re-opens immediately — the trial request after a cool-down does
        not get to spend the whole budget again on an upstream that is still
        down.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.setdefault(name, _BreakerState())
            state.failures += 1
            if state.failures >= self.threshold:
                state.open_until = now + self.cooldown
                return True
            return False

    def record_throttle(
        self, name: str, seconds: float | None = None, *, now: float | None = None
    ) -> float:
        """Pause *name* without counting a failure against it; return the pause.

        A rate limit says "not yet", not "broken", so it must not move the
        failure count: three 429s in a burst are not the three consecutive
        failures the cool-down exists for, and treating them as such latches a
        working provider out of rotation for five minutes at exactly the moment
        the product needs it most.

        *seconds* is the provider's own ``Retry-After``, clamped to
        ``throttle_pause_max`` — see the comment on the clamp for why a header
        we do not control is not allowed to set an unbounded pause. **When it
        gives none,
        the pause is ours to choose, and choosing it badly is what took the
        product down.** This used to fall back to
        ``llm_rate_limit_backoff_seconds`` — the delay between two attempts
        *within* one call, 2 seconds, which is the right number for "wait a beat
        and try again" and a catastrophic one for "this provider is out of
        quota". Groq sends ``Retry-After`` and so was paused for the ~300s it
        asked for; OpenRouter and Gemini do not, so they were paused for two
        seconds and then hit again, three HTTP calls at a time, around the clock.
        On production that was 3059 provider calls a day for 147 completions,
        which kept all three tiers exhausted and left every draft on the
        deterministic template — and a template draft never auto-sends.

        So an unasked-for pause starts at ``throttle_pause`` and **doubles per
        consecutive throttle** up to ``throttle_pause_max``: a provider that is
        briefly busy comes back in a minute, and one that is out of quota for the
        day backs off to the ceiling instead of spending the quota it is waiting
        for. A success clears the count (``record_success`` drops the state), so
        recovery costs one call, not a climb back down.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.setdefault(name, _BreakerState())
            state.throttles += 1
            if seconds is None:
                # 2**0 on the first throttle, so the base is the base. The
                # exponent is capped before it is raised, not after: a provider
                # out of quota for a week accumulates throttles indefinitely,
                # and `60.0 * 2**1024` is an OverflowError rather than a large
                # number — a crash out of the one function whose entire job is
                # to keep a throttled provider from becoming an outage.
                steps = min(state.throttles - 1, self._max_doubling_steps)
                pause = min(self.throttle_pause * (2**steps), self.throttle_pause_max)
            else:
                pause = seconds
            # Clamped whoever chose it. The escalating branch above already
            # respects the ceiling; a provider's own `Retry-After` did not, and
            # it is a header we neither control nor sanity-check. One upstream
            # answering `Retry-After: 86400` — a daily quota reset, a gateway's
            # own idea of a backoff, a typo in a value meant as milliseconds —
            # latched a working provider out of the chain for a day, from a
            # single response, with no way to clear it short of a restart.
            #
            # Honouring the ceiling instead costs one extra request per pause
            # window against a provider that may still say no, and buys a chain
            # that recovers on its own. Where the provider really is out until
            # tomorrow, the escalation above walks the pause back up to this
            # same ceiling within a few calls.
            pause = max(0.0, min(pause, self.throttle_pause_max))
            state.open_until = max(state.open_until, now + pause)
            return pause

    def record_success(self, name: str) -> None:
        with self._lock:
            self._state.pop(name, None)

    def reset(self) -> None:
        """Clear all state — used by tests and after a config change."""
        with self._lock:
            self._state.clear()

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Current state, for the health endpoint / debugging."""
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "failures": state.failures,
                    # Distinguishes "out of quota" from "broken" at a glance:
                    # a provider paused with failures 0 and throttles climbing
                    # is rate limited, not down.
                    "throttles": state.throttles,
                    "seconds_until_retry": max(0.0, state.open_until - now),
                }
                for name, state in self._state.items()
            }


breaker = CircuitBreaker(
    threshold=settings.llm_breaker_threshold,
    cooldown_seconds=float(settings.llm_breaker_cooldown_seconds),
    throttle_pause_seconds=float(settings.llm_rate_limit_pause_seconds),
    throttle_pause_max_seconds=float(settings.llm_rate_limit_pause_max_seconds),
)


# --------------------------------------------------------------------------- #
# The chain                                                                    #
# --------------------------------------------------------------------------- #


class AllProvidersFailed(RuntimeError):
    """Every configured provider failed or was skipped.

    Callers should fall back to their own deterministic template.
    :func:`app.services.openrouter_client.chat_completion` converts this into
    ``OpenRouterError`` so the existing ``except OpenRouterError`` handlers at
    every call site keep working unchanged.
    """


class AllProvidersRateLimited(AllProvidersFailed):
    """Every provider was rate limited — nothing is broken, we asked too fast.

    A subclass so that the twenty-odd ``except OpenRouterError`` call sites keep
    their existing behaviour untouched, while the few that can do something
    better than a template can tell the two apart. The difference matters most
    where the fallback is *sent* rather than shown: a generic reply mailed to a
    recruiter cannot be taken back, and "the model was busy for ten seconds" is
    a bad reason to spend a candidate's first impression on boilerplate.
    """


class _Pacer:
    """A floor on how often each provider is called.

    The breaker handles providers that are down; this handles the traffic that
    makes them say no. A scan that finds sixty recruiter emails would otherwise
    fire sixty requests at a free tier as fast as the worker could loop, which
    manufactures the 429s the retry logic above then has to recover from. One
    call every ``llm_min_interval_seconds`` is slower per email and much faster
    over a burst, because none of it is spent in backoff.

    Per-process, like the breaker: two workers pace independently, which is the
    same trade the breaker already makes and for the same reason.
    """

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait_for(self, name: str, interval: float) -> float:
        """Block until *name* may be called again; return how long that took."""
        if interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            earliest = self._last.get(name, 0.0) + interval
            delay = max(0.0, earliest - now)
            # Reserved before releasing the lock, so concurrent callers queue
            # behind each other rather than all reading the same "last" value
            # and firing together — which is the burst this exists to prevent.
            self._last[name] = now + delay
        if delay:
            _sleep(delay)
        return delay

    def reset(self) -> None:
        with self._lock:
            self._last.clear()


pacer = _Pacer()


#: The least time worth starting an HTTP request with. Below this the request
#: cannot plausibly complete, so spending one buys nothing and costs a real
#: round trip — and, worse, a timeout the breaker would read as the provider
#: failing when the only thing that failed is our own clock.
_MIN_USEFUL_TIMEOUT = 1.0


@dataclass(frozen=True)
class Completion:
    """A finished completion plus which provider actually served it."""

    text: str
    provider: str
    model: str


# Statuses that mean "ask again shortly" rather than "this is broken". 429 is
# the free tier's normal steady state under load; 503/529 are the overload
# signals the same providers send when they shed traffic instead of queueing it.
_TRANSIENT_STATUSES = frozenset({429, 503, 529})

# Providers that answer 200 with an error body say it in words instead.
_RATE_LIMIT_PHRASES = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "quota",
    "temporarily rate",
    "overloaded",
    "please try again later",
)


def _looks_rate_limited(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES)


# What a provider says when the model id is not one of its own. Matched on the
# body rather than on 404 alone, because these APIs also answer 400 for it and
# because a bare 404 can equally mean a mistyped base URL — a distinction the
# operator needs in the message, but not one that changes the remedy.
_MODEL_NOT_FOUND_PHRASES = (
    "model not found",
    "model_not_found",
    "no such model",
    "unknown model",
    "does not exist",
    "is not a valid model",
    "invalid model",
    "has been decommissioned",
    "decommissioned",
    "model has been deprecated",
    "no endpoints found",
)


def _looks_like_missing_model(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(phrase in lowered for phrase in _MODEL_NOT_FOUND_PHRASES)


# What a provider says when the prompt does not fit. Matched on the body
# because the status is 400 at every provider here and 400 is also what a
# malformed request gets — and because 413 (which some gateways in front of
# these APIs return instead) needs no phrase at all.
_TOO_LARGE_PHRASES = (
    "maximum context length",
    "context length exceeded",
    "context_length_exceeded",
    "reduce the length",
    "too many tokens",
    "token limit",
    "prompt is too long",
    "input is too long",
    "request too large",
    "request entity too large",
    "payload too large",
    "string too long",
)


def _looks_too_large(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(phrase in lowered for phrase in _TOO_LARGE_PHRASES)


def _too_large(provider: Provider, detail: str) -> RequestTooLarge:
    return RequestTooLarge(
        f"{provider.name} refused the prompt as too large for {provider.model!r} "
        f"({detail.strip() or 'no detail given'}) — the input needs trimming, "
        f"not a retry"
    )


# What a provider says about a key when it is not using a status code to say
# it. Narrower than the lists above on purpose: "invalid" and "key" are common
# words, and mistaking an ordinary refusal for a permanent auth fault would put
# a false entry on `/health` telling an operator to rotate a working key.
_AUTH_FAILURE_PHRASES = (
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "no api key",
    "api key not valid",
    "api key expired",
    "invalid authentication",
    "authentication_error",
    "unauthorized",
    "unauthenticated",
    "user not found",
)


def _looks_like_auth_failure(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(phrase in lowered for phrase in _AUTH_FAILURE_PHRASES)


def _auth_failed(provider: Provider, detail: str) -> AuthenticationFailed:
    """Build the error, naming the setting an operator has to change.

    Same reasoning as :func:`_model_not_found`: the reader is already debugging
    something else, and should not also have to map "cerebras" onto
    ``CEREBRAS_API_KEY`` in their head.
    """
    return AuthenticationFailed(
        f"{provider.name} rejected our API key "
        f"({detail.strip() or 'no detail given'}) — the key is missing, "
        f"expired or revoked; set {provider.name.upper()}_API_KEY to a valid one"
    )


def _model_not_found(provider: Provider, detail: str) -> ModelNotFound:
    """Build the error, naming the setting an operator has to change.

    The env var is in the message because the alternative is a reader mapping
    "groq" back to ``GROQ_MODEL`` themselves at the exact moment they are
    already debugging something else.
    """
    return ModelNotFound(
        f"{provider.name} does not recognise model {provider.model!r} "
        f"({detail.strip() or 'no detail given'}) — the model id has most "
        f"likely been retired upstream; set {provider.name.upper()}_MODEL to a "
        f"current one"
    )


def _detail_from_payload(data, default: str = "") -> str:
    """The provider's own words for a refusal, out of an already-parsed body.

    ``error`` is a **dict** at OpenRouter and Groq (``{"error": {"message":
    …}}``) and a bare **string** at several of the OpenAI-compatible layers in
    front of Gemini and Cerebras, and at every gateway that normalises an
    upstream refusal on its way through. Both shapes reach both readers below,
    which is why the tolerance lives in one function instead of at each of them.

    Split out of :func:`_error_message` because the 200-with-an-error-body path
    in :func:`_call` needs the same answer from a payload it has *already*
    parsed, and was reaching into it by hand::

        (data.get("error") or {}).get("message", "no choices returned")

    which is a ``.get`` on a string the moment ``error`` is one — an
    ``AttributeError``, out of a function whose whole contract is to raise
    :class:`LLMError`. Nothing catches it: :func:`_attempt_with_backoff` handles
    ``LLMError`` and ``RateLimited``, ``chat_completion`` converts
    ``AllProvidersFailed``, and every call site guards on ``OpenRouterError``.
    So the one provider shape that says "rate limited" in a 200 body took out
    the whole chain — the remaining providers were never tried, the caller's
    deterministic template never ran, and an API route 500'd or a Celery task
    died where a degraded answer was the designed outcome.

    *default* is what to say when the body names no error at all. Without one
    the fallback is the body itself, which is the right answer for a response
    that *was* an error and the wrong one for a 200 that merely lacks
    ``choices``.
    """
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        if error is not None:
            return str(error)[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return default or str(data)[:300]


def _error_message(resp) -> str:
    """The provider's own words for why it refused, from an error response.

    Tolerant of every shape these APIs use — see :func:`_detail_from_payload` —
    and of a body that is not JSON at all, because a gateway between us and the
    provider may answer with HTML and that must not turn a diagnosable refusal
    into a parse error.
    """
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - any unparseable body falls back to text
        return (getattr(resp, "text", "") or "")[:300]
    return _detail_from_payload(data)


def _retry_after_seconds(resp) -> float | None:
    """The ``Retry-After`` the provider asked for, in seconds, if it gave one.

    Only the delta-seconds form is honoured. The HTTP-date form is legal and
    essentially unused by these APIs, and guessing at a clock skew to parse it
    would buy a worse number than the caller's own backoff.
    """
    headers = getattr(resp, "headers", None) or {}
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _call(
    provider: Provider,
    messages: list[dict[str, str]],
    *,
    model: str | None,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """One provider attempt.

    Raises :class:`RateLimited` when the provider asked us to slow down and
    :class:`LLMError` on every other failure. The distinction is the caller's
    cue to retry rather than to write the provider off — see
    :class:`RateLimited`.
    """
    payload = {
        # An explicit per-call model override only makes sense for the provider
        # it was written for; everyone else gets their own configured model.
        "model": model if (model and provider.name == "openrouter") else provider.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        resp = httpx.post(
            provider.url, json=payload, headers=provider.headers(), timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"{provider.name} request failed: {exc}") from exc

    # Checked off the status code rather than the raised error: httpx only
    # attaches a response to HTTPStatusError, so reading it here is what makes
    # a 429 distinguishable from a connection reset at all.
    status = getattr(resp, "status_code", None)
    if status in _TRANSIENT_STATUSES:
        raise RateLimited(
            f"{provider.name} is rate limited (HTTP {status})",
            retry_after=_retry_after_seconds(resp),
        )

    # Every refusal is read for its body before raise_for_status, which
    # discards it. That body is the only thing that separates the three
    # permanent faults below from the transient noise the breaker exists to
    # absorb — and, for the ones that are nobody's outage, the only thing that
    # keeps them from being counted as one.
    refusal = ""
    if isinstance(status, int) and status >= 400:
        refusal = detail = _error_message(resp)

        # An expired key is the failure most likely to be mistaken for weather.
        # 403 as well as 401: several of these gateways answer 403 for a
        # disabled or out-of-credit key rather than 401.
        if status in (401, 403):
            raise _auth_failed(provider, detail)

        # 413 needs no phrase; 400 does, because it is also what a genuinely
        # malformed request gets.
        if status == 413 or (status == 400 and _looks_too_large(detail)):
            raise _too_large(provider, detail)

        # A retired model id is reported as 404 (OpenRouter, Groq) or 400 (some
        # OpenAI-compatible layers). A bare 404 can equally mean a mistyped
        # base URL — a distinction the operator needs in the message, but not
        # one that changes the remedy.
        if status == 404 or (status == 400 and _looks_like_missing_model(detail)):
            raise _model_not_found(provider, detail)

    try:
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # The provider's own words, when it gave any — read once, above.
        # `raise_for_status` reports only the status line, so a 500 whose body
        # says "upstream model timed out" used to reach the log as "request
        # failed: Server error '500'": the same string for every distinct
        # cause, which is what makes a chain of absorbed failures unreadable
        # after the fact. A `ValueError` from a 2xx body that is not JSON has
        # no refusal to quote and keeps the bare message.
        suffix = f": {refusal}" if refusal and refusal not in str(exc) else ""
        raise LLMError(f"{provider.name} request failed: {exc}{suffix}") from exc

    # OpenRouter (and Gemini's compat layer) report upstream errors as a 200
    # with an `error` body rather than a non-2xx status — including the rate
    # limits, which is why the phrase check is here and not only on the status.
    if isinstance(data, dict) and "choices" not in data:
        detail = _detail_from_payload(data, "no choices returned")
        if _looks_rate_limited(detail):
            raise RateLimited(
                f"{provider.name} is rate limited: {detail}",
                retry_after=_retry_after_seconds(resp),
            )
        # OpenRouter reports a retired model this way too — 200, no choices,
        # "No endpoints found for <model>" — so the same check has to run on
        # this path or the exact failure that took production down is the one
        # shape we miss. The other two permanent faults reach us the same way
        # for the same reason: a gateway that normalises an upstream refusal
        # into a 200 normalises *every* upstream refusal, not just the ones we
        # happened to meet first.
        if _looks_like_missing_model(detail):
            raise _model_not_found(provider, detail)
        if _looks_too_large(detail):
            raise _too_large(provider, detail)
        if _looks_like_auth_failure(detail):
            raise _auth_failed(provider, detail)
        raise LLMError(f"{provider.name} request failed: {detail}")

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{provider.name} returned an unexpected payload: {exc}") from exc

    # The free reasoning models park the answer in `reasoning` with a null
    # `content` — see the openrouter_client docstring.
    text = message.get("content") or message.get("reasoning") or ""
    if not isinstance(text, str) or not text.strip():
        raise LLMError(f"{provider.name} returned an empty completion")
    return text.strip()


def complete(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 800,
    timeout: float = 15.0,
) -> Completion:
    """Try each configured provider in order; return the first success.

    Raises :class:`AllProvidersFailed` when none of them produce usable text —
    the signal for the caller to use its own static template.

    *timeout* bounds one HTTP request. The whole trip is bounded separately, by
    ``llm_total_deadline_seconds``, because those two numbers were never the
    same thing and only one of them was ever enforced. A provider that accepts
    the connection, holds it for the full *timeout* and then answers 429 is
    retried — correctly, it is a throttle — and the retry budget that was
    supposed to stop that counted only the seconds spent *asleep between*
    attempts, not the minutes spent waiting inside them. At the shipped numbers
    that is three attempts of 60s against four providers: 720 seconds of one
    caller's wall clock, policed by a budget of 20.

    The deadline is the outer bound on all of it. It is the larger of the
    caller's own *timeout* and the setting, so asking for a single long attempt
    still buys one; past it, the providers not yet reached are skipped exactly
    as an open breaker skips them, and the caller falls back to its template —
    which is the designed outcome for "the chain could not answer in time" and
    is a great deal better than answering eleven minutes late.
    """
    providers = _providers()
    if not providers:
        raise AllProvidersFailed(
            "No LLM provider is configured — set OPENROUTER_API_KEY, "
            "GEMINI_API_KEY, GROQ_API_KEY or CEREBRAS_API_KEY"
        )

    deadline = time.monotonic() + max(
        timeout, float(settings.llm_total_deadline_seconds)
    )
    errors: list[str] = []
    # Whether every provider we actually got a verdict from said "too fast".
    # A single hard failure anywhere makes this a real outage rather than a
    # throttle, and the caller should treat it as one.
    throttled_only = True
    saw_verdict = False

    for provider in providers:
        if breaker.is_open(provider.name):
            logger.debug("llm provider %s skipped (breaker open)", provider.name)
            errors.append(f"{provider.name}: skipped, circuit open")
            continue

        remaining = deadline - time.monotonic()
        if remaining < _MIN_USEFUL_TIMEOUT:
            # Not enough left to be worth a request. Skipped rather than
            # attempted with a sliver of a timeout, which would spend a real
            # round trip and a real slot on a call that cannot finish, and
            # would then be recorded against the breaker as if the provider had
            # failed us.
            logger.warning(
                "llm provider %s skipped — the chain is out of time", provider.name
            )
            errors.append(f"{provider.name}: skipped, chain deadline reached")
            continue

        started = time.monotonic()
        outcome = _attempt_with_backoff(
            provider,
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            deadline=deadline,
        )
        saw_verdict = True

        if isinstance(outcome, str):
            breaker.record_success(provider.name)
            _clear_model_error(provider.name)
            _clear_auth_error(provider.name)
            logger.info(
                "llm served by %s (%s) in %.2fs",
                provider.name,
                provider.model,
                time.monotonic() - started,
            )
            return Completion(
                text=outcome, provider=provider.name, model=provider.model
            )

        if isinstance(outcome, RateLimited):
            # Not a failure — see CircuitBreaker.record_throttle. Pause this
            # provider for what it asked for, or — when it asked for nothing —
            # for an escalating pause the breaker chooses. Passing None rather
            # than a default here is the point: `or` would also swallow a
            # legitimate `Retry-After: 0`, and the breaker cannot escalate a
            # number it was handed.
            pause = breaker.record_throttle(provider.name, outcome.retry_after)
            logger.warning(
                "llm provider %s rate limited (%s) — paused %.1fs, not counted "
                "against the breaker",
                provider.name,
                outcome,
                pause,
            )
        elif isinstance(outcome, ModelNotFound):
            # Still a failure for the breaker — there is no point calling a
            # provider whose model does not exist — but logged at ERROR and
            # recorded, because unlike everything else here it will not fix
            # itself and no amount of waiting helps.
            throttled_only = False
            breaker.record_failure(provider.name)
            _record_model_error(provider.name, str(outcome))
            logger.error("llm provider %s is misconfigured: %s", provider.name, outcome)
        elif isinstance(outcome, AuthenticationFailed):
            # Handled exactly like a retired model, for exactly the same
            # reason, into a different map — see AuthenticationFailed.
            throttled_only = False
            breaker.record_failure(provider.name)
            _record_auth_error(provider.name, str(outcome))
            logger.error(
                "llm provider %s rejected our credentials: %s", provider.name, outcome
            )
        elif isinstance(outcome, ChainDeadlineReached):
            # Not the provider's failure and not even its verdict — see the
            # class. Recorded in the error list so the terminal message says
            # why the chain stopped short, and nowhere else.
            throttled_only = False
        elif isinstance(outcome, RequestTooLarge):
            # Deliberately *not* recorded against the breaker: the provider is
            # healthy and the prompt is ours. See RequestTooLarge for what
            # counting it did to everybody else's requests.
            throttled_only = False
            logger.warning(
                "llm provider %s could not fit the prompt (%s) — trying the "
                "next provider, whose context window may be larger",
                provider.name,
                outcome,
            )
        else:
            throttled_only = False
            tripped = breaker.record_failure(provider.name)
            logger.warning(
                "llm provider %s failed (%s)%s",
                provider.name,
                outcome,
                " — circuit opened" if tripped else "",
            )
        errors.append(str(outcome))

    detail = "; ".join(errors)
    if saw_verdict and throttled_only:
        raise AllProvidersRateLimited("All LLM providers are rate limited: " + detail)
    raise AllProvidersFailed("All LLM providers failed: " + detail)


def _attempt_with_backoff(
    provider: Provider,
    messages: list[dict[str, str]],
    *,
    model: str | None,
    temperature: float,
    max_tokens: int,
    timeout: float,
    deadline: float,
) -> str | LLMError:
    """Call *provider*, retrying only what is worth retrying.

    Returns the completion text, or the last error rather than raising it — the
    chain needs to look at *which kind* of error it was to decide between
    pausing the provider and writing it off, and an exception object is the
    honest way to hand that back without a second control-flow channel.

    Only :class:`RateLimited` is retried. Retrying a hard failure here would
    multiply the timeout the breaker exists to spend once.

    **The retry budget is wall-clock, not sleep.** It used to add up the pauses
    between attempts and nothing else, which measures the cheap half: a fast
    429 costs almost exactly its backoff, so the two numbers agreed in every
    healthy case and diverged by two orders of magnitude in the one that
    mattered. An upstream that holds the connection for the full *timeout*
    before refusing spends that time just as surely as a sleep does — more
    expensively, in fact, since it is holding a socket as well as the caller —
    and a budget that cannot see it is not a budget. ``llm_rate_limit_max_wait_
    seconds`` now means what its name always said: how long this provider may
    have in total before we move on.

    *deadline* is the chain's outer bound (see :func:`complete`), and each
    attempt's timeout is clipped to what is left of it so the last request
    cannot run past the end.
    """
    attempts = max(0, settings.llm_rate_limit_retries) + 1
    budget = settings.llm_rate_limit_max_wait_seconds
    started = time.monotonic()
    called = False
    last: LLMError = LLMError(f"{provider.name} was never called")

    for attempt in range(attempts):
        pacer.wait_for(provider.name, settings.llm_min_interval_seconds)
        remaining = deadline - time.monotonic()
        if remaining < _MIN_USEFUL_TIMEOUT:
            if not called:
                # The pacer's own wait crossed the deadline, so this provider
                # never got a request at all. Reported as its own kind rather
                # than as the placeholder error below, which the chain would
                # otherwise record against the breaker — marking a provider
                # unhealthy on the evidence that *we* ran out of time before
                # speaking to it.
                return ChainDeadlineReached(
                    f"{provider.name} was not called — the chain ran out of time"
                )
            logger.warning(
                "llm provider %s gave up retrying — the chain is out of time",
                provider.name,
            )
            break
        called = True
        try:
            return _call(
                provider,
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=min(timeout, remaining),
            )
        except RateLimited as exc:
            last = exc
            if attempt == attempts - 1:
                break
            # The provider's own number when it gave one; otherwise widen the
            # gap each time, because the thing that got us throttled was the
            # rate we were already using.
            delay = exc.retry_after
            if delay is None:
                delay = settings.llm_rate_limit_backoff_seconds * (2**attempt)
            spent = time.monotonic() - started
            if spent + delay > budget:
                # Waiting past the budget would push the cost of the throttle
                # onto a caller that is holding a database transaction open.
                # Hand it back and let the next provider try.
                break
            logger.info(
                "llm provider %s rate limited, retrying in %.1fs (attempt %d/%d)",
                provider.name,
                delay,
                attempt + 1,
                attempts,
            )
            _sleep(delay)
        except LLMError as exc:
            return exc

    return last


__all__ = [
    "AllProvidersFailed",
    "AllProvidersRateLimited",
    "AuthenticationFailed",
    "ChainDeadlineReached",
    "CircuitBreaker",
    "Completion",
    "LLMError",
    "ModelNotFound",
    "Provider",
    "RateLimited",
    "RequestTooLarge",
    "auth_errors",
    "breaker",
    "complete",
    "configured_providers",
    "is_configured",
    "model_errors",
    "pacer",
]
