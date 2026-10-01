"""Scheduled follow-up — one future nudge on an outreach thread.

Rows are created up-front when the initial outreach is sent, one per step in the
campaign's follow-up sequence, each with the wall-clock time it becomes due. A
beat task sweeps for due rows; the email body is composed at send time (not
schedule time) so it can reflect what the recruiter has done in the meantime.

Cancelling is a status change, never a delete: "we stopped following up because
they replied on day 4" is history worth keeping.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.application import Application


class FollowUpStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    # The step has been actioned: its email exists and is queued (or parked as a
    # draft when the campaign wants review first). Delivery is tracked on the
    # email itself, not here — this row's job ends once the message is written.
    SENT = "SENT"
    CANCELLED = "CANCELLED"  # recruiter replied, or the user stopped the campaign
    FAILED = "FAILED"


class FollowUpTemplate(str, enum.Enum):
    """Which angle the follow-up takes, chosen from the thread's state."""

    NO_RESPONSE = "NO_RESPONSE"          # silence since the first email
    OPENED_NO_REPLY = "OPENED_NO_REPLY"  # they engaged but didn't write back
    PARTIAL_RESPONSE = "PARTIAL_RESPONSE"  # they replied without a decision
    FINAL = "FINAL"                      # last touch, closes the loop politely


class FollowUp(Base, TimestampMixin):
    __tablename__ = "follow_ups"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The email this follow-up produced, once sent.
    email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL"), index=True
    )

    # 1-based position in the sequence.
    step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    # When the step handed its message to the sender. Stays NULL for a step
    # whose email was parked for review — that one has not been sent, and
    # nothing later fills this in when the user approves it. The email's own
    # ``sent_at`` is the authority on delivery either way; this is a convenience
    # for "when did the automation act", not a second copy of that answer.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[FollowUpStatus] = mapped_column(
        SAEnum(FollowUpStatus, native_enum=False, length=16),
        default=FollowUpStatus.SCHEDULED,
        nullable=False,
        index=True,
    )
    template: Mapped[FollowUpTemplate] = mapped_column(
        SAEnum(FollowUpTemplate, native_enum=False, length=24),
        default=FollowUpTemplate.NO_RESPONSE,
        nullable=False,
    )
    # Why it was cancelled / how it failed.
    note: Mapped[str | None] = mapped_column(Text)

    application: Mapped[Application] = relationship(back_populates="follow_ups")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<FollowUp id={self.id} step={self.step} status={self.status}>"
