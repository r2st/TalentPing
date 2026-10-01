"""The shared LLM entry point: the free tier's rough edges, handled.

The free reasoning models (``openai/gpt-oss-20b:free``) routinely return
``content: null`` with the answer in ``reasoning``, and OpenRouter reports
upstream failures as a 200 with an ``error`` body. Both used to crash the caller.

``chat_completion`` now runs the whole provider chain, so these tests configure
OpenRouter alone and patch transport at :mod:`app.services.llm_router`. The
multi-provider fallback itself is covered in ``test_llm_router.py``.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import llm_router
from app.services import openrouter_client as client
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
    looks_like_reasoning,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _isolated_chain(monkeypatch):
    """One provider, no leftover breaker state, regardless of the real .env."""
    for name in ("openrouter", "gemini", "groq", "cerebras"):
        monkeypatch.setattr(
            llm_router.settings, f"{name}_api_key", "", raising=False
        )
    llm_router.breaker.reset()
    yield
    llm_router.breaker.reset()


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setattr(
        llm_router.settings, "openrouter_api_key", "test-key", raising=False
    )


def _reply(monkeypatch, payload, status_code=200):
    monkeypatch.setattr(
        llm_router.httpx, "post", lambda *a, **kw: _Response(payload, status_code)
    )


class TestChatCompletion:
    def test_requires_at_least_one_provider(self):
        with pytest.raises(OpenRouterError, match="No LLM provider is configured"):
            chat_completion([{"role": "user", "content": "hi"}])

    def test_returns_content(self, with_key, monkeypatch):
        _reply(monkeypatch, {"choices": [{"message": {"content": "  hello  "}}]})
        assert chat_completion([{"role": "user", "content": "hi"}]) == "hello"

    def test_falls_back_to_reasoning_when_content_is_null(self, with_key, monkeypatch):
        """The provider quirk that used to raise AttributeError mid-campaign."""
        _reply(
            monkeypatch,
            {"choices": [{"message": {"content": None, "reasoning": "the answer"}}]},
        )
        assert chat_completion([{"role": "user", "content": "hi"}]) == "the answer"

    def test_empty_completion_raises(self, with_key, monkeypatch):
        _reply(monkeypatch, {"choices": [{"message": {"content": None, "reasoning": ""}}]})
        with pytest.raises(OpenRouterError, match="empty"):
            chat_completion([{"role": "user", "content": "hi"}])

    def test_upstream_error_in_a_200_body_raises(self, with_key, monkeypatch):
        """OpenRouter reports provider failures as HTTP 200 with an error body."""
        _reply(monkeypatch, {"error": {"message": "upstream timeout", "code": 502}})
        with pytest.raises(OpenRouterError, match="upstream timeout"):
            chat_completion([{"role": "user", "content": "hi"}])

    def test_malformed_payload_raises(self, with_key, monkeypatch):
        _reply(monkeypatch, {"choices": []})
        with pytest.raises(OpenRouterError):
            chat_completion([{"role": "user", "content": "hi"}])

    def test_transport_failure_raises(self, with_key, monkeypatch):
        def _boom(*args, **kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(llm_router.httpx, "post", _boom)
        with pytest.raises(OpenRouterError, match="failed"):
            chat_completion([{"role": "user", "content": "hi"}])


class TestLooksLikeReasoning:
    @pytest.mark.parametrize(
        "text",
        [
            "We need to write a cold email to the recruiter.",
            "The user says: a=hello. So we should produce JSON.",
            "Let's write a summary that emphasises Python.",
        ],
    )
    def test_detects_a_scratchpad(self, text):
        assert looks_like_reasoning(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Hi Sam,\n\nI'm a backend engineer with eight years of experience.",
            "Strong match for this role — worth applying today.",
            "",
        ],
    )
    def test_passes_real_prose(self, text):
        assert not looks_like_reasoning(text)

    def test_one_incidental_marker_is_not_enough(self):
        """A real email may say 'we must' once; that alone must not reject it."""
        assert not looks_like_reasoning(
            "Hi Sam, I think we must be a good match for your backend team."
        )


class TestExtractJsonObject:
    def test_parses_a_bare_object(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_strips_markdown_fences(self):
        assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_finds_an_object_inside_prose(self):
        """The whole reason JSON is requested: it survives a leaked scratchpad."""
        raw = 'We need to answer. So the output is {"a": 1}. Done.'
        assert extract_json_object(raw) == {"a": 1}

    def test_returns_none_for_junk(self):
        assert extract_json_object("no json here") is None
        assert extract_json_object("") is None
        assert extract_json_object("{not valid json}") is None

    def test_returns_none_for_a_json_array(self):
        assert extract_json_object("[1, 2, 3]") is None
