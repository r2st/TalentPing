"""Open and click events on an outbound message.

One row per recorded interaction. The counters on :class:`~app.models.email.Email`
are derived from these and can be rebuilt from them; this table is the source of
truth.

Two fields exist because open tracking is unreliable in ways worth recording
rather than hiding (see docs/features/email-tracking.md §2):

* ``is_prefetch`` — the event arrived within seconds of the send, so it is very
  probably Gmail's image proxy caching the pixel rather than a human reading the
  message. Kept, because it still evidences delivery, but excluded from rates.
* ``ip_hash`` — never the raw address. The recipient is a third party who never
  signed up for this product; a salted hash deduplicates a repeat opener and is
  useless for locating anyone.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.email import Email


class EmailEventType(str, enum.Enum):
    OPEN = "OPEN"
    CLICK = "CLICK"


class EmailEvent(Base, TimestampMixin):
    __tablename__ = "email_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Ownership. There is no business/tenant concept in this codebase; user_id
    # is the scoping key every model uses.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True, nullable=False
    )

    event_type: Mapped[EmailEventType] = mapped_column(
        SAEnum(EmailEventType, native_enum=False, length=8), nullable=False
    )
    # The click target. Null for opens.
    url: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    # sha256(ip + jwt_secret), truncated. Never the address itself.
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    # Very probably a mail-proxy prefetch rather than a human. Excluded from rates.
    is_prefetch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )

    email: Mapped[Email] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<EmailEvent {self.event_type} email={self.email_id}>"
