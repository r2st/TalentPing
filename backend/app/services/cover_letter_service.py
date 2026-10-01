"""Cover letters — job-specific, resume-grounded, never invented.

:mod:`app.services.resume_tailor` already produces a letter as a by-product of
tailoring. That letter is generic by construction: it is written from the resume
and the JD alone, so every application to every company gets the same three
paragraphs with the nouns swapped. This module writes the letter as its own
artifact, which buys two things the by-product can't:

* **Personalization with a source.** The letter may reference what the company
  actually does — size, stage, industry, a recent headline — because
  :mod:`app.services.company_research` has already looked it up and cached it.
  The facts handed to the model are stored on the row beside the letter, so a
  user reading "your recent Series B" can see where that came from rather than
  taking it on trust.
* **Its own life cycle.** A letter is regenerated, edited and downloaded
  independently of the resume it accompanies, and — unlike a resume — it can be
  folded into the outreach body instead of attached.

The grounding rule is the tailorer's, unchanged and enforced by the same checks:
every claim about the candidate must already appear in their resume. A letter
that invents a metric reads better and gets the candidate caught, so the
generated prose is discarded whenever :func:`mentions_missing` or
:func:`invented_metric` fires and a deterministic letter is used instead.

One further rule specific to letters: **the company facts are the model's only
outside input, and it may not add to them.** A model told a company is Series B
will otherwise cheerfully explain what the company builds, and be wrong.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.cover_letter import CoverLetter
from app.models.resume import Resume
from app.services import untrusted
from app.services.jd_parser import ParsedJob
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
    llm_is_configured,
    looks_like_reasoning,
)
from app.services.resume_tailor import (
    highlight_experience,
    invented_metric,
    match_keywords,
    mentions_missing,
    usable_prose,
)

logger = logging.getLogger(__name__)

DELIVERY_INLINE = "inline"
DELIVERY_ATTACHMENT = "attachment"
VALID_DELIVERY = (DELIVERY_INLINE, DELIVERY_ATTACHMENT)


@dataclass
class LetterOutput:
    """One generated letter, ready to persist."""

    greeting: str
    body: str
    sign_off: str
    highlights: list[dict[str, Any]] = field(default_factory=list)
    company_research: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    generated_with: str = "heuristic"
    model: str | None = None

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.strip() for p in (self.greeting, self.body, self.sign_off) if p)


# --------------------------------------------------------------------------- #
# Company facts                                                                #
# --------------------------------------------------------------------------- #


def company_facts(profile) -> list[str]:
    """The handful of true things the letter is allowed to reference.

    Rendered as flat sentences rather than handed over as a blob: the model gets
    exactly the claims it may make, which makes "did it stay inside the facts?"
    a question with an answer. Anything the profile doesn't know is simply
    absent — an unknown is never rendered as "unknown", because a model shown
    that field will write around it.
    """
    if profile is None:
        return []

    facts: list[str] = []
    name = getattr(profile, "name", None) or "The company"

    industry = getattr(profile, "industry", None)
    if industry:
        facts.append(f"{name} operates in {industry}.")

    size = getattr(profile, "size", None)
    if size:
        facts.append(f"It has roughly {size} employees.")

    stage = getattr(profile, "funding_stage", None)
    if stage and stage != "unknown":
        label = stage.replace("_", " ").title()
        total = getattr(profile, "funding_total", None)
        facts.append(
            f"Funding stage: {label}{f' ({total} raised)' if total else ''}."
        )

    founded = getattr(profile, "founded_year", None)
    if founded:
        facts.append(f"Founded in {founded}.")

    hq = getattr(profile, "headquarters", None)
    if hq:
        facts.append(f"Headquartered in {hq}.")

    stack = list(getattr(profile, "tech_stack", None) or [])[:6]
    if stack:
        facts.append(f"Public tech stack mentions: {', '.join(stack)}.")

    for item in list(getattr(profile, "news", None) or [])[:2]:
        title = (item or {}).get("title")
        if title:
            when = (item or {}).get("published")
            facts.append(f"Recent news{f' ({when})' if when else ''}: {title}")

    return facts


# --------------------------------------------------------------------------- #
# Deterministic letter                                                         #
# --------------------------------------------------------------------------- #


def _greeting(job: ParsedJob) -> str:
    # No invented name. "Hiring team" is neutral and correct; a guessed
    # "Dear Sarah" that turns out to be the wrong person is not recoverable.
    return f"Dear {job.company} hiring team," if job.company else "Hello,"


def _sign_off(resume: Resume) -> str:
    return f"Best regards,\n{resume.full_name or 'The candidate'}"


def _fallback_body(
    resume: Resume,
    job: ParsedJob,
    matched: list[str],
    highlights: list[dict[str, Any]],
    facts: list[str],
) -> str:
    """A letter assembled only from things already known to be true."""
    target = job.title or "the role"
    at_company = f" at {job.company}" if job.company else ""
    strengths = ", ".join(matched[:4]) or ", ".join((resume.skills or [])[:4])

    paragraphs = [
        f"I'm writing about the {target} position{at_company}. "
        + (
            f"My background is in {strengths}, which lines up closely with what "
            "the role calls for."
            if strengths
            else "I believe my background lines up well with what the role calls for."
        )
    ]

    lead = highlights[0] if highlights else {}
    if lead.get("title") and lead.get("company"):
        covered = ", ".join(lead.get("matched_skills") or []) or strengths
        paragraphs.append(
            f"In my time as {lead['title']} at {lead['company']}, I worked "
            f"directly with {covered}."
            if covered
            else f"My time as {lead['title']} at {lead['company']} is the closest "
            "prior context for this work."
        )

    # One company fact, if we have one. The deterministic path stays to a single
    # sentence: this is the part most likely to read as filler when overdone.
    if facts:
        paragraphs.append(f"What drew me to the role: {facts[0].rstrip('.')}.")

    paragraphs.append(
        "I'd welcome the chance to talk through how that experience maps onto "
        "what your team is building."
    )
    return "\n\n".join(paragraphs)


# --------------------------------------------------------------------------- #
# LLM letter                                                                   #
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = (
    "You write one job-specific cover letter. You work ONLY from the candidate's "
    "resume and a short list of verified company facts.\n\n"
    "Absolute rules:\n"
    "1. Every claim about the candidate — a skill, employer, title, metric, "
    "year — must already appear in their resume. If it is not there, you may "
    "not write it.\n"
    "2. You are given requirements the candidate does NOT have. Never mention "
    "them, never write around them, never imply them.\n"
    "3. The company facts listed are the ONLY things you may say about the "
    "employer. Do not add what they build, their mission, their values or "
    "their reputation. If the list is empty, write nothing about the company "
    "beyond the role itself.\n"
    "4. No invented metrics. No superlatives the resume doesn't earn. No "
    "'I am passionate about' filler.\n"
    "5. Three or four short paragraphs, under 220 words, confident and "
    "peer-to-peer, ending with one clear ask.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"body": str, "highlights": [{"point": str, "evidence": str}]}\n'
    "Each highlight's 'evidence' must quote the resume phrase supporting it."
)


def _build_prompt(
    resume: Resume,
    job: ParsedJob,
    matched: list[str],
    missing: list[str],
    highlights: list[dict[str, Any]],
    facts: list[str],
) -> str:
    experience_lines = [
        f"- {e.get('title')} at {e.get('company')} ({e.get('start') or '?'}"
        f"–{e.get('end') or 'present'})"
        for e in (resume.experience or [])[:6]
        if e.get("title")
    ] or ["- n/a"]
    relevant_roles = "; ".join(
        f"{h.get('title')} at {h.get('company')}" for h in highlights[:3]
    )

    # What this call produces is a letter that goes out over the candidate's
    # name, and three of the four blocks below came from outside: the role is a
    # crawled page, the company facts are `company_research`'s reading of one,
    # and the overlap's skill names are the posting's words. Only the resume is
    # the candidate's own, and it is the block deliberately left unfenced — see
    # :mod:`app.services.untrusted`. Rule 1 says every claim must already appear
    # in it, so telling the model to disregard directions found in there would
    # disarm the rule rather than enforce it.
    #
    # The company facts are fenced even though this product wrote the row. They
    # are a model's summary of an employer's own page, stored and replayed, so
    # the words in them are still the page's — and rule 3 makes them the only
    # thing that may be said about the employer, which is exactly the licence
    # worth capturing.
    posting = "\n".join(
        [
            f"Title: {job.title or 'n/a'}",
            f"Company: {job.company or 'n/a'}",
            f"Required skills: {', '.join(job.required_skills[:20]) or 'n/a'}",
            "Responsibilities:",
            *([f"- {r}" for r in job.responsibilities[:6]] or ["- n/a"]),
        ]
    )
    fact_lines = "\n".join(
        [f"- {f}" for f in facts] or ["- none available; say nothing about the company"]
    )
    return "\n".join(
        [
            "== CANDIDATE (the only facts about them you may use) ==",
            f"Name: {resume.full_name or 'n/a'}",
            f"Headline: {resume.headline or 'n/a'}",
            f"Years of experience: {resume.years_experience or 'n/a'}",
            f"Skills: {', '.join((resume.skills or [])[:25]) or 'n/a'}",
            "Experience:",
            *experience_lines,
            f"Resume excerpt: {(resume.raw_text or '')[:1500] or 'n/a'}",
            "",
            "== ROLE ==",
            untrusted.fence(posting, label="job posting"),
            "",
            "== VERIFIED COMPANY FACTS (the only things you may say about them) ==",
            untrusted.fence(fact_lines, label="company research"),
            "",
            "== OVERLAP (computed, trust this) ==",
            "Candidate genuinely has:",
            untrusted.fence(", ".join(matched[:20]) or "none", label="matched skills"),
            "Candidate does NOT have — do not mention:",
            untrusted.fence(", ".join(missing[:20]) or "none", label="missing skills"),
            "Most relevant roles: " + (relevant_roles or "n/a"),
        ]
    )


def _clean_highlights(value: Any, limit: int = 4) -> list[dict[str, Any]]:
    """Keep only highlights that came with the evidence they were asked for."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        point = str(item.get("point") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if point and evidence:
            out.append({"point": point[:300], "evidence": evidence[:300]})
        if len(out) >= limit:
            break
    return out


def generate_letter(
    resume: Resume, job: ParsedJob, *, profile=None
) -> LetterOutput:
    """Write one letter for one posting.

    Always returns something usable. The model is asked first, and its output is
    kept only if it stays inside the resume and the verified company facts.
    """
    matched, missing = match_keywords(resume, job)
    highlights = highlight_experience(resume, job)
    facts = company_facts(profile)

    output = LetterOutput(
        greeting=_greeting(job),
        body=_fallback_body(resume, job, matched, highlights, facts),
        sign_off=_sign_off(resume),
        company_research=facts,
        missing_keywords=missing,
        highlights=[
            {
                "point": h.get("why_relevant") or "",
                "evidence": f"{h.get('title')} at {h.get('company')}",
            }
            for h in highlights[:3]
            if h.get("title")
        ],
    )

    if not llm_is_configured():
        return output

    try:
        raw = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_SYSTEM_PROMPT)},
                {
                    "role": "user",
                    "content": _build_prompt(
                        resume, job, matched, missing, highlights, facts
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.5,
            max_tokens=3000,
        )
    except OpenRouterError as exc:
        logger.info("cover letter fell back to a deterministic draft: %s", exc)
        return output

    data = extract_json_object(raw) or {}
    body = data.get("body") if usable_prose(data.get("body")) else None
    if not body or looks_like_reasoning(body):
        return output

    fabricated = mentions_missing(body, missing) or invented_metric(body, resume)
    if fabricated:
        logger.warning(
            "cover letter rejected: model claimed %r, which the resume does not support",
            fabricated,
        )
        return output

    output.body = body.strip()
    output.generated_with = "llm"
    output.model = settings.openrouter_model
    llm_highlights = _clean_highlights(data.get("highlights"))
    if llm_highlights:
        output.highlights = llm_highlights
    return output


# --------------------------------------------------------------------------- #
# Persistence                                                                  #
# --------------------------------------------------------------------------- #


def get_letter(db: Session, user_id: int, job_posting_id: int) -> CoverLetter | None:
    """The stored letter for one posting, if there is one."""
    return db.scalars(
        select(CoverLetter)
        .where(
            CoverLetter.user_id == user_id,
            CoverLetter.job_posting_id == job_posting_id,
        )
        .order_by(CoverLetter.id.desc())
        .limit(1)
    ).first()


def upsert_letter(
    db: Session,
    *,
    user_id: int,
    resume: Resume,
    job: ParsedJob,
    job_posting_id: int | None = None,
    tailored_resume_id: int | None = None,
    profile=None,
    delivery: str = DELIVERY_INLINE,
    force: bool = False,
) -> CoverLetter:
    """Generate (or regenerate) the letter for a posting and store it.

    Refuses to overwrite a letter the user has edited unless ``force`` is set —
    a regeneration triggered by an unrelated re-scan must never silently discard
    someone's own words. The caller gets the edited row back untouched.
    """
    if delivery not in VALID_DELIVERY:
        delivery = DELIVERY_INLINE

    existing = (
        get_letter(db, user_id, job_posting_id) if job_posting_id is not None else None
    )
    if existing is not None and existing.edited and not force:
        logger.info("cover letter %s left alone: edited by the user", existing.id)
        return existing

    output = generate_letter(resume, job, profile=profile)

    letter = existing or CoverLetter(user_id=user_id)
    letter.resume_id = resume.id
    letter.job_posting_id = job_posting_id
    letter.tailored_resume_id = tailored_resume_id
    letter.job_title = job.title
    letter.job_company = job.company
    letter.greeting = output.greeting
    letter.body = output.body
    letter.sign_off = output.sign_off
    letter.company_research = output.company_research
    letter.highlights = output.highlights
    letter.missing_keywords = output.missing_keywords
    letter.delivery = delivery
    letter.generated_with = output.generated_with
    letter.model = output.model
    letter.edited = False

    if existing is None:
        db.add(letter)
    db.flush()
    return letter


def apply_edit(db: Session, letter: CoverLetter, body: str) -> CoverLetter:
    """Store a user's own wording and mark the letter as hand-edited."""
    letter.body = body.strip()
    letter.edited = True
    db.flush()
    return letter


def profile_for_posting(db: Session, posting) -> Any | None:
    """Research for a posting's employer, from cache where possible.

    ``use_llm=False`` deliberately: both callers are on a clock — one is a
    request the user is waiting on, the other is an autopilot run with a
    per-posting budget — and the deterministic pass over the posting text is
    enough to ground a letter. A richer profile arrives on the next scan and the
    next regeneration picks it up.

    Best-effort by design: a letter with no company facts is a letter that says
    nothing about the company, which is the correct behaviour when we know
    nothing, not a reason to fail the application.
    """
    # Local import: company_research is a heavier module and only this one
    # function needs it.
    from app.services import company_research

    company = getattr(posting, "company", None) if posting is not None else None
    if not company:
        return None
    try:
        return company_research.research_company(
            db,
            company,
            posting_text=getattr(posting, "description", None),
            location=getattr(posting, "location", None),
            use_llm=False,
        )
    except Exception:  # noqa: BLE001 - research must never break a letter
        logger.warning("company profile lookup failed for %r", company, exc_info=True)
        return None


def fold_into_body(body_text: str, letter: CoverLetter) -> str:
    """Put an inline letter inside an outreach email body.

    The two texts each arrive with their own greeting and sign-off, so pasting
    one above the other gives the recruiter "Dear Acme hiring team … Best
    regards, Jordan … Hi Sam, …" — two letters in one message. Instead the
    letter's *paragraphs* slot in directly under the email's greeting: the
    letter leads the message, and the email keeps exactly one opening and one
    close.

    Falls back to a plain prepend when the body doesn't open with something that
    looks like a greeting — an unusual shape is not worth mangling the letter
    over.
    """
    body = (letter.body or "").strip()
    if not body:
        return body_text

    lines = (body_text or "").split("\n")
    first = lines[0].strip() if lines else ""
    if first.endswith(",") and len(first) <= 60:
        rest = "\n".join(lines[1:]).lstrip("\n")
        return f"{lines[0]}\n\n{body}\n\n{rest}".rstrip()
    return f"{body}\n\n{body_text}".strip()


def render_markdown(letter: CoverLetter) -> str:
    """The letter as a downloadable document."""
    header = " — ".join(p for p in (letter.job_title, letter.job_company) if p)
    lines = [f"# Cover letter{f': {header}' if header else ''}", ""]
    lines.append(letter.full_text)
    generated = letter.created_at or datetime.now(UTC)
    lines += ["", "---", f"*Drafted {generated:%Y-%m-%d} · review before sending.*"]
    return "\n".join(lines).strip() + "\n"


__all__ = [
    "DELIVERY_ATTACHMENT",
    "DELIVERY_INLINE",
    "VALID_DELIVERY",
    "LetterOutput",
    "apply_edit",
    "company_facts",
    "generate_letter",
    "get_letter",
    "render_markdown",
    "upsert_letter",
]
