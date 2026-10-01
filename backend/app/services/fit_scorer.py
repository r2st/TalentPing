"""Job–candidate fit scoring — a 0-100 verdict the candidate can act on.

Weights:

    skills 30 · role 25 · experience 15 · location 12 · salary 8 · industry 10

The scoring itself is **deterministic**. That is a deliberate choice: a candidate
who sees 72 today must see 72 tomorrow, the dashboard aggregates these numbers,
and "why did it drop 9 points?" needs an answer that isn't "the model felt
different". An LLM is used only to write the human-readable summary, and never to
move the number.

Dimensions the posting doesn't mention are *neutral*, not zero — a JD with no
salary band shouldn't drag a strong match down to 60. Neutral means the
dimension scores 0.7 and says so in its note.

Neutrality is load-bearing, and it used to be unbounded, which was a bug worth
spelling out. The original table had no dimension asking *is this even the kind
of job this person does?* — it scored skills, seniority, location, pay and
sector, all of which go neutral on a thin posting. A posting nothing could be
read off scored 0.7 across the board, which is exactly 70.0/100: the default
``min_fit_score`` an autopilot user applies at. Tag it remote and location went
to 1.0, lifting it to 74.5 — "good". That is how a Registered Nurse posting
earned an application from a backend engineer. Two things close it:

* :func:`score_role` compares the posting's *title* against the roles the
  candidate is actually chasing. It is the one dimension that scores near-zero
  rather than neutral on a clear mismatch, because an unrelated title is
  evidence, not an absence of it.
* :data:`UNEVALUATED_CAP` catches the rest. When neither skills nor role could
  be judged, the posting is not a good match — it is an *unknown* one, and it is
  capped below the "good" band rather than allowed to coast on neutrality.

Every dimension reads the resume *and* :class:`Targeting` — what the candidate
actually asked for, from a :class:`~app.models.profile.Profile` or from their
autopilot preferences. The distinction matters most for location. A resume says
where someone *is*; only the preferences say where they will *work*. Scoring
against the resume alone meant a candidate living in Berlin who had written down
"London, remote" saw Berlin roles score 1.0 and London roles score 0.5 — the
opposite of what they asked for, from a dimension that looked like it was
answering the question. Stated locations win when there are any; the resume's own
location is the fallback for a candidate who never said.

On top of that sits :func:`rerank` — Scout's second opinion. Keyword overlap
cannot see that a payments engineer moving into infrastructure is a natural next
step, or that a "junior" title at a company growing 3× is a better year than a
senior title at a company that isn't. So the deterministic score stays the
*filter* (fast, reproducible, runs on every posting) and the LLM runs only over
the handful that survive it, writing a separate ``llm_fit_score`` that never
edits the number the feed sorts and reports on.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
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
from app.services.places import (
    names_arrangement,
    place_covers,
    place_tokens,
    places_overlap,
)
from app.services.resume_parser import split_remote
from app.services.skill_aliases import (
    SKILL_ALIAS_INDEX,
    canonical_skill,
    normalize_text,
)

#: The name this module has always compared under. Re-exported rather than
#: redefined: `resume_tailor` and `interview_prep_service` import
#: `normalize_text` from here, and a second definition is how the haystack and
#: its matcher stopped agreeing the last time.
_normalize = normalize_text

logger = logging.getLogger(__name__)

WEIGHTS: dict[str, float] = {
    "skills": 0.30,
    "role": 0.25,
    "experience": 0.15,
    "location": 0.12,
    "salary": 0.08,
    "industry": 0.10,
}

# What a dimension scores when the posting simply doesn't say. Not 0 (which would
# punish the candidate for the employer's omission) and not 1 (which would make
# vague postings look like perfect matches).
NEUTRAL = 0.7

# The ceiling on a posting no dimension could actually judge — neither its skills
# nor its title told us anything about whether the candidate belongs in it. Sits
# below the "good" band (65) and below every default apply threshold, so an
# unreadable posting shows up as "worth a look" rather than "worth an email".
UNEVALUATED_CAP = 55.0

_SENIORITY_RANK = {"junior": 1, "mid": 2, "senior": 3, "lead": 4, "exec": 5}


@dataclass
class Targeting:
    """What the candidate said they want, independent of what their resume says.

    Built from a :class:`~app.models.profile.Profile` or, for a user who has
    none, from their :class:`~app.models.autopilot.AutopilotPreference`. Every
    field is optional and an empty one means "no opinion" — the dimension falls
    back to reading the resume, which is what the scorer did before profiles
    existed.

    Keeping this separate from the resume is the point. The resume is evidence
    about the past; this is a statement about the future, and where the two
    disagree the statement wins.
    """

    roles: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    # Skills this intent leads with — a subset or restatement of the resume's,
    # emphasising the half that argues for *these* roles.
    skills: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_only: bool = False
    salary_min: int | None = None
    salary_max: int | None = None
    seniority: str | None = None

    # ---- Constraints the score does not weigh ----
    # These three are never scored. They are read by the gates in
    # :mod:`app.services.search_filters`, which run *after* the score and cannot
    # be outvoted by it — the point of a constraint is that a strong showing
    # elsewhere does not buy an exception. They ride on ``Targeting`` because
    # every gate already receives one, and a second parameter threaded through
    # the pipeline for the same profile's fields would be the same object under
    # another name.
    employment_types: list[str] = field(default_factory=list)
    company_sizes: list[str] = field(default_factory=list)
    excluded_companies: list[str] = field(default_factory=list)

    # Carried for callers that want to name the source in a note or a log line.
    label: str | None = None

    @classmethod
    def from_profile(cls, profile) -> Targeting:
        return cls(
            roles=list(profile.target_roles or []),
            industries=list(profile.target_industries or []),
            skills=list(profile.skills or []),
            locations=list(profile.location_preferences or []),
            remote_only=bool(profile.remote_only),
            salary_min=profile.salary_min,
            salary_max=profile.salary_max,
            seniority=profile.experience_level,
            employment_types=list(profile.employment_types or []),
            company_sizes=list(profile.company_sizes or []),
            excluded_companies=list(profile.excluded_companies or []),
            label=profile.name,
        )

    @classmethod
    def from_preference(cls, pref) -> Targeting:
        """The pre-profile fallback: the single global preference row."""
        return cls(
            roles=list(pref.target_roles or []),
            industries=list(pref.target_industries or []),
            locations=list(pref.locations or []),
            remote_only=bool(pref.remote_only),
            salary_min=pref.salary_min,
            label=None,
        )


# The "no opinion on file" targeting, so every dimension can take one
# unconditionally instead of branching on None.
NO_TARGETING = Targeting()


@dataclass
class Dimension:
    score: float  # 0..1
    note: str
    # False when the dimension fell back to NEUTRAL because there was nothing to
    # judge, rather than because it judged and found the middle. The overall
    # score needs the difference: "we looked and it's average" and "we couldn't
    # look" must not both read as 0.7 with no consequence.
    known: bool = True


@dataclass
class FitResult:
    overall: float  # 0..100
    skills: Dimension
    role: Dimension
    experience: Dimension
    location: Dimension
    salary: Dimension
    industry: Dimension

    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    recommendation: str = "poor"
    summary: str = ""
    # Set when UNEVALUATED_CAP held the score down, so callers can say why.
    capped: bool = False

    def notes(self) -> dict[str, str]:
        return {
            "skills": self.skills.note,
            "role": self.role.note,
            "experience": self.experience.note,
            "location": self.location.note,
            "salary": self.salary.note,
            "industry": self.industry.note,
        }

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["notes"] = self.notes()
        return data


def _mentions(haystack: str, needle: str) -> bool:
    token = _normalize(needle)
    if not token:
        return False
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", haystack))


# Three spellings in :data:`~app.services.skill_aliases.SKILL_ALIASES` are also
# ordinary English words, and the
# expansion made that cost real. A posting asking for "Golang" now also looks
# for a bare "go", which is the match the group exists for — a resume that only
# ever writes "Go" is a Go resume — but "go" is the commonest verb in the
# language, so "the go-to engineer for payments", "owned go-live for three
# regions" and "ready to go above and beyond" all counted as eight years of it.
# "React quickly to production incidents" counted as the view library, and
# "kept the project on the rails" as the web framework.
#
# It lands on skills, the heaviest dimension at 0.30, and it lands silently: a
# false match is precisely a skill that never reaches ``missing_skills``, so
# the candidate is never shown it, the tailoring prompt is never asked to close
# it, and `skills_gap` cannot sum what was never written down.
#
# What separates the two senses is grammar rather than vocabulary — the tool is
# a bare noun, and the English word sits in a phrase that gives it away. Each
# entry below names only those phrasings, so everything else keeps matching as
# it did.
#
# Deliberately one-sided, and deliberately partial. Being wrong the other way
# means telling a candidate with five years of Go to go and learn Go, which is
# the failure the alias groups exist to stop; so a phrasing that is genuinely
# ambiguous is left matching rather than guessed at. "Migrated the admin to
# React to cut bundle size" and "we react to feedback" are the same four words
# in the same order, and only one of them is the library — neither is filtered.
@dataclass(frozen=True)
class _Ambiguity:
    """The phrasings that make one spelling the English word, not the tool."""

    #: Word immediately before it.
    before: frozenset[str] = frozenset()
    #: Word immediately after it.
    after: frozenset[str] = frozenset()
    #: A two-word run before it — ``("to", {"ready"})`` reads "ready to go".
    #: Some idioms put the giveaway one word further out than the last.
    before_pair: tuple[str, frozenset[str]] | None = None


_AMBIGUOUS_SPELLINGS: dict[str, _Ambiguity] = {
    # The verb takes a particle; the language does not. Leaves "built services
    # in Go and Python", "migrated from Ruby to Go", "Go Developer" and a bare
    # skills-list entry matching exactly as before.
    "go": _Ambiguity(
        after=frozenset(
            {
                "to", "live", "above", "beyond", "through", "back", "away",
                "wrong", "further", "public", "over", "into", "under",
                "forward", "hand", "straight", "off", "out", "up", "down",
                "unnoticed", "unanswered",
            }
        ),
        before_pair=("to", frozenset({"ready", "good", "able", "willing", "raring", "set"})),
    ),
    # Only the two phrasings that cannot be the library. "React to" on its own
    # is not one of them — see the note above.
    "react": _Ambiguity(
        after=frozenset(
            {
                "quickly", "rapidly", "immediately", "swiftly", "promptly",
                "instantly", "appropriately", "accordingly", "decisively",
                "calmly",
            }
        ),
        before_pair=("to", frozenset({"able", "ready", "quick", "slow", "fast", "unable"})),
    ),
    # "guard rails", "on the rails", "off the rails". Not a bare "the": "the
    # Rails app" and "the Rails upgrade" are the framework, so the idiom is
    # matched on all three of its words.
    "rails": _Ambiguity(
        before=frozenset({"guard", "guide", "guard-rails"}),
        before_pair=("the", frozenset({"on", "off"})),
    ),
    # "spark a redesign", "spark interest". The engine is a bare noun; the verb
    # takes an object, and the objects are abstractions rather than systems.
    # Listed here because a resume can say the word without the platform being
    # anywhere near it — which was already true before the "Apache Spark" group
    # above was added, and is now reachable from a posting that spells it out.
    "spark": _Ambiguity(
        after=frozenset(
            {
                "joy", "interest", "innovation", "ideas", "conversations",
                "conversation", "change", "growth", "curiosity", "debate",
                "discussion", "creativity", "a", "an", "the", "new",
            }
        ),
    ),
}

#: The same boundary `_mentions` enforces with lookaround, as a token list, so
#: the two cannot disagree about what counts as an occurrence. The trailing
#: group catches a full stop, which `_normalize` keeps: a particle that opens
#: the next sentence is not this sentence's, and it is also what stops
#: "React.js" from being read as the verb "react" followed by "js".
_WORD_RE = re.compile(r"([a-z0-9]+)(\.?)")


def _names_the_tool(haystack: str, spelling: str) -> bool:
    """Whether any occurrence of *spelling* is the tool and not the English word.

    One is enough: a resume that says "ready to go above and beyond" and also
    "services written in Go" is a Go resume.
    """
    rule = _AMBIGUOUS_SPELLINGS[spelling]
    words = _WORD_RE.findall(haystack)
    for i, (word, stop) in enumerate(words):
        if word != spelling:
            continue
        after = None if stop else (words[i + 1][0] if i + 1 < len(words) else None)
        if after is not None and after in rule.after:
            continue
        if i and words[i - 1][0] in rule.before:
            continue
        if rule.before_pair is not None:
            joiner, openers = rule.before_pair
            if i >= 2 and words[i - 1][0] == joiner and words[i - 2][0] in openers:
                continue
        return True
    return False



def skill_mentioned(haystack: str, skill: str) -> bool:
    """Whether *haystack* names *skill* under any spelling it is written with.

    Public for the same reason :func:`canonical_skill` is: this is not the only
    place a posting's spelling has to be reconciled with the candidate's.
    :mod:`app.services.resume_tailor` splits the same posting against the same
    resume to decide what the tailored documents may say, and a second, simpler
    matcher there meant the fit score credited "Postgres" while the tailor
    screen listed it as a gap.

    Plurals are tried alongside the alias group, and for the same reason: a
    posting asking for "Microservices" against a resume that wrote
    "microservice" is one document's house style, not a gap in someone's
    experience. Only the trailing "s", and only on tokens long enough that
    removing it leaves a word — so "aws" and "js" are left alone, and the
    manufactured forms that are not words ("kubernete") simply match nothing.

    The spellings in :data:`_AMBIGUOUS_SPELLINGS` are read by
    :func:`_names_the_tool` rather than matched flat, because they are also
    ordinary English words.
    """
    token = _normalize(skill)
    if not token:
        return False
    for spelling in SKILL_ALIAS_INDEX.get(token, (token,)):
        if spelling in _AMBIGUOUS_SPELLINGS:
            if _names_the_tool(haystack, spelling):
                return True
            continue
        if _mentions(haystack, spelling):
            return True
        if len(spelling) > 3 and spelling.endswith("s"):
            if _mentions(haystack, spelling[:-1]):
                return True
        elif len(spelling) > 3 and _mentions(haystack, spelling + "s"):
            return True
    return False


# --------------------------------------------------------------------------- #
# Role relevance                                                               #
# --------------------------------------------------------------------------- #

# Words that describe the *shape* of a job rather than the job itself. Stripped
# before two titles are compared, so "Senior Backend Engineer (Remote, f/m/d)"
# and "Backend Engineer" are recognised as the same role while "Backend
# Engineer" and "Registered Nurse" stay firmly apart.
#
# Seniority words go in here too. That is deliberate: this dimension answers
# "same line of work?" and :func:`score_experience` already answers "right
# level?". Counting seniority twice would penalise it twice.
_TITLE_NOISE = frozenset(
    (
        # Level — score_experience's question, not this one's.
        "senior", "sr", "junior", "jr", "staff", "principal", "lead", "entry",
        "level", "mid", "associate", "chief", "head", "vp", "president",
        "director", "i", "ii", "iii", "iv", "v", "x",
        # Working arrangement — score_location's question.
        "remote", "hybrid", "onsite", "anywhere", "worldwide", "distributed",
        "relocation", "full", "part", "time", "contract", "permanent",
        "freelance", "temporary", "fte", "perm",
        # Glue and posting boilerplate, including the "(m/f/d)" German boards
        # append to every title.
        "the", "a", "an", "and", "or", "of", "for", "to", "in", "at", "with",
        "our", "we", "you", "your", "us", "new", "team", "role", "position",
        "opening", "job", "jobs", "hiring", "urgent", "apply", "now",
        "m", "f", "d", "w", "h",
    )
)

# Titles for the same work differ mostly by vocabulary, so a handful of
# equivalences buys more precision than any amount of fuzzy string distance.
# Kept small and one-directional on purpose — every entry here is a claim that
# two words mean the same job, and a wrong one silently widens the net.
_TITLE_SYNONYMS = {
    "developer": "engineer",
    "dev": "engineer",
    "programmer": "engineer",
    "engineering": "engineer",
    "swe": "engineer",
    "sde": "engineer",
    "coder": "engineer",
    "backend": "back-end",
    "frontend": "front-end",
    "fullstack": "full-stack",
    "devops": "infrastructure",
    "sre": "infrastructure",
    "platform": "infrastructure",
    "ml": "machine-learning",
    "ai": "machine-learning",
    "analytics": "data",
    "ux": "design",
    "ui": "design",
    "designer": "design",
    "pm": "product",
    "qa": "quality",
    "sdet": "quality",
}

# The same equivalences, for the spellings that take more than one word.
#
# Every canonical value in :data:`_TITLE_SYNONYMS` above is a single token, and
# a single token is only ever produced by a single word — so the table's claims
# were reachable from exactly one spelling of each role. "ML Engineer" became
# ``machine-learning engineer``; "Machine Learning Engineer" stayed three
# separate words and shared nothing with it but "engineer". The two titles that
# the table exists to call identical scored 0.65, *adjacent*.
#
# "SRE" against "Site Reliability Engineer" was the same omission with the worst
# possible ending: ``infrastructure`` against ``site reliability engineer``
# overlaps in nothing at all, so :func:`score_role` returned 0.05 — "not a role
# you're targeting" — for the exact job the candidate had written down.
#
# It is not a hypothetical pairing. :data:`app.services.role_expansion._SYNONYMS`
# deliberately searches boards for "SRE", "ML Engineer", "Back End Developer" and
# "Front End Developer" on behalf of candidates who wrote the long forms; every
# posting that widening found came back to a scorer that marked it down for
# using the vocabulary we asked for.
#
# Collapsed before the single-word table is consulted, so both spellings arrive
# at the same token and the claim only has to be made once. Nothing new is
# asserted here — each entry is the multi-word form of an equivalence the table
# above already states.
_TITLE_PHRASES = {
    "machine learning": "machine-learning",
    "deep learning": "machine-learning",
    "artificial intelligence": "machine-learning",
    "back end": "back-end",
    "front end": "front-end",
    "full stack": "full-stack",
    "site reliability": "infrastructure",
    "quality assurance": "quality",
    "user experience": "design",
    "user interface": "design",
}

# Longest first, so "machine learning" is not clipped by a shorter phrase that
# starts inside it. Word-boundary guarded for the same reason `_mentions` is:
# "backend" must not be rewritten from the "back end" sitting inside it.
_TITLE_PHRASE_RE = re.compile(
    r"(?<![a-z0-9])("
    + "|".join(
        re.escape(phrase)
        for phrase in sorted(_TITLE_PHRASES, key=len, reverse=True)
    )
    + r")(?![a-z0-9])"
)

# The head nouns that name a *craft* rather than a job. Sharing one of these and
# nothing else is the weakest overlap two titles can have and still have one.
#
# It matters because the "same role" test below divides by the length of the
# shorter title, and a title one token long passes that test on its only word.
# So a posting advertised as, simply, "Engineer" scored 1.0 — "is the role
# you're targeting" — against *every* target ending in the same word: against
# "AI Engineer", against "Backend Engineer", against both at once. "Manager"
# did it to "Engineering Manager"; "Developer" did it to anything.
#
# The result was upside down. "Software Engineer" against an "AI Engineer"
# target scored 0.65, because it had the decency to say which kind — so a
# posting that told us *more* about itself scored *worse* than one that told us
# nothing. The dimension was rewarding the absence of information, at a quarter
# of the fit score, on a number that decides whether autopilot writes to a
# stranger.
#
# These words are drawn from the canonical values `_TITLE_SYNONYMS` already
# collapses to, plus the handful of other nouns that end job titles across the
# whole market. Deliberately *not* here: "product", "data", "quality",
# "infrastructure", "machine-learning" and the rest. Those name the job. "PM"
# reducing to `product` and "SRE" to `infrastructure` are one-token titles that
# still say which job, and they stay perfect matches for the roles they abbreviate.
_GENERIC_CRAFT = frozenset(
    {
        "engineer",
        "manager",
        "design",
        "analyst",
        "scientist",
        "architect",
        "consultant",
        "specialist",
        "administrator",
        "coordinator",
        "technician",
    }
)


def _title_tokens(title: str | None) -> list[str]:
    """A title reduced to the words that say what the job *is*.

    Order is preserved and duplicates dropped, because the last surviving token
    is treated as the head noun — "engineer" in "senior backend engineer",
    "nurse" in "registered nurse" — and that is what carries the comparison.
    """
    out: list[str] = []
    # Phrases first: they span the spaces `split` is about to cut on, so there
    # is no later point at which "site reliability" is still one thing.
    normalized = _TITLE_PHRASE_RE.sub(
        lambda m: _TITLE_PHRASES[m.group(1)], _normalize(title or "")
    )
    for raw in normalized.split():
        token = _TITLE_SYNONYMS.get(raw)
        if token is None:
            # Plurals only after the synonym lookup, so "devops" isn't mangled
            # into "devop" before it can be recognised.
            singular = raw[:-1] if len(raw) > 3 and raw.endswith("s") else raw
            token = _TITLE_SYNONYMS.get(singular, singular)
        if token in _TITLE_NOISE or len(token) < 2:
            continue
        if token not in out:
            out.append(token)
    return out


def title_relevance(job_title: str | None, target: str) -> float:
    """How close *job_title* is to one role the candidate wants, 0..1.

    Three bands rather than a continuous score, because the underlying signal is
    coarse and a smooth number would imply a precision it doesn't have:

    ``1.0``  same role — the titles agree once noise is stripped.
    ``0.65`` adjacent — different specialism, same craft ("Python Developer"
             against a "Backend Engineer" target). Worth surfacing; the skills
             dimension decides whether it is worth applying to.
    ``0.3``  loose — a word in common but a different head noun.
    ``0.0``  unrelated — nothing in common at all.
    """
    job = _title_tokens(job_title)
    want = _title_tokens(target)
    if not job or not want:
        return 0.0

    shared = set(job) & set(want)
    if not shared:
        return 0.0
    # Identical once the noise is gone: nothing left to distinguish them.
    if set(job) == set(want):
        return 1.0
    # "Almost entirely the same title" — but only when the overlap includes
    # something that names the *job*. Two titles whose only shared word is the
    # craft they belong to are adjacent at best, however short either one is.
    if shared - _GENERIC_CRAFT and len(shared) / min(len(job), len(want)) >= 0.75:
        return 1.0
    # Same head noun ("engineer", "nurse", "designer") with different modifiers.
    return 0.65 if job[-1] == want[-1] else 0.3


def role_targets(resume: Resume, targeting: Targeting | None = None) -> list[str]:
    """The titles this candidate is plausibly in the market for.

    A profile's roles *replace* the resume's rather than leading them. Merging
    the two sounds generous and quietly breaks the feature: two profiles backed
    by the same document would both inherit its titles, both score the same on
    every posting, and "which profile matched?" would stop meaning anything. When
    someone has written down what this profile is for, that is what it is for.

    Without stated roles the resume answers instead — its own targets first (the
    candidate's words about where they are going), then history and headline for
    the many resumes that never state one.
    """
    stated = [r for r in ((targeting.roles if targeting else []) or []) if r.strip()]
    if stated:
        return stated

    out: list[str] = []
    seen: set[str] = set()
    sources = [
        *(resume.target_roles or []),
        *(
            e.get("title", "")
            for e in (resume.experience or [])[:5]
            if isinstance(e, dict)
        ),
        resume.headline or "",
    ]
    for title in sources:
        if not isinstance(title, str):
            continue
        key = " ".join(_title_tokens(title))
        if key and key not in seen:
            seen.add(key)
            out.append(title.strip())
    return out


def best_role_match(job_title: str | None, targets: list[str]) -> tuple[float, str | None]:
    """The closest of *targets* to *job_title*, as ``(relevance, target)``."""
    best: tuple[float, str | None] = (0.0, None)
    for target in targets:
        score = title_relevance(job_title, target)
        if score > best[0]:
            best = (score, target)
    return best


def score_role(
    resume: Resume, job: ParsedJob, targeting: Targeting | None = None
) -> Dimension:
    """Is this the kind of job the candidate is actually looking for?

    The dimension the scorer was missing, and the reason irrelevant postings
    used to clear the bar. Unlike every other dimension, a confident *mismatch*
    scores near zero rather than neutral: a title in a different line of work is
    a fact about the posting, not a gap in it.
    """
    targets = role_targets(resume, targeting)
    if not targets:
        return Dimension(NEUTRAL, "No target roles on file to compare against.", known=False)
    if not _title_tokens(job.title):
        return Dimension(NEUTRAL, "The posting has no usable job title.", known=False)

    score, matched = best_role_match(job.title, targets)
    title = job.title
    if score >= 1.0:
        return Dimension(1.0, f"'{title}' is the role you're targeting ({matched}).")
    if score >= 0.65:
        return Dimension(0.65, f"'{title}' is adjacent to your target ({matched}).")
    if score > 0:
        return Dimension(0.3, f"'{title}' only loosely overlaps {matched}.")
    return Dimension(
        0.05, f"'{title}' is not a role you're targeting ({', '.join(targets[:3])})."
    )


# --------------------------------------------------------------------------- #
# Dimensions                                                                   #
# --------------------------------------------------------------------------- #


def score_skills(
    resume: Resume, job: ParsedJob, targeting: Targeting | None = None
) -> tuple[Dimension, list[str], list[str]]:
    """Overlap between the posting's asks and the candidate's actual resume.

    Required skills carry full weight; preferred skills count for a third. The
    match runs against the whole resume text, not just the skills list, so a tool
    named only in a job bullet still counts — and against the profile's own
    skills, which are the same kind of claim made one level closer to the job
    being applied for.
    """
    haystack = _normalize(
        " ".join(
            [
                resume.raw_text or "",
                resume.summary or "",
                resume.headline or "",
                " ".join(resume.skills or []),
                " ".join(targeting.skills if targeting else []),
                " ".join(
                    f"{e.get('title', '')} {e.get('company', '')}"
                    for e in (resume.experience or [])
                ),
            ]
        )
    )

    # ``s.strip()``, not ``s``. A blank entry is truthy the moment it holds a
    # space, and a skill that normalises to nothing can never be found in any
    # haystack — so it is scored as a required skill the candidate is missing,
    # permanently and by construction. One of those on a two-skill posting
    # halves the heaviest dimension in the table, and the blank is then printed
    # into ``missing_skills``: the "Not on your resume" list the candidate
    # reads, the gap the tailoring prompt is told to close, and a line in the
    # summary prompt. A non-breaking space off a scraped posting is the shape
    # this arrives in.
    # ``isinstance`` as well, because the truthiness test it replaces also
    # happened to screen out a stray ``None``, and ``.strip()`` would raise on
    # one. Same guard `location_match` puts on the other list of user strings.
    required = [
        s for s in dict.fromkeys(job.required_skills) if isinstance(s, str) and s.strip()
    ]
    preferred = [
        s
        for s in dict.fromkeys(job.preferred_skills)
        if isinstance(s, str) and s.strip() and s not in required
    ]

    if not required and not preferred:
        return (
            Dimension(NEUTRAL, "The posting lists no specific skills to match on.", known=False),
            [],
            [],
        )

    matched: list[str] = []
    missing: list[str] = []
    earned = 0.0
    possible = 0.0

    for skill in required:
        possible += 1.0
        if skill_mentioned(haystack, skill):
            earned += 1.0
            matched.append(skill)
        else:
            missing.append(skill)

    for skill in preferred:
        possible += 0.33
        if skill_mentioned(haystack, skill):
            earned += 0.33
            matched.append(skill)
        else:
            missing.append(skill)

    ratio = earned / possible if possible else NEUTRAL
    required_hits = sum(1 for s in required if s in matched)
    note = (
        f"{required_hits} of {len(required)} required skills present"
        if required
        else f"{len(matched)} of {len(preferred)} preferred skills present"
    )
    if missing:
        note += f"; missing {', '.join(missing[:4])}"
    return Dimension(round(min(1.0, ratio), 4), note), matched, missing


def score_experience(
    resume: Resume, job: ParsedJob, targeting: Targeting | None = None
) -> Dimension:
    """Seniority and year-count alignment.

    Being *over*-qualified is penalised, but far more gently than being under:
    a senior applying to a mid role is a plausible choice, a junior applying to a
    staff role is usually not.

    The level compared is the one the candidate is applying at, which is the
    profile's when it has one — someone stepping up to lead has a senior resume
    and a lead intent, and it is the intent they want matched.
    """
    have_years = resume.years_experience
    want_years = job.years_required
    have_level = (targeting.seniority if targeting else None) or resume.seniority
    have_rank = _SENIORITY_RANK.get((have_level or "").lower())
    want_rank = _SENIORITY_RANK.get((job.seniority or "").lower())

    if want_years is None and want_rank is None:
        return Dimension(
            NEUTRAL, "The posting doesn't state a required experience level.", known=False
        )

    scores: list[float] = []
    notes: list[str] = []

    if want_years is not None and have_years is not None:
        if have_years >= want_years:
            excess = have_years - want_years
            # >8 years past the ask starts reading as a mismatch to a recruiter.
            scores.append(1.0 if excess <= 8 else 0.75)
            notes.append(f"{have_years} yrs vs {want_years} required")
        else:
            gap = want_years - have_years
            scores.append(max(0.0, 1.0 - gap * 0.2))
            notes.append(f"{have_years} yrs vs {want_years} required (short by {gap})")
    elif want_years is not None:
        scores.append(NEUTRAL)
        notes.append(f"asks for {want_years} yrs; your resume doesn't state a total")

    if want_rank is not None and have_rank is not None:
        delta = have_rank - want_rank
        if delta == 0:
            scores.append(1.0)
        elif delta > 0:
            scores.append(max(0.5, 1.0 - delta * 0.2))
        else:
            scores.append(max(0.0, 1.0 + delta * 0.35))
        notes.append(f"{have_level} vs {job.seniority} level")
    elif want_rank is not None:
        scores.append(NEUTRAL)
        notes.append(f"{job.seniority}-level role")

    score = sum(scores) / len(scores) if scores else NEUTRAL
    return Dimension(round(score, 4), "; ".join(notes) or "Experience level looks aligned.")


def looks_remote(location: str | None) -> bool:
    """Whether a location string is really a working arrangement.

    Boards disagree about which field carries this: some set a structured remote
    flag, some only ever write "Remote" or "Anywhere" where the city goes. Both
    mean the role travels, so both are read the same way.

    The vocabulary moved to :func:`app.services.places.names_arrangement`, which
    is where :func:`app.services.ats_boards._looks_remote` — the other reader of
    a location field — now gets it too. It was four English words here, and this
    feed is remote-first: a board writing "Télétravail" or "Praca zdalna" in the
    city slot got the *disqualifying* answer, on this dimension and in
    `auto_apply_service.location_gate` both.
    """
    return names_arrangement(location)


def location_match(job_location: str, wanted: list[str]) -> str | None:
    """The first of *wanted* that covers *job_location*, or ``None``.

    Containment, not equality — see :func:`app.services.places.place_covers`. A
    shared word still carries most of it, so "London" matches "London, UK" and
    "Greater London Area", the same place under three boards' worth of
    formatting. But a candidate who names an *area* is naming everything in it,
    and matching by shared word alone could not see that: "usa" and "Concord, CA"
    have no word in common, so a candidate who wrote "usa" was told that no job
    in America was one of their locations. On production that vetoed 24 replies
    to real recruiters, and dragged the location dimension of every US role from
    1.0 to 0.15, which pulled the fit score down far enough to matter twice.
    """
    if not place_tokens(job_location):
        return None
    for place in wanted:
        if not isinstance(place, str) or not place.strip():
            continue
        if looks_remote(place):
            continue
        if place_covers(place, job_location):
            return place.strip()
    return None


def score_location(
    resume: Resume, job: ParsedJob, targeting: Targeting | None = None
) -> Dimension:
    """Does the role sit somewhere the candidate will actually work?

    Remote-first: a remote role fits anyone, so it scores full marks, and that
    single rule is what makes a remote-only candidate's feed work at all.

    Everything else is judged against the places the candidate *named* —
    ``targeting.locations`` — falling back to the location on their resume when
    they never named any. This ordering is the whole point of the dimension. The
    resume says where they live; the preferences say where they'll work, and for
    anyone who has moved, is moving, or would commute, those are different
    answers. A stated preference that the posting misses is a real miss (0.15),
    not the soft 0.5 an unstated one earns, because the candidate told us and
    the posting still doesn't qualify.

    The resume's location is not always a place. ``extract_location`` carries a
    remote marker through in the same string, deliberately, so
    ``suggest_locations`` can read it — which means this fallback has to take it
    back off before it compares anything. It did not, and asked
    :func:`app.services.places.places_overlap` whether Berlin and "Remote" were
    the same city. The answer was no, which is the score this branch wanted
    anyway; the sentence attached to it — "you're in Remote" — named a place
    that does not exist and claimed ``known``.
    """
    remote_only = bool(targeting.remote_only) if targeting else False
    wanted = [p for p in (targeting.locations if targeting else []) if p]

    if job.remote is True:
        return Dimension(1.0, "Remote role — location is not a constraint.")

    job_location = (job.location or "").strip()
    if looks_remote(job_location):
        return Dimension(1.0, "Remote role — location is not a constraint.")

    # A remote-only candidate looking at a role that isn't remote: the one case
    # where the posting is disqualified rather than merely marked down. Said
    # plainly so the note explains the score instead of hinting at it.
    if remote_only:
        return Dimension(
            0.05,
            f"You're looking for remote work; this role is "
            f"{f'in {job_location}' if job_location else 'not listed as remote'}.",
        )

    if not job_location:
        return Dimension(NEUTRAL, "The posting doesn't state a location.", known=False)

    if wanted:
        matched = location_match(job_location, wanted)
        if matched:
            return Dimension(1.0, f"{job_location} is one of your locations ({matched}).")
        return Dimension(
            0.15,
            f"{job_location} isn't one of your locations ({', '.join(wanted[:3])}).",
        )

    # "Remote" is a working arrangement the candidate wrote where the city goes,
    # and ``extract_location`` carries it through on purpose. It is not a place,
    # and it has to come off before anything here compares one.
    candidate_location, resume_remote = split_remote(resume.location)
    if candidate_location is None:
        if resume_remote:
            # The resume names no place at all — it names an arrangement. The
            # scores are the ones the miss already earned, because a candidate
            # who works remotely is no better matched to a Berlin office than a
            # candidate who lives elsewhere; what was wrong was the sentence
            # explaining them, which asserted a city called Remote.
            #
            # Not routed through the ``remote_only`` branch above, and not
            # scored like it. That branch is a preference the candidate *set*,
            # and it is worth 0.05 because they set it. This is a word on a
            # resume that nobody has confirmed, and ``targeting.remote_only``
            # being false may well be them having said no.
            return Dimension(
                0.25 if job.remote is False else 0.5,
                f"Your resume says remote and names no place; this role is "
                f"{f'on-site in {job_location}' if job.remote is False else f'in {job_location}'}.",
            )
        return Dimension(
            NEUTRAL, f"Role is in {job_location}; your resume has no location.", known=False
        )

    if places_overlap(job_location, candidate_location):
        return Dimension(1.0, f"Both in {job_location}.")
    if job.remote is False:
        return Dimension(0.25, f"On-site in {job_location}; you're in {candidate_location}.")
    return Dimension(0.5, f"Role is in {job_location}; you're in {candidate_location}.")


def score_salary(
    resume: Resume, job: ParsedJob, targeting: Targeting | None = None
) -> Dimension:
    """Compare the posted band against what the candidate expects.

    A stated floor is used when there is one — a number the candidate chose beats
    one worked out from their seniority, and it is the only figure here that is
    not a guess. Missing it is scored on how far short the posting falls, so a
    band a little under the ask is a stretch rather than a rejection.

    Without a stated floor the expectation is inferred from seniority, and the
    dimension stays deliberately forgiving: it flags bands that look low for the
    level rather than pretending to know what any individual wants.
    """
    if job.salary_min is None and job.salary_max is None:
        return Dimension(NEUTRAL, "The posting doesn't publish a salary band.", known=False)

    top = job.salary_max or job.salary_min or 0

    # ``is not None``, not truthiness. ``salary_min`` is validated ``ge=0`` on
    # both the profile and the preference schema, so **zero is an accepted
    # value** and it is a different statement from leaving the field empty: it
    # says "pay is not what I am filtering on", and every band on earth clears
    # it. Read as falsy, it fell through to the seniority guess below — the one
    # branch this function's docstring says a stated floor exists to beat — and
    # a candidate who had explicitly stopped filtering on pay was told their
    # $100,000 band "looks low for a senior candidate" and scored 0.91 instead
    # of 1.0. A guess, printed as a finding, over the top of an answer the user
    # had already given.
    wanted = targeting.salary_min if targeting else None
    if wanted is not None:
        if top >= wanted:
            return Dimension(1.0, f"Band ({job.salary_text}) clears your {wanted:,} floor.")
        # Only reachable with a floor of zero against a *negative* band, which
        # is a misparse rather than an offer; guarded because the ratio would
        # otherwise divide by it.
        ratio = top / wanted if wanted else 0.0
        return Dimension(
            round(max(0.1, ratio), 4),
            f"Band ({job.salary_text}) is below your {wanted:,} floor.",
        )

    level = (targeting.seniority if targeting else None) or resume.seniority
    rank = _SENIORITY_RANK.get((level or "").lower())
    if rank is None:
        return Dimension(
            0.85, f"Band published ({job.salary_text}); no seniority on file to compare."
        )

    # Rough market floors by level, in **annual US dollars** — which is what
    # ``ParsedJob.salary_min/max`` hold, whatever the posting was written in.
    # :func:`app.services.jd_parser.extract_salary` annualises through
    # :data:`app.services.pay_period.PERIOD_MULTIPLIERS` and converts through
    # :data:`app.services.currency.CURRENCY_TO_USD` before the figure ever
    # reaches this module, so "¥8,000,000 per year" arrives as 53,600 and is
    # compared against the same scale as everything else. Only ``salary_text``
    # keeps the employer's own spelling, and it is display copy — never read
    # back as a number.
    floors = {1: 45_000, 2: 75_000, 3: 110_000, 4: 150_000, 5: 200_000}
    floor = floors[rank]

    if top >= floor:
        return Dimension(1.0, f"Band ({job.salary_text}) is at or above {level} level.")
    ratio = top / floor if floor else 0.0
    return Dimension(
        round(max(0.15, ratio), 4),
        f"Band ({job.salary_text}) looks low for a {level} candidate.",
    )


def industry_matches(job_industry: str, target: str) -> bool:
    """Whether *target* names the same sector the posting is in.

    Containment, but of *words* — the same rule :func:`_mentions` applies to
    every other dimension here, and the one this dimension was missing. It read
    ``job_industry in target or target in job_industry`` on the raw strings,
    which is substring containment, and short industry names are substrings of
    unrelated long ones: "ai" sits inside "retail", "maritime" and "training",
    and "it" inside "hospitality" and "security". A candidate targeting AI
    therefore scored a retail posting 1.0 on this dimension — worth a tenth of
    the overall score — under the note "retail is one of your target
    industries", which is not a near-miss but a plainly false sentence shown to
    the user.

    Both directions still match, because both are real: a "media" target covers
    a "social media" posting, and a "social media" target covers a "media" one.
    What no longer matches is a word fragment.
    """
    job_key, target_key = _normalize(job_industry), _normalize(target)
    if not job_key or not target_key:
        return False
    return _mentions(job_key, target_key) or _mentions(target_key, job_key)


def score_industry(
    resume: Resume, job: ParsedJob, targeting: Targeting | None = None
) -> Dimension:
    """Does the posting's sector match where the candidate has been aiming?"""
    job_industry = (job.industry or "").lower().strip()
    if not job_industry:
        return Dimension(NEUTRAL, "The posting doesn't signal a clear industry.", known=False)

    stated = (targeting.industries if targeting else []) or resume.target_industries
    targets = [i.lower() for i in (stated or [])]
    if any(industry_matches(job_industry, t) for t in targets):
        return Dimension(1.0, f"{job_industry} is one of your target industries.")

    # Not a stated target, but if they've worked there it still counts.
    history = _normalize(
        " ".join(
            f"{e.get('company', '')} {e.get('title', '')}"
            for e in (resume.experience or [])
        )
        + " "
        + (resume.raw_text or "")[:3000]
    )
    if _mentions(history, job_industry):
        return Dimension(0.8, f"You have {job_industry} exposure in your history.")
    if targets:
        return Dimension(
            0.45, f"{job_industry} isn't among your targets ({', '.join(targets[:3])})."
        )
    return Dimension(
        NEUTRAL, f"{job_industry} role; no industry preference on file.", known=False
    )


# --------------------------------------------------------------------------- #
# Aggregation                                                                  #
# --------------------------------------------------------------------------- #


def recommendation_for(score: float) -> str:
    """Bucket a 0-100 score into advice. Thresholds from research §2.2."""
    if score >= 80:
        return "strong"
    if score >= 65:
        return "good"
    if score >= 45:
        return "stretch"
    return "poor"


_SUMMARY_PROMPT = (
    "You explain a job-fit score to a job seeker in 2-3 short sentences. You are "
    "given the computed score and its breakdown — never contradict, recalculate "
    "or restate the numbers as different ones. Be direct and useful: say what "
    "makes this a good or bad use of their time, and what the biggest gap is. No "
    "greeting, no sign-off, no bullet points.\n\n"
    'Return ONLY a JSON object: {"summary": str}'
)


def _llm_summary(result: FitResult, job: ParsedJob) -> str | None:
    """Ask the model to phrase the verdict. Never lets it change the number.

    The response is requested as JSON rather than prose: the free reasoning
    models leak their scratchpad into the text field, and pulling one named key
    out of an object is immune to that.
    """
    if not llm_is_configured():
        return None
    breakdown = "\n".join(f"- {k}: {v}" for k, v in result.notes().items())
    # Everything below the score line is the posting's own words, one fold
    # later. The title and company came off the page; `missing_skills` is the
    # list `jd_parser` read out of the description; and the per-dimension notes
    # interpolate posting values into our sentences ("Fintech isn't among your
    # targets", "Remote, and you asked for remote"). So the fence goes around
    # the lot of it, and only the number — which this function is forbidden to
    # move and does not read back — stays outside.
    material = (
        f"Role: {job.title or 'n/a'} at {job.company or 'n/a'}\n"
        f"Breakdown:\n{breakdown}\n"
        f"Missing skills: {', '.join(result.missing_skills[:8]) or 'none'}"
    )
    try:
        raw = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_SUMMARY_PROMPT)},
                {
                    "role": "user",
                    "content": (
                        f"Overall fit: {result.overall:.0f}/100 "
                        f"({result.recommendation})\n"
                        + untrusted.fence(material, label="job posting and its scoring")
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.3,
            max_tokens=1200,
        )
    except OpenRouterError as exc:
        logger.info("fit summary fell back to template: %s", exc)
        return None

    data = extract_json_object(raw) or {}
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip() and not looks_like_reasoning(summary):
        return summary.strip()
    return None


def _template_summary(result: FitResult, job: ParsedJob) -> str:
    target = job.title or "this role"
    if result.capped:
        # Saying "weak match" would be a claim we haven't earned — the posting
        # was simply too thin to judge. Tell the candidate that instead.
        #
        # *Which* thing was missing has to be worked out rather than asserted.
        # ``capped`` is ``not skills.known and not role.known``, and the role
        # dimension goes unknown for two unrelated reasons: the posting had no
        # usable title, or **the candidate has no target roles on file**. The
        # sentence named only the first, so a brand-new account — no profile
        # filled in yet, which is the state that reaches this branch most often
        # — was told a posting plainly headed "Senior Backend Engineer" had "no
        # title we can place". A false statement about the posting, and it sent
        # the user to open a job when what actually needed doing was to write
        # down what they are looking for.
        #
        # ``_title_tokens`` is the same test ``score_role`` used to reach the
        # verdict, so the explanation cannot drift from the score.
        if not _title_tokens(job.title):
            missing = "it lists no skills, and no title we can place against your targets"
            advice = "Open it before spending time on it."
        else:
            missing = (
                "it lists no skills, and there are no target roles on file to "
                "place its title against"
            )
            advice = "Add your target roles and it can be scored properly."
        # "this role" is already a whole noun phrase; a second determiner in
        # front of it reads as "in this this role posting".
        subject = f"this {job.title} posting" if job.title else "this posting"
        return f"Not enough in {subject} to judge the fit — {missing}. {advice}"
    verdict = {
        "strong": f"Strong match for {target} — worth a tailored application today.",
        "good": f"Solid match for {target}. Tailor the resume and it's worth applying.",
        "stretch": f"A stretch for {target}. Apply if you can close the gap in the cover letter.",
        "poor": f"Weak match for {target}. Your time is better spent elsewhere.",
    }[result.recommendation]
    gap = (
        f" Biggest gap: {', '.join(result.missing_skills[:3])}."
        if result.missing_skills
        else ""
    )
    return f"{verdict}{gap} {result.skills.note}."


def score_fit(
    resume: Resume,
    job: ParsedJob,
    *,
    targeting: Targeting | None = None,
    explain: bool = True,
) -> FitResult:
    """Score a resume against a parsed posting, 0-100, with a full breakdown.

    *targeting* is what the candidate asked for — a profile's, or their autopilot
    preferences'. Omit it and every dimension reads the resume alone, which is
    the behaviour this function had before profiles existed.
    """
    targeting = targeting or NO_TARGETING
    skills, matched, missing = score_skills(resume, job, targeting)
    role = score_role(resume, job, targeting)
    experience = score_experience(resume, job, targeting)
    location = score_location(resume, job, targeting)
    salary = score_salary(resume, job, targeting)
    industry = score_industry(resume, job, targeting)

    overall = 100.0 * (
        skills.score * WEIGHTS["skills"]
        + role.score * WEIGHTS["role"]
        + experience.score * WEIGHTS["experience"]
        + location.score * WEIGHTS["location"]
        + salary.score * WEIGHTS["salary"]
        + industry.score * WEIGHTS["industry"]
    )
    overall = round(max(0.0, min(100.0, overall)), 1)

    # Nothing was actually established about this posting: no skills to match
    # and no title we could place. Neutrality on every dimension would otherwise
    # add up to a passing score built entirely out of missing information, which
    # is what put irrelevant employers in the send queue.
    capped = not skills.known and not role.known
    if capped:
        overall = min(overall, UNEVALUATED_CAP)

    result = FitResult(
        overall=overall,
        skills=skills,
        role=role,
        experience=experience,
        location=location,
        salary=salary,
        industry=industry,
        matched_skills=matched,
        missing_skills=missing,
        recommendation=recommendation_for(overall),
        capped=capped,
    )
    result.summary = (
        (_llm_summary(result, job) if explain else None)
        or _template_summary(result, job)
    )
    return result


# --------------------------------------------------------------------------- #
# Scout's re-rank                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class RerankCandidate:
    """One posting offered to Scout for a second opinion.

    ``key`` is whatever the caller needs back to write the verdict down — a job
    posting id, in practice. It is echoed through the model, so it stays short
    and opaque rather than carrying meaning the model might try to interpret.
    """

    key: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    salary_text: str | None = None
    remote: bool | None = None
    description: str | None = None
    fit_score: float | None = None
    recommendation: str | None = None
    missing_skills: list[str] = field(default_factory=list)

    def as_prompt_block(self, index: int) -> str:
        facts = " · ".join(
            p
            for p in (
                self.company,
                self.location,
                "remote" if self.remote else None,
                self.salary_text,
            )
            if p
        )
        lines = [
            f"[{index}] id={self.key}",
            f"    role: {self.title or 'untitled'}",
        ]
        if facts:
            lines.append(f"    at: {facts}")
        if self.fit_score is not None:
            lines.append(
                f"    keyword fit: {self.fit_score:.0f}/100"
                + (f" ({self.recommendation})" if self.recommendation else "")
            )
        if self.missing_skills:
            lines.append(f"    not on their resume: {', '.join(self.missing_skills[:6])}")
        if self.description:
            lines.append(f"    posting: {_condense(self.description, 900)}")
        return "\n".join(lines)


@dataclass
class RerankVerdict:
    """Scout's read on one posting: a 0-100 score and why."""

    key: str
    llm_fit_score: float
    reasoning: str


def _condense(text: str, limit: int) -> str:
    """Squash whitespace and cut to *limit* — prompts pay per character."""
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _role_line(entry: dict) -> str:
    """One "Title at Company" line, with either half possibly missing.

    Built by joining what is present rather than by formatting both and
    stripping the connector back off. ``"Title at Company".strip(" at")`` reads
    like it removes a dangling " at ", but ``str.strip`` takes a *set of
    characters*, not a suffix — so it also ate any leading or trailing ``a``,
    ``t`` or space belonging to the text itself. "Engineer at Meta" came out as
    "Engineer at Me", and a title-only entry for "Data Scientist" came out as
    "Data Scientis".

    That went into the prompt as the candidate's employment history, which is
    the evidence Scout weighs a career step against. Nothing failed and nothing
    looked wrong in the output — the model was simply reasoning about somebody
    who worked at "Me".
    """
    title = (entry.get("title") or "").strip()
    company = (entry.get("company") or "").strip()
    if title and company:
        return f"{title} at {company}"
    return title or company


def _candidate_profile(resume: Resume, targeting: Targeting | None = None) -> str:
    """The candidate, in the few lines that matter for a trajectory judgement.

    When the shortlist was scored for one profile, Scout is told which — its
    roles, places and floor — so the second opinion is offered on the same
    question the first one answered rather than on the resume in general.
    """
    history = [
        _role_line(e)
        for e in (resume.experience or [])[:5]
        if e.get("title") or e.get("company")
    ]
    lines = [
        f"Headline: {resume.headline or 'n/a'}",
        f"Seniority: {resume.seniority or 'n/a'} · {resume.years_experience or '?'} yrs experience",
        f"Location: {resume.location or 'n/a'}",
        f"Skills: {', '.join((resume.skills or [])[:25]) or 'n/a'}",
    ]
    if history:
        lines.append("Recent roles: " + " ← ".join(history))

    want = targeting or NO_TARGETING
    if want.label:
        lines.append(f"Applying under their '{want.label}' profile.")
    roles = want.roles or list(resume.target_roles or [])
    if roles:
        lines.append(f"Targeting: {', '.join(roles[:5])}")
    industries = want.industries or list(resume.target_industries or [])
    if industries:
        lines.append(f"Industries of interest: {', '.join(industries[:5])}")
    if want.locations or want.remote_only:
        places = ", ".join(want.locations[:5]) or "anywhere"
        lines.append(
            f"Will work: {places}{' · remote only' if want.remote_only else ''}"
        )
    if want.salary_min:
        lines.append(f"Salary floor: {want.salary_min:,}")
    if resume.summary:
        lines.append(f"Summary: {_condense(resume.summary, 400)}")
    return "\n".join(lines)


_RERANK_PROMPT = (
    "You are {agent}, a career agent re-ranking a job seeker's shortlist. Every "
    "posting below already passed a keyword-based fit filter, so surface-level "
    "skill overlap is settled and is NOT what you are judging.\n\n"
    "Judge each posting on the four things keyword matching cannot see:\n"
    "1. Career trajectory — is this the natural, or a better, next step from "
    "where they have been?\n"
    "2. Skill transferability — do their existing skills carry into this role "
    "even when the posting names different tools?\n"
    "3. Company culture fit — what the posting's own language says about how "
    "this team works, against the environments this candidate has thrived in.\n"
    "4. Growth potential — scope, ownership and what the role sets them up for "
    "in two years.\n\n"
    "Score each posting 0-100 on those four dimensions only. Be willing to "
    "disagree with the keyword score in either direction — that disagreement is "
    "the value you add. Spread the scores out; ranking everything 70-80 is "
    "useless to the candidate.\n\n"
    "For each posting write 1-2 sentences of reasoning, addressed to the "
    "candidate as 'you', naming the specific thing that moved your score. Never "
    "invent facts about the company or the candidate that are not in the text "
    "given to you. No greeting, no bullet points.\n\n"
    "Return ONLY a JSON object of the form:\n"
    '{{"rankings": [{{"id": "<the id given>", "score": <0-100>, '
    '"reasoning": "<1-2 sentences>"}}]}}\n'
    "Include every posting exactly once. Use the ids exactly as given."
)


def _parse_rerank(raw: str, allowed: set[str]) -> dict[str, RerankVerdict]:
    """Pull verdicts out of the model's JSON, dropping anything malformed.

    Every field is validated rather than trusted: an id that wasn't offered, a
    score outside 0-100, or reasoning that is really the model's scratchpad all
    get dropped individually. A bad entry costs that one posting its re-rank
    instead of the whole batch.
    """
    data = extract_json_object(raw) or {}
    rankings = data.get("rankings")
    if not isinstance(rankings, list):
        return {}

    verdicts: dict[str, RerankVerdict] = {}
    for entry in rankings:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("id", "")).strip()
        if key not in allowed or key in verdicts:
            continue
        raw_score = entry.get("score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float, str)):
            continue
        try:
            score = float(raw_score)
        except ValueError:
            continue
        if not 0 <= score <= 100:
            continue
        reasoning = entry.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            continue
        reasoning = _condense(reasoning, 600)
        if looks_like_reasoning(reasoning):
            continue
        verdicts[key] = RerankVerdict(
            key=key, llm_fit_score=round(score, 1), reasoning=reasoning
        )
    return verdicts


def rerank(
    resume: Resume,
    candidates: list[RerankCandidate],
    *,
    targeting: Targeting | None = None,
    top_n: int | None = None,
) -> dict[str, RerankVerdict]:
    """Have Scout re-rank the best of a scan, keyed by ``RerankCandidate.key``.

    Only the ``top_n`` highest deterministic scores are sent, in one call: a scan
    can surface dozens of postings and the free tier will not carry a request per
    job, but the candidate only reads the top of the list anyway.

    Returns ``{}`` — never raises, never partially applies — when no provider is
    configured, the chain fails, or the response can't be trusted. Callers keep
    the deterministic ordering in that case, which is the ordering the feed had
    before this function existed.

    The shortlist is **fenced** (:mod:`app.services.untrusted`) and the
    candidate's own profile above it is not — the same asymmetry `reply_agent`
    and `form_answers` draw, for the same reason: one half is a stranger's
    words, the other half is this product's own records, and telling the model
    to disregard directions found in the records would disarm the grounding.

    It belongs here more than at most call sites, because this verdict is the
    one the product *acts on unattended*. ``auto_apply_service`` reads
    ``llm_fit_score`` as Scout's veto — a posting below
    ``autopilot_min_llm_fit_score`` is dropped whatever the keyword score said —
    so a line inside a scraped description asking for a particular number is a
    line asking to be let past the last judgement standing between a crawled
    posting and an email sent while the candidate is asleep. It reads the whole
    batch in one call, so the same line can also mark *every other* posting
    down, which quietly empties a good scan. And ``reasoning`` is rendered
    straight onto the plan the user reads ("Second opinion scored it 88/100"),
    which makes the text a display surface as well as a gate.

    A fence is a boundary the model can see rather than a guarantee, and it sits
    in front of the checks that were already here: `_parse_rerank` still drops
    an id that was not offered, a score outside 0-100, and reasoning that is
    really the model's scratchpad. Note also that ``_condense`` collapses each
    description to one line, so a posting cannot forge a second ``[n] id=``
    entry — only argue inside its own.
    """
    if not settings.llm_rerank_enabled or not candidates:
        return {}
    # The only gate in the codebase that already asked the whole chain rather
    # than one provider — it just did it by hand, which meant a fifth provider
    # would have been added to the router and missed here.
    if not llm_is_configured():
        return {}

    limit = settings.llm_rerank_top_n if top_n is None else top_n
    if limit <= 0:
        return {}
    shortlist = sorted(
        candidates, key=lambda c: (c.fit_score if c.fit_score is not None else -1.0), reverse=True
    )[:limit]

    blocks = "\n\n".join(c.as_prompt_block(i + 1) for i, c in enumerate(shortlist))
    try:
        raw = chat_completion(
            [
                {
                    "role": "system",
                    "content": untrusted.guarded(
                        _RERANK_PROMPT.format(agent=settings.agent_name)
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"CANDIDATE\n{_candidate_profile(resume, targeting)}\n\n"
                        f"SHORTLIST ({len(shortlist)} postings)\n"
                        + untrusted.fence(blocks, label="job postings")
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.4,
            # Reasoning models spend most of the budget thinking before the JSON
            # appears; a tight cap truncates the object and loses the batch.
            max_tokens=400 * len(shortlist) + 800,
            timeout=90.0,
        )
    except OpenRouterError as exc:
        logger.info("scout re-rank unavailable, keeping deterministic order: %s", exc)
        return {}

    verdicts = _parse_rerank(raw, {c.key for c in shortlist})
    logger.info("scout re-ranked %d of %d shortlisted postings", len(verdicts), len(shortlist))
    return verdicts


__all__ = [
    "NEUTRAL",
    "NO_TARGETING",
    "UNEVALUATED_CAP",
    "WEIGHTS",
    "Dimension",
    "FitResult",
    "RerankCandidate",
    "RerankVerdict",
    "Targeting",
    "best_role_match",
    "canonical_skill",
    "industry_matches",
    "location_match",
    "looks_remote",
    "normalize_text",
    "recommendation_for",
    "rerank",
    "role_targets",
    "score_experience",
    "score_fit",
    "score_industry",
    "score_location",
    "score_role",
    "score_salary",
    "score_skills",
    "skill_mentioned",
    "title_relevance",
]
