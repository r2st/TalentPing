"""Schemas for the LinkedIn connection.

The password goes one way only. It is accepted on
:class:`LinkedInCredentialsIn`, encrypted immediately, and there is deliberately
no schema anywhere that can return it — not masked, not truncated.
"""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class LinkedInCredentialsIn(BaseModel):
    """The candidate's LinkedIn sign-in, stored encrypted at rest."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=200, repr=False)


class LinkedInBudgetOut(BaseModel):
    allowed: bool
    used: int
    limit: int
    remaining: int
    reason: str | None = None
    retry_after_seconds: int = 0


class LinkedInStatusOut(BaseModel):
    """Everything the settings screen shows about the connection."""

    connected: bool
    email: str | None = None
    # not_connected | connected | challenge_required | invalid_credentials | error
    status: str
    # Whether this deployment allows Easy Apply automation at all.
    enabled: bool
    has_session: bool = False
    session_saved_at: str | None = None
    easy_apply_total: int = 0
    last_apply_at: str | None = None
    last_error: str | None = None
    # Always supplied by ``linkedin_service.status_summary`` — both of its
    # branches call ``apply_budget(...).as_dict()``, including the one for an
    # account that does not exist yet.
    budget: LinkedInBudgetOut
