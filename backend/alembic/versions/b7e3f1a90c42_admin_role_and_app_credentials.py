"""Admin role and dashboard-managed deployment credentials

Two additions that go together, because one is the authorization for the other.

``users.role`` is ``user`` or ``admin``, defaulting to ``user`` — so every
existing account keeps exactly the access it had, and a deployment upgrading to
this has zero administrators until one is named. ``ADMIN_EMAILS`` is what names
the first one (reconciled at login, see ``routers.auth._apply_admin_bootstrap``)
so bootstrapping does not require SQL on production.

``app_credentials`` holds *overrides* for values that until now arrived only
through the environment: a row wins for its key, and no row means the ``.env``
value stands. An empty table is therefore a complete no-op, which is what this
migration leaves behind — nothing is backfilled into it, because the environment
is already the source and copying it in would turn a fallback into a snapshot
that stops tracking the deploy.

Values are Fernet-encrypted by the application before they reach the column
(``services.crypto``), so this is deliberately ``Text`` and not something that
looks readable.

Revision ID: b7e3f1a90c42
Revises: a4f7c209d3e8
Create Date: 2026-08-05
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e3f1a90c42"
down_revision: str | None = "a4f7c209d3e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default rather than a backfill: it is what makes the column
    # non-null for the rows that already exist *and* for any row written by a
    # worker still running the previous release during the deploy.
    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.String(length=20),
            nullable=False,
            server_default="user",
        ),
    )

    op.create_table(
        "app_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(length=320), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Unique, not merely indexed. The store upserts on `key`, so two rows for
    # one credential would make "which value is live?" depend on row order —
    # and the answer would differ per worker.
    op.create_index(
        "ix_app_credentials_key", "app_credentials", ["key"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_app_credentials_key", table_name="app_credentials")
    op.drop_table("app_credentials")
    op.drop_column("users", "role")
