"""Interview prep — what to read on the train to the interview.

A recruiter writing back with "when are you free?" is where the automation
stops being useful and the candidate has to perform. This builds the briefing
for that moment out of what the product already knows: the posting the outreach
targeted, the resume it was scored against, and the conversation so far.

Three things come out of it:

* **Company research** — a short factual summary, plus bullets lifted straight
  from the posting (location, comp, stack). Never invented: with no posting
  attached the summary says so rather than making up a company profile.
* **Questions to expect** — the role's own requirements turned back into
  questions, so preparation is against *this* posting rather than a generic
  list.
* **Talking points, each with its evidence** — the skills the resume genuinely
  covers, paired with the line on the resume that backs them. The counterpart is
  ``gaps``: requirements the resume doesn't cover, which the candidate is far
  better off rehearsing than discovering in the room.

The model only ever *phrases* this. The skills split (matched vs missing) comes
from the deterministic fit scorer, so prep can never claim a strength the
resume doesn't support — the same invariant tailoring runs under, enforced the
same way: every model-written talking point is run through
:func:`~app.services.resume_tailor.mentions_missing` and
:func:`~app.services.resume_tailor.invented_metric` before it ships. With no
provider configured the whole briefing still renders from templates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.models.resume import Resume
from app.services import untrusted
from app.services.fit_scorer import normalize_text, score_fit, skill_mentioned
from app.services.jd_parser import ParsedJob, parse_job
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
    looks_like_reasoning,
)
from app.services.resume_tailor import invented_metric, mentions_missing

logger = logging.getLogger(__name__)

# Enough of a posting to be worth parsing; below this it is a stub row.
_MIN_DESCRIPTION_CHARS = 80

# Asked at every interview, whatever the role. The role-specific questions are
# generated from the posting and lead; these backfill so a thin posting still
# produces a usable list.
_UNIVERSAL_QUESTIONS: list[tuple[str, str]] = [
    (
        "Walk me through your background.",
        "The opener at almost every interview — have a two-minute version ready.",
    ),
    (
        "Why this role, and why now?",
        "They are checking the move is deliberate rather than a mass application.",
    ),
    (
        "Tell me about something you shipped end to end.",
        "Pick one project you can go three questions deep on.",
    ),
    (
        "Describe a time a project went badly. What did you do?",
        "They want the recovery and what you changed afterwards, not a hero story.",
    ),
]

_QUESTIONS_TO_ASK: list[str] = [
    "What does the first 90 days look like for whoever takes this role?",
    "How does the team decide what to work on next?",
    "What's the thing about this team you'd change if you could?",
    "How is success measured for this role after a year?",
]


@dataclass
class PrepContext:
    """Everything the briefing is built from, resolved once by the router.

    ``application_id`` is null when the briefing is for a posting the user has
    not applied to — a job pasted in, or one still sitting in the feed. Nothing
    below reads it; it is carried so the response can point back at the thread
    when there is one.
    """

    application_id: int | None = None
    company: str | None = None
    role: str | None = None
    location: str | None = None
    salary_text: str | None = None
    remote: bool | None = None
    seniority: str | None = None
    industry: str | None = None
    description: str | None = None
    # The recruiter's own words so far — the only account of what they want.
    conversation: list[str] = field(default_factory=list)


@dataclass
class Prep:
    """The finished briefing."""

    company_research: str
    company_facts: list[str] = field(default_factory=list)
    questions: list[tuple[str, str | None]] = field(default_factory=list)
    talking_points: list[tuple[str, str | None]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    questions_to_ask: list[str] = field(default_factory=list)
    generated_with: str = "template"


# --------------------------------------------------------------------------- #
# Deterministic material                                                       #
# --------------------------------------------------------------------------- #


def _parsed_job(context: PrepContext) -> ParsedJob:
    """The posting as structure. Heuristics only — no LLM on this path.

    Prep runs while the user is waiting on a page, and the parse only feeds the
    skills split and the fact bullets; an LLM pass here would double the latency
    to sharpen fields the briefing barely uses.
    """
    text = (context.description or "").strip()
    if len(text) < _MIN_DESCRIPTION_CHARS:
        job = ParsedJob(title=context.role, company=context.company)
    else:
        job = parse_job(text, use_llm=False)
        job.title = job.title or context.role
        job.company = job.company or context.company
    job.location = job.location or context.location
    job.salary_text = job.salary_text or context.salary_text
    job.industry = job.industry or context.industry
    if job.remote is None:
        job.remote = context.remote
    job.seniority = job.seniority or context.seniority
    return job


def _facts(job: ParsedJob) -> list[str]:
    """Factual bullets, every one of them lifted from data we hold."""
    out: list[str] = []
    if job.company:
        out.append(f"Company: {job.company}")
    if job.title:
        out.append(f"Role: {job.title}")
    if job.location or job.remote is not None:
        where = job.location or "Location not stated"
        if job.remote:
            where = f"{where} · remote"
        out.append(where)
    if job.seniority:
        out.append(f"Seniority: {job.seniority}")
    if job.years_required:
        out.append(f"Asks for {job.years_required}+ years")
    if job.salary_text:
        out.append(f"Compensation: {job.salary_text}")
    if job.industry:
        out.append(f"Industry: {job.industry}")
    if job.required_skills:
        out.append("Stack named in the posting: " + ", ".join(job.required_skills[:8]))
    return out


def _evidence_for(resume: Resume, skill: str) -> str | None:
    """The line on the resume that backs a talking point, if there is one.

    Matched with :func:`~app.services.fit_scorer.skill_mentioned`, the same
    matcher that produced the skill split this citation is attached to, for the
    two reasons that matcher exists.

    Bare containment cited roles that never used the skill. "Go" is inside
    "goals" and inside "Chicago", so a retail resume was told to "lead with your
    Go work" and pointed at *Regional Manager at Chicago Retail* as the proof;
    "R" and "C" cited the first role on any resume at all. This is read minutes
    before the candidate says it out loud to the person who can check.

    And it is spelling-blind, so it lost real evidence too: a posting asking for
    "PostgreSQL" against a resume that writes "Postgres" found nothing and fell
    through to the years-of-experience line — a citation that backs no
    particular skill.

    The skills list is consulted before that fallback. The fit scorer has
    already found the skill *somewhere*, and "listed under skills" is a smaller
    claim than a role, but it is about this skill, which the years line is not.
    """
    for entry in resume.experience or []:
        haystack = normalize_text(
            " ".join(
                str(entry.get(key) or "") for key in ("title", "company", "summary")
            )
        )
        if skill_mentioned(haystack, skill):
            title = entry.get("title") or "Role"
            company = entry.get("company")
            return f"{title} at {company}" if company else str(title)
    if resume.headline and skill_mentioned(normalize_text(resume.headline), skill):
        return resume.headline
    for own in resume.skills or []:
        if own and skill_mentioned(normalize_text(own), skill):
            return f"Listed on your resume under skills: {own}"
    if resume.years_experience:
        return f"{resume.years_experience} years' experience on the resume"
    return None


def _template_questions(job: ParsedJob) -> list[tuple[str, str | None]]:
    """Turn the posting's own requirements back into questions."""
    questions: list[tuple[str, str | None]] = []
    for skill in job.required_skills[:4]:
        questions.append(
            (
                f"How have you used {skill} in production?",
                f"Named as a requirement in the {job.title or 'posting'}.",
            )
        )
    for duty in job.responsibilities[:2]:
        trimmed = duty.strip().rstrip(".")
        if trimmed:
            questions.append(
                (
                    f"Tell me about a time you had to {trimmed[0].lower()}{trimmed[1:]}.",
                    "Listed under responsibilities — expect a behavioural version of it.",
                )
            )
    questions.extend(_UNIVERSAL_QUESTIONS)
    return questions[:8]


def _template_research(context: PrepContext, job: ParsedJob) -> str:
    """A summary that only ever restates what the posting said.

    With no posting attached this deliberately says so. A confident paragraph
    about a company we know nothing about is the one output that could actively
    mislead someone walking into an interview.
    """
    if not job.company and not job.title:
        return (
            "There's no job posting attached to this application, so there's "
            "nothing to research from yet. Open the company's careers page and "
            "the recruiter's original email before the call."
        )

    company = job.company or "The company"
    parts = [f"{company} is hiring for {job.title or 'this role'}"]
    if job.location:
        parts.append(f"based in {job.location}{' (remote)' if job.remote else ''}")
    elif job.remote:
        parts.append("as a remote role")
    summary = " ".join(parts) + "."

    if job.industry:
        summary += f" The posting places them in {job.industry}."
    if job.required_skills:
        summary += (
            " The requirements lean on "
            + ", ".join(job.required_skills[:4])
            + " — expect the conversation to go there."
        )
    if context.conversation:
        summary += (
            " Re-read what the recruiter has already told you: it is the only "
            "first-hand account of what they're looking for."
        )
    return summary


# --------------------------------------------------------------------------- #
# LLM phrasing                                                                 #
# --------------------------------------------------------------------------- #

_PREP_PROMPT = (
    "You brief a job candidate before an interview. You are given a job posting, "
    "the candidate's resume facts, and a pre-computed split of which required "
    "skills the resume covers and which it does not.\n\n"
    "Hard rules:\n"
    "- Never claim the candidate has a skill that is not in the matched list.\n"
    "- Never invent facts about the company. If the posting doesn't say it, "
    "don't say it.\n"
    "- Talking points must cite evidence from the resume facts given to you.\n"
    "- Be concrete and short. No greetings, no encouragement, no filler.\n\n"
    "Return ONLY a JSON object with these keys:\n"
    '{"company_research": str (2-4 sentences), '
    '"questions": [{"question": str, "why": str}] (5-8 items), '
    '"talking_points": [{"point": str, "evidence": str}] (3-5 items), '
    '"questions_to_ask": [str] (3-4 items)}'
)


def _llm_prep(
    context: PrepContext,
    job: ParsedJob,
    matched: list[str],
    missing: list[str],
    resume: Resume,
) -> dict | None:
    """Ask a model to phrase the briefing. Returns None on any failure."""
    experience = "; ".join(
        " ".join(
            filter(None, (str(e.get("title") or ""), str(e.get("company") or "")))
        ).strip()
        for e in (resume.experience or [])[:5]
    )
    # Three sources, and only one of them is ours. The posting was written by
    # the employer and reached here through a crawler; the conversation is a
    # stranger's mail verbatim; the resume facts between them are this
    # product's own records, which is why they are the half left outside the
    # fence — see :mod:`app.services.untrusted`.
    #
    # The two skill lists sit on the boundary and are fenced by their values
    # rather than by their labels. The *split* is ours — it comes from the
    # deterministic scorer and is the invariant this whole module runs under —
    # but the skill names inside it are the posting's own words, up to 120
    # characters each off the LLM parsing path, which is room for a sentence.
    # So the labels stay outside, where they read as the rules they are, and
    # the words they name go inside.
    posting = (
        f"Role: {job.title or 'n/a'} at {job.company or 'n/a'}\n"
        f"Location: {job.location or 'n/a'}{' (remote)' if job.remote else ''}\n"
        f"Seniority: {job.seniority or 'n/a'} | Years asked for: {job.years_required or 'n/a'}\n"
        f"Compensation: {job.salary_text or 'not stated'}\n"
        f"Required skills: {', '.join(job.required_skills[:12]) or 'not stated'}\n"
        f"Responsibilities: {' | '.join(job.responsibilities[:6]) or 'not stated'}"
    )
    user_content = (
        "THE POSTING\n"
        + untrusted.fence(posting, label="job posting")
        + "\n\nTHE CANDIDATE\n"
        f"Candidate headline: {resume.headline or 'n/a'}\n"
        f"Years of experience: {resume.years_experience or 'n/a'}\n"
        f"Past roles: {experience or 'n/a'}\n"
        "MATCHED skills (safe to claim):\n"
        + untrusted.fence(", ".join(matched[:12]) or "none", label="matched skills")
        + "\nMISSING skills (do NOT claim):\n"
        + untrusted.fence(", ".join(missing[:12]) or "none", label="missing skills")
    )
    if context.conversation:
        recent = " || ".join(msg[:300] for msg in context.conversation[-3:])
        user_content += (
            "\n\nWHAT THE RECRUITER HAS WRITTEN SO FAR\n"
            + untrusted.fence(recent, label="recruiter email")
        )

    try:
        raw = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_PREP_PROMPT)},
                {"role": "user", "content": user_content},
            ],
            temperature=0.4,
            max_tokens=1600,
        )
    except OpenRouterError as exc:
        logger.info("interview prep fell back to template: %s", exc)
        return None

    data = extract_json_object(raw)
    if not data:
        return None
    research = data.get("company_research")
    if not isinstance(research, str) or not research.strip():
        return None
    if looks_like_reasoning(research):
        return None
    return data


def _pairs(value, first: str, second: str, limit: int) -> list[tuple[str, str | None]]:
    """Coerce a model's list-of-objects into (text, note) pairs, dropping junk."""
    out: list[tuple[str, str | None]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), None))
        elif isinstance(item, dict):
            text = item.get(first)
            if isinstance(text, str) and text.strip():
                note = item.get(second)
                out.append(
                    (text.strip(), note.strip() if isinstance(note, str) and note.strip() else None)
                )
        if len(out) >= limit:
            break
    return out


def _grounded_points(
    points: list[tuple[str, str | None]], missing: list[str], resume: Resume
) -> list[tuple[str, str | None]]:
    """Drop model-written talking points the resume doesn't actually support.

    A talking point is the one section of the briefing phrased as a claim about
    the candidate, and it is read minutes before they say it out loud to the
    person who can check. "Lead with your Kafka work" against a resume with no
    Kafka on it is not a weak suggestion, it is a rehearsal for getting caught —
    and the same briefing was listing Kafka under ``gaps`` two fields later.

    So the guards tailoring and cover letters already run are applied here too,
    over the point *and* its evidence: the point may not name a requirement the
    fit scorer put on the missing list, and it may not cite a number that
    appears nowhere in the candidate's own document.

    Deliberately narrower than the whole briefing. ``company_research`` and
    ``questions`` are *supposed* to name the skills the resume lacks — "How have
    you used Kafka in production?" is the single most useful question prep can
    produce for this candidate — and ``gaps`` is nothing but that list. Only a
    claim of strength has to be grounded, so only claims of strength are checked;
    filtering per point rather than rejecting the section keeps one bad line from
    costing the good ones.
    """
    kept: list[tuple[str, str | None]] = []
    for point, evidence in points:
        text = f"{point}\n{evidence or ''}"
        fabricated = mentions_missing(text, missing) or invented_metric(text, resume)
        if fabricated:
            logger.warning(
                "interview prep dropped a talking point claiming %r, "
                "which the resume does not support",
                fabricated,
            )
            continue
        kept.append((point, evidence))
    return kept


def _strings(value, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()][:limit]


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #


def build_prep(context: PrepContext, resume: Resume | None) -> Prep:
    """Build the briefing, using a model to phrase it when one is reachable."""
    job = _parsed_job(context)

    matched: list[str] = []
    missing: list[str] = []
    if resume is not None:
        # explain=False keeps this off the LLM path — we only want the split,
        # and the prose is written once, below, rather than twice.
        result = score_fit(resume, job, explain=False)
        matched, missing = result.matched_skills, result.missing_skills

    talking_points: list[tuple[str, str | None]] = []
    if resume is not None:
        for skill in matched[:5]:
            talking_points.append(
                (
                    f"Lead with your {skill} work — the posting asks for it directly.",
                    _evidence_for(resume, skill),
                )
            )
        if not talking_points and resume.headline:
            talking_points.append(
                (f"Open with your {resume.headline} background.", resume.headline)
            )

    gaps = [
        f"{skill} — the posting asks for it and your resume doesn't show it. "
        "Have an honest answer ready for how you'd pick it up."
        for skill in missing[:5]
    ]

    prep = Prep(
        company_research=_template_research(context, job),
        company_facts=_facts(job),
        questions=_template_questions(job),
        talking_points=talking_points,
        gaps=gaps,
        questions_to_ask=list(_QUESTIONS_TO_ASK[:4]),
        generated_with="template",
    )

    if resume is None:
        return prep

    data = _llm_prep(context, job, matched, missing, resume)
    if not data:
        return prep

    questions = _pairs(data.get("questions"), "question", "why", 8)
    points = _grounded_points(
        _pairs(data.get("talking_points"), "point", "evidence", 5), missing, resume
    )
    asks = _strings(data.get("questions_to_ask"), 4)

    prep.company_research = data["company_research"].strip()
    # Each section falls back independently: a model that returned good prose
    # but a malformed question list shouldn't cost the user the whole briefing.
    prep.questions = questions or prep.questions
    prep.talking_points = points or prep.talking_points
    prep.questions_to_ask = asks or prep.questions_to_ask
    prep.generated_with = "llm"
    return prep


__all__ = ["Prep", "PrepContext", "build_prep"]
