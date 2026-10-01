"""Record the role-relevance dimension on a fit score

Adds ``fit_scores.role_score`` — how close the posting's title is to the roles
the candidate is actually chasing. The dimension that decides whether a posting
is the *kind* of job this person does, which the score previously never asked.

Left null for existing rows on purpose. Those scores were computed by a weight
table that had no role term, so any value written here would be invented; a null
says "scored before this existed", which is the truth. Rows re-score naturally
the next time their posting is looked at.

Purely additive; the downgrade is a clean reversal.

Revision ID: c8f1a3d75e02
Revises: a4c9f2e18d63
Create Date: 2026-07-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8f1a3d75e02"
down_revision: str | None = "a4c9f2e18d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fit_scores", sa.Column("role_score", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("fit_scores", "role_score")
