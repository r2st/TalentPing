"""Resume model — the candidate's profile, derived entirely from an upload.

TalentPing has no manual profile form: everything the outreach engine needs
(name, headline, skills, target roles, seniority) is extracted from the PDF at
upload time. A user can hold several resumes — one per role they're targeting —
and each campaign runs off exactly one of them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign
    from app.models.profile import Profile
    from app.models.user import User


class Resume(Base, TimestampMixin):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    filename: Mapped[str | None] = mapped_column(String(255))
    # Full extracted text — the raw material for AI personalization.
    raw_text: Mapped[str | None] = mapped_column(Text)

    # ---- The file the candidate actually uploaded ----
    # Everything above this line is *derived* from the upload, and for a long
    # time that was all we kept: the bytes were read, parsed and dropped. Which
    # meant the document a recruiter opened was never the candidate's — it was a
    # PDF re-rendered from the extracted fields, in our layout, under a filename
    # we invented. Two resumes for the same person even rendered to the same
    # name. "The wrong CV was attached" is exactly what that looks like from the
    # receiving end, and no amount of fixing the *selection* could have helped:
    # the selection was right and the bytes were still ours.
    #
    # So the original is kept and sent verbatim. The re-render survives only as
    # the fallback for rows uploaded before this column existed.
    #
    # Deferred: listing resumes reads a dozen rows and needs none of the bytes,
    # and this is capped at the 10 MB the upload endpoint accepts.
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    file_content_type: Mapped[str | None] = mapped_column(String(120))
    # Non-null exactly when ``file_bytes`` holds the upload. Kept separately so
    # "is there an original?" is answerable — and a filename choosable — without
    # loading a multi-megabyte blob to find out.
    file_size: Mapped[int | None] = mapped_column(Integer)

    # ---- Auto-extracted structure (no user input) ----
    full_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(String(255))
    headline: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    years_experience: Mapped[int | None] = mapped_column(Integer)
    seniority: Mapped[str | None] = mapped_column(String(50))  # junior|mid|senior|lead|exec

    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_industries: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{company, title, start, end}, ...]
    experience: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # [{school, degree, field, year}, ...]
    education: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    links: Mapped[list[str]] = mapped_column(JSON, default=list)

    # The resume used by default when a campaign doesn't name one.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # heuristic | llm — how the structured fields were derived.
    parsed_with: Mapped[str | None] = mapped_column(String(20))

    user: Mapped[User] = relationship(back_populates="resumes")
    campaigns: Mapped[list[Campaign]] = relationship(back_populates="resume")
    # The profiles arguing with this document. A resume can back more than one —
    # the same CV reads as "backend" or as "platform" depending on the intent
    # wrapped around it.
    profiles: Mapped[list[Profile]] = relationship(back_populates="resume")

    @property
    def has_original_file(self) -> bool:
        """Whether the upload itself is on file, without loading it."""
        return bool(self.file_size)

    @property
    def display_label(self) -> str:
        """Short human label for the resume (used in the UI picker)."""
        role = (self.target_roles or [None])[0]
        return role or self.headline or self.filename or f"Resume #{self.id}"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Resume id={self.id} name={self.full_name!r}>"
