"""Globally-shared cache of scraped company recruiting contacts.

Crawling a careers page is slow and rude to repeat, and the answer is the same
for every candidate — so results are cached *globally* (not per user) keyed by
company domain. Any user targeting Stripe reuses the Stripe crawl until it goes
stale.

Rows are also written for failed crawls (``status='empty'``) so we don't re-crawl
a dead end on every campaign.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin


class RecruiterCache(Base, TimestampMixin):
    __tablename__ = "recruiter_cache"
    __table_args__ = (UniqueConstraint("domain", name="uq_recruiter_cache_domain"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    company: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    # No `index=True` — `uq_recruiter_cache_domain` above already indexes this
    # column, and the lookup here is always an equality on it. See
    # `c5a9e63b17d4`.
    domain: Mapped[str] = mapped_column(String(255), nullable=False)

    # Flat list of discovered addresses, most confident first.
    emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Richer records: [{email, name, title, confidence, kind}, ...]
    contacts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    source_url: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)
    # ok | empty | error
    status: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Times this cache row saved a crawl — useful for spotting hot companies.
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def is_fresh(self, ttl_days: float) -> bool:
        """True when the cached crawl is recent enough to reuse.

        A float, because a crawl that *failed* does not earn the same lifetime
        as one that succeeded. See ``career_scraper._cache_ttl_days``.
        """
        if self.scraped_at is None:
            return False
        scraped = self.scraped_at
        if scraped.tzinfo is None:  # SQLite round-trips naive datetimes
            scraped = scraped.replace(tzinfo=UTC)
        return (datetime.now(UTC) - scraped) < timedelta(days=ttl_days)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<RecruiterCache {self.domain} contacts={len(self.contacts or [])}>"
