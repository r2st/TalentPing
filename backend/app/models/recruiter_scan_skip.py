"""Mail the scanner has already fetched once and dismissed on sight.

:mod:`app.services.inbound_scanner` runs five filters over a mailbox, and its
first one — ``known_ids``, built from :class:`~app.models.recruiter_email.RecruiterEmail`
— is what makes a rescan cheap: a message we have a row for is skipped before it
is even fetched. Every filter after it runs on the *fetched* message, because
the only thing Gmail's list call returns is an id and a thread id.

So a message that is fetched and then dismissed leaves no trace, and filter 1
cannot suppress it. The next scan fetches the same body again, and the one after
that, for as long as the message stays inside the rolling
``recruiter_scan_window_days`` window. At the 5-minute beat cadence that is 288
fetches a day per dismissed message.

That is not a hypothetical shape. It is what a production mailbox was doing when
this table was written: 394 messages listed, 152 of them fetched, **145
dismissed as blocked senders**, 0 detected — every five minutes, all day. The
per-run cap does not bound it, because the cap counts messages that *survive*
(``len(result.messages) >= cap``) and none of these do.

The same defect was found and fixed once already for delivery-failure notices,
which is why :func:`app.services.recruiter_reply_service._persist_bounce`
exists — a bounce "has to be *remembered* in the same breath it is counted".
This table is that lesson applied to the dismissals that are not bounces.

**Why not a ``RecruiterEmail`` row, like a bounce gets.** Two reasons. That
table's docstring says "one row per detected inbound message", and a matrimonial
site's newsletter is not one; and every such row is visible in the Recruiter
Inbox's *All* tab, so 145 of them per mailbox would bury the mail the user
actually opened the product to read. A dismissal is scanner bookkeeping, not
correspondence, so it gets a table the inbox never queries — and no body, which
is what keeps 145 newsletters a mailbox from becoming a storage problem.

**Only the filters that read the sender's address get a row.** ``blocked_sender``
and ``from_self`` are decided by the ``From:`` header alone, so re-reading the
message can never change the answer and remembering the verdict loses nothing.
The other two dismissals — the opt-out heuristic and the bounce sniffer — read
the *body*, and are exactly the kind of judgement that has been wrong here
before: a platform footer's ``Unsubscribe:`` line once read as the sender's own
words and retired nineteen live recruiters. Those two stay re-evaluated on every
pass, so fixing the heuristic re-opens the mail it misjudged. Sticking them here
would make each mistake permanent, which is the one thing this table must not
buy with its savings.

**Removing a domain from the blocklist does not re-open its dismissed mail.**
The rows outlive the list that created them. That is bounded — the Gmail query
only ever looks back ``recruiter_scan_window_days``, so at most one window's
mail is affected — and recoverable, because the row records both the address and
the reason: deleting the rows for one domain puts its mail back in front of the
classifier on the next scan.

Rows are cheap and bounded by real mail volume: one per distinct dismissed
message per user, written once and never updated.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

#: Why the message was dismissed. Plain strings rather than an enum, matching
#: ``recruiter_scan_runs.trigger``: this is a label on an append-only row, and
#: it is read by humans asking "why is that email not in my inbox?".
#:
#: Both are decided by the sender's address alone — see the module docstring for
#: why no body-reading filter may join them.
REASON_BLOCKED_SENDER = "blocked_sender"
REASON_FROM_SELF = "from_self"


class RecruiterScanSkip(Base, TimestampMixin):
    __tablename__ = "recruiter_scan_skips"
    __table_args__ = (
        # What makes the write idempotent, and what settles the race between two
        # overlapping scans that both fetched the same message before either
        # stored it. The scanner's read of this table is a SELECT and the write
        # is an INSERT, exactly as with ``uq_recruiter_email_user_message``.
        UniqueConstraint(
            "user_id", "gmail_message_id", name="uq_recruiter_scan_skip_user_message"
        ),
        # The scanner's only read: this user's dismissals inside the dedup
        # window, newest first.
        Index("ix_recruiter_scan_skips_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # SET NULL rather than CASCADE, matching ``recruiter_emails`` and
    # ``recruiter_scan_runs``: disconnecting a mailbox must not forget which of
    # its mail had already been dismissed, or the next scan re-fetches all of it.
    gmail_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="SET NULL"), index=True
    )
    gmail_message_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # blocked_sender | from_self — see the constants above.
    reason: Mapped[str] = mapped_column(String(32), nullable=False)

    # The sender, so a domain that turns out to have been blocked in error can
    # be found and un-dismissed without reading Gmail. This is the only reason
    # the column exists; nothing in the scan path reads it.
    from_address: Mapped[str | None] = mapped_column(String(320))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<RecruiterScanSkip user={self.user_id} "
            f"{self.reason} {self.from_address}>"
        )


__all__ = [
    "REASON_BLOCKED_SENDER",
    "REASON_FROM_SELF",
    "RecruiterScanSkip",
]
