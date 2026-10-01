"""Interview prep schemas — what the candidate reads before the call."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class PrepQuestion(BaseModel):
    """A question the candidate should expect, and why it gets asked."""

    question: str
    why: str | None = None


class TalkingPoint(BaseModel):
    """Something to lead with, tied to the evidence on the resume that backs it.

    ``evidence`` is what keeps this honest: a talking point with nothing on the
    resume behind it is a claim the candidate has to defend in the room.
    """

    point: str
    evidence: str | None = None


class JobPrepRequest(BaseModel):
    """Prep for a posting, with or without an application behind it.

    Either ``job_posting_id`` (something in the feed) or ``description`` (a JD
    pasted straight in) must be given. When both are, the pasted text wins —
    the user typed it more recently than the crawler found the row.
    """

    job_posting_id: int | None = None
    description: str | None = Field(default=None, max_length=20_000)
    # Only read when there is no posting row to take them from.
    company: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    # Which resume to score the posting against. Defaults to the user's default.
    resume_id: int | None = None

    @model_validator(mode="after")
    def _needs_a_posting(self) -> JobPrepRequest:
        if self.job_posting_id is None and not (self.description or "").strip():
            raise ValueError("Provide either job_posting_id or description")
        return self


class InterviewPrepOut(BaseModel):
    # Null when the briefing is for a posting with no application behind it.
    application_id: int | None = None
    company: str | None = None
    role: str | None = None

    company_research: str
    # Short factual bullets pulled from the posting — location, comp, stack.
    company_facts: list[str] = Field(default_factory=list)

    questions: list[PrepQuestion] = Field(default_factory=list)
    talking_points: list[TalkingPoint] = Field(default_factory=list)
    # Requirements the resume doesn't cover — better rehearsed than discovered.
    gaps: list[str] = Field(default_factory=list)
    questions_to_ask: list[str] = Field(default_factory=list)

    # llm | template — the UI badges which one produced this.
    generated_with: str = "template"
