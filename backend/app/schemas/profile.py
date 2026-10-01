"""Profile schemas — the several jobs one candidate would take.

Validation here is deliberately thin. A profile is the user's own statement of
what they want; the only things worth refusing are those that would make the
matcher behave strangely later — an empty name, a salary band that runs
backwards, an unknown seniority word.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

EXPERIENCE_LEVELS = "^(junior|mid|senior|lead|exec)$"
EMPLOYMENT_TYPES = "^(full_time|part_time|contract|internship|temporary)$"
COMPANY_SIZES = "^(startup|scaleup|midsize|enterprise)$"

# The constraint lists are short by construction — there are five employment
# types and four size tiers in total, so a list longer than the vocabulary is a
# client bug rather than a preference. The exclusion list is the one that grows,
# and 50 is well past any real candidate's list of employers to avoid while
# still bounding what a gate has to walk on every posting of every run.
_MAX_EXCLUDED = 50


class ProfileBase(BaseModel):
    target_roles: list[str] = Field(default_factory=list, max_length=12)
    target_industries: list[str] = Field(default_factory=list, max_length=12)
    skills: list[str] = Field(default_factory=list, max_length=60)
    location_preferences: list[str] = Field(default_factory=list, max_length=12)
    remote_only: bool = False
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    experience_level: str | None = Field(default=None, pattern=EXPERIENCE_LEVELS)

    # ---- Constraints ----
    # Validated against a closed vocabulary rather than accepted as free text:
    # these are matched by equality inside a gate, so a typo would not be a
    # loose filter, it would be a filter that silently rejects every posting.
    employment_types: list[Annotated[str, Field(pattern=EMPLOYMENT_TYPES)]] = Field(
        default_factory=list, max_length=5
    )
    company_sizes: list[Annotated[str, Field(pattern=COMPANY_SIZES)]] = Field(
        default_factory=list, max_length=4
    )
    excluded_companies: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=_MAX_EXCLUDED
    )

    @model_validator(mode="after")
    def _band_runs_forwards(self) -> ProfileBase:
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_max < self.salary_min
        ):
            raise ValueError("salary_max must be at or above salary_min")
        return self


class ProfileCreate(ProfileBase):
    name: str = Field(min_length=1, max_length=120)
    resume_id: int | None = None
    # Which connected mailbox this profile's outreach argues from. Null means the
    # user's primary — which is what every single-mailbox user gets without the
    # UI ever showing them the control.
    gmail_account_id: int | None = None
    is_active: bool = True
    # Making a profile the default demotes whichever one held it. Left false so
    # creating a second profile never quietly redirects the first one's work.
    is_default: bool = False


class ProfileUpdate(BaseModel):
    """Every field optional so the UI can PATCH one switch at a time."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    resume_id: int | None = None
    gmail_account_id: int | None = None
    target_roles: list[str] | None = Field(default=None, max_length=12)
    target_industries: list[str] | None = Field(default=None, max_length=12)
    skills: list[str] | None = Field(default=None, max_length=60)
    location_preferences: list[str] | None = Field(default=None, max_length=12)
    remote_only: bool | None = None
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    experience_level: str | None = Field(default=None, pattern=EXPERIENCE_LEVELS)
    employment_types: list[Annotated[str, Field(pattern=EMPLOYMENT_TYPES)]] | None = (
        Field(default=None, max_length=5)
    )
    company_sizes: list[Annotated[str, Field(pattern=COMPANY_SIZES)]] | None = Field(
        default=None, max_length=4
    )
    excluded_companies: (
        list[Annotated[str, Field(min_length=1, max_length=120)]] | None
    ) = Field(default=None, max_length=_MAX_EXCLUDED)
    is_active: bool | None = None
    is_default: bool | None = None


class ProfileFromResume(BaseModel):
    """Create a profile pre-filled from one resume's parse.

    The same extraction the setup wizard pre-fills preferences from, aimed at a
    single document — which is exactly what a per-role profile wants.
    """

    resume_id: int
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool = True


class ProfileOut(ProfileBase):
    id: int
    user_id: int
    resume_id: int | None = None
    gmail_account_id: int | None = None
    name: str
    is_active: bool
    is_default: bool
    created_at: datetime

    # Denormalized for the UI, so a profile list doesn't need a resume lookup per
    # row to say which document each one carries.
    resume_label: str | None = None

    model_config = {"from_attributes": True}

    @field_validator(
        "target_roles",
        "target_industries",
        "skills",
        "location_preferences",
        "employment_types",
        "company_sizes",
        "excluded_companies",
        mode="before",
    )
    @classmethod
    def _null_list_reads_as_empty(cls, value):
        """A stored ``null`` in a JSON list column reads as "nothing", not a 500.

        These columns are ``NOT NULL``, but SQLAlchemy's ``JSON`` type writes
        Python ``None`` as the JSON value ``null`` rather than SQL ``NULL``, so
        the constraint never fired and a ``PATCH {"target_roles": null}`` was
        accepted. ``app.core.patching`` now refuses that write, but it cannot
        un-write the rows that got through: reading one raised a validation
        error on the way out, which made *every* read of that profile — and of
        the whole list containing it — a 500, including the PATCH that would
        have repaired it. Coercing on the way out is what leaves that state
        recoverable rather than terminal.
        """
        return [] if value is None else value

    @classmethod
    def from_profile(cls, profile) -> ProfileOut:
        out = cls.model_validate(profile)
        out.resume_label = (
            profile.resume.display_label if profile.resume is not None else None
        )
        return out
