"""Per-job bullet rewriting — the candidate's own achievements, re-angled.

:mod:`app.services.resume_tailor` reorders skills and re-words the summary. What
it does not touch is the part a hiring manager actually reads: the bullets under
each role. A candidate who ran a payments platform and is applying to an
infrastructure team has the right experience described for the wrong audience,
and reordering their skill list does not fix that.

The rewrite is an **edit of their own claims**, never an addition. Concretely,
a rewritten bullet may:

* lead with the part of the achievement this posting cares about,
* use the posting's vocabulary for a thing the candidate already did,
* drop detail that is irrelevant here.

It may not introduce a technology, a metric, a scope or an outcome the original
bullet didn't contain. :func:`bullet_is_grounded` is what enforces that, and a
bullet that fails it is replaced by the original rather than dropped — a
truthful bullet that reads slightly wrong for the role still beats a polished
one the candidate has to defend in an interview.

Bullets come out of ``resume.raw_text`` rather than the parsed ``experience``
list, because the parser only ever captured the role headers (company, title,
dates). :func:`extract_role_bullets` walks the Experience section and associates
each bullet line with the role heading above it. Plenty of resumes have no
bullets at all — a prose CV, a European-style one — and that is a normal
outcome, not a failure: the rewrite simply has nothing to do.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.core.config import settings
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
from app.services.places import fold_diacritics
from app.services.resume_tailor import invented_metric, mentions_missing

logger = logging.getLogger(__name__)

# Section headers that end the experience block. Everything after one of these
# belongs to a different part of the CV and its lines are not job bullets.
_SECTION_END = re.compile(
    r"^\s*(education|skills|projects|certifications?|publications?|awards?|"
    r"languages?|interests?|volunteer|references?)\b",
    re.I,
)
_SECTION_START = re.compile(r"^\s*(experience|employment|work history|career)\b", re.I)

# The same headers with the words resumes actually put in front of them. Both
# patterns above are anchored at the start of the line, and the two commonest
# headings in the whole format begin with a word neither of them looks for:
# "Work Experience" / "Professional Experience", and "Technical Skills".
#
# Each miss costs something different, and both are silent. A resume whose
# experience section is headed "PROFESSIONAL EXPERIENCE" never opens the section
# at all, so `extract_role_bullets` returns nothing and the entire rewrite is a
# no-op — the feature simply does not run, and says nothing about it. A resume
# headed "TECHNICAL SKILLS" never *closes* it, so the skill list is read as more
# bullets under the last job held: "Python, FastAPI, PostgreSQL, AWS" arrives on
# the tailored document as an achievement, under a role, in front of a recruiter.
_QUALIFIER = (
    r"(?:professional|work|relevant|additional|other|prior|previous|past|recent"
    r"|related|industry|selected|technical|core|key|personal|academic|further)"
)
_QUALIFIED_START = re.compile(
    rf"^\s*(?:{_QUALIFIER}\s+){{1,2}}(?:experience|employment|work history|career)\b",
    re.I,
)
_QUALIFIED_END = re.compile(
    rf"^\s*(?:{_QUALIFIER}\s+){{1,2}}(?:education|skills|projects|certifications?"
    r"|publications?|awards?|languages?|interests?|volunteer|references?)\b",
    re.I,
)

# A qualified header is only read as one when the line is shaped like a header.
# A qualifier in front of a keyword is also how a *role* line reads — "Technical
# Projects Lead at Acme, 2019 - 2021" — and mistaking that for the Projects
# section would close the experience block early and drop every bullet after it.
# Section headers are short, few words, and never carry a date range.
_MAX_HEADING_CHARS = 48
_MAX_HEADING_WORDS = 4

# A bullet, however the CV writes it: -, *, •, ‣, ▪, or a digit-dot list.
_BULLET = re.compile(r"^\s*(?:[-*•‣▪◦·–—]|\d+[.)])\s+(?P<text>.+)$")

# A role heading: it names a company and usually a date range. Detected loosely,
# because every resume formats this differently ("Title, Company 2021 - Present",
# "Title at Company", "Company | Title | 2021-2023").
_YEAR_RANGE = re.compile(
    r"(19|20)\d{2}\s*[-–—]\s*((19|20)\d{2}|present|current|now)", re.I
)

MAX_BULLETS_PER_ROLE = 8


def _is_heading_shaped(line: str) -> bool:
    """Whether *line* could be a section header at all, on shape alone."""
    stripped = line.strip()
    return (
        bool(stripped)
        and len(stripped) <= _MAX_HEADING_CHARS
        and len(stripped.split()) <= _MAX_HEADING_WORDS
        and not _YEAR_RANGE.search(stripped)
    )


def section_marker(line: str) -> str | None:
    """``"start"``, ``"end"``, or None — whether *line* bounds the experience block.

    The bare forms are matched unconditionally, exactly as they always were. The
    qualified forms go through :func:`_is_heading_shaped` first, so widening the
    vocabulary cannot make a role line start closing sections.
    """
    if _SECTION_START.match(line):
        return "start"
    if _SECTION_END.match(line):
        return "end"
    if not _is_heading_shaped(line):
        return None
    if _QUALIFIED_START.match(line):
        return "start"
    if _QUALIFIED_END.match(line):
        return "end"
    return None


def _looks_like_role_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _BULLET.match(line):
        return False
    # A date range is the strongest single signal, and the common case.
    if _YEAR_RANGE.search(stripped):
        return True
    # Otherwise: short, and joins two things with a separator ("Title at Co").
    return len(stripped) < 120 and bool(
        re.search(r"\bat\b|\||,|—|–", stripped) and not stripped.endswith(".")
    )


def extract_role_bullets(resume: Resume) -> dict[str, list[str]]:
    """Bullets from the resume text, grouped by the role they sit under.

    Keyed on the raw heading line, lowercased. Matching back to a parsed
    experience entry happens leniently at read time in :func:`bullets_for`,
    because the heading in the text ("Senior Backend Engineer at Acme Corp
    2018 - 2021") is never equal to the parsed (company, title) pair.
    """
    text = resume.raw_text or ""
    if not text.strip():
        return {}

    lines = text.splitlines()
    out: dict[str, list[str]] = {}
    current: str | None = None
    in_experience = False

    for line in lines:
        marker = section_marker(line)
        if marker == "start":
            in_experience = True
            current = None
            continue
        if marker == "end":
            # Skills/Education end the run of roles; anything after is not a
            # bullet about a job.
            in_experience = False
            current = None
            continue
        if not in_experience:
            continue

        bullet = _BULLET.match(line)
        if bullet:
            if current is None:
                continue
            body = bullet.group("text").strip()
            if body:
                out.setdefault(current, []).append(body[:600])
            continue

        if _looks_like_role_heading(line):
            current = line.strip().lower()

    return out


def bullets_for(
    role_bullets: dict[str, list[str]], company: str | None, title: str | None
) -> list[str]:
    """The bullets belonging to one role, matched leniently on its heading.

    The heading in the text ("Senior Backend Engineer at Acme Corp 2018 - 2021")
    is not the parsed pair, so an exact key lookup would never hit. A heading
    matches when it mentions the company — the one field boards and parsers
    agree on.
    """
    company_key = (company or "").strip().lower()
    title_key = (title or "").strip().lower()
    if not company_key and not title_key:
        return []

    best: list[str] = []
    for heading, bullets in role_bullets.items():
        if company_key and company_key in heading:
            # A title match on top of the company disambiguates a promotion.
            if title_key and title_key in heading:
                return bullets[:MAX_BULLETS_PER_ROLE]
            if not best:
                best = bullets
    return best[:MAX_BULLETS_PER_ROLE]


# --------------------------------------------------------------------------- #
# Grounding                                                                    #
# --------------------------------------------------------------------------- #

_WORD = re.compile(r"[a-z0-9+#.]+")
# Words a rewrite may introduce freely: they carry no claim. Anything outside
# this set that isn't in the original bullet is a new assertion about the
# candidate's experience.
_CONNECTIVES = {
    "a", "an", "the", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "as", "into", "across", "over", "under", "via",
    "that", "which", "who", "this", "these", "those", "it", "its", "their",
    "i", "we", "my", "our", "was", "were", "is", "are", "be", "been", "being",
    "had", "has", "have", "did", "do", "does", "will", "would", "can", "could",
    "led", "ran", "built", "drove", "owned", "shipped", "delivered", "designed",
    "developed", "created", "implemented", "managed", "maintained", "improved",
    "reduced", "increased", "scaled", "migrated", "launched", "supported",
    "worked", "helped", "enabled", "ensured", "established", "grew", "cut",
    "team", "teams", "engineer", "engineers", "engineering", "system",
    "systems", "service", "services", "platform", "product", "production",
    "end", "new", "key", "core", "full", "while", "including", "through",
    "responsible", "using", "used", "up", "out", "down", "more", "than",
}


def _words(text: str) -> set[str]:
    """The vocabulary of *text*, folded onto ASCII before it is split.

    `_WORD` is `[a-z0-9+#.]+` and an accented letter is not in it, so without
    the fold it is a word boundary: "Déployé" split into ``{"d", "ploy"}``,
    "Présenté les résultats" into ``{"pr", "sent", "les", "r", "sultats"}``.

    Both sides of the subtraction below go through here, so a shredded
    vocabulary is not merely noisy — it is wrong in both directions:

    * The model rewrites the bullet and normalises the accent away, which it
      does routinely. "Déployé ... la sécurité" becomes "Deploye ... la
      securite", and neither of those words appears in the original's
      fragments, so the rewrite is judged to have invented two technologies
      and is discarded. Every bullet on a non-English resume, every time,
      silently — the candidate just sees untailored bullets.
    * A fragment can be a real word. "Présenté" leaves "sent" behind, so a
      rewrite that introduces "sent" reads as already-claimed. That is the
      direction this check exists to stop.

    Same helper and same ordering as `skill_aliases.normalize_text`, which is
    what `mentions_missing` and `invented_metric` — the other two checks in
    :func:`bullet_is_grounded` — already compare under.
    """
    return set(_WORD.findall(fold_diacritics((text or "").lower())))


def bullet_is_grounded(rewritten: str, original: str, resume: Resume, missing: list[str]) -> bool:
    """True when a rewritten bullet asserts nothing the original didn't.

    Three checks, cheapest first. The vocabulary check is the load-bearing one:
    a rewrite that introduces "Kubernetes" into a bullet about Postgres has
    invented experience, however fluent it reads. Connectives and generic
    engineering verbs are exempt, because otherwise no rewrite could ever pass.
    """
    if not rewritten.strip():
        return False
    if mentions_missing(rewritten, missing):
        return False
    if invented_metric(rewritten, resume):
        return False

    introduced = _words(rewritten) - _words(original) - _CONNECTIVES
    # Also allow anything the candidate claims elsewhere on their own resume:
    # moving a real skill into the bullet where it is relevant is a re-angle,
    # not an invention.
    introduced -= _words(resume.raw_text or "")
    introduced -= _words(" ".join(resume.skills or []))
    return not introduced


# --------------------------------------------------------------------------- #
# Rewriting                                                                    #
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = (
    "You re-angle a candidate's resume bullets at one job description. You EDIT "
    "existing bullets. You never write new ones and you never add facts.\n\n"
    "For each bullet you may: lead with the part this posting cares about, use "
    "the posting's vocabulary for something the candidate already did, and drop "
    "detail irrelevant to this role.\n\n"
    "You may NOT: introduce a technology, tool, metric, number, scope, team "
    "size or outcome that is not in the original bullet. You may not merge two "
    "bullets. You may not invent a bullet. If a bullet is already well-aimed, "
    "return it unchanged.\n\n"
    "Keep each bullet to one sentence, under 240 characters, starting with a "
    "past-tense verb.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"bullets": [{"index": int, "text": str}]}\n'
    "'index' is the bullet's position in the list you were given."
)


def _build_prompt(
    bullets: list[str], job: ParsedJob, company: str | None, title: str | None
) -> str:
    """The user message: the candidate's own bullets, and a stranger's posting.

    Only the posting is fenced, the same asymmetry
    :func:`app.services.resume_tailor._build_prompt` draws and for the same
    reason. The bullets are the candidate's own sentences and the system prompt
    makes them the only material a rewrite may assert — telling the model to
    disregard directions found in there would disarm that rule rather than
    enforce it. The posting is a page nobody here has read.

    This module was the second half of one tailoring run and was missed when
    the first half was fenced: `smart_apply_service.tailor_and_save` calls
    `resume_tailor.tailor_resume` and then this, on the same `ParsedJob`. So
    the summary paragraph was hardened while the bullets underneath it — the
    lines that carry the candidate's claimed achievements onto the PDF an
    employer receives — took the same posting bare, next to section headers
    written in the same `== NAME ==` shape a `responsibilities` entry is free
    to forge.

    `bullet_is_grounded` still discards a rewrite that introduces a word the
    original and the resume do not have, and this sits in front of that rather
    than in place of it. It is not redundant: that check reads vocabulary, so
    it cannot see a rewrite that only ever drops, merges or re-aims — and
    "Director", "Head of" or any other word already somewhere in the resume's
    raw text passes it.
    """
    posting = "\n".join(
        [
            f"Title: {job.title or 'n/a'}",
            f"Required skills: {', '.join(job.required_skills[:15]) or 'n/a'}",
            "Responsibilities:",
            *([f"- {r}" for r in job.responsibilities[:6]] or ["- n/a"]),
        ]
    )
    return "\n".join(
        [
            f"== ROLE THE BULLETS DESCRIBE ==\n{title or '?'} at {company or '?'}",
            "",
            "== TARGET POSTING ==",
            untrusted.fence(posting, label="job posting"),
            "",
            "== BULLETS TO RE-ANGLE (edit these, invent nothing) ==",
            *(f"[{i}] {b}" for i, b in enumerate(bullets)),
        ]
    )


def rewrite_bullets(
    bullets: list[str],
    job: ParsedJob,
    resume: Resume,
    missing: list[str],
    *,
    company: str | None = None,
    title: str | None = None,
) -> tuple[list[str], str]:
    """Re-angle one role's bullets at the posting.

    Returns ``(bullets, generated_with)``. Rewrites are accepted individually:
    a model that grounds four bullets and invents a metric in the fifth keeps
    the four, and the fifth falls back to the original. Rejecting the whole
    batch would throw away good work over one bad line.
    """
    if not bullets:
        return [], "heuristic"
    if not llm_is_configured():
        return list(bullets), "heuristic"

    try:
        raw = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_SYSTEM_PROMPT)},
                {"role": "user", "content": _build_prompt(bullets, job, company, title)},
            ],
            model=settings.openrouter_model,
            temperature=0.4,
            max_tokens=2500,
        )
    except OpenRouterError as exc:
        logger.info("bullet rewrite fell back to the originals: %s", exc)
        return list(bullets), "heuristic"

    if looks_like_reasoning(raw):
        return list(bullets), "heuristic"

    data = extract_json_object(raw) or {}
    proposed = data.get("bullets")
    if not isinstance(proposed, list):
        return list(bullets), "heuristic"

    out = list(bullets)
    accepted = 0
    for item in proposed:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(out):
            continue

        text = str(item.get("text") or "").strip()
        if not text or looks_like_reasoning(text):
            continue
        if not bullet_is_grounded(text, bullets[index], resume, missing):
            logger.info(
                "bullet rewrite rejected (introduced a claim the original lacks): %r", text
            )
            continue
        out[index] = text[:400]
        accepted += 1

    return out, ("llm" if accepted else "heuristic")


def tailor_experience_bullets(
    resume: Resume, job: ParsedJob, highlights: list[dict[str, Any]], missing: list[str]
) -> tuple[list[dict[str, Any]], str]:
    """Attach original and rewritten bullets to each highlighted role.

    Both lists are stored: showing the rewrite beside the original is how a user
    checks that nothing was added, which is the only way this feature can be
    trusted rather than believed.
    """
    role_bullets = extract_role_bullets(resume)
    if not role_bullets:
        for entry in highlights:
            entry.setdefault("original_bullets", [])
            entry.setdefault("bullets", [])
        return highlights, "heuristic"

    generated_with = "heuristic"
    for entry in highlights:
        originals = bullets_for(role_bullets, entry.get("company"), entry.get("title"))
        entry["original_bullets"] = originals
        if not originals:
            entry["bullets"] = []
            continue
        rewritten, how = rewrite_bullets(
            originals,
            job,
            resume,
            missing,
            company=entry.get("company"),
            title=entry.get("title"),
        )
        entry["bullets"] = rewritten
        if how == "llm":
            generated_with = "llm"

    return highlights, generated_with


__all__ = [
    "MAX_BULLETS_PER_ROLE",
    "bullet_is_grounded",
    "bullets_for",
    "extract_role_bullets",
    "rewrite_bullets",
    "section_marker",
    "tailor_experience_bullets",
]
