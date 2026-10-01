"""Remember the address a recruiter asked to be answered on

Most recruiting platforms — Gem, Loxo, Bullhorn, and every in-house ATS that
does a mail merge — send from an unmonitored address and put the actual human in
``Reply-To``. The scanner had no column for that, so two things happened, both
bad.

The classifier saw ``noreply@`` in the ``From`` and filed the message as
``NOT_RECRUITER`` on the strength of an address the recruiter never chose. That
is a real person writing about a real job, discarded before anything read the
words. And on the messages that did survive, the reply was addressed to the
``noreply@`` — mail into a void, which is worse than not replying, because the
product reports it as answered.

``reply_to_address`` is stored beside ``from_address`` rather than replacing it,
because they answer different questions. The ``From`` is who wrote, and that is
what the inbox should show. The ``Reply-To`` is where an answer goes, and that is
what the reply pipeline and the recruiter contact row should key on (see
``RecruiterEmail.reply_address``).

Nullable with no backfill: the header wasn't captured on existing rows and can't
be recovered from what we stored, and ``reply_address`` falls back to the ``From``
exactly as before for every one of them.

Revision ID: d2b6e8f04a71
Revises: c1f7b3a25e94
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "d2b6e8f04a71"
down_revision: str | None = "c1f7b3a25e94"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "recruiter_emails",
        sa.Column("reply_to_address", sa.String(length=320), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recruiter_emails", "reply_to_address")
