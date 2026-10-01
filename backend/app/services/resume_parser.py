"""Resume parsing — text extraction plus structured field inference.

TalentPing asks the user for nothing but the file, so this module has to produce
everything the outreach engine needs: who they are, what they do, how senior they
are, and what they should be pitched for.

Two file formats, because those are the two candidates actually have. A resume
lives in Word until the moment it is sent, and a product that only accepts the
exported PDF is asking the user to go and export one — the single most common
reason an upload fails.

Two layers, and the first always runs:

1. **Heuristics** (:func:`parse_heuristic`) — regex/keyword extraction over the
   raw text. Deterministic, offline, and the only path exercised in tests.
2. **LLM enrichment** (:func:`enrich_with_llm`) — an OpenRouter free model fills
   the fields heuristics are weakest at (headline, target roles, summary, job
   history). Any failure or malformed JSON degrades silently to layer 1, so a
   flaky model never blocks an upload.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any
from xml.etree import ElementTree

from app.core import office_xml
from app.core.config import settings
from app.services.currency import (
    APOSTROPHE_GROUPED,
    CURRENCY_PATTERN,
    GROUPING_CHARS,
    INDIAN_GROUPED,
    SPACE_GROUPED,
    usd_rate,
)
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    extract_json_object,
    llm_is_configured,
)
from app.services.pay_period import annual_multiplier
from app.services.skill_aliases import alias_spellings

try:  # pypdf is a runtime dep; guard so tests can run without a PDF present.
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# A small, extensible skill taxonomy for keyword extraction. Entries are matched
# on word boundaries, so "go" never matches "google".
_SKILL_TAXONOMY = [
    "python", "java", "javascript", "typescript", "go", "golang", "rust", "c++", "c#",
    "ruby", "php", "swift", "kotlin", "scala", "matlab",
    "sql", "postgresql", "mysql", "mongodb", "redis", "elasticsearch", "snowflake",
    "dynamodb", "cassandra", "clickhouse",
    "react", "vue", "angular", "svelte", "next.js", "node.js", "express",
    "fastapi", "django", "flask", "spring", "rails", "laravel", ".net",
    "kubernetes", "docker", "terraform", "ansible", "jenkins", "github actions",
    "aws", "gcp", "azure", "serverless",
    "kafka", "spark", "airflow", "dbt", "hadoop", "flink",
    "pandas", "numpy", "pytorch", "tensorflow", "scikit-learn", "langchain",
    "machine learning", "deep learning", "nlp", "computer vision", "llm",
    "data engineering", "data science", "devops", "sre", "ci/cd", "observability",
    "graphql", "rest", "grpc", "microservices", "system design",
    "product management", "agile", "scrum", "jira", "roadmap", "stakeholder management",
    "figma", "sketch", "ux research", "ui design", "prototyping",
    "sales", "marketing", "seo", "content strategy", "salesforce", "hubspot",
    "excel", "tableau", "power bi", "looker",
]

# Section headers we use to slice the document.
#
# Every entry here is also a *boundary*: a section runs until the next one starts,
# so a heading missing from this table doesn't merely go unread — it gets swallowed
# by whichever section precedes it. That is why headings the parser extracts
# nothing from ("projects", "certifications") are still listed.
_SECTION_PATTERNS = {
    "summary": (
        r"(?:executive\s+|professional\s+|career\s+)?"
        r"(?:summary|profile|objective|about\s+me)"
    ),
    "experience": (
        r"(?:work\s+|professional\s+|employment\s+|relevant\s+)?experience"
        r"|employment\s+history|work\s+history|career\s+history"
    ),
    "education": r"education|academic\s+background|qualifications",
    "skills": r"(?:technical\s+|core\s+)?skills|technologies|competencies|expertise",
    # Not extracted into a field — resumes have no projects column — but named so
    # it terminates the section above it instead of being read as part of it.
    "projects": (
        r"(?:key\s+|selected\s+|personal\s+|side\s+)?projects"
        r"|open\s+source(?:\s*&\s*projects)?"
    ),
    "certifications": r"certifications?|awards?|publications?|patents?",
}

# Any known heading, occupying its whole line. Used by reflow to protect a real
# heading from being glued onto the paragraph above it.
_HEADING_ONLY_RE = re.compile(
    rf"^(?:{'|'.join(_SECTION_PATTERNS.values())})\s*:?$", re.I
)

#: An address, with the domain split into labels rather than swept up as one
#: run of word characters. The old `[\w-]+\.[\w.-]+` tail could not tell where
#: the domain ended, so it also swallowed a trailing dot and — the failure that
#: matters — whatever the PDF glued onto the end of it. See `_trim_glued_tail`.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

#: A lower-case letter followed by an upper-case one, inside the last label of
#: a domain. A TLD is never spelt that way, so the boundary is where the PDF
#: welded the next header field onto the address.
_GLUED_TAIL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
#: A phone number as a resume header writes one, **including its country code**.
#:
#: Two branches, because a number that says which country it is may write its
#: groups however that country writes them, and one that does not has to look
#: like a number to be read as one at all.
#:
#: The old pattern had a single shape — an optional 1-3 digit prefix, an
#: optional parenthesised area code, then two groups of 3-4 digits. It had no
#: slot for the short unparenthesised area code most of the world uses ("+44
#: **20** 7946", "+61 **2** 9374"), so the "+CC" was eaten as the area code and
#: the "+" was orphaned, and the match began at the *area* code instead:
#: "+44 20 7946 0958" came back as "20 7946 0958". Worse, the single trailing
#: group could not cover a 5+5 split, so "+91 98765 43210" came back as
#: "98765 4321" — not a shortened number, a **wrong** one.
#:
#: These are not display strings. `resume_pdf` prints this in the header of the
#: tailored resume the candidate sends out, and `form_apply_service` and
#: `career_apply_service` type it into the application form. Every candidate
#: outside North America was applying with a number nobody could call back;
#: only "+1 (555) 123-4567" survived, because the parentheses gave the area
#: code the slot the others needed.
#:
#: Neither branch decides how many digits a phone number has — `extract_phone`
#: checks that, and it is the check that keeps a year range or a metric out.
_PHONE_RE = re.compile(
    # Said which country it is, so the groups after it may be any length: "+33
    # 1 42 68 53 00" is a Paris landline written the way France writes it.
    #
    # The country code may be bracketed — "(+34) 612 34 56 78" is how Spain
    # writes it, and "(+91)", "(+44)", "(+61)" are all ordinary. Without the
    # brackets here the "+34" matched on its own, failed the digit-count check
    # below as a two-digit number, and the scan resumed *inside* the number:
    # what came back was "612 34 56 78", a nine-digit local number with no
    # country on it. That is a number nobody outside Spain can dial, printed on
    # the resume the candidate sends out and typed into the employer's form —
    # the same failure the leading-"+" branch was written to fix, one bracket
    # over.
    r"(?:\(?\+\d{1,3}\)?[\s.-]?)(?:\(\d{1,4}\)[\s.-]?)?\d{1,5}(?:[\s.-]?\d{1,5}){0,6}"
    # Didn't, so the first group carries the weight: three digits at least,
    # which is what keeps "2018 - 2022" and "12 000" from reading as numbers.
    r"|(?:\(\d{2,4}\)[\s.-]?)?\d{3,5}(?:[\s.-]?\d{2,5}){1,4}"
)
# A link, with or without the scheme. Candidates write "linkedin.com/in/name" and
# "github.com/name" far more often than they write the https:// in front of them,
# and requiring the scheme meant the two links a recruiter most wants were the two
# we never captured. The bare form needs a path to qualify, so a domain mentioned
# in prose ("we migrated off heroku.com") stays out.
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s,;)\]]+"
    r"|(?:[\w-]+\.)+(?:com|org|net|io|dev|ai|co|me|app|xyz)/[^\s,;)\]]+",
    re.I,
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

# "5+ years of experience", "over 8 years’ experience", and the shape a resume
# summary line actually uses: "10 years of software engineering experience".
# The old fixed qualifier list read "professional experience" but not
# "professional software engineering experience", so the candidate's own stated
# total was skipped and the number fell back to inference off date ranges.
#
# The words in between are captured, because they decide whether the years are
# this person's: "25 years of combined experience" is the team's.
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)[ \t’'`]*(?:of[ \t]+)?"
    r"((?:[A-Za-z][\w\-/&+]*[ \t]+){0,4}?)experience",
    re.I,
)

#: Years belonging to a group rather than to the candidate.
_SHARED_YEARS_RE = re.compile(
    r"\b(?:combined|collective|cumulative|aggregate|between\s+us|team|team's|teams)\b",
    re.I,
)
# Date ranges in job entries: "2019 - 2023", "Jan 2019 – Present", and — the shape
# most resumes actually use — "May 2004 – Apr 2006". A month on the *closing* side
# was previously unreadable, and since a job entry is only recognised by its dates,
# that quietly reduced a nine-job history to the one job written "2025 – Present".
#
# The month is matched with a leading \b so "RemoteSep 2025" is read as the year
# 2025 rather than as a month glued to the location beside it — the sort of run a
# PDF hands over constantly.
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_RANGE_RE = re.compile(
    rf"(?:\b{_MONTH}\s*,?\s*)?((?:19|20)\d{{2}})"
    rf"\s*(?:-|–|—|to)+\s*"
    rf"(?:\b{_MONTH}\s*,?\s*)?((?:19|20)\d{{2}}|present|current|now|ongoing)",
    re.I,
)

# A salary a candidate has written down for themselves: "Salary expectation:
# $180,000", "Desired compensation — 150k+". The label is required; a bare number
# anywhere in a resume is far more likely to be a budget they managed than a
# figure they want paid.
_SALARY_LABEL = (
    # "salary expectations", "compensation range", or just "salary:"
    #
    # "CTC" — cost to company — is here because it is the term, not a synonym:
    # an Indian resume states its number under that label and no other, and
    # without it the rupee half of `_MONEY` almost never gets to fire. Safe as a
    # bare label in a way none of its neighbours would be, since the letters are
    # not also an English word.
    r"\b(?:salary|compensation|remuneration|ctc)\b\s*"
    r"(?:\b(?:expectation|expectations|requirement|requirements|range|target)\b\s*)?"
    # "expected base", "desired comp", "minimum salary"
    r"|\b(?:expected|desired|target|minimum|preferred|seeking)\b\s*"
    r"\b(?:salary|compensation|comp|ctc|pay|package|base|remuneration)\b\s*"
    # "Desired rate: $95/hr" — how a contractor states the same want, and the
    # only way they state it. Kept in its own alternative because "rate" is the
    # one noun here that is also an ordinary metric word: "target rate: 25,000"
    # is a number of units, not money. The caller throws that one out (see
    # :func:`extract_salary_expectation`), which is why the group is named.
    r"|\b(?:expected|desired|target|minimum|preferred|seeking)\b\s*"
    r"(?:\b(?:hourly|daily|weekly|monthly|contract|contracting|day)\b\s*)?"
    r"(?P<rate>\brate\b)\s*"
)
# The number itself: "$180,000", "180k", "USD 180,000", "£85,000", "₹18,00,000".
# A range yields both ends; the caller keeps the lower one, since a floor is what
# the preference means.
#
# The currency marker is captured rather than merely tolerated. What this
# function produces becomes `profiles.salary_min`, which is held against the
# bands `jd_parser.extract_salary` reads — and those are dollars. Both sides have
# to be, or the comparison is between two different units: a candidate writing
# "Expected CTC: ₹18,00,000" was stating about twenty-two thousand dollars and
# had it stored as one-point-eight million, a floor no posting on earth clears,
# which empties their feed. The marker set and the rates are `jd_parser`'s, so
# the two halves of every salary comparison cannot drift apart.
#
# The grouping alternatives are that module's too, and for the same reason: a
# lakh is written "1,00,000", two digits at a time, and `\d{1,3}(?:,\d{3})+`
# reads the first two of them and stops.
#
# *Every* one of them, which is the part that had drifted. This pattern grouped
# on the comma alone while `jd_parser` grouped on "[.,]" and now on the
# apostrophe as well — so the two halves of the comparison this module's
# docstring says must not disagree were reading the same string differently.
# "Salary expectation: €90.000" was a $99,000 band when an employer wrote it and
# nothing at all when the candidate did: the figure fell through to the bare
# `\d{2,7}` alternative, which matched the "90", and 90 is under
# `_SALARY_MIN_PLAUSIBLE` and was dropped.
#
# Dropped silently, and into a guess. A stated floor that parses to None is not
# a candidate with no floor — `preference_suggester` substitutes a band inferred
# from their seniority and labels it inferred, so the number they actually chose
# is replaced by one nobody chose. Every European and Swiss spelling of an
# expectation landed there.
_MONEY = (
    rf"(?P<currency>{CURRENCY_PATTERN})?\s*"
    # `SPACE_GROUPED` first, so "Expected salary: 90 000 €" is ninety thousand
    # rather than the ninety the "[.,]" shape stops at. Safe here for the same
    # reason it is safe in `jd_parser._AMOUNT`: this fragment is only ever
    # reached behind a salary label, so a headcount cannot arrive at it.
    rf"(?P<amount>{SPACE_GROUPED}|{INDIAN_GROUPED}|{APOSTROPHE_GROUPED}"
    rf"|\d{{1,3}}(?:[.,]\d{{3}})+|\d{{2,7}})"
    rf"\s*(?P<thousands>k\b)?"
    # A marker written *after* the figure, which is how a European candidate
    # writes one: "Expected salary: 90 000 €", "Expected salary: 180 000 PLN".
    # Not captured — the group above is only the leading form — but pulled
    # *inside* the match, because the rate is read with
    # `usd_rate(match.group(0))` and a marker outside the match is a marker
    # that call cannot see. "€90 000" was converted and the identical "90 000 €"
    # was not, so the same expectation set two different floors depending on
    # which side of the number the candidate put the sign.
    rf"(?:\s?(?:{CURRENCY_PATTERN}))?"
)
_SALARY_EXPECTATION_RE = re.compile(
    rf"(?:{_SALARY_LABEL})\s*[:\-–—]?\s*{_MONEY}", re.I
)
#: A label describing what the candidate is paid *now*, immediately in front of
#: a figure. "Current CTC 12,00,000" and "Present salary: $150,000" are labelled
#: figures the pattern above matches happily, and they are the wrong number:
#: this function returns the *lowest* statement it finds as the floor, and what
#: someone earns today is almost always under what they are asking for. A resume
#: carrying both would have had its floor set by the one it is trying to leave.
#:
#: Anchored to the end of the text before the match, with a few characters of
#: slack for a stray word, so it only fires on a qualifier attached to *this*
#: label rather than one anywhere earlier in the line.
_CURRENT_PAY_RE = re.compile(
    r"\b(?:current|currently|present|existing|latest|previous|drawing"
    r"|last\s+drawn)\b[^\n]{0,12}$",
    re.I,
)
# The two bounds a labelled figure has to sit inside, and they are checked
# against different things because they were written to catch different things.
#
# The floor catches a figure too *small to be written as a salary at all* — a
# monthly rate under an annual label, a page number, a stray "12". That is a
# fact about the digits on the page, so it is checked against the figure as
# written, before any conversion. Checked after, every rupee salary in the
# world fails it: ₹12,00,000 is an ordinary Indian mid-level wage and fourteen
# thousand dollars.
#
# The ceiling catches a figure that is real money but not *this person's pay* —
# a budget they managed, a revenue line. That is a claim about how much money it
# is, which only means anything in one currency, so it is checked after
# conversion. Checked before, ₹50,00,000 — sixty thousand dollars, an ordinary
# senior salary in India — reads as five million and is thrown out as revenue.
_SALARY_MIN_PLAUSIBLE = 20_000
_SALARY_MAX_PLAUSIBLE = 2_000_000

_SENIORITY_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("exec", ("chief", "cto", "ceo", "cfo", "vp of", "vice president", "head of")),
    ("lead", ("staff", "principal", "lead", "team lead", "tech lead", "architect",
              "director", "manager")),
    ("senior", ("senior", "sr.", "sr")),
    ("junior", ("junior", "jr.", "jr", "intern", "graduate", "entry level", "trainee")),
]


def marker_pattern(marker: str) -> re.Pattern[str]:
    """A whole-word pattern for *marker*, tolerant of the spacing it is written
    with.

    Matched as bare substrings these markers are a minefield: "cto" sits inside
    "refactored", "factory", "vector" and "detector", so any resume with one of
    those words in its first lines was filed as an *exec*. "architect" sits
    inside "architecture" and "intern" inside "internal", each demoting or
    promoting a candidate by two levels on a word about their work rather than
    their level.

    The trailing boundary is dropped after "sr." and friends, because there is
    no word boundary between a full stop and the space that follows it.
    """
    body = r"\s+".join(re.escape(part) for part in marker.split())
    head = r"\b" if marker[:1].isalnum() else ""
    tail = r"\b" if marker[-1:].isalnum() else ""
    return re.compile(head + body + tail, re.I)


_SENIORITY_MARKER_RES: list[tuple[str, tuple[tuple[str, re.Pattern[str]], ...]]] = [
    (level, tuple((m, marker_pattern(m)) for m in markers))
    for level, markers in _SENIORITY_MARKERS
]

#: Words that cancel a marker when they follow it. "Graduate" names a level in
#: "Graduate Software Engineer" and an education row in "Graduate School" — and
#: the education row is the one that turns up on a twenty-year veteran's resume.
_MARKER_VETO: dict[str, re.Pattern[str]] = {
    "graduate": re.compile(
        r"\s+(?:school|degree|studies|program|programme|certificate|diploma"
        r"|research|coursework|student|thesis)\b",
        re.I,
    ),
}

# Common resume noise that must never be mistaken for a person's name.
_NAME_STOPWORDS = {
    "curriculum", "vitae", "resume", "cv", "profile", "summary", "contact",
    "confidential", "page",
}


@dataclass
class ParsedResume:
    """Everything the outreach engine needs, inferred from the PDF alone."""

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    headline: str | None = None
    summary: str | None = None
    years_experience: int | None = None
    seniority: str | None = None
    skills: list[str] = field(default_factory=list)
    target_roles: list[str] = field(default_factory=list)
    target_industries: list[str] = field(default_factory=list)
    experience: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    parsed_with: str = "heuristic"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #


# How much of a PDF is read before we stop. A resume is one to five pages and a
# padded academic CV is twenty; nothing a human wrote to be read by a recruiter
# is near either number.
#
# Both ceilings are here because either one alone leaves the other open. `.docx`
# has had a budget since `app.core.office_xml` — a hostile document that is a
# valid zip of valid XML still has to fit inside one — and the PDF path had
# none, so extraction cost whatever the file asked it to. pypdf refuses a single
# absurdly-compressed stream, and that is the only limit it applies: a page
# holding a content stream that inflates to just under its threshold costs
# seconds of CPU and hundreds of megabytes, and a file may hold as many such
# pages as fit. Measured, one 23 KB PDF spent 7.6 seconds and 340 MB of RSS,
# well inside every limit above it — the upload cap is 10 MB.
#
# The character budget is the one that actually bounds the work, since pages are
# cheap until they carry text. It is far above `_LLM_INPUT_CHARS`, so nothing
# that survives this was going to reach the model anyway.
MAX_PDF_PAGES = 120
MAX_EXTRACTED_CHARS = 400_000


def extract_text_from_pdf(data: bytes) -> str:
    """Extract plain text from a PDF byte payload.

    The text is reflowed (see :func:`_reflow_shattered_lines`) because pypdf
    reports one line per text object, which for a great many real resumes is one
    line per *word*.

    Truncated rather than refused past :data:`MAX_PDF_PAGES` or
    :data:`MAX_EXTRACTED_CHARS`. A real document that trips either has already
    given up everything the parser reads — the name, contact block and first
    jobs are on page one — so refusing it would cost a genuine user their upload
    to punish a shape only a hostile file has a reason to take.
    """
    if PdfReader is None:  # pragma: no cover
        raise RuntimeError("pypdf is not installed")
    reader = PdfReader(io.BytesIO(data))
    chunks: list[str] = []
    size = 0
    unreadable_pages = 0
    for index, page in enumerate(reader.pages):
        if index >= MAX_PDF_PAGES:
            logger.warning("resume PDF truncated at %s pages", MAX_PDF_PAGES)
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not lose the document
            # Counted, not merely skipped. Every page failing yields an empty
            # string, and the caller cannot tell that from a PDF whose pages
            # are images — the two reach the user as the same "we could not
            # read your resume" and want opposite answers ("the file is
            # damaged" versus "run it through OCR"). Debug per page because a
            # scanned document produces one of these for every page it has;
            # the summary below is the line worth alerting on.
            logger.debug(
                "resume PDF page %s could not be extracted", index, exc_info=True
            )
            unreadable_pages += 1
            continue
        chunks.append(text)
        size += len(text)
        if size >= MAX_EXTRACTED_CHARS:
            logger.warning(
                "resume PDF truncated at %s characters", MAX_EXTRACTED_CHARS
            )
            break
    joined = "\n".join(chunks)[:MAX_EXTRACTED_CHARS]
    if unreadable_pages:
        # WARNING only when it cost the document something. A page dropped out
        # of twelve is a curiosity; every page dropped is the whole upload, and
        # it used to leave no trace at all — the parser returned "" and the
        # failure surfaced to the user as a resume with no experience on it.
        logger.log(
            logging.WARNING if not joined.strip() else logging.INFO,
            "resume PDF: %s of %s page(s) could not be extracted",
            unreadable_pages,
            unreadable_pages + len(chunks),
            extra={"unreadable_pages": unreadable_pages, "pages": len(chunks)},
        )
    return _reflow_shattered_lines(_normalize_whitespace(joined))


# A PDF has no notion of a line: it has text objects at coordinates. pypdf emits a
# newline between objects it can't prove are on the same run, so a document whose
# layout positions each word separately — justified text, tracked-out headings,
# anything exported from Google Docs — extracts as one word per line, with the
# spaces between them arriving as lines of their own.
#
# Every heuristic below reads whole lines: the name is line one, an experience
# entry needs its title, company and dates together, and a section body is sliced
# between heading lines. Against shattered text all of them return nothing, and a
# stray body word sitting alone on a line ("experience", "profile") is
# indistinguishable from a real heading — which is worse than nothing, because
# slicing then starts from the middle of a paragraph.
#
# So the paragraphs are put back together. Only when the document is actually
# shattered, and never for .docx, whose lines come from real paragraph marks.
_SHATTER_MIN_LINES = 20
_SHATTER_RATIO = 0.5
# A line ending here finished a thought, so the next word starts a new line rather
# than being glued onto it. Without this the whole page rejoins into one line and
# the heuristics are no better off than before.
_LINE_ENDINGS = (".", "!", "?", ":")


def _looks_shattered(lines: list[str]) -> bool:
    """Whether extraction lost this document's line structure.

    Measured as the share of non-empty lines holding a single word. Prose sits
    near zero; the pathology this repairs sits near one. The threshold is
    deliberately far from both, and the line floor keeps a short document — a
    one-line covering note, a nearly empty page — from tripping it by accident.
    """
    content = [line.strip() for line in lines if line.strip()]
    if len(content) < _SHATTER_MIN_LINES:
        return False
    orphans = sum(1 for line in content if " " not in line)
    return orphans / len(content) >= _SHATTER_RATIO


def _is_heading_line(line: str) -> bool:
    """Whether *line* is a section heading on its own — never absorbed by reflow.

    Same case test :func:`_section_body_offset` applies, for the same reason: a
    lowercase "experience" with no colon is a word out of a sentence, and gluing
    it back into that sentence is exactly right. "EDUCATION" is a heading and must
    survive as its own line.
    """
    if not _HEADING_ONLY_RE.match(line):
        return False
    return not line.islower() or line.endswith(":")


def _reflow_shattered_lines(text: str) -> str:
    """Rejoin words that extraction split onto lines of their own.

    Returns *text* unchanged unless :func:`_looks_shattered` recognises the
    damage, so a PDF that extracted cleanly is never touched.
    """
    lines = text.splitlines()
    if not _looks_shattered(lines):
        return text

    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        absorb = (
            out
            and " " not in line
            and not out[-1].endswith(_LINE_ENDINGS)
            and not _is_heading_line(line)
        )
        if absorb:
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return "\n".join(out)


# WordprocessingML. A .docx is a zip of XML parts, so this needs no library at
# all — deliberately, because the alternative is a new runtime dependency on a
# box this product deploys to natively, for a format whose text layer is four
# element names wide.
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_BODY = "word/document.xml"


def _docx_part_text(xml: bytes) -> str:
    """Plain text out of one WordprocessingML part, preserving line structure.

    Four elements carry everything that matters: ``w:t`` is a run of text,
    ``w:tab`` and ``w:br`` are whitespace the author typed, and ``w:p`` ends a
    paragraph. Ignoring the paragraph boundary would run the whole CV into one
    line, and every heuristic downstream — the name on line one, the section
    headings, the role/company split — reads line by line.
    """
    root = office_xml.parse_xml(xml)
    lines: list[str] = []
    for paragraph in root.iter(f"{_W_NS}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            tag = node.tag
            if tag == f"{_W_NS}t":
                parts.append(node.text or "")
            elif tag == f"{_W_NS}tab":
                parts.append("\t")
            elif tag == f"{_W_NS}br":
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _docx_part_order(names: list[str]) -> list[str]:
    """Header parts, then the body, then footers — the order a reader sees them.

    Resumes routinely put the candidate's name and contact details in a Word
    header, and :func:`parse_heuristic` reads the name off the first line. Body
    first would file the header's name as a stray line halfway down.
    """
    headers = sorted(n for n in names if n.startswith("word/header"))
    footers = sorted(n for n in names if n.startswith("word/footer"))
    return [*headers, _DOCX_BODY, *footers]


def extract_text_from_docx(data: bytes) -> str:
    """Extract plain text from a .docx byte payload.

    Raises ``ValueError`` for anything that isn't a Word document, so the router
    can answer with a clean 422 rather than a stack trace — the same contract
    :func:`extract_text_from_pdf` gets from pypdf. A document that *is* a zip of
    XML but a hostile one — a decompression bomb, a thousand header parts, a
    billion-laughs DTD — raises the same way, from :mod:`app.core.office_xml`,
    which is where every limit on reading one lives.
    """
    try:
        archive = office_xml.open_document(data)
    except office_xml.OfficeDocumentError as exc:
        raise ValueError(f"not a Word document — {exc}") from exc

    with archive:
        names = archive.namelist()
        if _DOCX_BODY not in names:
            # A zip without a document part is some other Office format — most
            # often a .doc renamed, or a .pages/.odt export.
            raise ValueError("not a Word document (no word/document.xml)")

        chunks: list[str] = []
        for part in _docx_part_order(names):
            if part not in names:
                continue
            try:
                chunks.append(_docx_part_text(archive.read(part)))
            except ElementTree.ParseError:
                # One unreadable header must not cost us the whole resume.
                continue

    return _normalize_whitespace("\n".join(c for c in chunks if c))


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Heuristic extraction
# --------------------------------------------------------------------------- #


#: Every spelling each taxonomy entry may be written under, compiled once.
#:
#: The taxonomy is a list of *canonical* names, and it was matched as a list of
#: literal strings — so this function found a skill only when the document
#: happened to spell it the way the table does. Nobody agreed to that. "ReactJS",
#: "NodeJS", "NextJS" and "VueJS" are how a great many postings and resumes write
#: those four, and every one of them extracted **nothing at all**; so did
#: "Postgres", "k8s", "sklearn", "cpp", "csharp" and "dotnet".
#:
#: That is not a near-miss in a display string. This function is the producer for
#: both sides of the fit comparison — :func:`app.services.jd_parser.parse_heuristic`
#: builds a posting's ``required_skills`` and ``preferred_skills`` from it, and
#: the resume's ``skills`` come from it too — and skills are the heaviest
#: dimension of the score at 0.30. A posting advertised in the JS-suffix style
#: arrived at the scorer with *no requirements*, so there was nothing to match,
#: nothing to put on ``missing_skills`` for the candidate to read, nothing for
#: the tailoring prompt to close and nothing for `extract_keywords` to protect a
#: slot for in the ATS keyword list.
#:
#: :data:`app.services.skill_aliases.SKILL_ALIASES` has known these spellings all
#: along — it is what makes ``skill_mentioned`` match "k8s" against a resume
#: saying "Kubernetes". It was only ever read by the consumer. Reading it here
#: too is what stops the producer from being the narrower of the two.
_SKILL_SPELLINGS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (
        skill,
        tuple(
            re.compile(r"(?<![a-z0-9])" + re.escape(spelling) + r"(?![a-z0-9])")
            for spelling in (
                skill,
                *alias_spellings(skill, known=frozenset(_SKILL_TAXONOMY)),
            )
        ),
    )
    for skill in _SKILL_TAXONOMY
)


def extract_skills(text: str) -> list[str]:
    """Return taxonomy skills that appear in the resume text, in taxonomy order.

    Reported under the taxonomy's own name whichever spelling was found, so a
    posting that said "ReactJS" and one that said "React.js" produce the same
    ``required_skills`` and compare equal downstream.
    """
    lowered = text.lower()
    found: list[str] = []
    for skill, patterns in _SKILL_SPELLINGS:
        if any(pattern.search(lowered) for pattern in patterns):
            found.append(skill)
    # "golang" and "go" are the same skill to a recruiter.
    if "golang" in found and "go" in found:
        found.remove("golang")
    return found


def _trim_glued_tail(address: str) -> str:
    """Cut the next header field back off an address the PDF welded it onto.

    A PDF header is a row of fields, and the extractor hands them over with the
    separator missing far more often than not: what comes out is
    ``jane.doe@example.comLinkedIn`` or ``jane@example.co.ukPhone``. The domain
    pattern cannot see the seam — ``comLinkedIn`` is word characters all the
    way — so the address stored for the candidate was one no mail server would
    accept.

    That address is not decoration. `resume_pdf` prints it in the header of the
    tailored resume the candidate sends out, and `form_apply_service` and
    `career_apply_service` type it into the employer's application form, so
    every reply went nowhere and the candidate had no way to see why.

    The seam is a case change. A top-level domain is a single case — ``com``,
    ``COM``, ``co.uk`` — and never ``comLinkedIn``, so a lower-to-upper
    boundary inside the last label is the glue and not part of the address.
    Nothing else is cut: a domain with no such boundary is returned untouched,
    which is every correctly extracted address.
    """
    head, _, domain = address.rpartition("@")
    labels = domain.split(".")
    trimmed = _GLUED_TAIL_RE.split(labels[-1], maxsplit=1)[0]
    if trimmed == labels[-1]:
        return address
    labels[-1] = trimmed
    return f"{head}@{'.'.join(labels)}"


def extract_email(text: str) -> str | None:
    match = _EMAIL_RE.search(text)
    return _trim_glued_tail(match.group(0).strip(".,;")) if match else None


def extract_phone(text: str) -> str | None:
    """Find a phone number, skipping digit runs that are really years or metrics.

    Only the header block is searched — that's where contact details live, and it
    keeps us away from dates and figures in the body.
    """
    head = "\n".join(text.splitlines()[:15])
    for match in _PHONE_RE.finditer(head):
        candidate = match.group(0).strip()
        digits = re.sub(r"\D", "", candidate)
        if not 9 <= len(digits) <= 15:
            continue
        return candidate
    return None


def extract_links(text: str) -> list[str]:
    seen: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;)")
        if url not in seen:
            seen.append(url)
    return seen[:10]


def _name_candidates(line: str) -> list[str]:
    """The fragments of a header line that could be a name, in reading order.

    Convention puts the name alone on line one, and when it is, the whole line is
    the only candidate. But a header laid out across one row —
    ``SUBHENDU DAS +1-365-275-4408 | me@example.com | Toronto, Canada`` — extracts
    as a single line, and testing only whole lines threw that name away for having
    a phone number next to it. So the line is also split on the separators a
    header uses, and each fragment is cut at its first digit: whatever precedes
    the contact details is what's left.

    The comma is one of those separators, and the whole line is tried **last**
    rather than first because of it. A resume header writes the name and then
    what the name is entitled to: "Maria Garcia, PhD", "John Smith, MBA",
    "Alex Chen, PMP, CSM", "Jane Doe, Senior Software Engineer". The word test
    below splits on commas as though they were spaces, so the first three came
    back as "Maria Garcia Phd", "John Smith Mba" and "Alex Chen Pmp Csm" — and
    the fourth was five words, failed, and let line two answer instead, which
    made the candidate's name "Austin Tx".

    This is not a display string. `resume_pdf` prints it at the top of the
    tailored resume the candidate sends out, and `form_apply_service` types it
    into the employer's "Full name" field.
    """
    fragments = [*re.split(r"[|•·,]|\s{2,}|\s[—–]\s", line), line]
    out: list[str] = []
    for fragment in fragments:
        cut = re.split(r"\d", fragment, maxsplit=1)[0]
        # Trim to the letters: "SUBHENDU DAS +1-365-..." cuts at the digit and
        # leaves a "+" that would otherwise fail the word test below.
        #
        # A *letter*, not `A-Za-z`. The class used to be ASCII, so a name
        # beginning with one — "Łukasz Nowak", "Ólafur Jónsson" — had its first
        # letter trimmed off as punctuation, and what was left ("ukasz Nowak")
        # then failed the capitalisation test below. See :data:`_NAME_WORD_RE`
        # for what that cost.
        trimmed = re.sub(r"^[\W\d_]+|(?:(?!['’.])[\W\d_])+$", "", cut)
        if trimmed and trimmed not in out:
            out.append(trimmed)
    return out


#: One word of a name: a letter, then letters and the marks a name carries.
#:
#: ASCII (``[A-Z][a-zA-Z'’.-]*``) is what this was, and it did not merely fail
#: to read an accented name — it read the next line instead. "José García" and
#: "Ana Sofía Müller" failed the test, the loop moved on, and the *job title*
#: underneath became the candidate's name: "Senior Software Engineer". Where
#: the header put contact details on line two, the location answered instead,
#: and "Zoë O'Brien" came out as "Dublin Ireland" — the same failure as the
#: "Austin Tx" one that :func:`_name_candidates` documents, arriving through
#: the alphabet rather than through the commas.
#:
#: This is not a display string. `resume_pdf` prints it at the top of the
#: tailored resume the candidate sends out, `form_apply_service` types it into
#: the employer's "Full name" field, and the outreach composer signs with it.
#:
#: An ALL CAPS accented name ("MARÍA GARCÍA") always worked, because the old
#: test had a second branch — ``w.isupper()`` — that asks about case rather
#: than about the alphabet. Only Title Case was affected, which is the way
#: almost every resume writes it.
_NAME_WORD_RE = re.compile(r"[^\W\d_](?:[^\W\d_]|['’.\-])*")


def extract_name(text: str) -> str | None:
    """Guess the candidate's name from the first few lines.

    Takes the first fragment of the header block that looks like 2–4 capitalized
    words and isn't a heading or a contact detail.
    """
    for raw in text.splitlines()[:8]:
        for line in _name_candidates(raw.strip()):
            if len(line) > 60:
                continue
            if _EMAIL_RE.search(line) or _URL_RE.search(line):
                continue
            if _HEADING_ONLY_RE.match(line):
                # "PROFESSIONAL EXPERIENCE" title-cases into a plausible-looking
                # two-word name, and is the first thing a nameless resume offers.
                continue
            words = [w for w in re.split(r"[\s,]+", line) if w]
            if not 2 <= len(words) <= 4:
                continue
            if any(w.lower().strip(".") in _NAME_STOPWORDS for w in words):
                continue
            # Accept Title Case and ALL CAPS (both common in resume headers).
            if all(
                _NAME_WORD_RE.fullmatch(w) and w[:1].isupper() for w in words
            ):
                return " ".join(w.title() if w.isupper() else w for w in words)
    return None


#: "Remote" standing on its own as a *field* of the header, rather than as a
#: word in a sentence. Bounded on both sides by a line edge or one of the marks
#: a header uses to separate its fields, so "Jane Doe | Remote | jane@x.com",
#: "Location: Remote", "Remote — Europe" and "London, UK (Remote)" all count,
#: and "Remote Site Engineer" and "experienced in remote collaboration" do not.
#:
#: The hyphen is deliberately not a separator here: "Remote-First Engineering
#: Leader" is a headline, not an address.
_REMOTE_FIELD_RE = re.compile(
    r"(?:^|[|•·—–,():;/])\s*remote\s*(?:$|[|•·—–,():;/])", re.I
)


#: ``City, Region`` as a header line writes it.
#:
#: Unicode-lettered for the reason :data:`_NAME_WORD_RE` gives: an ASCII class
#: does not read "München, Germany" or "Kraków, Poland" at all, so a candidate
#: whose own city is spelt the way its inhabitants spell it had no location on
#: file — no timezone for `send_time`, no market benchmark for
#: `salary_service`, and a location dimension scored on nothing.
#:
#: The leading character is "a letter that is not ASCII lowercase" rather than
#: "an uppercase letter", which Python's ``re`` has no way to say. The gap is a
#: lowercase accented letter opening a line, and a line beginning that way is
#: not a place.
#:
#: The two-letter region code stays ASCII on purpose: postal abbreviations are.
_PLACE_WORD = r"[^\W\da-z_](?:[^\W\d_]|[.\- ]){2,30}"
_PLACE_LINE_RE = re.compile(
    rf"({_PLACE_WORD}),\s*([A-Z]{{2}}|{_PLACE_WORD})(?:\s|$|\|)"
)


def extract_location(text: str) -> str | None:
    """The location in the header block — a place, "Remote", or both.

    "Remote" is carried through because it is a preference the candidate stated
    and the only place they state it. :func:`app.services.preference_suggester.suggest_locations`
    already knows to split it back out and flip ``remote_only``, and had done
    since it was written — but only ever fired on "Remote, US", because that is
    the one spelling that also satisfies the ``City, Region`` pattern below.
    The common ones — a line reading just "Remote", or "Remote (EU)", or
    "Location: Remote" — returned None, so a remote-only candidate's feed was
    filled with on-site roles and nothing on the page said why.

    A place and a remote marker are not alternatives: "London, UK (Remote)" is
    a candidate who lives in London and works from it, and both halves matter —
    the place to the location filter, the marker to ``remote_only``.

    **The candidate's own name is not a place.** ``City, Region`` is the shape
    of a location and it is also the shape of the line a resume opens with:
    "Maria Garcia, PhD", "John Smith, MBA", "Jane Doe, Senior Software
    Engineer". That line comes first, this loop takes the first match it finds,
    and so the header's own name was stored as ``profiles.location`` — where
    `send_time` reads it for a timezone, `salary_service` for a market
    benchmark, and `fit_scorer` to tell the candidate "you're in Maria Garcia".
    """
    pattern = _PLACE_LINE_RE
    name = (extract_name(text) or "").lower()
    place: str | None = None
    remote = False
    for raw in text.splitlines()[:12]:
        line = raw.strip()
        if _EMAIL_RE.search(line):
            # Contact lines pack email + location together; strip the address.
            line = _EMAIL_RE.sub(" ", line)
        if _REMOTE_FIELD_RE.search(line):
            remote = True
        if place is not None:
            continue
        match = pattern.search(line)
        if match:
            city, region = match.group(1).strip(), match.group(2).strip()
            if city.lower() in _NAME_STOPWORDS:
                continue
            if name and " ".join(city.lower().split()) == name:
                continue
            place = f"{city}, {region}"
    if place is None:
        return "Remote" if remote else None
    # "Remote, US" already says it; appending would read as "Remote, US (Remote)".
    if remote and "remote" not in place.lower():
        return f"{place} (Remote)"
    return place


#: The remote marker inside an already-extracted location, for taking it back
#: out again. Deliberately looser than :data:`_REMOTE_FIELD_RE`, which has to
#: find the word in a whole resume header full of prose: by the time a string
#: reaches here it *is* a location, so a bare word boundary is enough and there
#: is no sentence left for it to match the wrong half of.
_REMOTE_IN_PLACE_RE = re.compile(r"\bremote\b", re.I)

#: What is left holding a place apart from its marker once the marker is gone —
#: "London, UK (Remote)" leaves a dangling "(", "Remote — Austin, TX" a dash.
_PLACE_EDGE = " ,;|/()-–—"


def split_remote(location: str | None) -> tuple[str | None, bool]:
    """Take :func:`extract_location`'s output back apart: ``(place, remote)``.

    The inverse of the join at the end of :func:`extract_location`, and it lives
    beside it for the reason :mod:`app.services.pay_period` gives for its own
    table: the two halves cannot be allowed to disagree about what the joined
    string means, and they will if each reader spells the split itself.

    Both readers did. :func:`app.services.preference_suggester.suggest_locations`
    had this rule inline and correct.
    :func:`app.services.fit_scorer.score_location` had no rule at all, and read
    the whole string as a place name — so a resume that said "Remote" and
    nothing else scored an on-site role by asking whether Berlin and "Remote"
    were the same city, and told the candidate "you're in Remote".

    ``place`` is ``None`` when the location was only ever a marker. That is not
    the same as an empty location and callers must not treat it as one: the
    candidate did say something, and what they said was that they work
    remotely.
    """
    raw = " ".join((location or "").split())
    if not raw:
        return None, False
    if not _REMOTE_IN_PLACE_RE.search(raw):
        return raw, False
    place = " ".join(_REMOTE_IN_PLACE_RE.sub(" ", raw).strip(_PLACE_EDGE).split())
    return (place or None), True


def _employment_scopes(text: str) -> list[str]:
    """The parts of a resume whose date ranges are employment, best first.

    A degree is dated the same way a job is, and a resume lists both. Reading
    every date range on the page and taking the outer span therefore measures
    the years since the candidate started *university*, not the years they have
    worked: a 2007-2011 B.S. above a 2019-Present job reads as nineteen years of
    experience instead of seven. That number is not decoration — it goes onto
    application forms as the candidate's own answer, into the fit score's
    experience dimension, and into cover letters.

    Preference order, rather than a single scope, because the heading that would
    make the first choice correct is exactly the thing a shattered PDF loses:

    1. The experience section, when the resume has one and it carries dates.
    2. Failing that, everything except the education section — which still
       removes the degree years, the single largest source of the overshoot.
    3. Failing that, the whole document, which is where this started.
    """
    scopes: list[str] = []
    body = _section(text, "experience")
    if body:
        scopes.append(body)
    span = _section_span(text, "education")
    if span:
        scopes.append(text[: span[0]] + "\n" + text[span[1] :])
    scopes.append(text)
    return scopes


def _span_from_ranges(scope: str) -> int | None:
    """Years between the earliest start and latest end dated in *scope*."""
    ranges = _RANGE_RE.findall(scope)
    if not ranges:
        return None
    this_year = date.today().year
    starts, ends = [], []
    for start, end in ranges:
        starts.append(int(start))
        ends.append(int(end) if end.isdigit() else this_year)
    span = max(ends) - min(starts)
    return span if 0 < span <= 60 else None


def _shared_claim(text: str, match: re.Match[str]) -> bool:
    """Whether a year count belongs to a group instead of to the candidate.

    "Led a team with 25 years of combined experience" is a fact about the team.
    Taking the largest claim on the page — which is right, because resumes
    restate the real total per section — makes exactly this line win.
    """
    if _SHARED_YEARS_RE.search(match.group(2)):
        return True
    return bool(_SHARED_YEARS_RE.search(text[max(0, match.start() - 30) : match.start()]))


def extract_years_experience(text: str) -> int | None:
    """Years of experience — a stated claim if present, else inferred from dates."""
    stated = [
        int(m.group(1))
        for m in _YEARS_RE.finditer(text)
        if 0 < int(m.group(1)) <= 60 and not _shared_claim(text, m)
    ]
    if stated:
        # Take the largest claim; resumes often restate it per section.
        return max(stated)

    # Fall back to the span between the earliest and latest *employment* years.
    for scope in _employment_scopes(text):
        span = _span_from_ranges(scope)
        if span is not None:
            return span
    return None


@dataclass(frozen=True, slots=True)
class StatedSalary:
    """A floor the candidate wrote down, and what had to be done to it.

    The two flags are the whole reason this type exists. ``usd`` is very often
    not the number on the page — "Desired rate: $95/hr" is stored as 197,600 and
    "Expected CTC: ₹22,00,000" as 26,400 — and the field it fills is presented
    to the user under the note "The expectation stated on your resume." A
    derived figure shown under a sentence claiming the candidate chose it reads
    as a parse error, and the user's repair for a parse error is to type over
    it. So the suggester says which of the two things happened, and the number
    stops looking like a bug.
    """

    usd: int
    annualised: bool
    converted: bool

    def derivation(self) -> str | None:
        """How *usd* differs from what the candidate wrote, or None if it doesn't."""
        if self.annualised and self.converted:
            return "annualised and converted to USD"
        if self.annualised:
            return "annualised to a year's pay"
        if self.converted:
            return "converted to USD"
        return None


def stated_salary_expectation(text: str) -> StatedSalary | None:
    """A salary the candidate stated for themselves, or None if they didn't.

    **The figure returned is in US dollars**, converted from whatever the
    candidate wrote it in — see :data:`_MONEY`. It becomes ``profiles.salary_min``
    and is compared against bands that :func:`app.services.jd_parser.extract_salary`
    has already put in dollars, and a comparison between two currencies is not a
    rougher answer than one, it is a different question.

    **The figure returned is a year's pay**, annualised from whatever period the
    candidate quoted at :data:`app.services.pay_period.PERIOD_MULTIPLIERS`, for
    the same reason and against the same bands. A resume that says "Expected
    salary: €8.000 per month" is stating €96,000; the identical string in an
    employer's posting has been annualised since `jd_parser` learned to, and
    this side could not, because the code to do it lived in `jd_parser` and
    `jd_parser` imports this module. Monthly is how most of Europe and India
    quote pay, so this was not an edge: the floor came out twelve times under
    what the candidate asked for, or — under
    :data:`_SALARY_MIN_PLAUSIBLE` once converted — was dropped for one nobody
    chose.

    Only labelled figures count ("Salary expectation: $180,000"), and only ones
    in a plausible annual range. A range gives its lower bound: the preference
    this feeds is a *floor*, and reading "$150k–$180k" as 180 would quietly hide
    every job the candidate said they'd take.

    Returns None rather than a guess — the suggester falls back to a band
    inferred from seniority, and marks it as inferred rather than stated.
    """
    best: StatedSalary | None = None
    for match in _SALARY_EXPECTATION_RE.finditer(text):
        if _CURRENT_PAY_RE.search(text[: match.start()]):
            continue
        digits, thousands = match.group("amount"), match.group("thousands")
        value = int(re.sub(rf"[{re.escape(GROUPING_CHARS)}]", "", digits))
        if thousands:
            value *= 1_000
        # The whole match, not just the `currency` group: an unmarked figure
        # written in the lakh grouping is a rupee figure, and it is the digits
        # that say so. See :func:`app.services.currency.usd_rate`.
        rate = usd_rate(match.group(0))
        # Annualised before the floor is checked, because the floor's job is to
        # throw out a figure too small to be a *year's* pay and a rate that
        # names its period is not that figure — "$95 per hour" is $197,600 and
        # was being dropped for looking like the number ninety-five.
        multiplier = annual_multiplier(text, match.start(), match.end())
        # "rate" is a label for money only when the figure looks like money.
        # It is also the word in "target rate: 25,000 units shipped", where
        # nothing marks a currency and nothing names a period — and 25,000 is
        # over the floor, so without this the candidate's stated minimum became
        # a throughput number off their last job.
        if match.group("rate") and not (match.group("currency") or multiplier != 1):
            continue
        annualised = multiplier != 1 and value * multiplier * rate <= _SALARY_MAX_PLAUSIBLE
        if annualised:
            value *= multiplier
        if value < _SALARY_MIN_PLAUSIBLE:
            continue
        value = round(value * rate)
        if value > _SALARY_MAX_PLAUSIBLE:
            continue
        # Several labelled figures on one resume ("expected base", "target
        # package") describe the same want; the lowest is the honest floor.
        if best is None or value < best.usd:
            best = StatedSalary(
                usd=value, annualised=annualised, converted=rate != 1.0
            )
    return best


def extract_salary_expectation(text: str) -> int | None:
    """The dollar floor from :func:`stated_salary_expectation`, or None.

    Every caller that only wants the number, which until the suggester needed
    to explain the number was every caller there was.
    """
    stated = stated_salary_expectation(text)
    return stated.usd if stated is not None else None


def infer_seniority(text: str, years: int | None) -> str | None:
    """Classify seniority from title markers, falling back to years of experience."""
    head = "\n".join(text.splitlines()[:25])
    for level, markers in _SENIORITY_MARKER_RES:
        for marker, pattern in markers:
            veto = _MARKER_VETO.get(marker)
            for match in pattern.finditer(head):
                if veto is not None and veto.match(head, match.end()):
                    continue
                return level
    if years is None:
        return None
    if years >= 12:
        return "lead"
    if years >= 6:
        return "senior"
    if years >= 3:
        return "mid"
    return "junior"


def _section_body_offset(line: str, pattern: str) -> int | None:
    """Where a section's body starts on *line*, or None if it isn't that heading.

    Three shapes, all of them things real resumes extract as:

    * ``EXPERIENCE`` — the heading alone on its row; the body starts on the next
      line.
    * ``Experience:`` — the same, with a colon.
    * ``EDUCATION B.Sc. Physics | 1999 - 2002`` — the heading and the first row of
      its body on one line. A PDF has no rows, only coordinates, so any heading
      that shares a baseline with the text beside it extracts exactly like this.
      Insisting a heading be alone on its line is what made a five-page CV parse
      down to its skills: "EDUCATION" and "TECHNICAL SKILLS" were both there, both
      unreadable, and the slicer instead locked onto the first lowercase
      "experience" it found in the summary paragraph.

    The case test is what keeps the third shape honest. "Experience building
    payment systems" opens a summary, not a section, and the only thing telling a
    heading apart from a sentence that starts with the same word is that a heading
    is capitalised — or punctuated with a colon.
    """
    match = re.match(rf"[ \t]*({pattern})[ \t]*(:?)", line, re.I)
    if match is None:
        return None
    heading, colon = match.group(1), match.group(2)
    if heading.islower() and not colon:
        # A word out of a paragraph. Shattered extractions strew these about.
        return None
    if not line[match.end():].strip():
        return len(line)  # Heading alone; body begins after the newline.
    return match.end() if colon or heading.isupper() else None


def _heading_positions(text: str) -> list[tuple[str, int, int]]:
    """Every section heading in *text*, in document order.

    Each entry is ``(key, heading offset, body offset)``. The heading offset is
    where the *next* section's body has to stop, so an inline heading is never
    left dangling on the end of the section above it.
    """
    found: list[tuple[str, int, int]] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\n")
        for key, pattern in _SECTION_PATTERNS.items():
            body = _section_body_offset(line, pattern)
            if body is not None:
                found.append((key, offset, offset + body))
                break
        offset += len(raw)
    return found


def _section_span(text: str, key: str) -> tuple[int, int] | None:
    """``(start, end)`` of a named section's body, or None when it isn't present.

    A section runs from its heading to whichever other known heading comes next —
    which is why :data:`_SECTION_PATTERNS` lists headings nothing reads.
    """
    headings = _heading_positions(text)
    for index, (found, _, body_at) in enumerate(headings):
        if found != key:
            continue
        end = headings[index + 1][1] if index + 1 < len(headings) else len(text)
        return (body_at, max(body_at, end))
    return None


def _section(text: str, key: str) -> str:
    """Return the body of a named section, or '' when it isn't present."""
    span = _section_span(text, key)
    return text[span[0] : span[1]].strip() if span else ""


def extract_headline(text: str) -> str | None:
    """The role line under the name — 'Senior Backend Engineer' and the like."""
    role_words = (
        "engineer", "developer", "designer", "manager", "analyst", "scientist",
        "architect", "consultant", "specialist", "director", "lead", "researcher",
        "marketer", "recruiter", "administrator", "strategist", "producer",
    )
    for raw in text.splitlines()[:10]:
        line = raw.strip()
        if not (3 <= len(line) <= 90):
            continue
        if _EMAIL_RE.search(line) or _URL_RE.search(line):
            continue
        if any(w in line.lower() for w in role_words):
            return line.strip(" |·•-")
    return None


# Where a job header starts inside a line that has a paragraph in front of it.
# Prose runs in lowercase and a job title is capitalised, so the boundary is a
# lowercase word followed by an ALL-CAPS one. Requiring caps rather than merely a
# capital is what tells "…query execution CTO & FOUNDER | OneNet Inc" apart from a
# company named mid-sentence ("…integrated Giant Protocol"), which a bare
# capital-letter test would cut at instead.
# \s+ rather than \s: a PDF puts two and three spaces between runs constantly, and
# a single-space rule missed the boundary whenever it did.
_LABEL_START_RE = re.compile(r"(?<=[a-z0-9)\]])\s+(?=[A-Z]{2,}\b)")
# How far back from the dates a job header can reasonably reach.
_LABEL_WINDOW = 120
# "Title | Company", "Title at Company", "Title, Company", "Title — Company".
_LABEL_SEPARATORS = re.compile(r"\s+at\s+|\s*[|•·]\s*|\s+[—–]\s+|,\s+")

_MAX_EXPERIENCE_ENTRIES = 12


def _job_label(before: str) -> str:
    """The job header out of whatever text precedes a date range.

    Ideally that text *is* the header. When a PDF puts the previous bullet on the
    same line — and it routinely does, because a PDF has rows only by coordinate —
    the tail of a paragraph arrives with it, and the prose is dropped here.
    """
    window = before[-_LABEL_WINDOW:]
    starts = [m.end() for m in _LABEL_START_RE.finditer(window)]
    if starts:
        window = window[starts[-1] :]
    elif len(before) > _LABEL_WINDOW:
        window = window.partition(" ")[2]  # Drop the word the window cut in half.
    return window.strip(" ,|–—-•·\t")


# An acronym, kept as the candidate wrote it. Everything longer is a word that a
# resume happens to have set in caps.
_ACRONYM_MAX = 3


def _decapitalise(value: str) -> str:
    """Title-case an ALL-CAPS heading, leaving acronyms alone.

    Resumes set job titles in caps for layout, and those titles now reach a
    recruiter's inbox — "SOFTWARE ARCHITECT" reads as shouting. Plain ``.title()``
    is not the answer: it turns "CTO & FOUNDER" into "Cto & Founder". Only words
    long enough not to be an acronym are recased, so "PRINCIPAL AI ENGINEER"
    becomes "Principal AI Engineer".
    """
    return " ".join(
        word.title() if word.isupper() and len(word) > _ACRONYM_MAX else word
        for word in value.split()
    )


def _split_label(label: str) -> tuple[str | None, str | None]:
    """Split a job header into ``(title, company)``, dropping any trailing parts.

    A header carries a location as often as not ("CTO | OneNet Inc | Canada"), and
    keeping it meant the company field read "OneNet Inc | Canada" — which then went
    into an email as the name of the company.
    """
    parts = [
        _decapitalise(part.strip())
        for part in _LABEL_SEPARATORS.split(label)
        if part.strip()
    ]
    if not parts:
        return None, None
    return parts[0], (parts[1] if len(parts) > 1 else None)


def extract_experience(text: str) -> list[dict[str, Any]]:
    """Pull rough {company, title, start, end} records from the experience section.

    Anchored on the date ranges rather than on lines. A line was the wrong unit:
    entries were skipped for being over a length cap, and only the first of several
    on one line was ever read — both of which a PDF produces by itself, without the
    resume doing anything unusual.
    """
    body = _section(text, "experience")
    if not body:
        return []
    entries: list[dict[str, Any]] = []
    for raw in body.splitlines():
        line = raw.strip(" •-·\t")
        cursor = 0
        for match in _RANGE_RE.finditer(line):
            title, company = _split_label(_job_label(line[cursor : match.start()]))
            cursor = match.end()
            entries.append(
                {
                    "title": title,
                    "company": company,
                    "start": match.group(1),
                    "end": match.group(2).title(),
                }
            )
            if len(entries) >= _MAX_EXPERIENCE_ENTRIES:
                return entries
    return entries


def extract_education(text: str) -> list[dict[str, Any]]:
    """Pull rough {school, degree, year} records from the education section."""
    body = _section(text, "education")
    if not body:
        return []
    degree_re = re.compile(
        r"\b(b\.?s\.?c?|m\.?s\.?c?|ph\.?d|b\.?tech|m\.?tech|mba|bachelor'?s?|"
        r"master'?s?|doctorate|associate'?s?)\b",
        re.I,
    )
    entries: list[dict[str, Any]] = []
    for raw in body.splitlines():
        line = raw.strip(" •-·\t")
        if not line or len(line) > 160:
            continue
        degree = degree_re.search(line)
        year = _YEAR_RE.search(line)
        if degree is None and year is None:
            continue
        entries.append(
            {
                "school": line,
                "degree": degree.group(0) if degree else None,
                "year": year.group(0) if year else None,
            }
        )
        if len(entries) >= 6:
            break
    return entries


def extract_summary(text: str) -> str | None:
    body = _section(text, "summary")
    return " ".join(body.split())[:600] or None if body else None


def derive_target_roles(headline: str | None, experience: list[dict[str, Any]]) -> list[str]:
    """Best-guess roles to pitch: the headline plus the most recent job titles."""
    roles: list[str] = []
    for candidate in [headline, *(e.get("title") for e in experience)]:
        if not candidate:
            continue
        cleaned = re.sub(r"\s+", " ", candidate).strip(" ,|·•-")
        if 2 < len(cleaned) <= 60 and cleaned.lower() not in {r.lower() for r in roles}:
            roles.append(cleaned)
        if len(roles) >= 3:
            break
    return roles


def parse_heuristic(text: str) -> ParsedResume:
    """Structured extraction with no network calls — always available."""
    years = extract_years_experience(text)
    headline = extract_headline(text)
    experience = extract_experience(text)
    return ParsedResume(
        full_name=extract_name(text),
        email=extract_email(text),
        phone=extract_phone(text),
        location=extract_location(text),
        headline=headline,
        summary=extract_summary(text),
        years_experience=years,
        seniority=infer_seniority(text, years),
        skills=extract_skills(text),
        target_roles=derive_target_roles(headline, experience),
        experience=experience,
        education=extract_education(text),
        links=extract_links(text),
        parsed_with="heuristic",
    )


# --------------------------------------------------------------------------- #
# LLM enrichment
# --------------------------------------------------------------------------- #

_LLM_SYSTEM_PROMPT = (
    "You extract structured data from resumes. Return ONLY a JSON object — no "
    "prose, no markdown fence — with these keys:\n"
    '{"full_name": str|null, "location": str|null, "headline": str|null, '
    '"summary": str|null, "years_experience": int|null, '
    '"seniority": "junior"|"mid"|"senior"|"lead"|"exec"|null, '
    '"skills": [str], "target_roles": [str], "target_industries": [str], '
    '"experience": [{"company": str, "title": str, "start": str, "end": str}], '
    '"education": [{"school": str, "degree": str, "year": str}]}\n'
    "headline is a short role line (e.g. 'Senior Backend Engineer'). target_roles "
    "are 1-3 job titles this person should be pitched for. target_industries are "
    "1-3 industries they have worked in. Never invent facts: use null or [] when "
    "the resume does not say."
)

_LLM_ALLOWED_SENIORITY = {"junior", "mid", "senior", "lead", "exec"}


def _coerce_str_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:100])
        if len(out) >= limit:
            break
    return out


def _coerce_dict_list(value: Any, keys: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        record = {
            k: (str(item[k])[:120] if item.get(k) not in (None, "") else None) for k in keys
        }
        if any(record.values()):
            out.append(record)
        if len(out) >= limit:
            break
    return out


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the JSON object out of a model response, fences and all.

    A second copy of this lived here and drifted: it kept the first-brace to
    last-brace span after the shared one moved to a balanced scan, so a resume
    parse was still losing every response whose prose carried a brace. There is
    one implementation now, and this name stays only because the module's tests
    and call sites use it.
    """
    return extract_json_object(raw)


def _merge_llm(base: ParsedResume, data: dict[str, Any]) -> ParsedResume:
    """Overlay validated LLM fields onto the heuristic result.

    The heuristic values for email/phone/links are kept unconditionally — a regex
    beats a model at copying an address without typos.
    """
    for key in ("full_name", "location", "headline", "summary"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            setattr(base, key, value.strip()[: 600 if key == "summary" else 255])

    years = data.get("years_experience")
    if isinstance(years, (int, float)) and 0 < int(years) <= 60:
        base.years_experience = int(years)

    seniority = data.get("seniority")
    if isinstance(seniority, str) and seniority.lower() in _LLM_ALLOWED_SENIORITY:
        base.seniority = seniority.lower()

    # Union the skills so a model omission can't drop a keyword we did find.
    llm_skills = _coerce_str_list(data.get("skills"), 40)
    base.skills = list(dict.fromkeys([*base.skills, *(s.lower() for s in llm_skills)]))

    for key, limit in (("target_roles", 5), ("target_industries", 5)):
        values = _coerce_str_list(data.get(key), limit)
        if values:
            setattr(base, key, values)

    experience = _coerce_dict_list(
        data.get("experience"), ("company", "title", "start", "end"), 12
    )
    if experience:
        base.experience = experience
    education = _coerce_dict_list(data.get("education"), ("school", "degree", "year"), 6)
    if education:
        base.education = education

    base.parsed_with = "llm"
    return base


# How much resume the model is shown. The old budget was 6k characters, on the
# belief that it "covers a long resume" — a five-page CV extracts to around 20k, so
# 6k reached the middle of the second job and stopped. Everything the prompt asks
# for and heuristics are weakest at (the rest of the work history, education,
# which sit at the *end* of a resume) was behind the cut, and because
# :func:`_merge_llm` only overwrites a field the model answered, the result was a
# profile confidently listing two jobs and no degree.
_LLM_INPUT_CHARS = 24_000
# When even that isn't enough, this much of the top is kept verbatim — name,
# contact block, headline, summary — and the rest of the budget is split between
# the sections the prompt actually asks about.
_LLM_HEAD_SHARE = 3
_LLM_BODY_SECTIONS = ("experience", "education", "skills")


def _llm_input(text: str, limit: int = _LLM_INPUT_CHARS) -> str:
    """The resume as the model should see it, within *limit* characters.

    Short of the limit this is the whole document. Past it, a blind prefix is the
    one thing it must not be: a resume is ordered oldest-question-first — who am I,
    then what I did, then where I studied — so cutting the tail cuts precisely the
    fields being asked for. The head is kept for identity, and each remaining
    section gets a share of what's left, so a long resume loses the middle of a
    paragraph instead of its entire work history.
    """
    if len(text) <= limit:
        return text

    head_chars = limit // _LLM_HEAD_SHARE
    head = text[:head_chars]
    parts: list[str] = []
    share = (limit - len(head)) // len(_LLM_BODY_SECTIONS)
    for key in _LLM_BODY_SECTIONS:
        span = _section_span(text, key)
        if span is None or span[1] <= head_chars:
            continue  # Absent, or already inside the head.
        body = text[max(span[0], head_chars) : span[1]].strip()
        if body:
            parts.append(f"\n\n{key.upper()}\n{body[:share]}")
    if not parts:
        # Nothing recognisable to be selective about — spend the whole budget on
        # the front of the document rather than a third of it.
        return text[:limit]
    return "".join([head, *parts])[:limit]


def enrich_with_llm(text: str, base: ParsedResume) -> ParsedResume:
    """Ask an OpenRouter free model to fill in what heuristics missed.

    Returns *base* unchanged on any failure — parsing must never fail an upload.
    """
    if not llm_is_configured():
        return base
    try:
        raw = chat_completion(
            [
                {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                {"role": "user", "content": _llm_input(text)},
            ],
            model=settings.openrouter_model,
            temperature=0.0,
            max_tokens=1200,
        )
    except OpenRouterError:
        return base

    data = _extract_json_object(raw)
    return _merge_llm(base, data) if data else base


def parse_resume(text: str, *, use_llm: bool = True) -> ParsedResume:
    """Full parse: heuristics, then optional LLM enrichment on top."""
    parsed = parse_heuristic(text)
    return enrich_with_llm(text, parsed) if use_llm else parsed
