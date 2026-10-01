"""A file the *user* attached to an outbound message, by hand.

Everything else that travels with an email is resolved: the resume comes off
the candidate's profile, the cover letter off the composer's intent, and both
are produced at send time. That covers the automation and nothing else — a
recruiter who asks for a portfolio, a signed offer letter, a reference sheet or
a second CV had no way of getting one, because the pipeline could only send
documents it knew how to derive.

This is the manual half. Rows here are attached to exactly one email, ordered
after the resolved files, and stored rather than re-derived: there is nothing to
re-derive them from, and a message that has already gone out must be able to
show what it actually carried.

Bytes live in the row. The alternative is object storage, which is the right
answer at a size this product does not have — these are single documents capped
at a few megabytes, hung off a draft that is usually approved within the day.
``content`` is deferred so listing a thread never pays for them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.email import Email


class EmailAttachment(Base, TimestampMixin):
    __tablename__ = "email_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True, nullable=False
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(120), default="application/octet-stream", nullable=False
    )
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)

    email: Mapped[Email] = relationship(back_populates="user_attachments")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<EmailAttachment id={self.id} filename={self.filename!r}>"
