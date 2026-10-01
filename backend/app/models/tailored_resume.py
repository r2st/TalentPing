"""Tailored resume produced for one job description.

Every tailoring run is stored rather than streamed straight back: the candidate
needs to download it later, the fit scorer reuses the parsed job description, and
keeping the output lets us show what changed between the base resume and the
version that was actually sent.

The base resume is never mutated — this is a derived artifact pointing at it.

Two things are stored per run beyond the structured output: the **rewritten
bullets** (the candidate's own achievements, re-angled at this posting's
priorities) and the **rendered PDF**, so what the user downloads is byte-for-byte
what the pipeline produced rather than a re-render that could drift from it.

``cover_letter`` is retained as the legacy inline field for rows written before
letters became their own artifact; new work reads
:class:`~app.models.cover_letter.CoverLetter` via ``tailored_resume_id``.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.job import JobPosting
    from app.models.resume import Resume
    from app.models.user import User


class TailoredResume(Base, TimestampMixin):
    __tablename__ = "tailored_resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    job_posting_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )

    # ---- What it was tailored against ----
    job_title: Mapped[str | None] = mapped_column(String(500))
    job_company: Mapped[str | None] = mapped_column(String(255))
    job_url: Mapped[str | None] = mapped_column(Text)
    job_description: Mapped[str | None] = mapped_column(Text)

    # ---- The tailored output ----
    tailored_summary: Mapped[str | None] = mapped_column(Text)
    # Candidate's own skills, reordered so the JD's requirements lead.
    ordered_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{company, title, why_relevant, bullets: [...], original_bullets: [...]}, ...]
    # Both are kept: the rewrite is an edit of the candidate's own claims, and
    # showing it beside the original is how a user checks that nothing was added.
    highlighted_experience: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # JD keywords the candidate genuinely has, and the ones they don't.
    matched_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Legacy inline letter, for runs predating the cover_letters table.
    cover_letter: Mapped[str | None] = mapped_column(Text)

    # ---- Rendered PDF ----
    # Deferred: a list of tailoring runs (the dashboard, the Pipeline drawer)
    # reads a dozen rows and needs none of the bytes. Touching ``pdf_bytes``
    # issues its own SELECT.
    pdf_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    pdf_filename: Mapped[str | None] = mapped_column(String(255))
    pdf_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # llm | heuristic — how the tailoring was produced.
    generated_with: Mapped[str | None] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(120))
    # llm | heuristic — how the *bullets* were produced, tracked separately: the
    # summary can come off the model while the bullet rewrite is rejected for
    # inventing something, and the UI should say which happened.
    bullets_generated_with: Mapped[str | None] = mapped_column(String(20))

    user: Mapped[User] = relationship()
    resume: Mapped[Resume] = relationship()
    job_posting: Mapped[JobPosting | None] = relationship()

    @property
    def has_pdf(self) -> bool:
        """True when a rendered PDF is stored, without loading its bytes."""
        return self.pdf_generated_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<TailoredResume id={self.id} job={self.job_title!r}>"
