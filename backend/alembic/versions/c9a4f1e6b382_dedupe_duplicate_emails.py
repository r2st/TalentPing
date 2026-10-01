"""Merge duplicate threads, dedupe emails, and constrain both going forward

``recruiter_reply_service._write_reply`` used to build a fresh ``EmailThread``
every time it ran, with no check for whether one already existed for the
Gmail conversation it was answering. ``inbound_scanner.scan``'s "is this ours
already?" filter is a snapshot taken once per scan, so two messages on the
same brand-new Gmail thread that were listed in the same scan (or in
overlapping scans before the first message's thread row committed) both read
as first contact and both went through ``_write_reply`` independently — each
creating its own ``EmailThread`` carrying the identical ``gmail_thread_id``.

Nothing stopped there. ``inbox_tasks.poll_thread`` polls every ``EmailThread``
row with a Gmail id independently and, for each one, fetches the *entire*
history of that Gmail thread and stores whatever isn't already under that
row's own ``Email.thread_id``. Handed two thread rows for the same
conversation, it backfilled each with the message the other one had and it was
missing — so the recruiter's message, and every reply after it, ended up
stored twice, once per duplicate thread. That is where the ~40 duplicate
``emails`` rows came from: not a rejected insert, but two threads each quietly
believing itself the only one and copying the other's mail into itself on
every poll.

This migration cleans up what that produced and then closes the gap:

1. Every thread sharing a ``gmail_thread_id`` with an earlier one is merged
   into the earliest — its emails are repointed at the survivor.
2. Every email sharing a ``gmail_message_id`` with an earlier one (which, once
   step 1 has run, means the exact duplicate rows described above) is
   deleted, keeping the earliest.
3. Thread ``message_count``/``last_message_at`` are recomputed from what
   actually survives, since both were maintained by hand and the deleted rows
   leave them wrong.
4. The now-empty loser threads are deleted.
5. Two partial unique indexes make both kinds of duplicate a constraint
   violation instead of a silent extra row from here on — see
   ``app.models.email.Email`` and ``app.models.email_thread.EmailThread``.

Every step is scoped to non-NULL ids, so a thread with no Gmail id yet
(``outreach_service`` creates the row before the first send) and an email we
composed ourselves (never had a Gmail id) are untouched throughout. Safe to
run twice: with no duplicates left, every ``WHERE`` below matches nothing.

Revision ID: c9a4f1e6b382
Revises: c5f2a9d84e17
Create Date: 2026-08-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9a4f1e6b382"
down_revision: str | None = "c5f2a9d84e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Repoint every duplicate-thread email at the earliest thread for that
    # gmail_thread_id. A no-op for emails already on the survivor.
    op.execute(
        """
        UPDATE emails
        SET thread_id = (
            SELECT MIN(t2.id)
            FROM email_threads t2
            WHERE t2.gmail_thread_id = (
                SELECT t1.gmail_thread_id FROM email_threads t1
                WHERE t1.id = emails.thread_id
            )
        )
        WHERE thread_id IN (
            SELECT id FROM email_threads
            WHERE gmail_thread_id IN (
                SELECT gmail_thread_id FROM email_threads
                WHERE gmail_thread_id IS NOT NULL
                GROUP BY gmail_thread_id
                HAVING COUNT(*) > 1
            )
        )
        """
    )

    # 2. One row per gmail_message_id, keeping the earliest. Global rather than
    # scoped to the merged threads above, so this also catches a duplicate
    # made by some path other than the one this migration's docstring traces.
    op.execute(
        """
        DELETE FROM emails
        WHERE gmail_message_id IS NOT NULL
        AND id NOT IN (
            SELECT MIN(id) FROM emails
            WHERE gmail_message_id IS NOT NULL
            GROUP BY gmail_message_id
        )
        """
    )

    # 3. The denormalized counters, recomputed from what survived steps 1-2.
    op.execute(
        """
        UPDATE email_threads
        SET message_count = (
            SELECT COUNT(*) FROM emails WHERE emails.thread_id = email_threads.id
        )
        """
    )
    op.execute(
        """
        UPDATE email_threads
        SET last_message_at = (
            SELECT MAX(sent_at) FROM emails WHERE emails.thread_id = email_threads.id
        )
        WHERE EXISTS (
            SELECT 1 FROM emails
            WHERE emails.thread_id = email_threads.id AND emails.sent_at IS NOT NULL
        )
        """
    )

    # 4. The loser threads are empty now (step 1 moved every email off them) —
    # safe to drop. ``emails.thread_id`` is ON DELETE CASCADE, which is exactly
    # the "empty anyway" case here, not a risk.
    op.execute(
        """
        DELETE FROM email_threads
        WHERE gmail_thread_id IS NOT NULL
        AND id NOT IN (
            SELECT MIN(id) FROM email_threads
            WHERE gmail_thread_id IS NOT NULL
            GROUP BY gmail_thread_id
        )
        """
    )

    # 5. Constrain both going forward. The plain indexes each column already
    # had are dropped rather than left standing beside the unique one: a
    # partial unique index answers every lookup a non-unique index would, so
    # the old one is a second btree earning its keep on nothing but writes.
    op.drop_index("ix_emails_gmail_message_id", table_name="emails")
    op.create_index(
        "uq_emails_gmail_message_id",
        "emails",
        ["gmail_message_id"],
        unique=True,
        postgresql_where=sa.text("gmail_message_id IS NOT NULL"),
        sqlite_where=sa.text("gmail_message_id IS NOT NULL"),
    )
    op.drop_index("ix_email_threads_gmail_thread_id", table_name="email_threads")
    op.create_index(
        "uq_email_threads_gmail_thread_id",
        "email_threads",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text("gmail_thread_id IS NOT NULL"),
        sqlite_where=sa.text("gmail_thread_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_email_threads_gmail_thread_id", table_name="email_threads")
    op.create_index("ix_email_threads_gmail_thread_id", "email_threads", ["gmail_thread_id"])
    op.drop_index("uq_emails_gmail_message_id", table_name="emails")
    op.create_index("ix_emails_gmail_message_id", "emails", ["gmail_message_id"])
    # The merge and the deletes are not reversible — which rows were duplicates
    # of which is gone the moment they're gone. Nothing to do here but leave
    # the cleaned-up data as it is.
