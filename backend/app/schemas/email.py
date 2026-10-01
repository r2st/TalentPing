"""Email / application-tracking schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.application import ApplicationStatus
from app.models.email import EmailDirection, EmailStatus, ReplyIntent


class EmailOut(BaseModel):
    id: int
    thread_id: int
    direction: EmailDirection
    status: EmailStatus
    from_address: str | None = None
    to_address: str | None = None
    subject: str | None = None
    body_text: str | None = None
    intent: ReplyIntent | None = None
    sentiment_score: float | None = None
    attachment_filename: str | None = None
    # The letter that travelled as a file with this message. Null when the
    # letter was folded into the body, or when none was sent.
    cover_letter_id: int | None = None
    sent_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ``emails.subject`` is ``varchar(998)`` — the RFC 5322 line limit, which is why
# the column is that width and not a round number. Unbounded here, a longer
# subject reached Postgres and came back as ``StringDataRightTruncation``: an
# unhandled 500 for what is only a too-long field, and one no test could see
# because SQLite stores an over-long value without complaint.
_SUBJECT_LIMIT = 998
# The body is ``text`` and has no column limit, but "no column limit" is not the
# same as "any size is sensible". Gmail refuses a message far below this, so a
# body past it cannot be sent — bouncing it at the edge beats storing a draft
# whose only future is a failed send.
_BODY_LIMIT = 512_000


class EmailUpdate(BaseModel):
    """User edits to a draft before approval/sending."""

    subject: str | None = Field(default=None, max_length=_SUBJECT_LIMIT)
    body_text: str | None = Field(default=None, max_length=_BODY_LIMIT)

    @field_validator("subject")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        """A subject is one header line, whatever was pasted into the box.

        `gmail_service._header_safe` would flatten this anyway — it has to, as
        the last thing standing between a stray newline and a send that fails on
        every sweep forever. Doing it here as well is what makes the round trip
        honest: the draft the user reads back is the subject that will go out,
        rather than one the sender quietly rewrites hours later.
        """
        if value is None:
            return None
        return " ".join(value.split())


class ThreadOut(BaseModel):
    id: int
    application_id: int
    subject: str | None = None
    message_count: int
    last_message_at: datetime | None = None
    emails: list[EmailOut] = []

    model_config = {"from_attributes": True}


class ApplicationOut(BaseModel):
    id: int
    user_id: int
    campaign_id: int
    recruiter_id: int
    status: ApplicationStatus
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ApplicationDetail(ApplicationOut):
    threads: list[ThreadOut] = []
