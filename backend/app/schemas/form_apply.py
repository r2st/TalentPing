"""Schemas for form-based applications and the answer bank."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.form_apply import ATSPlatform, FormApplyStatus
from app.schemas.linkedin import LinkedInBudgetOut, LinkedInStatusOut


class FormApplyRequest(BaseModel):
    """Start a form application against a saved posting.

    ``submit`` is the irreversible half and defaults to off: the run fills the
    form and stops at the button unless the caller explicitly asks for more.
    """

    submit: bool = False
    resume_id: int | None = None


class FormApplyUrlRequest(FormApplyRequest):
    """Same, for a URL the candidate pasted rather than a posting in the feed."""

    url: str = Field(max_length=2000)


class FormApplicationOut(BaseModel):
    id: int
    job_posting_id: int | None = None
    platform: ATSPlatform
    platform_label: str = ""
    url: str | None = None
    job_title: str | None = None
    company: str | None = None
    status: FormApplyStatus
    submit_requested: bool
    attempts: int
    max_attempts: int
    resume_uploaded: bool
    note: str | None = None
    error: str | None = None
    screenshot_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FormApplicationDetail(FormApplicationOut):
    """The full audit trail — what was filled, what was answered, what was seen."""

    filled_fields: list[str] = Field(default_factory=list)
    answers: list[dict[str, Any]] = Field(default_factory=list)
    unanswered: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)


class FormApplyProfileUpdate(BaseModel):
    """The answers only the candidate can give. Every field optional."""

    work_authorized: bool | None = None
    requires_sponsorship: bool | None = None
    willing_to_relocate: bool | None = None
    earliest_start: str | None = Field(default=None, max_length=120)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    desired_salary: str | None = Field(default=None, max_length=120)

    phone: str | None = Field(default=None, max_length=64)
    linkedin_url: str | None = Field(default=None, max_length=500)
    website_url: str | None = Field(default=None, max_length=500)
    github_url: str | None = Field(default=None, max_length=500)
    address_city: str | None = Field(default=None, max_length=120)
    address_country: str | None = Field(default=None, max_length=120)

    custom_answers: dict[str, str] | None = None
    llm_answers_enabled: bool | None = None


class FormApplyProfileOut(BaseModel):
    id: int
    user_id: int
    work_authorized: bool | None = None
    requires_sponsorship: bool | None = None
    willing_to_relocate: bool | None = None
    earliest_start: str | None = None
    notice_period_days: int | None = None
    desired_salary: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    website_url: str | None = None
    github_url: str | None = None
    address_city: str | None = None
    address_country: str | None = None
    custom_answers: dict[str, str] = Field(default_factory=dict)
    llm_answers_enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class PlatformSupportOut(BaseModel):
    platform: str
    label: str
    adapter: bool
    requires_account: bool


class FormApplyBudgetOut(BaseModel):
    """The form-apply run budget — a different quantity from the LinkedIn one.

    ``used``/``limit``/``remaining`` count *form submissions* across every ATS.
    ``linkedin`` is the separate Easy Apply cap, nested here because the Jobs
    page decides both buttons off one response.
    """

    used: int
    limit: int
    remaining: int
    linkedin: LinkedInBudgetOut


class FormApplyServiceStatus(BaseModel):
    """What the Jobs page needs to offer — or explain the absence of — Form apply."""

    enabled: bool
    browser_installed: bool
    screenshots: bool
    platforms: list[PlatformSupportOut] = Field(default_factory=list)
    budget: FormApplyBudgetOut
    linkedin: LinkedInStatusOut


class FormApplyDispatch(BaseModel):
    """The response to starting a run: the row, and whether a worker took it."""

    application: FormApplicationDetail
    queued: bool = False
    detail: str | None = None
