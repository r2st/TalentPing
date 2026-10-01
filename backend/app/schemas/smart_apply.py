"""Schemas for the Smart Apply endpoints: tailoring, fit scoring, job feed."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import settings
from app.core.enums import FilterEnum
from app.models.job import JobStatus


class JobInput(BaseModel):
    """How a caller names the job: pasted text, a URL, or a saved posting.

    Exactly one source is needed. Pasted text wins over a URL when both are
    given — it is what the user actually read, whereas a fetch can land on a
    cookie wall and happily parse that instead.
    """

    job_description: str | None = Field(default=None, max_length=60000)
    job_url: str | None = Field(default=None, max_length=2000)
    job_posting_id: int | None = None

    @model_validator(mode="after")
    def _require_a_source(self) -> JobInput:
        if not (self.job_description or self.job_url or self.job_posting_id):
            raise ValueError("Provide a job description, a job URL, or a job_posting_id")
        return self


class ParsedJobOut(BaseModel):
    """The structured posting both tailoring and scoring worked from."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    remote: bool | None = None
    seniority: str | None = None
    years_required: int | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    industry: str | None = None
    salary_text: str | None = None
    parsed_with: str = "heuristic"


# --------------------------------------------------------------------------- #
# Tailoring                                                                    #
# --------------------------------------------------------------------------- #


class TailorRequest(JobInput):
    resume_id: int | None = None
    # Also compute and store the fit score in the same round trip — the UI shows
    # both together, and re-parsing the JD twice is wasteful.
    include_fit_score: bool = True
    # Write the job-specific letter too. Off by default so the existing tailor
    # call keeps its latency; the Smart Apply UI asks for it explicitly.
    include_cover_letter: bool = False


class TailoredResumeOut(BaseModel):
    id: int
    resume_id: int
    job_posting_id: int | None = None

    job_title: str | None = None
    job_company: str | None = None
    job_url: str | None = None

    tailored_summary: str | None = None
    ordered_skills: list[str] = Field(default_factory=list)
    highlighted_experience: list[dict[str, Any]] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)
    cover_letter: str | None = None

    generated_with: str | None = None
    model: str | None = None
    # How the bullets were produced, tracked apart from the summary: a rejected
    # bullet rewrite doesn't make the whole run heuristic, and the UI says which.
    bullets_generated_with: str | None = None
    # Whether a PDF is stored for this run, without shipping the bytes. Drives
    # the download button; false means only the Markdown route is available.
    has_pdf: bool = False
    pdf_filename: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class CoverLetterOut(BaseModel):
    """One job-specific letter, as the application detail view renders it."""

    id: int
    resume_id: int
    job_posting_id: int | None = None
    tailored_resume_id: int | None = None

    job_title: str | None = None
    job_company: str | None = None

    greeting: str | None = None
    body: str | None = None
    sign_off: str | None = None
    # The whole letter as it is sent and downloaded.
    full_text: str = ""

    # The verified company facts the personalization was allowed to draw on.
    # Shown beside the letter so a reader can check the basis of a claim rather
    # than trusting it.
    company_research: list[str] = Field(default_factory=list)
    highlights: list[dict[str, Any]] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)

    delivery: str = "inline"
    edited: bool = False
    generated_with: str | None = None
    model: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class CoverLetterRequest(JobInput):
    """Ask for a letter for one posting."""

    resume_id: int | None = None
    tailored_resume_id: int | None = None
    # inline | attachment — how it travels with the outreach.
    delivery: str = Field(default="inline", pattern="^(inline|attachment)$")
    # Overwrite a letter the user has hand-edited. Off by default: a
    # regeneration triggered by a re-scan must never discard someone's own words.
    force: bool = False


class CoverLetterEdit(BaseModel):
    """A user's own wording, replacing the generated body."""

    body: str = Field(min_length=1, max_length=20000)


class TailorResponse(BaseModel):
    tailored: TailoredResumeOut
    parsed_job: ParsedJobOut
    fit: FitScoreOut | None = None
    # Rendered Markdown, so the UI can preview and download without a second call.
    markdown: str
    # The job-specific letter, when one was requested alongside the tailoring.
    cover_letter: CoverLetterOut | None = None


# --------------------------------------------------------------------------- #
# Fit scoring                                                                  #
# --------------------------------------------------------------------------- #


class FitRequest(JobInput):
    resume_id: int | None = None


class FitBreakdown(BaseModel):
    """One dimension's contribution, in the units the UI displays."""

    score: float  # 0..100 for this dimension alone
    weight: float  # its share of the overall score
    note: str


class FitScoreOut(BaseModel):
    id: int | None = None
    resume_id: int
    job_posting_id: int | None = None

    job_title: str | None = None
    job_company: str | None = None

    overall: float
    recommendation: str
    summary: str | None = None

    breakdown: dict[str, FitBreakdown] = Field(default_factory=dict)
    # True when `fit_scorer.UNEVALUATED_CAP` held `overall` down: neither the
    # skills nor the title could be judged, so the weighted total was built out
    # of neutrality alone. The client needs it to explain the gap — the capped
    # `overall` and the uncapped `breakdown` are both true and they do not add
    # up, and a bare "55" under six bars averaging 74 reads as an arithmetic bug.
    capped: bool = False
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class FitResponse(BaseModel):
    fit: FitScoreOut
    parsed_job: ParsedJobOut


# --------------------------------------------------------------------------- #
# Job feed / saved searches                                                    #
# --------------------------------------------------------------------------- #


class JobPostingOut(BaseModel):
    id: int
    search_id: int | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    salary_text: str | None = None
    # The advertised band, parsed. Null when the employer published none, which
    # is most of them — never read as "this role pays nothing".
    salary_min: int | None = None
    salary_max: int | None = None
    remote: bool | None = None
    source: str | None = None
    posted_at: datetime | None = None
    status: JobStatus
    fit_score: float | None = None
    # Scout's re-rank of the shortlist, and the sentence behind it. Both null
    # until the LLM pass runs — the deterministic score above stands alone.
    llm_fit_score: float | None = None
    llm_reasoning: str | None = None
    # Which of the candidate's profiles this posting scored best against — the
    # one whose resume and cover letter an application would carry. Null for a
    # user with no profiles, and for rows scored before profiles existed.
    matched_profile_id: int | None = None
    matched_profile_name: str | None = None
    # Set when this row is the same role as another, surfaced by a second board.
    duplicate_of_id: int | None = None
    # Every board carrying this role: [{"source": …, "url": …}].
    source_urls: list[dict[str, str]] = Field(default_factory=list)
    # Ghost-job signals. ``ghost_risk`` is 0-100 and ``ghost_level`` is the
    # badge derived from it; both null for rows stored before detection shipped,
    # which the UI shows as no badge rather than as a clean bill of health.
    ghost_risk: int | None = None
    ghost_level: str | None = None
    ghost_reasons: list[str] = Field(default_factory=list)
    # How many times this role has been re-advertised with a fresher date.
    repost_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("ghost_reasons", "source_urls", mode="before")
    @classmethod
    def _null_json_is_empty(cls, value):
        """A nullable JSON column reads as ``None``, and the feed wants a list.

        Both columns are written as lists on every path that creates a row, but
        rows predating either column exist and would otherwise 500 the whole
        feed on serialization rather than render one card without a badge.
        """
        return [] if value is None else value


class JobPostingDetail(JobPostingOut):
    description: str | None = None


# --------------------------------------------------------------------------- #
# Job intel: salary benchmark + company research                               #
# --------------------------------------------------------------------------- #


class SalaryBandOut(BaseModel):
    """The market range for this kind of role in this market."""

    role_family: str
    role_label: str
    seniority: str
    location_key: str
    location_label: str
    currency: str
    min: int
    median: int
    max: int
    # modelled | levels_fyi | glassdoor | manual — how much to claim for it.
    source: str
    sample_size: int


class SalaryComparisonOut(BaseModel):
    """The posting's own band, positioned against the market one."""

    offered_min: int | None = None
    offered_max: int | None = None
    offered_mid: int | None = None
    # Fraction of the market median: 0.12 is 12% above it.
    delta: float | None = None
    # above | at | below | unknown
    verdict: str
    label: str


class SalaryInsightOut(BaseModel):
    band: SalaryBandOut
    comparison: SalaryComparisonOut
    # True while the band is modelled rather than observed, so the card can say so.
    is_estimate: bool


class CompanyNewsOut(BaseModel):
    title: str
    published: str | None = None
    source: str | None = None


class CompanyProfileOut(BaseModel):
    name: str
    domain: str | None = None
    size: str | None = None
    employee_count: int | None = None
    founded_year: int | None = None
    headquarters: str | None = None
    industry: str | None = None
    funding_stage: str | None = None
    funding_total: str | None = None
    glassdoor_rating: float | None = None
    tech_stack: list[str] = Field(default_factory=list)
    news: list[CompanyNewsOut] = Field(default_factory=list)
    summary: str | None = None
    # llm | heuristic | manual. An `llm` card is recollection, not a lookup, and
    # the UI labels it as an estimate — `note` carries that caveat in words.
    source: str
    status: str
    note: str | None = None
    researched_at: datetime | None = None

    model_config = {"from_attributes": True}


class JobIntelOut(BaseModel):
    """Everything the expandable panel under a job card needs, in one call."""

    job_id: int
    # Scout's re-rank, repeated here so the panel is self-contained.
    llm_fit_score: float | None = None
    llm_reasoning: str | None = None
    salary: SalaryInsightOut | None = None
    company: CompanyProfileOut | None = None
    # Other boards carrying the same role, from the dedup pass.
    also_on: list[dict[str, str]] = Field(default_factory=list)


class JobPostingPatch(BaseModel):
    """Triage a posting: save it, dismiss it, mark it applied."""

    status: JobStatus


class BulkJobAction(FilterEnum):
    """What a bulk triage does to every posting in the selection.

    ``archive`` deletes the rows outright. There is no ARCHIVED status to move
    them to, and "dismissed" already hides a posting from the feed — so archive
    means the stronger thing, and the UI confirms before calling it.
    """

    SAVE = "save"
    APPLY = "apply"
    DISMISS = "dismiss"
    ARCHIVE = "archive"


class BulkJobRequest(BaseModel):
    job_ids: list[int] = Field(min_length=1, max_length=500)
    action: BulkJobAction


class BulkJobResult(BaseModel):
    action: BulkJobAction
    # Postings actually changed. Ids belonging to someone else, or already in
    # the target status, are reported rather than silently counted as done.
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    not_found: list[int] = Field(default_factory=list)


class JobSearchCreate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    resume_id: int | None = None
    roles: list[str] = Field(default_factory=list, max_length=10)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    location: str | None = Field(default=None, max_length=255)
    remote_only: bool = False
    min_fit_score: int = Field(default=60, ge=0, le=100)
    # Annual salary floor. Postings that advertise a band topping out below it
    # are screened out; postings that advertise nothing are still surfaced.
    min_salary: int | None = Field(default=None, ge=0, le=10_000_000)
    # Ghost-risk ceiling. Postings scoring above it are scanned, counted in the
    # run result, and not stored. 100 turns suppression off without turning the
    # badge off — the score is still computed and shown either way.
    max_ghost_risk: int = Field(
        default_factory=lambda: settings.ghost_default_max_risk, ge=0, le=100
    )
    interval_hours: int = Field(default=6, ge=1, le=168)

    @model_validator(mode="after")
    def _require_criteria(self) -> JobSearchCreate:
        if not self.roles and not self.keywords:
            raise ValueError("Give the search at least one role or keyword")
        return self


class JobSearchPatch(BaseModel):
    """A partial edit to a saved search.

    Every bound here is the one :class:`JobSearchCreate` already states. They are
    repeated rather than inherited because the two models differ in optionality
    on every field, but they must not differ in *limits*: a PATCH that accepted
    what a POST refuses is simply the way around the POST's validation, and this
    one was — ``roles`` and ``keywords`` had no cap at all, and ``location`` was
    unbounded against a ``varchar(255)`` column, so a long value answered 500
    from the database rather than 422 from the schema. ``tests/test_patch_bounds.py``
    holds the two side by side so they cannot drift apart again.
    """

    name: str | None = Field(default=None, max_length=255)
    roles: list[str] | None = Field(default=None, max_length=10)
    keywords: list[str] | None = Field(default=None, max_length=20)
    location: str | None = Field(default=None, max_length=255)
    remote_only: bool | None = None
    min_fit_score: int | None = Field(default=None, ge=0, le=100)
    min_salary: int | None = Field(default=None, ge=0, le=10_000_000)
    max_ghost_risk: int | None = Field(default=None, ge=0, le=100)
    interval_hours: int | None = Field(default=None, ge=1, le=168)
    is_active: bool | None = None


class JobSearchOut(BaseModel):
    id: int
    name: str
    resume_id: int | None = None
    roles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    location: str | None = None
    remote_only: bool
    min_fit_score: int
    min_salary: int | None = None
    max_ghost_risk: int
    interval_hours: int
    is_active: bool
    last_run_at: datetime | None = None
    last_error: str | None = None
    jobs_found: int
    created_at: datetime

    model_config = {"from_attributes": True}


class JobSearchRunResult(BaseModel):
    search_id: int
    scanned: int = 0
    added: int = 0
    below_threshold: int = 0
    # Screened out by the search's salary floor rather than by its fit bar.
    below_salary: int = 0
    duplicates: int = 0
    # Copies of a role found on a second board and folded into one row.
    merged: int = 0
    # Postings Scout gave a second opinion on.
    reranked: int = 0
    # Screened out above the search's ghost-risk ceiling, and roles this scan
    # saw re-advertised with a fresher date. Both reported so a scan that
    # suppressed half its finds can say so instead of just looking empty.
    ghosts: int = 0
    reposts: int = 0
    jobs: list[JobPostingOut] = Field(default_factory=list)
    detail: str | None = None


TailorResponse.model_rebuild()
