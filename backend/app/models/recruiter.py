"""Recruiter / contact model."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.user import User


class DeliveryState(str, enum.Enum):
    """Whether mail to this address actually arrives.

    Deliberately separate from ``opted_out``, which records *consent* — a human
    asking never to be contacted. Conflating the two is what made a full inbox
    permanently blacklist a good recruiter (see docs/features/bounce-handling.md).
    """

    OK = "OK"
    SOFT_BOUNCED = "SOFT_BOUNCED"    # temporary failure; still sendable
    HARD_BOUNCED = "HARD_BOUNCED"    # permanent failure; never send again


class Recruiter(Base, TimestampMixin):
    __tablename__ = "recruiters"
    __table_args__ = (
        # A given user cannot store the same recruiter email twice.
        UniqueConstraint("user_id", "email", name="uq_recruiter_user_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    company: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    industry: Mapped[str | None] = mapped_column(String(255))
    specialization: Mapped[str | None] = mapped_column(String(255))
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    # manual | import | careers_page | pattern
    source: Mapped[str | None] = mapped_column(String(100))
    # Where the address was found — the careers page URL for scraped contacts.
    source_url: Mapped[str | None] = mapped_column(Text)
    # 0..1 — scraped mailto links score higher than guessed role addresses.
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    # CAN-SPAM: once a recruiter opts out we never contact them again.
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The user's own decision to stop contacting this person — the third axis,
    # and deliberately not either of the other two.
    #
    # ``opted_out`` is the *recipient's* consent and ``delivery_state`` is
    # whether the address works; both are facts about the far end. This one is a
    # fact about the user: "I do not want the agent writing to them." Before it
    # existed the only way to express that was ``DELETE /recruiters/{id}``, which
    # was both too much and too little — it destroyed every application, thread
    # and message with the contact (``applications`` cascades from here), and the
    # next discovery run for that company re-created the row and started mailing
    # again, because ``_upsert_and_commit`` matches on the address and the
    # address was gone.
    #
    # A timestamp rather than a bool so the tracker can say *when*, and so the
    # column reads the same way as ``last_bounce_at`` beside it. Null means the
    # contact is the agent's to write to.
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- Deliverability (see services/bounce_service.py) ----
    # Whether mail to this address arrives. A hard bounce suppresses the contact
    # permanently; soft bounces accumulate and escalate at
    # ``bounce_service.SOFT_BOUNCE_LIMIT``.
    delivery_state: Mapped[DeliveryState] = mapped_column(
        SAEnum(DeliveryState, native_enum=False, length=16),
        default=DeliveryState.OK,
        nullable=False,
        index=True,
    )
    soft_bounce_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_bounce_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Shown in the tracker, so "why did this thread stop?" has an answer.
    last_bounce_reason: Mapped[str | None] = mapped_column(String(500))

    # IANA zone for send-time optimization, resolved once from the company's
    # researched headquarters / the posting / the address TLD and cached here.
    # Null means "not resolved yet", never "UTC" — see services/send_time.py.
    timezone: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="recruiters")
    applications: Mapped[list[Application]] = relationship(
        back_populates="recruiter", cascade="all, delete-orphan"
    )

    @property
    def is_excluded(self) -> bool:
        """True when the user has asked the agent not to write to this contact."""
        return self.excluded_at is not None
