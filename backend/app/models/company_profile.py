"""Cached company research — the context a posting never includes.

A job ad tells the candidate what the company wants. It rarely says how big the
company is, whether it just raised, what its own engineers say about it, or what
it shipped last month. That research is identical for every candidate looking at
that employer, so rows are **global** and keyed on the normalized company name,
the same way :class:`~app.models.recruiter_cache.RecruiterCache` treats a domain.

Freshness matters more here than for a careers-page crawl — funding and headcount
move — so the TTL is a week (``settings.company_profile_ttl_days``) rather than a
month.

Facts are stored with the ``source`` that produced them. Anything sourced ``llm``
is a language model's recollection, not a lookup, and the card labels it as an
estimate; ``heuristic`` rows are read straight off the posting text. Rows are
written even when research came back thin (``status='empty'``) so a company with
no available data isn't re-researched on every card open.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin


class CompanyProfile(Base, TimestampMixin):
    __tablename__ = "company_profiles"
    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_company_profile_normalized_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # app.services.job_dedup.normalize_company output — the cache key.
    # No `index=True` — `uq_company_profile_normalized_name` above already
    # indexes this column. See `c5a9e63b17d4`.
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255))

    # "1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5000+"
    size: Mapped[str | None] = mapped_column(String(32))
    employee_count: Mapped[int | None] = mapped_column(Integer)
    founded_year: Mapped[int | None] = mapped_column(Integer)
    headquarters: Mapped[str | None] = mapped_column(String(255))
    industry: Mapped[str | None] = mapped_column(String(120))

    # bootstrapped | seed | series_a … series_e | public | acquired | unknown
    funding_stage: Mapped[str | None] = mapped_column(String(32))
    funding_total: Mapped[str | None] = mapped_column(String(64))
    glassdoor_rating: Mapped[float | None] = mapped_column(Float)

    tech_stack: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{"title": …, "url": …, "published": "2026-06", "source": …}]
    news: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    summary: Mapped[str | None] = mapped_column(Text)

    # llm | heuristic | manual — how much the card should claim.
    source: Mapped[str] = mapped_column(String(32), default="heuristic", nullable=False)
    # ok | empty | error
    status: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    researched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Times this row saved a research call — cheap signal for hot employers.
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def is_fresh(self, ttl_days: int) -> bool:
        """True when the cached research is recent enough to reuse."""
        if self.researched_at is None:
            return False
        researched = self.researched_at
        if researched.tzinfo is None:  # SQLite round-trips naive datetimes
            researched = researched.replace(tzinfo=UTC)
        return (datetime.now(UTC) - researched) < timedelta(days=ttl_days)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<CompanyProfile {self.name!r} source={self.source} status={self.status}>"
