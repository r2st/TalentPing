"""Connected Gmail account — the candidate's OAuth sending identity.

One row per Google account a user connects. Outreach is sent through the Gmail
API as this account (SPF/DKIM/DMARC aligned), and the same grant is used to poll
threads for recruiter replies. Tokens are encrypted at rest with Fernet.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.gmail_watch import GmailWatch
    from app.models.user import User


class GmailAccount(Base, TimestampMixin):
    __tablename__ = "gmail_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # `unique=True` alone. Adding `index=True` on top does not add an index —
    # it changes which *kind* of object SQLAlchemy emits, from a unique
    # constraint to a unique index, and the migration that built production
    # emitted the constraint. The two spellings coexisting is how this column
    # ended up with two btrees in production. See `c5a9e63b17d4`.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))

    # OAuth tokens — encrypted at rest with Fernet (see services/crypto.py).
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Space-separated scopes as granted — replayed on refresh (see google_oauth).
    scopes: Mapped[str | None] = mapped_column(Text)
    # When the refresh token currently on this row was issued by Google.
    #
    # Not ``created_at``. A re-consent keeps the existing row — the match in the
    # callback is on ``google_sub`` — so ``created_at`` is when the mailbox was
    # *first* connected and says nothing about the age of the grant in the
    # column above it. On a deployment whose consent screen is unpublished those
    # two numbers diverge every week, and the age of the live grant is the only
    # one that predicts when sending stops.
    #
    # Null on every row written before this column, and on any row whose token
    # was replaced by something that forgot to stamp it. Null is read as "we
    # cannot say", never as "recently" — see ``gmail_accounts.grant_expiry``.
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # connected | revoked | error
    status: Mapped[str] = mapped_column(String(20), default="connected", nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Rolling send accounting for warm-up throttling.
    daily_send_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_count_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- Deliverability / reputation (see services/reputation_service.py) ----
    # When this mailbox first sent through TalentPing. Drives the warm-up ramp:
    # a brand-new personal inbox that suddenly sends 30 cold emails looks like a
    # compromised account, so the allowance starts at 3/day and grows over weeks.
    warmup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Total time this mailbox has spent under a reputation hold, in seconds. The
    # ramp's clock is `warmup_started_at + warmup_hold_seconds`, so days spent
    # forbidden to send do not count as days of earned trust.
    #
    # It is a column of its own rather than an adjustment to `warmup_started_at`
    # because the two answer different questions and have different owners.
    # `warmup_started_at` is "when did this address start sending", written by
    # `record_send` and restored from the `emails` table by
    # `adopt_send_history`; the hold is "how long has it been held", written
    # only by `_hold_until`. Folding the penalty into the start made them one
    # column with two meanings, and the restorer won: `reconcile_warmup_ramps`
    # runs every six hours, re-derives the start from real history, and so
    # erased every hold penalty within six hours of it being applied.
    warmup_hold_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Lifetime counters used for the bounce/complaint rate guardrails.
    sent_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bounce_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    complaint_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Set when a reputation guardrail trips (bounce rate too high). Sending is
    # held until this passes; a human reply or clean window clears it.
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(String(255))

    # ---- Inbound scan bookkeeping (see tasks/recruiter_reply_tasks.py) ----
    # When this *mailbox* was last read for inbound recruiter mail. The debounce
    # lives here rather than on ``RecruiterReplyPreference`` because that row is
    # one per user: with the gate keyed per user, the first mailbox scanned in a
    # cycle suppressed every other mailbox until the gap elapsed, so a second
    # account would look watched while never actually being read.
    # ``RecruiterReplyPreference.last_scan_at`` is still maintained as the
    # user-wide "when did anything last get read", which is what the UI shows.
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lifetime count of inbound recruiter messages detected in this mailbox.
    detected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="gmail_accounts")
    # The push subscription for this mailbox, when one has been registered.
    # Absent (or inactive) means replies arrive by polling instead.
    watch: Mapped[GmailWatch | None] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<GmailAccount {self.email} status={self.status}>"
