"""Gmail push-notification subscription for one connected mailbox.

Gmail's ``users.watch`` registers a Cloud Pub/Sub topic that Google publishes to
whenever the mailbox changes, and returns a ``historyId`` marking the point in
the mailbox's change log the subscription starts from. Instead of asking "has
anything happened?" every five minutes, the webhook is told the moment something
does — replies land in the product in seconds rather than minutes, and a mailbox
that is quiet all day costs zero API calls.

Two properties of the API shape this row:

* **Watches expire after 7 days.** Google says to renew at least daily. The
  ``expires_at`` column is what the renewal beat task sweeps on; letting it lapse
  silently turns push off with no error anywhere, which is exactly the failure
  this column exists to make visible.
* **Notifications carry no message content** — only the mailbox address and the
  new ``history_id``. The actual fetch is ``users.history.list`` from the last
  history id we stored, which is why that cursor lives here and is advanced only
  after the messages it covers have been processed.

``status`` is the fallback switch: anything other than ``active`` means polling
is this mailbox's only route, and :mod:`app.tasks.inbox_tasks` polls it normally.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.gmail_account import GmailAccount

# Google's hard ceiling on a watch. Ours is renewed well before this.
WATCH_TTL_DAYS = 7


class GmailWatch(Base, TimestampMixin):
    __tablename__ = "gmail_watches"

    id: Mapped[int] = mapped_column(primary_key=True)
    # One subscription per mailbox — calling watch() again replaces the previous
    # one at Google's end, so a second row could only ever be stale.
    gmail_account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # The Pub/Sub topic Google publishes to, as configured on the server.
    topic: Mapped[str | None] = mapped_column(String(500))
    # The mailbox change-log cursor. Advanced only once the history it covers has
    # been processed, so a crash mid-fetch replays rather than skips.
    history_id: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # active | failed | stopped — anything but active means polling covers this
    # mailbox instead.
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When Google last told us something changed. A watch that claims to be
    # active but has been silent for hours is the signal to fall back to polling.
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notifications_received: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    messages_ingested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    account: Mapped[GmailAccount] = relationship(back_populates="watch")

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<GmailWatch account={self.gmail_account_id} status={self.status}>"
