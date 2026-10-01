"""Resume tailoring — align an existing resume with one job description.

The rule this module is built around, from research §2.1: **the LLM edits, it
never rewrites.** Tailoring reorders, re-emphasises and re-words what the
candidate already claims; it does not add a skill, a company, a metric or a year
that isn't in their resume. A tool that invents "Kubernetes" to match a JD gets
the candidate through the ATS and destroyed in the interview.

Three things come out of a run:

* a **tailored summary** — the candidate's own profile, angled at this role;
* **reordered skills** — their real skill list, with the JD's requirements first;
* a **cover letter** — grounded in their actual experience.

Plus the honest part: which JD keywords they match, and which they don't. The
missing list is shown to the user, never quietly filled in.

Everything degrades to a deterministic path when no OpenRouter key is configured,
so the feature works (and is testable) offline.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.models.resume import Resume
from app.services import untrusted
from app.services.fit_scorer import normalize_text, skill_mentioned
from app.services.jd_parser import ParsedJob
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
    llm_is_configured,
    looks_like_reasoning,
)

logger = logging.getLogger(__name__)


@dataclass
class TailoredOutput:
    """The result of one tailoring run, ready to persist."""

    tailored_summary: str
    ordered_skills: list[str] = field(default_factory=list)
    highlighted_experience: list[dict[str, Any]] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    cover_letter: str = ""
    generated_with: str = "heuristic"
    model: str | None = None
    # How the *bullets* were produced, tracked apart from ``generated_with``:
    # the summary can come off the model while a bullet rewrite is rejected for
    # inventing something, and the UI should be able to say which happened.
    bullets_generated_with: str = "heuristic"


# The haystack form `skill_mentioned` documents, taken from the module that
# owns the matcher instead of kept as a second copy here.
#
# It *was* a second copy — the same `re.sub` character class, written out
# twice — and the two drifted the moment `fit_scorer._normalize` learned to
# fold accents. The needle came out "securite" and the haystack this module
# built came out "s curit", so every one of the four comparisons below stopped
# agreeing with the matcher consuming them: an accented skill on a French
# résumé read as absent from the candidate's own vocabulary, and
# `mentions_missing` — the guard that catches a cover letter claiming
# experience the candidate was found not to have — stopped recognising the
# claim it was looking for.
#
# Sharing the function is the point. This module already shares
# `skill_mentioned` for exactly this reason, and a haystack normalised by a
# private copy of its rules is the half of that contract that was still
# guesswork.
_normalize = normalize_text


def _candidate_vocabulary(resume: Resume) -> str:
    """Everything the candidate has actually claimed, as one lowercase haystack.

    Keyword matching runs against this rather than the skills list alone: a
    posting asking for "Terraform" should match a resume that mentions it in a
    job bullet even if the skills section never lists it.
    """
    parts = [
        resume.raw_text or "",
        resume.summary or "",
        resume.headline or "",
        " ".join(resume.skills or []),
        " ".join(
            f"{e.get('title', '')} {e.get('company', '')}"
            for e in (resume.experience or [])
        ),
    ]
    return _normalize(" ".join(parts))


def match_keywords(resume: Resume, job: ParsedJob) -> tuple[list[str], list[str]]:
    """Split the JD's requirements into what the candidate has and what they lack.

    A keyword counts as matched only if it appears in the candidate's own resume
    text. This is the guardrail that keeps the tailoring honest: the missing list
    is what the user is shown, and it is never handed to the LLM as something to
    write about.

    Matched with :func:`fit_scorer.skill_mentioned` rather than a matcher of its
    own, because "the same posting against the same resume" is the fit score's
    question too, and the two are read side by side. A private word-boundary
    test here knew no alias groups, so a posting asking for "Postgres" against a
    resume that says "PostgreSQL" scored as a match on the card and appeared as
    a gap on the tailor screen — which then told the user to go learn it, and
    forbade the cover letter from mentioning the thing they had actually done.
    """
    haystack = _candidate_vocabulary(resume)
    wanted: list[str] = []
    for skill in [*job.required_skills, *job.preferred_skills]:
        key = skill.lower().strip()
        if key and key not in wanted:
            wanted.append(key)

    matched: list[str] = []
    missing: list[str] = []
    for skill in wanted:
        if not _normalize(skill):
            continue
        (matched if skill_mentioned(haystack, skill) else missing).append(skill)
    return matched, missing


def order_skills(resume: Resume, job: ParsedJob) -> list[str]:
    """The candidate's own skills, sorted so the JD's requirements lead.

    Three tiers, each preserving the candidate's original order within it:
    required by the JD, preferred by the JD, then everything else. No skill is
    ever added — this is a permutation of what the resume already says.

    Tiered with :func:`fit_scorer.skill_mentioned`, the same matcher
    :func:`match_keywords` uses, rather than a set of lower-cased strings. The
    set was the last surviving copy of the simple matcher this module spent a
    round removing, and it failed in every way that one did — a resume saying
    "PostgreSQL" against a posting asking for "Postgres" was sorted into the
    *third* tier, which is the opposite of what this function exists to do:
    the requirement the candidate actually meets got pushed below every
    unrelated skill they listed, on the very document written to lead with it.
    "K8s"/"Kubernetes", ".NET"/"dotnet", "JS"/"JavaScript" and the plural forms
    ("Microservices" against "microservice") all did the same, and an accented
    skill did it in every posting: a bare ``.lower()`` does not fold accents, so
    a French résumé's "Sécurité" never matched a posting spelling it
    "Securite". `match_keywords` was already calling that skill matched, so the
    tailor screen told the candidate they had it while the tailored resume
    buried it.

    The haystack is one skill rather than a document, which is the only
    difference from `match_keywords`' call: needle from the posting, haystack
    from the candidate, so the two cannot drift again.
    """
    own = [s for s in (resume.skills or []) if s]
    required = [s for s in job.required_skills if s]
    preferred = [s for s in job.preferred_skills if s]

    def names_any(skill: str, wanted: list[str]) -> bool:
        haystack = _normalize(skill)
        return bool(haystack) and any(skill_mentioned(haystack, w) for w in wanted)

    tier_1: list[str] = []
    tier_2: list[str] = []
    tier_3: list[str] = []
    for skill in own:
        if names_any(skill, required):
            tier_1.append(skill)
        elif names_any(skill, preferred):
            tier_2.append(skill)
        else:
            tier_3.append(skill)
    return [*tier_1, *tier_2, *tier_3]


def highlight_experience(resume: Resume, job: ParsedJob) -> list[dict[str, Any]]:
    """Rank the candidate's roles by relevance to this posting.

    Relevance is title-overlap with the JD title plus skill mentions in the role's
    own text. Most recent order is used to break ties, so an equally relevant
    older role never leapfrogs a current one.
    """
    entries = list(resume.experience or [])
    if not entries:
        return []

    title_terms = {t for t in _normalize(job.title or "").split() if len(t) > 2}
    wanted = {s.lower() for s in [*job.required_skills, *job.preferred_skills]}

    ranked: list[tuple[float, int, dict[str, Any], list[str]]] = []
    for index, entry in enumerate(entries):
        blob = _normalize(" ".join(str(v) for v in entry.values() if v))
        # Title terms stay a containment test on purpose: "engineer" should
        # find "engineering", and a title word is a description of the work
        # rather than a claim about a named tool.
        title_hits = sum(1 for term in title_terms if term in blob)
        # Skills do not get that latitude. A skill printed here is a claim that
        # this role used it, so it goes through the same matcher the rest of the
        # module and the fit score use: named as a word, under any spelling.
        hits = sorted(skill for skill in wanted if skill_mentioned(blob, skill))
        # Recency is a tiebreaker, not a driver: index 0 is the current role.
        score = title_hits * 2.0 + len(hits) + max(0.0, 1.0 - index * 0.1)
        ranked.append((score, index, entry, hits))

    ranked.sort(key=lambda row: (-row[0], row[1]))

    out: list[dict[str, Any]] = []
    for score, _, entry, matched in ranked[:5]:
        out.append(
            {
                "company": entry.get("company"),
                "title": entry.get("title"),
                "start": entry.get("start"),
                "end": entry.get("end"),
                "relevance": round(score, 2),
                "matched_skills": matched[:8],
                "why_relevant": _why_relevant(entry, job, matched),
            }
        )
    return out


def _why_relevant(entry: dict[str, Any], job: ParsedJob, matched: list[str]) -> str:
    role = entry.get("title") or "This role"
    target = job.title or "the role"
    if matched:
        return f"{role} covers {', '.join(matched[:4])} — core to {target}."
    return f"{role} is the closest prior context for {target}."


# --------------------------------------------------------------------------- #
# Deterministic fallback                                                       #
# --------------------------------------------------------------------------- #


def _fallback_summary(resume: Resume, job: ParsedJob, matched: list[str]) -> str:
    """A tailored summary built only from fields the resume already holds."""
    who = resume.headline or (resume.target_roles or ["Experienced professional"])[0]
    years = (
        f"{resume.years_experience} years of experience"
        if resume.years_experience
        else "hands-on experience"
    )
    target = job.title or "this role"
    company = f" at {job.company}" if job.company else ""
    strengths = ", ".join(matched[:5]) or ", ".join((resume.skills or [])[:5])
    tail = f" Direct overlap with the role's requirements: {strengths}." if strengths else ""
    return (
        f"{who} with {years}, applying for {target}{company}.{tail}"
    ).strip()


def _fallback_cover_letter(
    resume: Resume, job: ParsedJob, matched: list[str], highlights: list[dict[str, Any]]
) -> str:
    name = resume.full_name or "the candidate"
    target = job.title or "the role"
    greeting = f"Dear {job.company} team," if job.company else "Hello,"
    strengths = ", ".join(matched[:4]) or ", ".join((resume.skills or [])[:4]) or "my background"
    # highlights[0] is the *most relevant* role, not necessarily the current one,
    # so the sentence must not claim recency.
    lead = highlights[0] if highlights else {}
    proof = (
        f"In my time as {lead.get('title')} at {lead.get('company')}, I worked "
        f"directly with {', '.join(lead.get('matched_skills') or []) or strengths}. "
        if lead.get("title") and lead.get("company")
        else ""
    )
    return (
        f"{greeting}\n\n"
        f"I'm writing about the {target} position"
        f"{f' at {job.company}' if job.company else ''}. "
        f"My background is in {strengths}, which lines up closely with what the "
        f"role calls for.\n\n"
        f"{proof}"
        f"I'd welcome the chance to talk through how that experience maps onto "
        f"what your team is building.\n\n"
        f"Best regards,\n{name}"
    )


# --------------------------------------------------------------------------- #
# LLM tailoring                                                                #
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = (
    "You tailor an existing resume to one job description. You EDIT, you never "
    "REWRITE, and you never INVENT.\n\n"
    "Absolute rules:\n"
    "1. Every skill, employer, title, metric and year you mention must already "
    "appear in the candidate's resume. If it is not there, you may not use it.\n"
    "2. You are given a list of requirements the candidate does NOT have. Never "
    "claim, imply, or write around them. Do not mention them at all.\n"
    "3. Preserve the candidate's voice — adjust emphasis and ordering, not "
    "identity. No buzzword padding, no superlatives they didn't earn.\n"
    "4. Keep the summary to 2-3 sentences. Keep the cover letter under 200 words, "
    "in a confident peer-to-peer tone with one clear closing ask.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"tailored_summary": str, "cover_letter": str, '
    '"experience_notes": [{"company": str, "why_relevant": str}]}'
)


def _build_prompt(
    resume: Resume,
    job: ParsedJob,
    matched: list[str],
    missing: list[str],
    highlights: list[dict[str, Any]],
) -> str:
    experience_lines = [
        f"- {e.get('title')} at {e.get('company')} ({e.get('start') or '?'}"
        f"–{e.get('end') or 'present'})"
        for e in (resume.experience or [])[:6]
        if e.get("title")
    ]
    responsibility_lines = [f"- {r}" for r in job.responsibilities[:8]] or ["- n/a"]
    highlight_line = (
        "; ".join(f"{h.get('title')} at {h.get('company')}" for h in highlights[:3])
        or "n/a"
    )
    # The posting is a stranger's page and the resume is the candidate's own
    # document, and only one of them is fenced — see
    # :mod:`app.services.untrusted` and the asymmetry `reply_agent` draws.
    # Fencing the resume would be telling the model to disregard the one
    # document rule 1 says it may work from.
    #
    # The overlap lists straddle the two. The *split* is ours and is the
    # invariant this module runs under; the skill names inside it are the
    # posting's own words, up to 120 characters each off the LLM parsing path,
    # and every one the resume does not cover lands in the list rule 2 forbids.
    # So the labels stay outside, where they read as the rules they are, and
    # the words they name go inside.
    posting = "\n".join(
        [
            f"Title: {job.title or 'n/a'}",
            f"Company: {job.company or 'n/a'}",
            f"Location: {job.location or 'n/a'} (remote: {job.remote})",
            f"Seniority: {job.seniority or 'n/a'}",
            f"Years required: {job.years_required or 'n/a'}",
            f"Required skills: {', '.join(job.required_skills[:20]) or 'n/a'}",
            f"Preferred skills: {', '.join(job.preferred_skills[:12]) or 'n/a'}",
            "Responsibilities:",
            *responsibility_lines,
        ]
    )
    return "\n".join(
        [
            "== CANDIDATE (the only facts you may use) ==",
            f"Name: {resume.full_name or 'n/a'}",
            f"Headline: {resume.headline or 'n/a'}",
            f"Years of experience: {resume.years_experience or 'n/a'}",
            f"Seniority: {resume.seniority or 'n/a'}",
            f"Location: {resume.location or 'n/a'}",
            f"Skills: {', '.join((resume.skills or [])[:25]) or 'n/a'}",
            "Experience:",
            *(experience_lines or ["- n/a"]),
            f"Existing summary: {resume.summary or 'n/a'}",
            f"Resume excerpt: {(resume.raw_text or '')[:1500] or 'n/a'}",
            "",
            "== TARGET ROLE ==",
            untrusted.fence(posting, label="job posting"),
            "",
            "== OVERLAP (computed, trust this) ==",
            "Candidate genuinely has:",
            untrusted.fence(", ".join(matched[:20]) or "none", label="matched skills"),
            "Candidate does NOT have — do not mention:",
            untrusted.fence(", ".join(missing[:20]) or "none", label="missing skills"),
            f"Most relevant roles: {highlight_line}",
        ]
    )


def mentions_missing(text: str, missing: list[str]) -> str | None:
    """Return the first fabricated requirement the text mentions, if any.

    Alias-aware, like the split that produced *missing*: prose that claims
    "PostgreSQL" experience against a posting that asked for "Postgres" is
    claiming the thing the candidate was found not to have, whichever of the
    two words it reaches for.
    """
    haystack = _normalize(text)
    for skill in missing:
        needle = _normalize(skill)
        if not needle or len(needle) < 3:
            continue
        if skill_mentioned(haystack, needle):
            return skill
    return None


# Achievement-shaped numbers: "30%", "3x", "millions of requests", "handled 50000".
# Small bare integers are ignored — "8 years", "a team of 5" are rarely the lie,
# and flagging them would reject almost every otherwise-good letter.
#
# A grouped number needs its own branch, and it is the *leading* group that
# needs it. ``\d{3,}(?:[,.]\d{3})*`` cannot start on a group of one or two
# digits, so on "50,000" it did not match the number — it matched at the word
# boundary the comma creates and returned **"000"**. "1,200,000" came back as
# "200,000" and "$1,500,000" as "500,000".
#
# Both directions of that are wrong. The claim is compared on its digits, so
# "handled 50,000 requests" was checked against "000" — which any resume
# carrying a single round figure supports — and a letter that did get rejected
# named "000" as the number it could not trace, which is not a number the
# letter contains.
#
# The grouped branch is written first so it wins at a position both could
# start from; the bare branch keeps the three-digit floor that leaves "8
# years" alone.
_METRIC_RE = re.compile(
    r"\d+(?:\.\d+)?\s?%"
    r"|\b\d+(?:\.\d+)?\s?x\b"
    r"|\b\d{1,3}(?:[,.]\d{3})+\b"
    r"|\b\d{3,}\b"
    r"|\b(?:millions?|billions?|thousands?)\b",
    re.I,
)


#: One number as the resume writes it, with its group separators, so a document
#: is read as the numbers it contains rather than as one run of digits.
#:
#: No space in the separator class, deliberately. It would read the European
#: "50 000" as one number, and it would also fuse "2018 2023" into 20182023 —
#: which is the very thing this pattern exists to stop. The rare cost is a
#: space-grouped figure on the CV failing to support a comma-grouped one in the
#: letter, and that already failed before.
#:
#: The first branch ends on a digit so a trailing separator is left behind —
#: "Austin TX 78704." is the number 78704, not "78704." — and the bare ``\d``
#: tail catches a single-digit number, which that branch cannot match.
_NUMBER_RUN = re.compile(r"\d[\d,.]*\d|\d")


def _supporting_numbers(haystack: str) -> set[str]:
    """Every number the resume states, keyed on its digits alone.

    Separators are dropped from both sides of the eventual comparison, so
    "50,000" on the CV supports "50000" in the letter and "1.2" supports
    "1.2x". They are only ever dropped *within* one number, which is the whole
    point: the boundaries between numbers survive.
    """
    return {re.sub(r"\D", "", run) for run in _NUMBER_RUN.findall(haystack)}


def invented_metric(text: str, resume: Resume) -> str | None:
    """Return the first quantitative claim in *text* the resume doesn't support.

    Free models reliably embellish cover letters with impressive-sounding
    numbers — "reduced latency by 30%", "millions of requests per day" — that
    appear nowhere in the candidate's resume. Those get the candidate caught in
    an interview, so any metric we can't trace back to their own document
    disqualifies the generated prose.

    **The claim has to match a number, not a coincidence.** The digits used to
    be stripped from the *whole resume* and the claim looked for as a substring
    of what was left — ``"30" in "1415930221178704201820235"``. That string is
    an ordinary resume: a phone number, a zip code, two employment years and a
    team size, run together with every boundary between them removed. Fifty-odd
    two-digit windows sit in it, so roughly half of every "NN%" a model can
    invent is somewhere inside, and the check passed them.

    On the resume above — nothing in it but contact details, two dates and a
    team size — "reduced latency by 30%", "cut costs by 22%", "grew signups
    41%", "raised conversion 15%" and "improved throughput 3x" were all traced
    back to "the candidate's own document" and shipped in a cover letter. A
    single digit was the plainest case: ``"3" in`` any resume containing any
    digit at all is true, so every ``Nx`` claim under ten was waved through
    unconditionally.

    Comparing against the resume's numbers *as numbers* is the whole fix. It
    cannot cost a claim the resume really does state — the digits are still
    stripped inside each number, so "50,000" on the CV still supports "50000"
    in the letter — and it takes nothing away but the coincidences.

    What stays loose is a claim whose digits equal an unrelated number the
    resume does state: "5x" against "a team of 5". That is a coincidence
    between two numbers rather than between a number and a run of unrelated
    digits, and narrowing it further would mean insisting the resume's own
    figure be metric-shaped — which would reject "30%" in a letter written
    against a resume that said "improved conversion by 30 percent".
    """
    written = " ".join(
        [resume.raw_text or "", resume.summary or "", resume.headline or ""]
    )
    # The word branch below needs the folded text; the numbers need the
    # unfolded. `_normalize` keeps `a-z0-9+#.` and turns everything else into a
    # space, so a thousands comma becomes a gap — "50,000" on the CV arrives
    # here as "50 000", which is two numbers and neither of them is the one the
    # resume states. Reading the figures off the raw document keeps them whole;
    # nothing in a digit needs folding.
    haystack = _normalize(written)
    supported = _supporting_numbers(written)
    for match in _METRIC_RE.finditer(text or ""):
        claim = _normalize(match.group(0))
        # Compare on digits alone so "30 %" in the letter matches "30%" on the CV.
        digits = re.sub(r"\D", "", claim)
        if digits:
            if digits in supported:
                continue
        elif claim in haystack:
            continue
        return match.group(0)
    return None


def usable_prose(value: Any) -> bool:
    """True when a model-produced string is real prose, not a leaked scratchpad."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not looks_like_reasoning(value)
    )


def tailor_resume(resume: Resume, job: ParsedJob) -> TailoredOutput:
    """Produce a tailored summary, skill ordering, highlights and cover letter.

    The deterministic parts (ordering, matching, highlighting) always run and are
    never delegated. Only the prose is asked of the model, and if the model
    mentions a requirement the candidate doesn't have, its output is discarded in
    favour of the deterministic text — a hallucinated skill on a resume is worse
    than a plainer sentence.
    """
    matched, missing = match_keywords(resume, job)
    ordered = order_skills(resume, job)
    highlights = highlight_experience(resume, job)

    summary = _fallback_summary(resume, job, matched)
    cover = _fallback_cover_letter(resume, job, matched, highlights)
    generated_with = "heuristic"
    model: str | None = None

    if llm_is_configured():
        try:
            raw = chat_completion(
                [
                    {
                        "role": "system",
                        "content": untrusted.guarded(_SYSTEM_PROMPT),
                    },
                    {
                        "role": "user",
                        "content": _build_prompt(resume, job, matched, missing, highlights),
                    },
                ],
                model=settings.openrouter_model,
                # The free reasoning models spend most of their budget thinking
                # before they emit the object; a tight cap truncates mid-thought
                # and we get nothing parseable back.
                temperature=0.4,
                max_tokens=3000,
            )
            data = extract_json_object(raw) or {}
            llm_summary = data.get("tailored_summary") if usable_prose(
                data.get("tailored_summary")
            ) else None
            llm_cover = data.get("cover_letter") if usable_prose(data.get("cover_letter")) else None

            # One combined check: either both pieces of prose are trustworthy or
            # neither is used. A summary grounded in the resume paired with an
            # embellished cover letter is still an embellished application.
            candidate_text = f"{llm_summary or ''}\n{llm_cover or ''}"
            fabricated = mentions_missing(candidate_text, missing) or invented_metric(
                candidate_text, resume
            )
            if fabricated:
                logger.warning(
                    "tailoring rejected: model claimed %r, which the resume does not support",
                    fabricated,
                )
            else:
                if llm_summary:
                    summary = llm_summary.strip()
                    generated_with = "llm"
                if llm_cover:
                    cover = llm_cover.strip()
                    generated_with = "llm"
                highlights = _merge_experience_notes(highlights, data.get("experience_notes"))
                model = settings.openrouter_model if generated_with == "llm" else None
        except OpenRouterError as exc:
            # Never fail a tailoring request on an AI hiccup.
            logger.info("tailoring fell back to heuristics: %s", exc)

    return TailoredOutput(
        tailored_summary=summary,
        ordered_skills=ordered,
        highlighted_experience=highlights,
        matched_keywords=matched,
        missing_keywords=missing,
        cover_letter=cover,
        generated_with=generated_with,
        model=model,
    )


def _merge_experience_notes(
    highlights: list[dict[str, Any]], notes: Any
) -> list[dict[str, Any]]:
    """Overlay the model's per-role rationale onto the computed highlights."""
    if not isinstance(notes, list):
        return highlights
    by_company = {
        str(n.get("company", "")).strip().lower(): str(n.get("why_relevant", "")).strip()
        for n in notes
        if isinstance(n, dict) and n.get("why_relevant")
    }
    for entry in highlights:
        note = by_company.get(str(entry.get("company") or "").strip().lower())
        if note:
            entry["why_relevant"] = note[:400]
    return highlights


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #


def render_markdown(resume: Resume, tailored: Any) -> str:
    """Render a tailored resume as Markdown for download.

    Takes the persisted :class:`~app.models.tailored_resume.TailoredResume` row
    (or anything with the same attributes) so the download endpoint doesn't have
    to re-run tailoring.
    """
    contact = " · ".join(
        p for p in (resume.email, resume.phone, resume.location, *(resume.links or [])[:2]) if p
    )
    lines = [
        f"# {resume.full_name or 'Candidate'}",
        f"**{tailored.job_title or resume.headline or ''}**".strip("* "),
    ]
    if contact:
        lines.append(contact)
    lines.append("")

    if tailored.tailored_summary:
        lines += ["## Summary", tailored.tailored_summary, ""]

    if tailored.ordered_skills:
        lines += ["## Skills", ", ".join(tailored.ordered_skills), ""]

    if tailored.highlighted_experience:
        lines.append("## Experience")
        for entry in tailored.highlighted_experience:
            header = " — ".join(
                p for p in (entry.get("title"), entry.get("company")) if p
            )
            period = " – ".join(p for p in (entry.get("start"), entry.get("end")) if p)
            lines.append(f"### {header}{f' ({period})' if period else ''}")
            if entry.get("why_relevant"):
                lines.append(entry["why_relevant"])
            if entry.get("matched_skills"):
                lines.append(f"*Relevant: {', '.join(entry['matched_skills'])}*")
            lines.append("")

    if resume.education:
        lines.append("## Education")
        for entry in resume.education:
            parts = [
                entry.get("degree"),
                entry.get("field"),
                entry.get("school"),
                entry.get("year"),
            ]
            lines.append("- " + ", ".join(str(p) for p in parts if p))
        lines.append("")

    return "\n".join(lines).strip() + "\n"


__all__ = [
    "TailoredOutput",
    "highlight_experience",
    # The grounding guardrails are public because the cover letter writer
    # applies exactly the same ones — a letter that invents a metric is the
    # same failure as a resume that does, and there should be one implementation
    # of "did the model make this up?" rather than two that can drift.
    "invented_metric",
    "match_keywords",
    "mentions_missing",
    "order_skills",
    "render_markdown",
    "tailor_resume",
    "usable_prose",
]
