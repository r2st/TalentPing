"""Let a user stop the agent writing to one contact without destroying the file

``DELETE /recruiters/{id}`` was the only way to say "don't email this person",
and it was the wrong shape twice over.

Too much: ``recruiters.applications`` cascades, and an application cascades to
its thread, and a thread to its emails and their attachments and events. So
removing one contact from a list silently deleted every message ever exchanged
with them, the interview status events, and the follow-ups — the pipeline
history the whole product exists to accumulate.

Too little: it did not stick. ``recruiter_discovery._upsert_and_commit`` matches
on ``(user_id, email)``, so the next discovery run for that company re-created
the row it had just deleted, at full confidence, with the contact re-armed for
outreach. The user's decision survived exactly until the next crawl.

``excluded_at`` is that decision, stored where the send gates already look.
Nullable with no backfill: every existing row is a contact the agent may write
to, which is what null means.

Revision ID: a1c7f4e29d05
Revises: e7c31a5d9b40
Create Date: 2026-08-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c7f4e29d05"
down_revision: str | None = "e7c31a5d9b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recruiters",
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recruiters", "excluded_at")
