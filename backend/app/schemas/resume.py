"""Resume schemas — output only; every field is machine-extracted."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

# The same closed set `app/schemas/profile.py` validates against, and the same
# one `resume_parser.classify_seniority` produces.
SENIORITY_LEVELS = "^(junior|mid|senior|lead|exec)$"


class ResumeOut(BaseModel):
    id: int
    user_id: int
    filename: str | None = None

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    headline: str | None = None
    summary: str | None = None
    years_experience: int | None = None
    seniority: str | None = None

    skills: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)

    is_default: bool
    parsed_with: str | None = None
    created_at: datetime

    # ---- The upload behind the parse ----
    # Whether the candidate's own file is on record, and what it is. The preview
    # needs both before it opens anything: without an original the document it
    # gets back is a PDF rendered from the parse whatever the upload was called,
    # and a .docx original has no in-page viewer at all — so the UI offers it as
    # a download instead of an empty frame. Never the bytes; those are deferred
    # on the model precisely so listing resumes doesn't load them.
    has_original_file: bool = False
    file_content_type: str | None = None
    file_size: int | None = None

    model_config = {"from_attributes": True}


class ExperienceEntry(BaseModel):
    """One role on the CV, as the parser stores it and the user may fix it.

    Every field optional: a candidate correcting a garbled job title should not
    have to invent an end date to save it, and the composer reads these with
    ``.get`` throughout.
    """

    company: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    start: str | None = Field(default=None, max_length=40)
    end: str | None = Field(default=None, max_length=40)

    # Unknown keys are dropped rather than refused. The client round-trips
    # whatever the parser wrote, and a future parser field would otherwise make
    # every save of an old row a 422 — a validation rule that breaks editing for
    # data the product itself produced.
    model_config = {"extra": "ignore"}


class EducationEntry(BaseModel):
    school: str | None = Field(default=None, max_length=200)
    degree: str | None = Field(default=None, max_length=200)
    field: str | None = Field(default=None, max_length=200)
    year: str | None = Field(default=None, max_length=40)

    model_config = {"extra": "ignore"}


class ResumePatch(BaseModel):
    """Correcting the extraction.

    This used to be four fields — roles, industries, skills, default — and the
    limit was not a considered scope. Everything a recruiter actually *reads*
    was outside it: :class:`~app.services.ai_composer.CandidateContext` builds
    every outreach email from ``full_name``, ``headline``, ``years_experience``,
    ``seniority``, ``location`` and ``experience``, the cover letter signs off
    with ``full_name``, and ``career_apply_service`` types ``email`` and
    ``phone`` into real application forms. A CV whose header line parsed into
    the name field meant a candidate's every email opened under the wrong name,
    with no way to fix it short of deleting the resume and re-uploading a file
    that would parse the same way.

    Not editable here, deliberately: ``raw_text`` (the excerpt the composer
    quotes must stay what the document says, not what the user wishes it said),
    the stored file and its metadata (a patch is not an upload), and
    ``parsed_with`` (a record of how, not a field).

    Lists are bounded. Unbounded was not free even before this: ``skills`` is
    interpolated into every LLM prompt this product sends, so a client that
    posted ten thousand of them would have made every subsequent send expensive
    and then failed on context length, one user at a time.
    """

    full_name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=255)
    headline: str | None = Field(default=None, max_length=255)
    summary: str | None = Field(default=None, max_length=4000)
    # 60 rather than a round number: past a working life nobody is correcting a
    # parse, they are testing the endpoint.
    years_experience: int | None = Field(default=None, ge=0, le=60)
    seniority: str | None = Field(default=None, pattern=SENIORITY_LEVELS)

    target_roles: list[str] | None = Field(default=None, max_length=12)
    target_industries: list[str] | None = Field(default=None, max_length=12)
    skills: list[str] | None = Field(default=None, max_length=60)
    experience: list[ExperienceEntry] | None = Field(default=None, max_length=20)
    education: list[EducationEntry] | None = Field(default=None, max_length=10)
    links: list[str] | None = Field(default=None, max_length=10)

    is_default: bool | None = None


class SuggestedPreferenceValues(BaseModel):
    """The preference-form values a resume implies. Shape mirrors AutopilotUpdate."""

    target_roles: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_only: bool = False
    salary_min: int | None = None
    min_fit_score: int
    daily_application_limit: int
    auto_send: bool
    # How many emails the user approves by hand before auto-send takes over. The
    # suggester has always produced this; leaving it off the schema meant Pydantic
    # dropped it on the way out, the wizard never saw it, and every user saved a
    # trial of 0 — auto-send from the first email, which is the opposite of what
    # the suggestion promises in `notes`.
    auto_send_trial_approvals: int


class ExtractedProfile(BaseModel):
    """What the parse read off the resumes — shown so the user can check it.

    Facts, not preferences: the confirm step renders these as a "here's what we
    read" panel above the editable fields, so a bad extraction is visible before
    it silently shapes the search.
    """

    resume_ids: list[int] = Field(default_factory=list)
    resume_count: int = 0
    full_name: str | None = None
    location: str | None = None
    seniority: str | None = None
    years_experience: int | None = None
    skills: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)


class SuggestedPreferencesOut(BaseModel):
    """Suggestions plus provenance, so the wizard can mark what it pre-filled.

    ``sources`` maps a field name to ``resume`` (copied off the parse),
    ``inferred`` (derived from something else on it) or ``default`` (a product
    default, not resume-derived). A field missing from ``sources`` had no
    evidence behind it and is left empty for the user.
    """

    # The single resume these came from, or None when they were merged across
    # every resume the user has uploaded.
    resume_id: int | None = None
    suggestions: SuggestedPreferenceValues
    sources: dict[str, str] = Field(default_factory=dict)
    notes: dict[str, str] = Field(default_factory=dict)
    # Fields the resume informed — everything in `sources` except the defaults.
    prefilled_fields: list[str] = Field(default_factory=list)
    profile: ExtractedProfile = Field(default_factory=ExtractedProfile)
