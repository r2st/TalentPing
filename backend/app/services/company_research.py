"""Company research cards — the context a job ad leaves out.

A posting says what the employer wants. It almost never says how big they are,
whether they just raised, what their own engineers rate them, or what they
shipped last quarter — and those are exactly the things that decide whether an
application is worth writing.

Research runs in two tiers, cheapest first:

* **Heuristic** — read straight off the posting. Tech stack from the tools named
  in the body, industry from the JD parser, headcount and funding from the
  phrases companies use about themselves ("Series B", "over 500 employees",
  "founded in 2016"). Costs nothing, is never wrong about the posting because it
  *is* the posting, but only knows what the ad happened to mention.
* **LLM** — ask a free model what it knows about the employer, as JSON. Fills the
  gaps the ad leaves, and is the tier that can produce a Glassdoor rating or a
  funding stage at all.

The second tier's output is **recollection, not a lookup**, and it is labelled
that way end to end: rows carry ``source="llm"``, the API passes it through, and
the card tells the candidate the figures are AI-estimated and worth verifying.
Two specific guards follow from that:

* news items keep their headline and drop any URL the model offers — a
  hallucinated link that looks real is worse than no link;
* anything the heuristic tier knows from the posting text wins over the model,
  because one of them read the actual document.

Rows are **global**, keyed on the normalized company name, and cached for
``settings.company_profile_ttl_days`` (a week — funding and headcount move).
Empty results are stored too, so a company nothing is known about isn't
re-researched every time a card opens.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.company_profile import CompanyProfile
from app.services import untrusted
from app.services.jd_parser import detect_industry
from app.services.job_dedup import normalize_company
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
)

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")

# Tools a posting names, grouped so "postgres" and "postgresql" land on one chip.
#
# Literal spellings, matched on word boundaries by :data:`_TECH_PATTERNS` below
# — *not* by containment, which is what this table used to get. A needle that is
# also a fragment of an ordinary English word matched that word, and the words
# in question are the ones job ads are built out of:
#
#   "trusted by thousands"          -> Rust
#   "applicable federal and state laws" -> AWS
#   "build scalable services"       -> Scala
#   "safety guardrails"             -> Ruby
#   "we move swiftly"               -> Swift
#   "sparked a new category"        -> Spark
#   "a reactive culture"            -> React
#
# The EEO paragraph is the worst of these because it is boilerplate: a US
# posting is legally obliged to carry one, it says "laws", and so essentially
# every American job in the feed was researched as an AWS shop. And the chips
# are not decoration — :func:`app.services.cover_letter_service` writes them
# into the letter as "Public tech stack mentions: ...", so an invented chip is a
# claim the candidate makes to the employer about the employer's own stack.
#
# The old table half-knew this and patched it by hand: `" go "`, `"java "`,
# `"mongo "` and `"dbt "` all carry a padding space that is a boundary check
# spelled the only way containment allows. Those are gone; the boundary belongs
# in one place.
_TECH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Python": ("python",),
    "TypeScript": ("typescript",),
    "JavaScript": ("javascript", "node.js", "nodejs"),
    # See :data:`_GO_RE` — the one entry a boundary rule cannot finish.
    "Go": (),
    "Rust": ("rust",),
    "Java": ("java", "jvm"),
    "Kotlin": ("kotlin",),
    "Swift": ("swift", "swiftui"),
    "Ruby": ("ruby", "rails", "ruby on rails"),
    "C++": ("c++",),
    "C#": ("c#", ".net", "asp.net", "dotnet"),
    "PHP": ("php", "laravel"),
    "Scala": ("scala",),
    "Elixir": ("elixir", "phoenix framework"),
    "React": ("react", "react.js", "reactjs", "next.js"),
    "Vue": ("vue", "vue.js", "vuejs", "nuxt"),
    "Angular": ("angular", "angularjs"),
    "Django": ("django",),
    "FastAPI": ("fastapi",),
    "Flask": ("flask",),
    "Spring": ("spring boot", "spring framework"),
    "PostgreSQL": ("postgres", "postgresql"),
    "MySQL": ("mysql", "mariadb"),
    "MongoDB": ("mongodb", "mongo"),
    "Redis": ("redis",),
    "Elasticsearch": ("elasticsearch", "opensearch"),
    "Kafka": ("kafka",),
    "Snowflake": ("snowflake",),
    "dbt": ("dbt",),
    "Spark": ("spark", "pyspark"),
    "AWS": ("aws", "amazon web services"),
    "GCP": ("gcp", "google cloud"),
    "Azure": ("azure",),
    "Kubernetes": ("kubernetes", "k8s"),
    "Docker": ("docker",),
    "Terraform": ("terraform",),
    "GraphQL": ("graphql",),
    "PyTorch": ("pytorch",),
    "TensorFlow": ("tensorflow",),
}

# The boundary a needle is wrapped in before it is matched.
#
# The left guard refuses a needle that starts mid-word ("t|rust", "l|aws",
# "guard|rails", "py|spark"). The right guard refuses one that ends mid-word
# ("scala|ble", "swift|ly", "spark|ed", "react|ive", "java|script") — but it
# admits a following *digit*, because a version number is how a posting names
# the tool it means: "Python 3", "python3", "Vue3", "C++17".
#
# ``+`` and ``#`` are letters for this purpose, on both sides, so "c++" cannot
# be found inside "c+++" and "c#" cannot end a longer token.
_TECH_LEFT = r"(?<![a-z0-9+#])"
_TECH_RIGHT = r"(?![a-z+#])"

# Go, and why it is not in the table.
#
# "go" is a needle that is also the commonest verb in a job ad — "go above and
# beyond", "go the extra mile", "let's go". The old entry tried to buy a
# boundary with padding spaces, `" go "`, which matches every one of those
# sentences; a plain `\bgo\b` would too. Nothing about the token itself
# separates the language from the verb, so this reads the *context* instead:
#
# * the unambiguous spellings — "Golang", "goroutine";
# * a parenthesised "(Go)", which is how a stack list names it;
# * "Go" beside a slash — "Go/Python", "Python/Go" — which prose does not do;
# * "in Go", "with Go", "using Go", where the preposition marks it as a thing
#   rather than a movement. "and go" is deliberately absent: "and go above and
#   beyond" is the sentence this whole entry exists to refuse.
#
# The trailing guard excludes a hyphen as well as a letter, so "go-to-market"
# — a phrase every startup posting carries — is not a Go shop.
#
# Two lookbehinds rather than one with `\s?`: Python requires them fixed-width.
_GO_RE = re.compile(
    r"(?<![a-z0-9])golang(?![a-z])"
    r"|(?<![a-z0-9])goroutines?(?![a-z])"
    r"|(?<=\()go(?=\))"
    r"|(?<=/)go(?![a-z0-9+#-])"
    r"|(?<=/\s)go(?![a-z0-9+#-])"
    r"|(?<![a-z0-9])go(?=\s?/)"
    r"|(?<![a-z0-9])(?:in|with|using)\s+go(?![a-z0-9+#-])"
)

def _tech_pattern(name: str, needles: tuple[str, ...]) -> re.Pattern[str]:
    if name in _TECH_OVERRIDES:
        return _TECH_OVERRIDES[name]
    # An empty alternation compiles to the empty pattern, which matches at
    # position 0 of every posting — so a row left without needles would put its
    # chip on every company in the product, silently. Refused at import instead.
    if not needles:
        raise ValueError(f"{name!r} has no needles and no override pattern")
    return re.compile("|".join(f"{_TECH_LEFT}{re.escape(n)}{_TECH_RIGHT}" for n in needles))


_TECH_OVERRIDES: dict[str, re.Pattern[str]] = {"Go": _GO_RE}

_TECH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, _tech_pattern(name, needles)) for name, needles in _TECH_KEYWORDS.items()
)


_FUNDING_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("public", ("publicly traded", "publicly-traded", "nasdaq", "nyse",
                "ipo", "listed company", "fortune 500")),
    ("series_e", ("series e", "series f", "series g", "late stage")),
    ("series_d", ("series d",)),
    ("series_c", ("series c",)),
    ("series_b", ("series b",)),
    ("series_a", ("series a",)),
    ("seed", ("seed funded", "seed-funded", "seed round", "pre-seed", "pre seed")),
    ("bootstrapped", ("bootstrapped", "profitable and independent", "self-funded",
                      "self funded")),
)

# Boundary-matched for the same reason the stack table is, and with one
# concrete case behind it: "series a" is a prefix of "series and", so any
# posting containing the words "a series and ..." claimed a Series A round.
# "ipo" sits inside "equipo" and "lipo". The right guard refuses a trailing
# digit here — unlike the stack table, no phrase below wants a version number,
# and "fortune 500" must not be satisfied by "fortune 5000".
_FUNDING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        stage,
        re.compile(
            "|".join(
                rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])" for n in needles
            )
        ),
    )
    for stage, needles in _FUNDING_STAGES
)

_VALID_STAGES = {stage for stage, _ in _FUNDING_STAGES} | {"acquired", "unknown"}

# Headcount phrasings, mapped onto the buckets the card renders.
_SIZE_BUCKETS: tuple[tuple[int, str], ...] = (
    (10, "1-10"),
    (50, "11-50"),
    (200, "51-200"),
    (500, "201-500"),
    (1000, "501-1000"),
    (5000, "1001-5000"),
)
_SIZE_LABELS = {label for _, label in _SIZE_BUCKETS} | {"5000+"}

_HEADCOUNT_RE = re.compile(
    r"(?:over |more than |about |around |~|team of |we(?:'| a)re )?"
    r"(\d{1,3}(?:[,.]\d{3})*|\d{1,6})\+?\s*(?:\-|to)?\s*(?:\d{1,6})?\s*"
    r"(?:employees|people|engineers|team members|colleagues|staff)",
    re.I,
)
_FOUNDED_RE = re.compile(r"(?:founded|established|since)\s+(?:in\s+)?(19\d{2}|20\d{2})", re.I)


def size_bucket(count: int | None) -> str | None:
    """Map a headcount onto the label the card shows."""
    if not count or count <= 0:
        return None
    for ceiling, label in _SIZE_BUCKETS:
        if count <= ceiling:
            return label
    return "5000+"


# --------------------------------------------------------------------------- #
# Tier 1: what the posting itself says                                         #
# --------------------------------------------------------------------------- #


def extract_tech_stack(text: str | None, limit: int = 12) -> list[str]:
    """Tools named in a posting, in the order the card should list them."""
    if not text:
        return []
    body = " " + _WS_RE.sub(" ", text.lower()) + " "
    found = [name for name, pattern in _TECH_PATTERNS if pattern.search(body)]
    return found[:limit]


def detect_funding_stage(text: str | None) -> str | None:
    """Funding stage a company claims in its own job ad, if it claims one."""
    if not text:
        return None
    body = _WS_RE.sub(" ", text.lower())
    for stage, pattern in _FUNDING_PATTERNS:
        if pattern.search(body):
            return stage
    return None


def extract_headcount(text: str | None) -> int | None:
    """Headcount from phrases like "over 500 employees" or "a team of 30"."""
    if not text:
        return None
    match = _HEADCOUNT_RE.search(text)
    if not match:
        return None
    try:
        count = int(match.group(1).replace(",", "").replace(".", ""))
    except ValueError:
        return None
    # Job ads throw big round numbers around ("join 2 million users"); anything
    # past a plausible payroll is a different statistic.
    return count if 1 <= count <= 1_000_000 else None


def research_from_posting(company: str, posting_text: str | None, location: str | None) -> dict:
    """Everything the posting itself gives up about the employer."""
    founded = _FOUNDED_RE.search(posting_text or "")
    headcount = extract_headcount(posting_text)
    return {
        "name": company,
        "industry": detect_industry(posting_text or "") if posting_text else None,
        "tech_stack": extract_tech_stack(posting_text),
        "funding_stage": detect_funding_stage(posting_text),
        "employee_count": headcount,
        "size": size_bucket(headcount),
        "founded_year": int(founded.group(1)) if founded else None,
        "headquarters": location,
    }


# --------------------------------------------------------------------------- #
# Tier 2: what the model recalls                                               #
# --------------------------------------------------------------------------- #


_RESEARCH_PROMPT = (
    "You are {agent}, a career agent briefing a job seeker on a company before "
    "they apply. Report only what you actually know about this specific "
    "employer. An omitted field is useful; a confident guess is not — if you are "
    "unsure of a company, return nulls and an empty news list rather than "
    "plausible-sounding filler, and never confuse it with a similarly named one.\n\n"
    "Return ONLY a JSON object:\n"
    "{{\n"
    '  "summary": "one sentence on what the company does, or null",\n'
    '  "industry": "short label, or null",\n'
    '  "size": "one of 1-10, 11-50, 51-200, 201-500, 501-1000, 1001-5000, 5000+, or null",\n'
    '  "employee_count": <approximate integer, or null>,\n'
    '  "founded_year": <4-digit year, or null>,\n'
    '  "headquarters": "city, country, or null",\n'
    '  "funding_stage": "one of bootstrapped, seed, series_a, series_b, series_c, '
    'series_d, series_e, public, acquired, unknown",\n'
    '  "funding_total": "e.g. $45M, or null",\n'
    '  "glassdoor_rating": <number 1.0-5.0, or null>,\n'
    '  "tech_stack": ["known technologies, up to 12"],\n'
    '  "news": [{{"title": "headline", "published": "YYYY-MM", "source": "outlet"}}]\n'
    "}}\n"
    "Give at most 3 news items, most recent first, and only ones you are "
    "confident actually happened. Do not include URLs."
)


def _clean_str(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit] or None


def _clean_int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _clean_news(value: Any, limit: int = 3) -> list[dict[str, Any]]:
    """Headlines only.

    URLs are deliberately dropped: a model asked for a link will invent one that
    resolves to a 404 or, worse, to an unrelated live page. A headline the
    candidate can search for is honest; a fabricated link is not.
    """
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in value[: limit * 2]:
        if not isinstance(entry, dict):
            continue
        title = _clean_str(entry.get("title"), 240)
        if not title:
            continue
        items.append(
            {
                "title": title,
                "published": _clean_str(entry.get("published"), 20),
                "source": _clean_str(entry.get("source"), 80),
            }
        )
        if len(items) >= limit:
            break
    return items


def research_with_llm(company: str, *, hint: str | None = None) -> dict | None:
    """Ask the model what it knows about *company*. ``None`` when it can't say.

    Every field is validated and clamped on the way out — an out-of-range
    Glassdoor rating or a made-up funding stage is dropped rather than stored,
    so a sloppy response degrades to a thinner card instead of a wrong one.
    """
    try:
        raw = chat_completion(
            [
                {
                    "role": "system",
                    "content": untrusted.guarded(
                        _RESEARCH_PROMPT.format(agent=settings.agent_name)
                    ),
                },
                {
                    "role": "user",
                    # Both halves came off a posting. `hint` is 400 characters
                    # condensed straight out of the job description, and the
                    # company name was parsed from the same page. What comes
                    # back is clamped field by field on the way out — which
                    # bounds the damage without preventing it, since `summary`
                    # is 500 characters of free text that gets stored on the
                    # profile and shown on the card the user reads.
                    "content": untrusted.fence(
                        f"Company: {company}"
                        + (f"\nContext: {hint}" if hint else ""),
                        label="job posting",
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.2,
            max_tokens=1600,
        )
    except OpenRouterError as exc:
        logger.info("company research for %s fell back to the posting: %s", company, exc)
        return None

    data = extract_json_object(raw)
    if not data:
        return None

    size = _clean_str(data.get("size"), 32)
    stage = (_clean_str(data.get("funding_stage"), 32) or "").lower().replace(" ", "_")
    rating = data.get("glassdoor_rating")
    try:
        rating = round(float(rating), 1) if rating is not None else None
    except (TypeError, ValueError):
        rating = None

    stack = data.get("tech_stack")
    return {
        "summary": _clean_str(data.get("summary"), 500),
        "industry": _clean_str(data.get("industry"), 120),
        "size": size if size in _SIZE_LABELS else None,
        "employee_count": _clean_int(data.get("employee_count"), 1, 5_000_000),
        "founded_year": _clean_int(data.get("founded_year"), 1800, datetime.now(UTC).year),
        "headquarters": _clean_str(data.get("headquarters"), 255),
        "funding_stage": stage if stage in _VALID_STAGES else None,
        "funding_total": _clean_str(data.get("funding_total"), 64),
        "glassdoor_rating": rating if rating is not None and 1.0 <= rating <= 5.0 else None,
        "tech_stack": [
            s for s in (_clean_str(t, 40) for t in stack[:12]) if s
        ] if isinstance(stack, list) else [],
        "news": _clean_news(data.get("news")),
    }


# --------------------------------------------------------------------------- #
# Persistence                                                                  #
# --------------------------------------------------------------------------- #


def _has_content(facts: dict) -> bool:
    """True when the research actually tells the candidate something."""
    return any(
        facts.get(k)
        for k in (
            "summary", "industry", "size", "employee_count", "funding_stage",
            "glassdoor_rating", "tech_stack", "news", "founded_year",
        )
    )


def _apply(row: CompanyProfile, facts: dict, *, source: str, status: str) -> None:
    for attr in (
        "summary", "industry", "size", "employee_count", "founded_year",
        "headquarters", "funding_stage", "funding_total", "glassdoor_rating",
    ):
        if facts.get(attr) is not None:
            setattr(row, attr, facts[attr])
    if facts.get("tech_stack"):
        row.tech_stack = facts["tech_stack"]
    if facts.get("news"):
        row.news = facts["news"]
    row.source = source
    row.status = status
    row.note = (
        "Figures are recalled by a language model, not looked up — verify anything "
        "you plan to act on."
        if source == "llm"
        else "Read from the job posting itself."
    )
    row.researched_at = datetime.now(UTC)


def research_company(
    db: Session,
    company: str | None,
    *,
    posting_text: str | None = None,
    location: str | None = None,
    use_llm: bool = True,
    force: bool = False,
) -> CompanyProfile | None:
    """The cached profile for *company*, researching it when stale or missing.

    Returns ``None`` only when there is no company name to research. A company
    nothing could be found about still gets a row (``status='empty'``) so the
    next card open is a cache hit rather than another round trip.
    """
    name = (company or "").strip()
    key = normalize_company(name)
    if not key:
        return None

    row = db.scalar(select(CompanyProfile).where(CompanyProfile.normalized_name == key))
    if row is not None and not force and row.is_fresh(settings.company_profile_ttl_days):
        row.hit_count = (row.hit_count or 0) + 1
        db.commit()
        return row

    facts = research_from_posting(name, posting_text, location)
    source = "heuristic"

    if use_llm:
        recalled = research_with_llm(
            name, hint=_condense(posting_text, 400) if posting_text else None
        )
        if recalled:
            source = "llm"
            # The posting was actually read; the model was only remembering. So
            # anything the posting yielded stays, and the model fills the rest.
            merged = dict(recalled)
            for attr, value in facts.items():
                if value:
                    merged[attr] = value
            # Except the stack, where both tiers are additive.
            merged["tech_stack"] = _merge_stack(facts.get("tech_stack"), recalled.get("tech_stack"))
            facts = merged

    status = "ok" if _has_content(facts) else "empty"

    if row is None:
        row = CompanyProfile(name=name, normalized_name=key)
        _apply(row, facts, source=source, status=status)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Another scan researched the same employer first — take theirs.
            db.rollback()
            return db.scalar(
                select(CompanyProfile).where(CompanyProfile.normalized_name == key)
            )
    else:
        _apply(row, facts, source=source, status=status)
        db.commit()

    db.refresh(row)
    return row


def _merge_stack(*stacks: list[str] | None, limit: int = 14) -> list[str]:
    """Union of the tech stacks both tiers found, posting-derived ones first."""
    merged: list[str] = []
    seen: set[str] = set()
    for stack in stacks:
        for item in stack or []:
            folded = item.lower()
            if folded not in seen:
                seen.add(folded)
                merged.append(item)
    return merged[:limit]


def _condense(text: str | None, limit: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


__all__ = [
    "detect_funding_stage",
    "extract_headcount",
    "extract_tech_stack",
    "research_company",
    "research_from_posting",
    "research_with_llm",
    "size_bucket",
]
