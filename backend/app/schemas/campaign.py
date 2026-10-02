"""Campaign schemas — one create shape, because there is one way to start."""
from __future__ import annotations

from datetime import datetime

from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.campaign import CampaignStatus


class CampaignCreate(BaseModel):
    """Everything the user configures to launch outreach.

    Name and roles are optional — both are derived from the resume when omitted.
    At least one company or industry is required; without a target there is
    nothing to crawl.
    """

    name: str | None = Field(default=None, max_length=255)
    resume_id: int | None = None
    target_companies: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=50
    )
    target_industries: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=10
    )
    target_roles: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=10
    )
    auto_send: bool = True
    # Which connected mailbox this campaign sends from. Omitted means "the usual
    # one" — the matched profile's, else the primary — which is what every
    # single-mailbox user gets without ever seeing this field.
    gmail_account_id: int | None = None

    # ---- Follow-up sequence (research §2.3 defaults) ----
    follow_up_enabled: bool = True
    # Capped at 5: beyond that a sequence stops being persistence and starts
    # being harassment, and the reply rate goes with it.
    follow_up_count: int = Field(default=2, ge=0, le=5)
    # Days to the first nudge; later steps keep the configured spacing after it.
    # See ``follow_up_service.default_offsets``.
    follow_up_interval_days: int = Field(default=3, ge=1, le=30)
    follow_up_stop_on_reply: bool = True
    # Explicit day offsets from the initial send, e.g. [3, 7, 14]. Omitted means
    # "generate them from the two knobs above" — see
    # ``follow_up_service.sequence_offsets``, which has read this column since
    # sequences shipped while nothing was able to write one.
    #
    # The interval knob cannot express "day 3 then day 7": no integer interval
    # yields that pair under the widening shape the generator extends. That is
    # the whole reason the column exists, and it has been unreachable.
    follow_up_step_days: list[int] | None = Field(default=None, max_length=5)

    @field_validator("follow_up_step_days")
    @classmethod
    def _sane_sequence(cls, value: list[int] | None) -> list[int] | None:
        """Sorted, deduplicated, positive — the same rule the preferences row
        applies, because they end up in the same column on the same table."""
        if value is None:
            return None
        for day in value:
            if day < 1 or day > 365:
                raise ValueError("follow-up days must be between 1 and 365")
        return sorted(set(value)) or None

    @model_validator(mode="after")
    def _require_a_target(self) -> CampaignCreate:
        if not self.target_companies and not self.target_industries:
            raise ValueError("Pick at least one company or industry to target")
        return self


class CampaignOut(BaseModel):
    id: int
    user_id: int
    resume_id: int | None = None
    gmail_account_id: int | None = None
    name: str
    status: CampaignStatus
    auto_send: bool
    target_companies: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)

    follow_up_enabled: bool = True
    follow_up_count: int = 2
    follow_up_interval_days: int = 3
    follow_up_stop_on_reply: bool = True
    follow_up_step_days: list[int] | None = None

    companies_processed: int = 0
    contacts_found: int = 0
    emails_generated: int = 0
    last_error: str | None = None

    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CampaignStart(BaseModel):
    """Result of kicking off (or resuming) a campaign."""

    campaign: CampaignOut
    # True when the work was handed to a Celery worker; False when it ran inline
    # (no broker configured — the dev/test path).
    queued: bool
    detail: str | None = None
