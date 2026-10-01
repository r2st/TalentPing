"""Every status an application has held, and what put it there.

The status column answers "where is this now?". It has never been able to
answer "how did it get here?", and the two questions have different owners:
most transitions are the product's own doing (the send task, the follow-up
sequence, the reply classifier), but the board lets a user drag a card between
columns, and a pipeline that mixes the two without saying which is which is a
pipeline nobody can audit.

One row per transition, written once and never updated. ``source`` is the whole
point of the table — ``AUTOMATIC`` means something observable happened (an email
left, a reply arrived), ``MANUAL`` means a human asserted it. When an interview
shows up in the funnel, that distinction is the difference between "the
classifier read a reply this way" and "the candidate told us so", and only the
second is evidence about the world rather than about our own inference.

``from_status`` is null for the first row of an application's life, which is the
transition into ``QUEUED`` at creation.
"""
from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.application import ApplicationStatus
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.application import Application


class StatusEventSource(str, enum.Enum):
    """Who moved it."""

    # The product observed something: a send completed, a reply was classified,
    # a follow-up went out.
    AUTOMATIC = "AUTOMATIC"
    # A human dragged the card, or picked a stage from the menu.
    MANUAL = "MANUAL"


class ApplicationStatusEvent(Base, TimestampMixin):
    """One transition, as it happened. Append-only."""

    __tablename__ = "application_status_events"
    __table_args__ = (
        # The board reads "latest event per application" for a whole page of
        # cards at once, and the history panel reads one application's rows
        # newest-first. Both are this index.
        Index(
            "ix_status_events_application_created", "application_id", "created_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Denormalised from the application so the history can be filtered per user
    # without a join, and so the row still says whose pipeline it described
    # after the application is gone.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Null only for the row that records an application entering QUEUED.
    from_status: Mapped[ApplicationStatus | None] = mapped_column(
        SAEnum(ApplicationStatus, native_enum=False, length=24)
    )
    to_status: Mapped[ApplicationStatus] = mapped_column(
        SAEnum(ApplicationStatus, native_enum=False, length=24), nullable=False
    )
    source: Mapped[StatusEventSource] = mapped_column(
        SAEnum(StatusEventSource, native_enum=False, length=16), nullable=False
    )
    # Free text from whoever wrote the row: "recruiter replied (INTERESTED)",
    # "outreach sent", or a note the user typed when moving the card.
    reason: Mapped[str | None] = mapped_column(String(255))

    application: Mapped[Application] = relationship(back_populates="status_events")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        was = self.from_status.value if self.from_status else "-"
        return (
            f"<ApplicationStatusEvent app={self.application_id} "
            f"{was}->{self.to_status.value} {self.source.value}>"
        )


__all__ = ["ApplicationStatusEvent", "StatusEventSource"]
