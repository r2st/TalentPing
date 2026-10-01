"""Email thread model — wraps a Gmail conversation."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.email import Email


class EmailThread(Base, TimestampMixin):
    __tablename__ = "email_threads"

    # Backstop, not the primary defense — see
    # recruiter_reply_service._write_reply's thread lookup for the actual fix.
    # A Gmail thread id names one physical conversation; two of our rows
    # claiming it is always the duplicate-thread bug, never two legitimate
    # conversations. Partial because a thread starts out with no Gmail id at
    # all — see outreach_service, which creates the row before anything has
    # been sent.
    __table_args__ = (
        Index(
            "uq_email_threads_gmail_thread_id",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text("gmail_thread_id IS NOT NULL"),
            sqlite_where=text("gmail_thread_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Which connected mailbox this conversation lives in. A Gmail thread id is
    # only meaningful inside the mailbox that holds it, so a thread that does not
    # name its mailbox cannot be polled, replied to, or have its attachments
    # fetched once the user has more than one. Nullable because every row written
    # before this column existed belongs to whatever was primary at the time —
    # see :func:`app.services.gmail_accounts.resolve_for_thread`, which falls back
    # rather than guessing wrong.
    #
    # SET NULL, not CASCADE: disconnecting a mailbox must cost the thread its
    # address, not its history.
    gmail_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="SET NULL"), index=True
    )

    # Indexed by uq_email_threads_gmail_thread_id above, not a second plain
    # index — the partial unique one covers every lookup a non-unique index
    # would.
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(998))
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    application: Mapped[Application] = relationship(back_populates="threads")
    emails: Mapped[list[Email]] = relationship(
        back_populates="thread", cascade="all, delete-orphan", order_by="Email.id"
    )
