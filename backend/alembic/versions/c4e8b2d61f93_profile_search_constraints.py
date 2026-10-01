"""Employment type, company size and excluded companies on a search profile

Three constraint lists that the fit score deliberately does not weigh. See
``app/services/search_filters.py`` for why they are gates rather than
dimensions: a weighted average can be outvoted, and "I will not work for this
employer" is not a preference to be outvoted.

All three are ``NOT NULL`` with a ``'[]'`` server default, which is what lets
them be added to a populated table without a backfill pass. The models declare
only the Python-side ``default=list`` — the same asymmetry every other JSON list
column in this schema has, and one ``tests/test_schema_drift.py`` does not flag
because Alembic's comparison ignores server defaults.

An empty list means "no opinion", everywhere, and every existing profile
therefore keeps behaving exactly as it did: three gates that immediately return
``None``.

Revision ID: c4e8b2d61f93
Revises: b3d9c1f47a20
Create Date: 2026-08-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e8b2d61f93"
down_revision: str | None = "b3d9c1f47a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "profiles"
_COLUMNS = ("employment_types", "company_sizes", "excluded_companies")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            _TABLE,
            sa.Column(name, sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
