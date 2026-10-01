"""Market salary bands, cached per role family × seniority × location.

A benchmark is *not* about one posting — it is what the market pays for a kind
of role in a place, so rows are global rather than per user. Two candidates
looking at the same senior backend job in Berlin get the same band, and the
thousandth job in that bucket costs one lookup rather than a thousand.

The numbers come from :mod:`app.services.salary_service`, which derives them
deterministically from a role/seniority/location mapping. That determinism is
the point: a band that moves between page loads is worse than no band, because
the candidate is using it to decide whether an offer is low.

``sample_size`` and ``source`` are carried so a future upgrade to real market
data (levels.fyi, an employer survey) can land in the same table and be
distinguished from the modelled rows — the UI already reads ``source`` to decide
how much confidence to claim.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class SalaryBenchmark(Base, TimestampMixin):
    __tablename__ = "salary_benchmarks"
    __table_args__ = (
        UniqueConstraint(
            "role_family",
            "seniority",
            "location_key",
            name="uq_salary_benchmark_role_seniority_location",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Normalized bucket keys — "backend_engineer", "senior", "us_sf".
    role_family: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    seniority: Mapped[str] = mapped_column(String(16), nullable=False)
    location_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # Human-readable versions of the same, for the card's caption.
    role_label: Mapped[str | None] = mapped_column(String(120))
    location_label: Mapped[str | None] = mapped_column(String(120))

    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    salary_min: Mapped[int] = mapped_column(Integer, nullable=False)
    salary_median: Mapped[int] = mapped_column(Integer, nullable=False)
    salary_max: Mapped[int] = mapped_column(Integer, nullable=False)

    # modelled | levels_fyi | glassdoor | manual
    source: Mapped[str] = mapped_column(String(32), default="modelled", nullable=False)
    # Postings behind the number, when it came from observed data. Modelled rows
    # leave it at 0 and the UI says "estimated" rather than quoting a sample.
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def is_fresh(self, ttl_days: int) -> bool:
        """True when the cached band is recent enough to serve as-is."""
        if self.computed_at is None:
            return False
        computed = self.computed_at
        if computed.tzinfo is None:  # SQLite round-trips naive datetimes
            computed = computed.replace(tzinfo=UTC)
        return (datetime.now(UTC) - computed) < timedelta(days=ttl_days)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<SalaryBenchmark {self.seniority} {self.role_family} @ {self.location_key} "
            f"{self.salary_min}-{self.salary_max} {self.currency}>"
        )
