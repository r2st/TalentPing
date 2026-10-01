"""Writing the answer to a recruiter who wrote first.

:mod:`app.services.reply_agent` drafts replies on threads *we* started, where the
transcript carries most of the context. First contact has no transcript — one
message from a stranger and a profile — so the grounding has to come from the
matched profile's resume instead, and the invention guards matter more rather
than less.

**Only genuine first contact reaches this module**, and that is now checked
rather than assumed. It used to mean "we hold no record of this conversation",
which is a claim about our database and not about the message: a candidate who
had been mailing a recruiter from their own inbox for a week had no record here
either, so message six of an interview-scheduling thread was drafted with the
brief below — introduce yourself, say why the role interests you, ask what stage
the process is at — and the CV attached again. :mod:`app.services.conversation_stage`
reads the message's own headers, subject and quoted body, and hands anything
mid-conversation to :func:`reply_agent.draft_reply` with the transcript
recovered from the quote.

The guards are reused rather than reimplemented:
:func:`reply_agent._invents_specifics` catches a Tuesday that exists only in the
draft, and :func:`reply_agent._states_a_figure` catches a salary invented during
a negotiation. A model that invents an availability slot in a reply the user has
already approved is bad; one that invents it in a reply sent without review is
the worst thing this feature can do.

A third comes with them. ``_states_a_figure`` only looks for money once a
message is *already* talking about pay, which is right mid-negotiation and wrong
on first contact: a draft reading "I'm targeting around $250,000" uses no
compensation vocabulary at all, so that check saw nothing and the sentence would
have gone out setting the candidate's floor for them.
:func:`reply_agent._names_a_figure` looks for the money regardless of the words
around it. It was written here and now lives beside the other two, because the
gap it closes was never particular to first contact — a model volunteers a
number mid-thread just as readily.

Everything here is pure — a classification, a profile and a resume in, a
:class:`~app.services.reply_agent.ReplyDraft` out. No database, no Gmail. That is
what makes "this module cannot send anything" checkable rather than promised.
"""
from __future__ import annotations

import logging

from app.core.config import settings
from app.models.email import ReplyTemplate
from app.models.resume import Resume
from app.services import conversation_stage, untrusted
from app.services.openrouter_client import (
    OpenRouterError,
    RateLimitedError,
    chat_completion_detailed,
    extract_json_object,
    looks_like_reasoning,
)
from app.services.profile_service import ScoringTarget
from app.services.recruiter_classifier import Classification
from app.services.reply_agent import (
    ReplyDraft,
    _invents_specifics,
    _names_a_figure,
    _states_a_figure,
)

logger = logging.getLogger(__name__)

# A reply outside this range is not a reply. Too short means the model returned a
# fragment; too long means it wrote an essay to a stranger.
MIN_BODY_CHARS = 40
MAX_BODY_CHARS = 2000

# How much resume text the model is grounded on. Enough for the whole of a normal
# resume, bounded so a ten-page CV can't crowd out the instruction.
MAX_RESUME_CHARS = 4000


_SYSTEM_PROMPT = (
    "You write a job seeker's reply to a recruiter who contacted them out of the "
    "blue about a specific role.\n\n"
    "Absolute rules:\n"
    "1. Ground every claim about the candidate in the resume you are given. If "
    "the resume does not support it, do not say it.\n"
    "2. Never invent specifics: no exact dates or times, no notice period, no "
    "visa or work-authorization status, no salary figure, no acceptance of "
    "anything. If the recruiter asked something the resume cannot answer, ask "
    "them a question back instead of guessing.\n"
    "3. Availability may only be offered in general terms ('this week or next'). "
    "No calendar is connected, so a specific slot would be a fabrication.\n"
    "4. Name the role they wrote about, and give one concrete, resume-backed "
    "reason the candidate is interested.\n"
    "5. Ask about anything material they left out — compensation band, remote "
    "policy, team, stage of the process.\n"
    "6. Warm, direct, under 180 words. No flattery, no hard sell.\n\n"
    'Return ONLY a JSON object: {"subject": "...", "body": "...", "note": "...", '
    '"questions_asked": ["..."]}. `note` is one line for the candidate\'s eyes '
    "explaining the angle you took; it is never sent."
)


def _fallback_body(name: str, role: str | None, company: str | None) -> str:
    """The deterministic reply, used whenever the model can't be trusted.

    Deliberately plain. It says the true things we know — that the note was
    received, that there is interest, that detail is wanted — and nothing else.
    A generic reply is a worse letter than a good generation and a far better one
    than a confident fabrication.
    """
    about = f" about the {role} role" if role else ""
    at = f" at {company}" if company else ""
    return (
        f"Hi,\n\n"
        f"Thanks for reaching out{about}{at} — it caught my attention and I'd be "
        "glad to hear more.\n\n"
        "Could you share a bit more detail on the team, the scope of the role, "
        "and the compensation range you're working with? If it looks like a fit "
        "from there, I'm happy to find time for a short call in the next week or "
        "two.\n\n"
        f"Best,\n{name}"
    )


def _build_prompt(
    *,
    candidate_name: str,
    classification: Classification,
    target: ScoringTarget,
    resume: Resume,
    message_body: str,
    subject: str | None,
) -> str:
    targeting = target.targeting
    parts = [
        "== THE EMAIL YOU ARE REPLYING TO ==",
        # The one block here a stranger wrote. Everything below it — the parsed
        # role, the candidate, the resume — is this product's own records, and
        # fencing those would tell the model to disregard the facts it is
        # supposed to be grounding the reply in.
        untrusted.fence(
            f"Subject: {subject or '(none)'}\n{(message_body or '')[:3000]}",
            label="recruiter email",
        ),
        "",
        "== THE ROLE, AS FAR AS WE COULD READ IT ==",
        f"Title: {classification.role_title or 'not stated'}",
        f"Company: {classification.company or 'not stated'}",
        f"Location: {classification.location or 'not stated'}",
        f"Remote: {classification.remote if classification.remote is not None else 'not stated'}",
        f"Compensation mentioned: {classification.salary_text or 'not stated'}",
        "",
        "== THE CANDIDATE (the only facts you may use) ==",
        f"Name: {candidate_name}",
        f"Applying as: {target.label}",
        f"Roles they want: {', '.join(targeting.roles[:6]) or 'not stated'}",
        f"Skills they lead with: {', '.join(targeting.skills[:12]) or 'not stated'}",
        f"Places they'll work: {', '.join(targeting.locations[:6]) or 'not stated'}"
        + (" (remote only)" if targeting.remote_only else ""),
        "",
        "== THEIR RESUME ==",
        (resume.raw_text or "")[:MAX_RESUME_CHARS] or "(no resume text on file)",
    ]

    if classification.asks:
        parts += [
            "",
            "== QUESTIONS THEY ASKED — address each one ==",
            *(f"- {q}" for q in classification.asks),
        ]

    # Salary is the one field the candidate has stated that the model may use,
    # and only reactively. Volunteering a number to a stranger sets the
    # candidate's negotiating floor for them.
    if targeting.salary_min or targeting.salary_max:
        parts += [
            "",
            "== COMPENSATION ==",
            "The candidate has a target range on file. You may only refer to it "
            "if the recruiter asked about compensation, and then only as a range "
            "they are 'targeting'. Never state it as a minimum or a demand. If "
            "they did not ask, do not mention money except to ask for their band.",
        ]
    else:
        parts += [
            "",
            "== COMPENSATION ==",
            "The candidate has stated no target range. You MUST NOT name any "
            "figure. Ask for the band they are working with instead.",
        ]

    return "\n".join(parts)


def _subject_for(classification: Classification, original_subject: str | None) -> str:
    """Reply on the recruiter's own subject line, so it threads properly.

    The ``startswith("re:")`` test this used to do is the one
    :mod:`app.services.conversation_stage` documents as insufficient: the
    gateway tag goes in *front* of the marker, so "[EXTERNAL] Re: Interview
    Discussion" failed it and went back out as "Re: [EXTERNAL] Re: Interview
    Discussion". :func:`~app.services.conversation_stage.reply_subject` is the
    one answer all three reply paths now share.
    """
    return conversation_stage.reply_subject(
        original_subject,
        fallback=(classification.role_title or "").strip() or "your note",
    )


def draft(
    *,
    candidate_name: str,
    classification: Classification,
    target: ScoringTarget,
    message_body: str,
    subject: str | None = None,
) -> ReplyDraft:
    """Write the reply. Never sends; always returns something usable.

    ``generated_with`` is ``"llm"`` only when a model produced text that passed
    every guard, and ``fallback_reason`` says which guard refused when it did
    not. Both are for the caller; nothing here acts on them.

    **A template from this module can be sent unreviewed, and that is on
    purpose.** This docstring used to claim the opposite — "a templated reply is
    never sent unreviewed" — which is what
    :mod:`app.services.thread_reply_policy` enforces for the *other* pipeline
    and has never been true here. The two pipelines answer different questions:
    a mid-conversation template is contextual only in its shape, while this one
    names the role and the company the recruiter wrote about, and holding every
    template would switch auto-reply off for the whole of an LLM outage —
    exactly when a deterministic fallback is worth having. See the comment above
    ``route = decision.route`` in ``recruiter_reply_service._write_reply``.

    The single exception is ``"throttled"``, which that caller downgrades to a
    draft. Not for safety but for repetition: every provider being rate limited
    happens when a scan finds a lot of mail at once, and it is how nineteen
    recruiters came to receive the same paragraph, byte for byte, on the busiest
    day the inbox had.
    """
    resume = target.resume
    template = (
        ReplyTemplate.QUESTION if classification.asks else ReplyTemplate.INTERESTED
    )
    fallback = _fallback_body(
        candidate_name, classification.role_title, classification.company
    )
    result = ReplyDraft(
        body=fallback,
        template=template,
        note=(
            f"First reply to an inbound note about "
            f"{classification.role_title or 'a role'}"
            f"{f' at {classification.company}' if classification.company else ''}, "
            f"written against your {target.label} profile."
        ),
        open_questions=list(classification.asks),
    )

    if resume is None:
        logger.info("inbound reply fell back to template: no resume on the target")
        result.fallback_reason = "no_resume"
        return result

    try:
        completion = chat_completion_detailed(
            [
                {"role": "system", "content": untrusted.guarded(_SYSTEM_PROMPT)},
                {
                    "role": "user",
                    "content": _build_prompt(
                        candidate_name=candidate_name,
                        classification=classification,
                        target=target,
                        resume=resume,
                        message_body=message_body,
                        subject=subject,
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.6,
            max_tokens=1200,
        )
    except RateLimitedError as exc:
        # Temporary by definition. Marked so the caller holds this for review
        # instead of mailing the template out under the candidate's name — the
        # burst that causes the throttle is exactly when every recruiter would
        # otherwise get the same paragraph.
        logger.warning("inbound reply fell back to template, rate limited: %s", exc)
        result.fallback_reason = "throttled"
        return result
    except OpenRouterError as exc:
        logger.info("inbound reply fell back to template: %s", exc)
        result.fallback_reason = "unavailable"
        return result

    data = extract_json_object(completion.text)
    body = (data or {}).get("body") if data else None
    if not isinstance(body, str) or not body.strip():
        logger.info("inbound reply fell back to template: no body in the response")
        result.fallback_reason = "no_body"
        return result
    body = body.strip()

    # The guards, in order of how bad the failure would be.
    if looks_like_reasoning(body):
        logger.warning("inbound reply rejected: model returned its scratchpad")
        result.fallback_reason = "rejected_scratchpad"
        return result

    if not (MIN_BODY_CHARS <= len(body) <= MAX_BODY_CHARS):
        logger.warning("inbound reply rejected: body was %d chars", len(body))
        result.fallback_reason = "rejected_length"
        return result

    # The "thread" a first reply is checked against is the recruiter's message
    # plus the resume: anything concrete in the draft has to come from one of
    # them, or the model made it up.
    known_text = f"{message_body or ''}\n{resume.raw_text or ''}"
    fabricated = (
        _invents_specifics(body, known_text)
        or _states_a_figure(body, known_text)
        or _names_a_figure(body, known_text)
    )
    if fabricated:
        logger.warning(
            "inbound reply rejected: model asserted %r, unsupported by the message "
            "or the resume",
            fabricated,
        )
        result.fallback_reason = "rejected_invention"
        return result

    result.body = body
    result.generated_with = "llm"
    result.model = f"{completion.provider}:{completion.model}"
    note = (data or {}).get("note")
    if isinstance(note, str) and note.strip():
        result.note = note.strip()[:500]
    return result


def reply_subject(classification: Classification, original_subject: str | None) -> str:
    """The subject the reply goes out under. See :func:`_subject_for`."""
    return _subject_for(classification, original_subject)


__all__ = [
    "MAX_BODY_CHARS",
    "MIN_BODY_CHARS",
    "draft",
    "reply_subject",
]
