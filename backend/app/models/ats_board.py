"""Globally-shared cache of which ATS board a company publishes on.

A company's board token is a fact about the company, not about the candidate
looking at it — "Northwind Labs is on Greenhouse as ``northwindlabs``" is the same
answer for everyone. So this is cached globally by normalized company name, the
same posture as :class:`~app.models.recruiter_cache.RecruiterCache` and for the
same reason: discovering it costs requests, and repeating that per user would be
both slow and rude.

**Negative answers are stored too.** ``status='none'`` records "we looked and this
company has no readable public board", which is the common case and the one worth
remembering — without it every scan re-probes five platforms for every employer
that self-hosts its careers page.

**A failure is a third state, not a negative answer.** ``status='error'`` records
"we could not find out" — a vendor throttling us, a gateway erroring, a refused
connection, a certificate that expired over the weekend. That distinction is the
whole reason the state exists: it was written into this vocabulary from the start
and nothing ever produced it, so every transient failure was stored as ``none``
and one bad afternoon told every user of the deployment that eight employers had
no board until the fortnight was up.

Rows go stale on ``ats_board_cache_ttl_days`` rather than never, because companies
do migrate ATS vendors, and a token that has stopped working should eventually be
re-derived rather than suppressing the company forever. An ``error`` row expires
far sooner — ``ats_board_error_ttl_hours`` — because it describes a vendor's
afternoon rather than an employer, and those heal on their own.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class AtsBoard(Base, TimestampMixin):
    __tablename__ = "ats_boards"
    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_ats_board_normalized_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # As the employer was named when we looked, for display.
    company: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    # The identity key — see app.services.job_dedup.normalize_company.
    # No `index=True`: `uq_ats_board_normalized_name` above is already a unique
    # btree over exactly this column, and asking for a second index put two of
    # them on the table. See `c5a9e63b17d4`.
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # greenhouse | lever | ashby | workable | smartrecruiters. Null when no board
    # was found; these are deliberately not ATSPlatform values, which mean "we
    # have a form-filling adapter for this" — a different claim entirely.
    platform: Mapped[str | None] = mapped_column(String(32))
    # The company's identifier on that board, the one the JSON API is keyed on.
    board_token: Mapped[str | None] = mapped_column(String(120))
    # The human-facing board page, so the feed can link somewhere a person can read.
    board_url: Mapped[str | None] = mapped_column(Text)

    # ok | none | error
    status: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    # How the token was found, or why nothing was — shown in the scan report.
    note: Mapped[str | None] = mapped_column(Text)

    # Postings the last successful read returned. A board that suddenly returns
    # zero is worth noticing; it usually means a migrated token.
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Times this row saved a discovery. Cheap way to spot the hot employers.
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def is_fresh(self, ttl_days: float) -> bool:
        """True when the cached answer is recent enough to reuse as-is.

        A float, because not every kind of answer earns the same lifetime: an
        unreadable board is re-checked in hours where a found or absent one is
        trusted for weeks. See ``ats_boards._cache_ttl_days``.
        """
        if self.checked_at is None:
            return False
        checked = self.checked_at
        if checked.tzinfo is None:  # SQLite round-trips naive datetimes
            checked = checked.replace(tzinfo=UTC)
        return (datetime.now(UTC) - checked) < timedelta(days=ttl_days)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<AtsBoard {self.company} {self.platform}:{self.board_token}>"
