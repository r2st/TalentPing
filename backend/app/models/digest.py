"""Weekly digest preferences — the one email TalentPing sends *to* its user.

The product mails strangers all day on the candidate's behalf and has never
once mailed the candidate. The only notification surface is an in-app toast,
which requires the user to already be in the app — so a drafted reply waiting
for approval can sit unread for days while the recruiter who wrote it waits.

This row is the whole configuration: whether the digest goes out, when, and the
secret that lets a single click stop it. Exactly one per user, created lazily on
first read, and **on by default** — an opt-in digest is a digest nobody gets,
and the one email a week that says "three drafts are waiting" is the entire
point of the feature.

``unsubscribe_token`` is a random secret rather than the user id because the
unsubscribe link is necessarily unauthenticated: it is clicked from a mail
client with no session. The token can only ever switch this row off, which is
why an unauthenticated route is acceptable for it and would not be for
anything else.
"""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


def new_unsubscribe_token() -> str:
    """A URL-safe secret for the one-click opt-out link."""
    return secrets.token_urlsafe(32)


class DigestPreference(Base, TimestampMixin):
    __tablename__ = "digest_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 0 = Monday, matching ``date.weekday()``. Monday because the digest's job is
    # to set up the week, not to review one that is already over.
    weekday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Hour of day, UTC. Deliberately not the recipient's local time: every other
    # schedule in this system is UTC, and a digest arriving at an odd hour is a
    # far smaller problem than two schedulers disagreeing about what "8am" means.
    hour: Mapped[int] = mapped_column(Integer, default=8, nullable=False)

    unsubscribe_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, default=new_unsubscribe_token
    )

    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When we last *tried*, which is not the same fact as when we last sent —
    # and the difference is what stops a broken mailbox being retried hourly
    # for the rest of the week. See ``digest_service.is_due``.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="digest_preference")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<DigestPreference user={self.user_id} enabled={self.enabled}>"
