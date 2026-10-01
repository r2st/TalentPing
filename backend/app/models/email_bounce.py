"""A recorded delivery failure — the audit trail behind a suppressed address.

The recruiter row carries the *current* state (``delivery_state``,
``soft_bounce_count``); this table carries the history that produced it. Keeping
both means "why has this contact stopped receiving mail?" has an answer with a
date and the server's own words on it, which matters when the answer is wrong and
someone has to work out why.

``domain`` is denormalized off the address so the per-domain bounce-rate report
is a plain GROUP BY rather than a string function over 320-char values.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class BounceKind(str, enum.Enum):
    """Permanent vs transient. The distinction the old code did not have."""

    HARD = "HARD"  # 5.x.x — the address will never accept mail
    SOFT = "SOFT"  # 4.x.x — full mailbox, greylisting, a server having a day


class EmailBounce(Base, TimestampMixin):
    __tablename__ = "email_bounces"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    recruiter_id: Mapped[int | None] = mapped_column(
        ForeignKey("recruiters.id", ondelete="SET NULL"), index=True
    )
    # The outbound message that bounced, when it can be identified. A DSN does
    # not always quote enough to match one.
    email_id: Mapped[int | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL"), index=True
    )

    address: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    kind: Mapped[BounceKind] = mapped_column(
        SAEnum(BounceKind, native_enum=False, length=8), nullable=False
    )
    # The matched status code ("5.1.1", "550"), for auditing a misclassification.
    code: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<EmailBounce {self.kind} {self.address}>"
