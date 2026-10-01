"""Remember the Message-ID of the mail we send

Adds ``emails.rfc_message_id``: the RFC 5322 ``Message-ID`` Gmail stamped on a
message this product sent, read back from the API right after the send.

``emails.gmail_message_id`` is not that. It is Gmail's internal API handle,
redeemable only inside the mailbox that holds it, and no recipient's mail client
has ever seen one. So the product knew how to find its own sent mail and had no
way to *name* it — which meant a follow-up could not carry ``In-Reply-To`` or
``References``, because there was no id to put in them.

The consequence was on the wire. A follow-up went out with Gmail's ``threadId``
alone, which threads the conversation for recipients reading in Gmail and for
nobody else: in Outlook, Apple Mail, Thunderbird and every ATS that ingests mail,
a three-step sequence arrived as three unrelated messages that happened to share
a subject line. That is both the wrong reading experience and a textbook bulk
signal — near-identical unthreaded mail, repeated, from one sender.

Nullable and not backfilled. Nothing else in the database holds an RFC id for a
sent message, so there is nothing to backfill *from*; rows written before this
column keep sending follow-ups the way they always did, unthreaded, rather than
threading off a fabricated id.

Revision ID: f4a7b2c19e05
Revises: a1c93f27b6e4
Create Date: 2026-08-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4a7b2c19e05"
down_revision: str | None = "a1c93f27b6e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 998 is RFC 5322's own line-length ceiling, and the width `in_reply_to`
    # already uses for the same kind of value.
    op.add_column("emails", sa.Column("rfc_message_id", sa.String(length=998), nullable=True))


def downgrade() -> None:
    op.drop_column("emails", "rfc_message_id")
