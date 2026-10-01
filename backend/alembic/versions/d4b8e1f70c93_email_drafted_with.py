"""Record how each reply draft was written

``thread_reply_policy`` refuses to auto-send a draft that came from the
deterministic template rather than from a model. The reasoning is in that
module: when the LLM chain is down ``reply_agent`` returns a fallback that is
contextual only in its shape, which is a fine thing to show someone under
"Scout suggests" and not a thing to send in their name.

That rule was enforced at the moment a draft was written, and nowhere else,
because nothing on the row said which kind it was. ``inbox_tasks``'s
re-evaluation pass — the one that walks the *backlog* of unrouted drafts and
queues the ones that would have sent themselves — therefore passed a flat
``drafted_with="llm"`` and said so in a comment: the rows do not record it, and
treating them as generated is the honest reading.

It is not, and the population is why. A backlog of unrouted drafts is what
accumulates while the model chain is down; a chain that is down is what makes a
draft a template. So the drafts most likely to *be* templates were precisely the
ones being told they were generations, and the one rule standing between a
template and a recruiter's inbox was inverted for exactly the rows it existed
for. Production reached this state twice — once when a retired model id took the
chain down in silence, once when both mailboxes' OAuth grants expired.

The column is nullable and stays that way. Backfilling it would mean inventing
the answer for every existing row, which is the bug. ``None`` means "nothing
recorded this", the policy holds on it, and the drafts affected keep the status
they already have: waiting, now visibly so.

Revision ID: d4b8e1f70c93
Revises: c9a4f1e6b382
Create Date: 2026-08-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4b8e1f70c93"
down_revision: str | None = "c9a4f1e6b382"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("emails", sa.Column("drafted_with", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("emails", "drafted_with")
