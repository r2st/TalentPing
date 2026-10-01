"""The multi-provider fallback chain.

The product promise here is that AI never fails silently: if OpenRouter is down
the email still gets written by Gemini, Groq or Cerebras, and if every one of
them is down the caller falls back to its own deterministic template rather
than shipping an empty body.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import llm_router
from app.services.llm_router import (
    AllProvidersFailed,
    CircuitBreaker,
    complete,
)

MESSAGES = [{"role": "user", "content": "write a cold email"}]

ALL_PROVIDERS = ("openrouter", "gemini", "groq", "cerebras")


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def _ok(text="hello"):
    return _Response({"choices": [{"message": {"content": text}}]})


@pytest.fixture(autouse=True)
def _clean_breaker():
    llm_router.breaker.reset()
    yield
    llm_router.breaker.reset()


@pytest.fixture()
def keys(monkeypatch):
    """Configure whichever providers a test names; clear the rest."""

    def _configure(*names):
        for name in ALL_PROVIDERS:
            monkeypatch.setattr(
                llm_router.settings,
                f"{name}_api_key",
                "test-key" if name in names else "",
                raising=False,
            )

    return _configure


@pytest.fixture()
def transport(monkeypatch):
    """Route each provider's URL to a canned outcome, recording call order."""
    calls: list[str] = []

    def _install(outcomes: dict[str, object]):
        def _post(url, **kwargs):
            name = next(n for n in ALL_PROVIDERS if n in _host_key(url))
            calls.append(name)
            outcome = outcomes[name]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(llm_router.httpx, "post", _post)
        return calls

    return _install


def _host_key(url: str) -> str:
    """Map a provider base URL onto its provider name for the stub."""
    if "openrouter" in url:
        return "openrouter"
    if "generativelanguage" in url:
        return "gemini"
    if "groq" in url:
        return "groq"
    if "cerebras" in url:
        return "cerebras"
    return url


class TestOrdering:
    def test_openrouter_serves_when_healthy(self, keys, transport):
        keys(*ALL_PROVIDERS)
        calls = transport(dict.fromkeys(ALL_PROVIDERS, _ok("from openrouter")))

        result = complete(MESSAGES)

        assert result.text == "from openrouter"
        assert result.provider == "openrouter"
        assert calls == ["openrouter"], "no provider should be tried after a success"

    def test_falls_through_to_gemini(self, keys, transport):
        keys(*ALL_PROVIDERS)
        calls = transport(
            {
                "openrouter": httpx.ConnectError("no route"),
                "gemini": _ok("from gemini"),
                "groq": _ok("groq"),
                "cerebras": _ok("cerebras"),
            }
        )

        result = complete(MESSAGES)

        assert result.provider == "gemini"
        assert result.text == "from gemini"
        assert calls == ["openrouter", "gemini"]

    def test_walks_the_whole_chain_to_cerebras(self, keys, transport):
        keys(*ALL_PROVIDERS)
        calls = transport(
            {
                "openrouter": httpx.ConnectError("down"),
                "gemini": _Response({"error": {"message": "quota exceeded"}}),
                "groq": _Response({}, status_code=500),
                "cerebras": _ok("from cerebras"),
            }
        )

        result = complete(MESSAGES)

        assert result.provider == "cerebras"
        assert calls == list(ALL_PROVIDERS)

    def test_unconfigured_providers_are_skipped_not_attempted(self, keys, transport):
        """A provider with no key must not cost a request."""
        keys("groq")
        calls = transport(dict.fromkeys(ALL_PROVIDERS, _ok("from groq")))

        result = complete(MESSAGES)

        assert result.provider == "groq"
        assert calls == ["groq"]

    def test_each_provider_uses_its_own_model(self, keys, transport, monkeypatch):
        keys("gemini")
        seen = {}

        def _post(url, **kwargs):
            seen.update(kwargs["json"])
            return _ok()

        monkeypatch.setattr(llm_router.httpx, "post", _post)
        complete(MESSAGES)

        assert seen["model"] == llm_router.settings.gemini_model

    def test_an_openrouter_model_override_does_not_leak_to_others(
        self, keys, transport, monkeypatch
    ):
        """`model=` names an OpenRouter model; Gemini would 404 on it."""
        keys("openrouter", "gemini")
        models: list[str] = []

        def _post(url, **kwargs):
            models.append(kwargs["json"]["model"])
            if "openrouter" in url:
                raise httpx.ConnectError("down")
            return _ok()

        monkeypatch.setattr(llm_router.httpx, "post", _post)
        complete(MESSAGES, model="openai/gpt-oss-20b:free")

        assert models == ["openai/gpt-oss-20b:free", llm_router.settings.gemini_model]


class TestTerminalFailure:
    def test_all_failing_raises_for_the_static_template(self, keys, transport):
        keys(*ALL_PROVIDERS)
        transport(dict.fromkeys(ALL_PROVIDERS, httpx.ConnectError("down")))

        with pytest.raises(AllProvidersFailed) as excinfo:
            complete(MESSAGES)

        # The message names every provider tried, so the logs say what broke.
        assert all(name in str(excinfo.value) for name in ALL_PROVIDERS)

    def test_no_keys_at_all_raises(self, keys):
        keys()
        with pytest.raises(AllProvidersFailed, match="No LLM provider is configured"):
            complete(MESSAGES)

    def test_an_empty_completion_counts_as_a_failure(self, keys, transport):
        """A 200 with no usable text must fall through, not return "" upward."""
        keys("openrouter", "gemini")
        transport(
            {
                "openrouter": _Response(
                    {"choices": [{"message": {"content": None, "reasoning": ""}}]}
                ),
                "gemini": _ok("from gemini"),
                "groq": _ok(),
                "cerebras": _ok(),
            }
        )

        assert complete(MESSAGES).provider == "gemini"

    def test_reasoning_field_is_accepted_as_content(self, keys, transport):
        keys("openrouter")
        transport(
            {
                "openrouter": _Response(
                    {"choices": [{"message": {"content": None, "reasoning": "answer"}}]}
                )
            }
        )

        assert complete(MESSAGES).text == "answer"


class TestCircuitBreaker:
    def test_opens_after_the_threshold_and_closes_after_the_cooldown(self):
        breaker = CircuitBreaker(threshold=3, cooldown_seconds=300)

        assert breaker.record_failure("groq", now=0) is False
        assert breaker.record_failure("groq", now=1) is False
        assert breaker.is_open("groq", now=2) is False, "must not trip early"

        assert breaker.record_failure("groq", now=2) is True
        assert breaker.is_open("groq", now=100) is True
        assert breaker.is_open("groq", now=301) is True
        assert breaker.is_open("groq", now=303) is False, "cooldown has elapsed"

    def test_a_success_resets_the_failure_count(self):
        breaker = CircuitBreaker(threshold=3, cooldown_seconds=300)
        breaker.record_failure("groq", now=0)
        breaker.record_failure("groq", now=1)
        breaker.record_success("groq")

        # Without the reset this third failure would trip it.
        assert breaker.record_failure("groq", now=2) is False
        assert breaker.is_open("groq", now=3) is False

    def test_breakers_are_independent_per_provider(self):
        breaker = CircuitBreaker(threshold=2, cooldown_seconds=60)
        breaker.record_failure("groq", now=0)
        breaker.record_failure("groq", now=0)

        assert breaker.is_open("groq", now=1) is True
        assert breaker.is_open("gemini", now=1) is False

    def test_an_open_provider_is_skipped_entirely(self, keys, transport):
        """The whole point: a dead provider stops costing a timeout per call."""
        keys("openrouter", "gemini")
        calls = transport(
            {
                "openrouter": httpx.ConnectError("down"),
                "gemini": _ok("from gemini"),
                "groq": _ok(),
                "cerebras": _ok(),
            }
        )

        for _ in range(llm_router.settings.llm_breaker_threshold):
            assert complete(MESSAGES).provider == "gemini"

        attempts_before = len(calls)
        assert complete(MESSAGES).provider == "gemini"

        assert calls[attempts_before:] == ["gemini"], (
            "openrouter should be skipped once its breaker is open"
        )

    def test_recovery_after_the_cooldown(self, keys, transport, monkeypatch):
        keys("openrouter", "gemini")
        state = {"openrouter_up": False}

        def _post(url, **kwargs):
            if "openrouter" in url:
                if not state["openrouter_up"]:
                    raise httpx.ConnectError("down")
                return _ok("openrouter is back")
            return _ok("from gemini")

        monkeypatch.setattr(llm_router.httpx, "post", _post)

        clock = {"t": 0.0}
        monkeypatch.setattr(llm_router.time, "monotonic", lambda: clock["t"])

        for _ in range(llm_router.settings.llm_breaker_threshold):
            complete(MESSAGES)
        assert llm_router.breaker.is_open("openrouter") is True

        state["openrouter_up"] = True
        clock["t"] = llm_router.settings.llm_breaker_cooldown_seconds + 1

        assert complete(MESSAGES).provider == "openrouter"


class TestObservability:
    def test_logs_which_provider_served_the_request(self, keys, transport, caplog):
        keys("openrouter", "gemini")
        transport(
            {
                "openrouter": httpx.ConnectError("down"),
                "gemini": _ok("from gemini"),
                "groq": _ok(),
                "cerebras": _ok(),
            }
        )

        with caplog.at_level("INFO", logger="app.services.llm_router"):
            complete(MESSAGES)

        served = [r for r in caplog.records if "llm served by" in r.message]
        assert len(served) == 1
        assert "gemini" in served[0].getMessage()
        # And the failure that got us there is on the record too.
        assert any("openrouter failed" in r.getMessage() for r in caplog.records)

    def test_snapshot_reports_open_breakers(self):
        breaker = CircuitBreaker(threshold=1, cooldown_seconds=300)
        breaker.record_failure("groq")

        snap = breaker.snapshot()
        assert "groq" in snap
        assert 0 < snap["groq"]["seconds_until_retry"] <= 300
