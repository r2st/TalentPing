"""Give every user a token generation, so sessions can be revoked

Adds ``users.token_version``. Access tokens carry the generation they were
minted under; ``get_current_user`` refuses a token whose generation no longer
matches the row. That is what makes a password change end the sessions that
already exist — before this, a changed password left every issued token working
for the rest of its lifetime, which is a day by default.

Backfilled to 0 rather than to something derived from the row, and tokens
minted before this existed carry no generation claim and are read as 0. The two
agree, so deploying this does not sign the whole userbase out; the first
password change on an account is what starts moving its generation.

``server_default`` stays on the column rather than being dropped after the
backfill: rows are also created by fixtures and by hand, and a NOT NULL column
whose default lives only in the ORM is a column that fails an INSERT nobody
wrote in Python.

Revision ID: a7f3c9d21e64
Revises: c3f8a1e59d04
Create Date: 2026-08-02
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7f3c9d21e64"
down_revision: str | None = "c3f8a1e59d04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
