"""Cover letter written for one specific job posting.

Split out of :class:`~app.models.tailored_resume.TailoredResume`, which carried a
single ``cover_letter`` text column. A letter is its own artifact with its own
life: it is regenerated on its own, edited on its own, downloaded on its own, and
— unlike a tailored resume — it can be attached to the outreach that goes out.
Giving it a row means the application detail view can show which letter was
actually sent alongside which resume.

``job_posting_id`` is the anchor the product talks in terms of: one letter per
job. ``tailored_resume_id`` links it back to the resume it was written against so
the two can never drift into describing different candidates.

Same invariant as the tailorer: the letter is grounded in the resume. Anything
the model wrote that claims a skill or a metric the resume doesn't support is
rejected before it reaches this table (see
:mod:`app.services.cover_letter_service`).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.job import JobPosting
    from app.models.resume import Resume
    from app.models.tailored_resume import TailoredResume
    from app.models.user import User


class CoverLetter(Base, TimestampMixin):
    __tablename__ = "cover_letters"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The job this letter is about. The product's unit of work: one letter per
    # posting. SET NULL so pruning the feed never deletes written work.
    job_posting_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )
    # The outreach this letter went out with, once one exists.
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), index=True
    )
    # The tailored resume it was written to accompany.
    tailored_resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("tailored_resumes.id", ondelete="SET NULL"), index=True
    )

    # ---- What it was written against ----
    job_title: Mapped[str | None] = mapped_column(String(500))
    job_company: Mapped[str | None] = mapped_column(String(255))

    # ---- The letter ----
    greeting: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    sign_off: Mapped[str | None] = mapped_column(String(255))

    # The factual company notes the letter was allowed to draw on — kept so the
    # user can see what the personalization was based on rather than trusting it.
    company_research: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Resume-backed points the letter leads with, each with its evidence.
    highlights: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Requirements the candidate doesn't meet. Never written about; shown so the
    # user knows what the letter is deliberately silent on.
    missing_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)

    # inline | attachment — how it was (or will be) delivered with the outreach.
    delivery: Mapped[str] = mapped_column(String(16), default="inline", nullable=False)
    # True once the user has edited the body by hand. Regeneration refuses to
    # overwrite an edited letter without an explicit force.
    edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # llm | heuristic — how the prose was produced.
    generated_with: Mapped[str | None] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(120))

    user: Mapped[User] = relationship()
    resume: Mapped[Resume] = relationship()
    job_posting: Mapped[JobPosting | None] = relationship()
    application: Mapped[Application | None] = relationship()
    tailored_resume: Mapped[TailoredResume | None] = relationship()

    @property
    def full_text(self) -> str:
        """The letter as one block, the way it is sent and downloaded."""
        return "\n\n".join(
            part.strip()
            for part in (self.greeting, self.body, self.sign_off)
            if part and part.strip()
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<CoverLetter id={self.id} job={self.job_title!r}>"
