"""The single entry point every AI feature calls, plus the text-hygiene helpers.

Despite the name this is no longer OpenRouter-specific: :func:`chat_completion`
delegates to :mod:`app.services.llm_router`, which tries OpenRouter, Gemini,
Groq and Cerebras in order before giving up. The module keeps its name and its
``OpenRouterError`` so the eight call sites that already handle that exception
need no changes — a failure of the *whole chain* looks exactly like the single
provider failing used to, and every caller already falls back to a static
template on it.

We only ever use free-tier models (never Anthropic directly). Everything is
plain httpx so it stays trivially mockable in tests — no SDK, no hidden global
state.

One provider quirk shapes this module. The free tier's reasoning models
(``openai/gpt-oss-20b:free`` in particular) frequently return ``content: null``
with the whole response — chain-of-thought *and* the answer — parked in
``reasoning``. Two consequences:

* the client falls back to ``reasoning`` rather than crashing on ``None``;
* callers that put the text in front of a user must run it through
  :func:`looks_like_reasoning` first, or a candidate's outreach email ends up
  reading "We need to write a cold email. The user wants…".

Callers that ask for JSON are unaffected: the object is still in there, and the
extractor finds it.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.services import llm_router


class OpenRouterError(RuntimeError):
    """Raised when no provider in the chain could produce a completion."""


class RateLimitedError(OpenRouterError):
    """Every provider was rate limited, rather than broken.

    A subclass, so ``except OpenRouterError`` at every existing call site still
    catches it and still falls back to that call site's template. Callers that
    *send* what they generate can catch this narrower type instead and hold the
    message for a retry: being throttled for a few seconds is a poor reason to
    mail a recruiter boilerplate under the candidate's name.
    """


def _balanced_spans(text: str) -> list[tuple[int, int]]:
    """Every top-level ``{...}`` run in *text*, as inclusive ``(start, end)`` pairs.

    Braces inside JSON strings are ignored, so a value containing ``"}"`` — a
    salary range, a code sample in a cover letter — cannot close the object
    early. Escapes are tracked for the same reason: ``"\\\\"`` ends a string and
    ``"\\""`` does not.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            # Only meaningful inside an object we are already tracking; a quote
            # in the surrounding prose is not the start of a JSON string.
            in_string = depth > 0
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append((start, i))
    return spans


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the JSON object out of a model response, or ``None``.

    Tolerates markdown fences and surrounding prose — including a reasoning
    model's scratchpad, which is why asking for JSON is the reliable way to get
    clean user-facing text out of the free tier.

    That tolerance is the whole job, and spanning from the first ``{`` to the
    last ``}`` — which is what this did first — only delivers it when the prose
    holds no braces of its own. It often does: a scratchpad restating the shape
    it was asked for (``we need to emit {the fields}``), a trailing note about
    ``{placeholders}``, two objects where the model answered twice. In every one
    of those the span covers prose as well as JSON, ``json.loads`` refuses it,
    and the caller gets ``None`` — which each of the twenty-odd call sites reads
    as "the model failed" and answers with a static template. A usable answer
    was in the response the whole time.

    So the scan is by balanced braces instead, and each candidate is parsed on
    its own. Where more than one parses, the richest object wins — the answer
    carries more fields than an aside about the schema does — and an equally
    rich tie goes to the later one, because reasoning precedes its conclusion.
    Responses with a single object, which is nearly all of them, take the same
    path they always did.
    """
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)

    best: dict[str, Any] | None = None
    best_rank = (-1, -1)
    for start, end in _balanced_spans(text):
        try:
            parsed = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        rank = (len(parsed), end)
        if rank >= best_rank:
            best, best_rank = parsed, rank
    return best


#: A markdown fence wrapped around a whole answer. Requires the newline the
#: opener is followed by and the one the closer sits on, so a fence that is
#: really part of the prose — a reply quoting a snippet inline — is left alone.
#:
#: Deliberately not the pattern inside :func:`extract_json_object`, which is
#: looser (no newline required, ``re.S``) because a JSON object may be a single
#: line and nothing it can eat is content. Prose cannot afford that.
_CODE_FENCE_RE = re.compile(r"^```[a-z]*[ \t]*\r?\n|\r?\n```[ \t]*$", re.I)


def strip_code_fence(text: str) -> str:
    """Remove a markdown fence the model wrapped a *prose* answer in.

    Every JSON-parsing call site in this package is already fence-proof —
    :func:`extract_json_object` strips one before it looks for the object, and
    ``recruiter_discovery`` strips one again even though its prompt says "no
    markdown fence". Two independent places concluding that a model fences its
    answer whatever the prompt says is the evidence; this is that conclusion
    made available to the call sites that want prose back rather than JSON, and
    which therefore have nothing else standing between the fence and the reader.

    Where the reader is a recruiter, the fence is delivered: ``` on its own
    line above the greeting of a mail the candidate is judged by.

    Each end is stripped independently rather than only as a matched pair, so a
    response the token limit cut off mid-fence still loses its opener. The
    prompts that use this forbid markdown, so there is no legitimate body that
    ends in a fence for the trailing half to damage.
    """
    return _CODE_FENCE_RE.sub("", (text or "").strip()).strip()


# Openers and connectives that only ever show up in a model thinking out loud.
_REASONING_MARKERS = (
    "we need to",
    "the user says",
    "the user wants",
    "the user asks",
    "the instruction",
    "the developer",
    "let's produce",
    "let's write",
    "let me write",
    "so we need",
    "we must",
    "we should produce",
    "okay, so",
    "first, i",
)


def looks_like_reasoning(text: str) -> bool:
    """True when *text* reads as chain-of-thought rather than a finished answer.

    Deliberately conservative — it only fires on the opening of the text and on
    repeated first-person-planning markers, so a legitimate email that happens to
    contain "we must" once is not thrown away.
    """
    if not text:
        return False
    head = re.sub(r"\s+", " ", text.strip().lower())[:300]
    if head.startswith(_REASONING_MARKERS):
        return True
    return sum(1 for marker in _REASONING_MARKERS if marker in head) >= 2


def llm_is_configured() -> bool:
    """Whether any provider in the chain can serve a completion.

    Re-exported here because this is the module every AI feature already
    imports, and the gate belongs next to the call it guards. See
    :func:`app.services.llm_router.is_configured` for why asking about one
    provider's key was the wrong question.
    """
    return llm_router.is_configured()


def chat_completion_detailed(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 800,
    timeout: float = 60.0,
) -> llm_router.Completion:
    """Run the provider chain and return the text *plus* who served it.

    Use this over :func:`chat_completion` when the caller wants to record or
    display which model produced a piece of text. Raises ``OpenRouterError``
    when every provider in the chain has failed.
    """
    try:
        return llm_router.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except llm_router.AllProvidersRateLimited as exc:
        # Checked before the base class: RateLimitedError *is* an
        # OpenRouterError, so every existing handler still fires, but the ones
        # that can wait now have something to key off.
        raise RateLimitedError(str(exc)) from exc
    except llm_router.AllProvidersFailed as exc:
        # Re-raised as OpenRouterError so every existing call site's fallback
        # path triggers unchanged.
        raise OpenRouterError(str(exc)) from exc


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 800,
    timeout: float = 60.0,
) -> str:
    """Get a completion from the first provider in the chain that works.

    Tries OpenRouter → Gemini → Groq → Cerebras, skipping any without a
    configured key or with an open circuit breaker. Raises ``OpenRouterError``
    if no provider is configured or all of them fail — the caller's cue to fall
    back to its own static template.
    """
    return chat_completion_detailed(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    ).text
