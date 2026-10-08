"""Add OAuth SSO columns to users table

Revision ID: e1a2b3c4d5f6
Revises: d1e5f9a3c724
Create Date: 2026-10-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "e1a2b3c4d5f6"
down_revision: str | None = "d1e5f9a3c724"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("oauth_provider", sa.String(30), nullable=True))
    op.add_column("users", sa.Column("oauth_id", sa.String(255), nullable=True))
    op.alter_column("users", "hashed_password", server_default="", nullable=False)


def downgrade() -> None:
    op.drop_column("users", "oauth_id")
    op.drop_column("users", "oauth_provider")
