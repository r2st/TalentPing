"""Job–candidate fit score — a 0-100 verdict with its reasoning kept.

Scores are persisted rather than computed on the fly for three reasons: the feed
sorts by them, the dashboard reports on them, and a candidate who sees "72" today
should see the same number tomorrow unless their resume changed.

Weights live in :mod:`app.services.fit_scorer`; the row stores the resulting
sub-scores so the UI can show *why* without re-running anything.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.job import JobPosting
    from app.models.resume import Resume


class FitScore(Base, TimestampMixin):
    __tablename__ = "fit_scores"
    __table_args__ = (
        # One cached score per (profile, resume, job description). Re-scoring the
        # same triple updates the row instead of piling up duplicates.
        #
        # The profile belongs in the key because the score is no longer a
        # function of the resume alone: two profiles can share one document and
        # still disagree about a posting, because they want different places, pay
        # and titles. Keyed on the resume only, the second profile's verdict
        # would overwrite the first's and the feed would show whichever ran last.
        # Legacy rows carry a null profile and keep their own slot — in both
        # Postgres and SQLite, nulls never collide in a unique index.
        UniqueConstraint(
            "resume_id", "jd_hash", "profile_id", name="uq_fit_score_resume_jd_profile"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The profile this score was computed for. Null for rows written before
    # profiles existed and for users who have none.
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    job_posting_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )

    # sha256 of the normalized job description — the cache key.
    jd_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    job_title: Mapped[str | None] = mapped_column(String(500))
    job_company: Mapped[str | None] = mapped_column(String(255))

    overall: Mapped[float] = mapped_column(Float, nullable=False)
    skills_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # How close the posting's title is to the roles the candidate is chasing.
    # Nullable because rows scored before this dimension existed never had one,
    # and backfilling them would mean inventing a number.
    role_score: Mapped[float | None] = mapped_column(Float)
    experience_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    industry_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    location_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    salary_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    matched_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Per-dimension one-liners: {"skills": "8 of 11 required skills present", ...}
    notes: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    # strong | good | stretch | poor
    recommendation: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text)

    resume: Mapped[Resume] = relationship()
    job_posting: Mapped[JobPosting | None] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<FitScore id={self.id} overall={self.overall}>"
