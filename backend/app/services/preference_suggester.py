"""Preference suggestions — turn parsed resumes into a pre-filled setup form.

"Confirm your search" is thirteen fields, and the resumes already answer most of
them: the titles to chase, the city, the industries, and roughly what salary
floor makes sense for someone that senior. This module maps the parsed resumes
onto those knobs so the step reads as *review and confirm* rather than *fill from
scratch*.

A candidate may upload several resumes — one per role they're targeting — so
:func:`merge_suggestions` folds a whole set into one search. Merging is unions
and the most inclusive bound, never an average: uploading a second resume should
only ever widen what you get shown.

Two rules shape everything here:

* **Deterministic and offline.** No LLM call, no network. The resume was already
  parsed (and optionally LLM-enriched) at upload time; this is pure mapping, so
  the same resume always produces the same suggestions.
* **Nothing is invented.** A field with no evidence behind it is left out
  entirely — the UI shows it empty for the user to fill. Every value that *is*
  suggested carries a source (``resume`` | ``inferred`` | ``default``) and a
  one-line reason, so the form can mark it and the user can tell what came from
  where.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.models.resume import Resume
from app.services.resume_parser import split_remote, stated_salary_expectation

# ---- Product defaults --------------------------------------------------------
# Deliberately gentler than the model defaults: a brand-new user should start
# selective and quiet, and opt *into* volume once they've seen the output.
DEFAULT_DAILY_APPLICATION_LIMIT = 5
DEFAULT_MIN_FIT_SCORE = 60
# Auto-send *on*, behind a short trial. This used to suggest off, with the note
# "so you can read the first few emails before they go" — but nothing then turned
# it on after those few, so the honest description of that default was "off, and
# you must remember to come back". The trial does what the note always promised:
# the first three go to the review queue, and approving them graduates the user
# to hands-off without another visit to settings.
DEFAULT_AUTO_SEND = True
DEFAULT_AUTO_SEND_TRIAL_APPROVALS = 3

# The industry vocabulary the setup UI offers as chips. Free-text industries off
# the resume are folded into these where possible so they arrive pre-selected.
INDUSTRY_VOCABULARY: tuple[str, ...] = (
    "tech",
    "fintech",
    "ai",
    "healthcare",
    "ecommerce",
    "security",
    "data",
    "gaming",
)

# Aliases → canonical industry, most specific first: "financial technology" has
# to reach fintech before the generic "tech" bucket claims it.
_INDUSTRY_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("fintech", (
        "fintech", "financial technology", "financial services", "banking",
        "payments", "payment", "insurance", "insurtech", "lending", "trading",
        "crypto", "web3",
    )),
    ("ai", (
        "ai", "artificial intelligence", "machine learning", "deep learning",
        "ml", "llm", "nlp", "computer vision", "robotics",
    )),
    ("healthcare", (
        "healthcare", "health care", "healthtech", "medical", "biotech",
        "pharma", "pharmaceutical", "hospital", "life sciences", "digital health",
    )),
    ("ecommerce", (
        "ecommerce", "e-commerce", "commerce", "retail", "marketplace",
        "consumer goods", "d2c", "dtc",
    )),
    ("security", (
        "security", "cybersecurity", "cyber security", "infosec", "appsec",
        "identity", "privacy", "fraud",
    )),
    ("data", (
        "data", "analytics", "business intelligence", "big data",
        "data engineering", "data science",
    )),
    ("gaming", (
        "gaming", "games", "game development", "esports",
        "interactive entertainment",
    )),
    ("tech", (
        "tech", "technology", "software", "saas", "internet", "it services",
        "cloud", "developer tools", "devtools", "telecom", "hardware",
        "enterprise software",
    )),
]

# Skills that point at an industry on their own. Weaker evidence than a stated
# industry, so these only fill the gap when the resume named none.
_SKILL_INDUSTRY_HINTS: list[tuple[str, frozenset[str]]] = [
    ("ai", frozenset({
        "pytorch", "tensorflow", "scikit-learn", "langchain", "machine learning",
        "deep learning", "nlp", "computer vision", "llm",
    })),
    ("data", frozenset({
        "kafka", "spark", "airflow", "dbt", "hadoop", "flink", "snowflake",
        "clickhouse", "data engineering", "data science", "tableau", "power bi",
        "looker",
    })),
    ("security", frozenset({
        "security", "cybersecurity", "infosec", "appsec", "penetration testing",
        "iam", "siem",
    })),
    ("fintech", frozenset({
        "payments", "stripe", "plaid", "kyc", "ledger", "quickbooks",
    })),
    ("ecommerce", frozenset({"shopify", "magento", "woocommerce", "bigcommerce"})),
    ("gaming", frozenset({"unity", "unreal", "unreal engine", "godot"})),
    ("healthcare", frozenset({"hl7", "fhir", "hipaa", "dicom"})),
]

# Skills that say "this is a software career" and nothing more specific — a last
# resort so an engineer isn't shown zero industries.
_TECH_FALLBACK_SKILLS = frozenset({
    "python", "java", "javascript", "typescript", "go", "golang", "rust",
    "react", "vue", "angular", "node.js", "fastapi", "django", "spring",
    "kubernetes", "docker", "terraform", "aws", "gcp", "azure", "microservices",
    "system design", "devops", "sre",
})

_MAX_ROLES = 5
_MAX_ROLE_LENGTH = 60
_MAX_INDUSTRIES = 4
_MAX_LOCATIONS = 5

# "2021 - Present" outranks any real end year when ordering jobs by recency.
_PRESENT_MARKERS = frozenset({"present", "current", "now", "today", "ongoing"})
_ONGOING_YEAR = 9999
_YEAR_IN_TEXT_RE = re.compile(r"(?:19|20)\d{2}")

# Rough US-market floors by seniority band. Anchors for a *minimum*, not a market
# rate, and deliberately conservative: a floor set too high silently filters out
# jobs the user would have wanted to see.
_SALARY_FLOORS: dict[str, int] = {
    "junior": 80_000,
    "mid": 115_000,
    "senior": 150_000,
    "lead": 185_000,
    "exec": 230_000,
}
# The years each band starts at, for the per-year top-up on top of the floor.
_BAND_START_YEARS: dict[str, int] = {
    "junior": 0,
    "mid": 3,
    "senior": 6,
    "lead": 12,
    "exec": 15,
}
_YEAR_INCREMENT = 5_000
_MAX_YEAR_TOPUP = 25_000
_SALARY_ROUNDING = 5_000



@dataclass
class PreferenceSuggestions:
    """Suggested values for the preferences form, plus where each came from.

    ``sources`` and ``notes`` are keyed by form field name. A field absent from
    ``sources`` was not suggested at all — the UI leaves it empty.
    """

    target_roles: list[str] = field(default_factory=list)
    target_industries: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_only: bool = False
    salary_min: int | None = None
    min_fit_score: int = DEFAULT_MIN_FIT_SCORE
    daily_application_limit: int = DEFAULT_DAILY_APPLICATION_LIMIT
    auto_send: bool = DEFAULT_AUTO_SEND
    auto_send_trial_approvals: int = DEFAULT_AUTO_SEND_TRIAL_APPROVALS

    # field -> "resume" (copied straight off the parse) | "inferred" (derived
    # from something else on the resume) | "default" (a product default).
    sources: dict[str, str] = field(default_factory=dict)
    # field -> one-line explanation, shown next to the field in the wizard.
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def values(self) -> dict[str, Any]:
        """Just the form values, without the provenance metadata."""
        return {
            "target_roles": self.target_roles,
            "target_industries": self.target_industries,
            "locations": self.locations,
            "remote_only": self.remote_only,
            "salary_min": self.salary_min,
            "min_fit_score": self.min_fit_score,
            "daily_application_limit": self.daily_application_limit,
            "auto_send": self.auto_send,
            "auto_send_trial_approvals": self.auto_send_trial_approvals,
        }

    @property
    def prefilled_fields(self) -> list[str]:
        """Fields the resume actually informed — what the UI marks as suggested."""
        return [name for name, src in self.sources.items() if src != "default"]


# --------------------------------------------------------------------------- #
# Target roles
# --------------------------------------------------------------------------- #


def _clean_role(value: Any) -> str | None:
    """Tidy a job title, or None if it doesn't look like one."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip(" ,|·•-–—:")
    return cleaned if 2 < len(cleaned) <= _MAX_ROLE_LENGTH else None


def _dedupe(roles: list[str | None]) -> list[str]:
    """Drop blanks and case-insensitive repeats, keeping the first spelling."""
    out: list[str] = []
    seen: set[str] = set()
    for role in roles:
        if role and role.lower() not in seen:
            seen.add(role.lower())
            out.append(role)
    return out


def _job_year(value: Any, *, default: int) -> int:
    text = str(value or "").strip().lower()
    if text in _PRESENT_MARKERS:
        return _ONGOING_YEAR
    match = _YEAR_IN_TEXT_RE.search(text)
    return int(match.group(0)) if match else default


def _recency_key(entry: dict[str, Any]) -> tuple[int, int]:
    """Sort key for 'most recent job first', by end date then start date.

    Entries whose dates didn't parse sort to 0 and so keep their original
    relative order — which, on a resume, is already newest-first.
    """
    return (_job_year(entry.get("end"), default=0), _job_year(entry.get("start"), default=0))


def suggest_roles(
    stated: list[str] | None,
    headline: str | None,
    experience: list[dict[str, Any]] | None,
) -> tuple[list[str], str]:
    """Roles to chase, and whether they were stated or inferred.

    Stated ``target_roles`` win. When the parse produced none — common when a
    resume has no headline for the parser to read a role line off — the most
    recent job titles stand in: someone who was a Staff Engineer last should be
    pitched Staff Engineer roles. The headline is the last resort.
    """
    explicit = _dedupe([_clean_role(role) for role in stated or []])
    if explicit:
        return explicit[:_MAX_ROLES], "resume"

    jobs = [entry for entry in experience or [] if isinstance(entry, dict)]
    recent_first = sorted(jobs, key=_recency_key, reverse=True)
    titles = [_clean_role(entry.get("title")) for entry in recent_first]
    inferred = _dedupe([*titles, _clean_role(headline)])
    return inferred[:_MAX_ROLES], "inferred"


# --------------------------------------------------------------------------- #
# Industry mapping
# --------------------------------------------------------------------------- #


def _matches(text: str, alias: str) -> bool:
    """Whole-word alias match, so 'ai' never fires on 'retail'."""
    return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None


def canonical_industry(text: str) -> str | None:
    """Fold a free-text industry onto the UI's vocabulary, or None if it doesn't fit."""
    lowered = text.strip().lower()
    if not lowered:
        return None
    if lowered in INDUSTRY_VOCABULARY:
        return lowered
    for canon, aliases in _INDUSTRY_ALIASES:
        if any(_matches(lowered, alias) for alias in aliases):
            return canon
    return None


def suggest_industries(
    stated: list[str] | None, skills: list[str] | None
) -> tuple[list[str], str]:
    """Industries to pre-select, and whether they came off the resume or the skills.

    Stated industries win — they're what the candidate says they've worked in.
    Anything that doesn't fold into the vocabulary is kept verbatim so it still
    reaches the autopilot (which accepts free-text industries); the UI renders
    those as extra chips.
    """
    picked: list[str] = []
    for raw in stated or []:
        value = canonical_industry(raw) or " ".join(raw.split()).lower()
        if value and value not in picked:
            picked.append(value)
        if len(picked) >= _MAX_INDUSTRIES:
            break
    if picked:
        return picked, "resume"

    lowered = {s.strip().lower() for s in skills or []}
    for canon, keywords in _SKILL_INDUSTRY_HINTS:
        if lowered & keywords and canon not in picked:
            picked.append(canon)
        if len(picked) >= _MAX_INDUSTRIES:
            break
    if not picked and lowered & _TECH_FALLBACK_SKILLS:
        picked.append("tech")
    return picked, "inferred"


# --------------------------------------------------------------------------- #
# Salary floor
# --------------------------------------------------------------------------- #


def _band_from_years(years: int) -> str:
    """Seniority band from years alone — mirrors resume_parser.infer_seniority."""
    if years >= 12:
        return "lead"
    if years >= 6:
        return "senior"
    if years >= 3:
        return "mid"
    return "junior"


def suggest_salary_min(years: int | None, seniority: str | None) -> int | None:
    """A conservative salary floor from seniority, nudged up by years of experience.

    Returns None when the resume says neither — better an empty field than a
    number we made up.
    """
    band = (seniority or "").strip().lower()
    if band not in _SALARY_FLOORS:
        if years is None or years <= 0:
            return None
        band = _band_from_years(years)

    floor = _SALARY_FLOORS[band]
    if years is not None:
        extra_years = max(0, years - _BAND_START_YEARS[band])
        floor += min(extra_years * _YEAR_INCREMENT, _MAX_YEAR_TOPUP)
    return round(floor / _SALARY_ROUNDING) * _SALARY_ROUNDING


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #


def suggest_locations(location: str | None) -> tuple[list[str], bool]:
    """Split a parsed location into (places, remote_only).

    "Remote" on a resume is a preference, not a place: it flips the remote-only
    switch, and anything left over ("Remote — Austin, TX") stays as the location.

    The split itself now belongs to :func:`app.services.resume_parser.split_remote`,
    beside the function that joined the string in the first place. This was the
    only correct copy of the rule and it was private, so the fit scorer — the
    other reader of the same column — went without one entirely.
    """
    place, remote = split_remote(location)
    return ([place] if place else []), remote


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def suggest_preferences(resume: Resume) -> PreferenceSuggestions:
    """Map a parsed resume onto the autopilot preference fields."""
    out = PreferenceSuggestions()
    # Defaults are suggestions too — the UI shows them unmarked, but the user
    # should still see why 5/60/off is where the form starts.
    out.sources.update(
        {
            "min_fit_score": "default",
            "daily_application_limit": "default",
            "auto_send": "default",
            "auto_send_trial_approvals": "default",
        }
    )
    out.notes.update(
        {
            "min_fit_score": "A good starting point — raise it for fewer, better matches.",
            "daily_application_limit": "Starts small to keep your mailbox in good standing.",
            "auto_send": "On, after you've approved the first few yourself.",
            "auto_send_trial_approvals": (
                f"You approve {DEFAULT_AUTO_SEND_TRIAL_APPROVALS} by hand, then "
                "sending is automatic."
            ),
        }
    )

    roles, role_source = suggest_roles(
        resume.target_roles, resume.headline, resume.experience
    )
    if roles:
        out.target_roles = roles
        out.sources["target_roles"] = role_source
        out.notes["target_roles"] = (
            "From your titles and headline."
            if role_source == "resume"
            else "From your most recent job titles."
        )

    locations, remote = suggest_locations(resume.location)
    if locations:
        out.locations = locations
        out.sources["locations"] = "resume"
        out.notes["locations"] = "Where your resume says you are."
    if remote:
        out.remote_only = True
        out.sources["remote_only"] = "resume"
        out.notes["remote_only"] = "Your resume lists your location as remote."

    industries, industry_source = suggest_industries(
        resume.target_industries, resume.skills
    )
    if industries:
        out.target_industries = industries
        out.sources["target_industries"] = industry_source
        out.notes["target_industries"] = (
            "Industries on your resume."
            if industry_source == "resume"
            else "Guessed from your skills — worth a second look."
        )

    # A number the candidate wrote down beats one we worked out from their
    # seniority — it is the only figure on the page they actually chose.
    stated = stated_salary_expectation(resume.raw_text or "")
    if stated is not None:
        out.salary_min = stated.usd
        out.sources["salary_min"] = "resume"
        # The number in the box is very often not the number on the page: a
        # contractor's "$95/hr" is stored as 197,600 and an Indian candidate's
        # "₹22,00,000" as 26,400. Under a flat "stated on your resume" that
        # reads as a parse error, and the repair for a parse error is to type
        # over it — so the one figure the candidate actually chose gets
        # replaced by a round number they guessed at the keyboard.
        derivation = stated.derivation()
        out.notes["salary_min"] = (
            f"The expectation stated on your resume, {derivation}."
            if derivation
            else "The expectation stated on your resume."
        )
    else:
        salary = suggest_salary_min(resume.years_experience, resume.seniority)
        if salary is not None:
            out.salary_min = salary
            out.sources["salary_min"] = "inferred"
            detail = ", ".join(
                part
                for part in (
                    f"{resume.years_experience} years"
                    if resume.years_experience
                    else "",
                    (resume.seniority or "").strip(),
                )
                if part
            )
            out.notes["salary_min"] = (
                f"A rough floor for {detail} — set it to what you'd actually accept."
                if detail
                else "A rough floor — set it to what you'd actually accept."
            )

    return out


# --------------------------------------------------------------------------- #
# Merging a whole set of resumes
# --------------------------------------------------------------------------- #

# Seniority bands, weakest first — for picking the top band across resumes.
_SENIORITY_ORDER: tuple[str, ...] = ("junior", "mid", "senior", "lead", "exec")

_MAX_PROFILE_SKILLS = 40


@dataclass
class ExtractedProfile:
    """What the resumes say about the candidate, for the "we read this" summary.

    Not preferences — these are facts off the parse, shown so the user can see
    the extraction worked before they trust the search built from it.
    """

    resume_ids: list[int] = field(default_factory=list)
    full_name: str | None = None
    location: str | None = None
    seniority: str | None = None
    years_experience: int | None = None
    skills: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)


def _ordered(resumes: Sequence[Resume]) -> list[Resume]:
    """Default resume first, then newest — the order suggestions inherit."""
    return sorted(
        resumes, key=lambda r: (not bool(r.is_default), -(r.id or 0)),
    )


def _union(values: Sequence[Sequence[str]], limit: int) -> list[str]:
    """Concatenate, drop case-insensitive repeats, cap. First spelling wins."""
    out: list[str] = []
    seen: set[str] = set()
    for group in values:
        for value in group or []:
            key = str(value).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(value)
            if len(out) >= limit:
                return out
    return out


def extract_profile(resumes: Sequence[Resume]) -> ExtractedProfile:
    """Fold a set of resumes into one view of the candidate."""
    ordered = _ordered(resumes)
    seniorities = [
        (r.seniority or "").strip().lower()
        for r in ordered
        if (r.seniority or "").strip().lower() in _SENIORITY_ORDER
    ]
    years = [r.years_experience for r in ordered if r.years_experience]
    return ExtractedProfile(
        resume_ids=[r.id for r in ordered if r.id is not None],
        full_name=next((r.full_name for r in ordered if r.full_name), None),
        location=next((r.location for r in ordered if r.location), None),
        # The strongest band the candidate can evidence — someone with a staff
        # resume and an IC one is a staff candidate who also applies to IC roles.
        seniority=max(seniorities, key=_SENIORITY_ORDER.index) if seniorities else None,
        years_experience=max(years) if years else None,
        skills=_union([r.skills for r in ordered], _MAX_PROFILE_SKILLS),
        titles=_union([r.target_roles for r in ordered], _MAX_ROLES),
    )


def merge_suggestions(resumes: Sequence[Resume]) -> PreferenceSuggestions:
    """One set of suggestions covering every resume the user has uploaded.

    Unions for the list fields, "any resume says so" for remote, and the *lowest*
    salary floor across the set. Inclusive on purpose: a second resume is the
    user saying "also consider me for this", and a merge that narrowed the search
    would punish them for saying it.
    """
    ordered = _ordered(resumes)
    if not ordered:
        return PreferenceSuggestions()
    if len(ordered) == 1:
        return suggest_preferences(ordered[0])

    each = [suggest_preferences(resume) for resume in ordered]
    out = PreferenceSuggestions()

    # Defaults and their notes are identical across resumes; take the first.
    out.sources.update(each[0].sources)
    out.notes.update(each[0].notes)

    out.target_roles = _union([s.target_roles for s in each], _MAX_ROLES)
    out.target_industries = _union(
        [s.target_industries for s in each], _MAX_INDUSTRIES
    )
    out.locations = _union([s.locations for s in each], _MAX_LOCATIONS)
    out.remote_only = any(s.remote_only for s in each)

    # A field is only "from the resume" if some resume actually informed it; the
    # union being empty means none did, so drop the claim rather than badge an
    # empty field.
    for name, values in (
        ("target_roles", out.target_roles),
        ("target_industries", out.target_industries),
        ("locations", out.locations),
    ):
        contributors = [s.sources.get(name) for s in each if s.sources.get(name)]
        if values and contributors:
            # "resume" (stated somewhere) outranks "inferred" (derived).
            out.sources[name] = "resume" if "resume" in contributors else "inferred"
        else:
            out.sources.pop(name, None)
            out.notes.pop(name, None)
    if out.remote_only:
        out.sources["remote_only"] = "resume"
        out.notes["remote_only"] = "One of your resumes lists your location as remote."
    else:
        out.sources.pop("remote_only", None)
        out.notes.pop("remote_only", None)

    floors = [(s.salary_min, s.sources.get("salary_min")) for s in each if s.salary_min]
    if floors:
        # Stated expectations win over inferred bands; within either, the lowest
        # floor keeps the most jobs visible.
        stated = [value for value, source in floors if source == "resume"]
        out.salary_min = min(stated) if stated else min(value for value, _ in floors)
        out.sources["salary_min"] = "resume" if stated else "inferred"
        out.notes["salary_min"] = (
            "The lowest expectation stated across your resumes."
            if stated
            else "A rough floor from your experience — set it to what you'd accept."
        )
    else:
        out.sources.pop("salary_min", None)
        out.notes.pop("salary_min", None)

    # The per-resume wording no longer applies, but the distinction it drew does:
    # a note must still say whether the value was stated or guessed at.
    if "target_roles" in out.sources:
        out.notes["target_roles"] = (
            "From the titles across your resumes."
            if out.sources["target_roles"] == "resume"
            else "From the most recent job titles on your resumes."
        )
    if "target_industries" in out.sources:
        out.notes["target_industries"] = (
            "Industries named on your resumes."
            if out.sources["target_industries"] == "resume"
            else "Guessed from your skills — worth a second look."
        )
    if "locations" in out.sources:
        out.notes["locations"] = "Where your resumes say you are."

    return out
