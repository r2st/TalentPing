"""A connected LinkedIn account — credentials and the Easy Apply budget.

LinkedIn has no application API. Easy Apply is a browser flow, so driving it
means signing in as the candidate, which means holding their password. That is a
genuinely worse thing to store than an OAuth token (it is reusable everywhere,
and it is the candidate's own account on the line), so:

* The password is encrypted at rest with the same Fernet key as Gmail's refresh
  tokens (:mod:`app.services.crypto`) and is only ever decrypted inside a worker
  that is about to type it into LinkedIn's own login form.
* A successful login's **session state** is stored too, so the steady state is
  cookie reuse and the password is touched roughly once a month rather than once
  per application.
* ``daily_apply_count`` enforces the Easy Apply ceiling
  (``settings.linkedin_easy_apply_daily_limit``, 25/day). LinkedIn restricts
  accounts that apply at machine speed, and the account it restricts is the
  candidate's.
* A challenge (2FA, CAPTCHA, "unusual activity") sets ``status`` to
  ``challenge_required`` and stops. Nothing here tries to defeat one — the
  candidate signs in themselves and the automation resumes on their cookies.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class LinkedInAccount(Base, TimestampMixin):
    __tablename__ = "linkedin_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Fernet-encrypted. Never logged, never returned by the API.
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Playwright storage_state JSON, encrypted. Present after a successful login;
    # cleared when LinkedIn invalidates it.
    session_state_encrypted: Mapped[str | None] = mapped_column(Text)
    session_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # connected | challenge_required | invalid_credentials | error | disabled
    status: Mapped[str] = mapped_column(
        String(24), default="connected", nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    # ---- Easy Apply budget (rolling 24h) ----
    daily_apply_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_count_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_apply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    easy_apply_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship()

    @property
    def is_usable(self) -> bool:
        """True when the automation may attempt a sign-in with this row."""
        return self.status in ("connected", "error")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<LinkedInAccount user={self.user_id} status={self.status}>"
