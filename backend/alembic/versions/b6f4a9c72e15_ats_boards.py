"""ATS board cache — which company publishes on which public JSON board

Adds ``ats_boards``: a globally-shared record of the ATS a company's careers
board runs on and the token its public API is keyed by, so discovering it is paid
for once by whoever looks first rather than once per user per scan.

Negative results are rows too (``status='none'``), which is the point — without
them every scan would re-probe five platforms for every employer that self-hosts.

Revision ID: b6f4a9c72e15
Revises: a2e9c4f70b31
Create Date: 2026-07-30
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b6f4a9c72e15"
down_revision: str | None = "a2e9c4f70b31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ats_boards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=True),
        sa.Column("board_token", sa.String(length=120), nullable=True),
        sa.Column("board_url", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="none"
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("jobs_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_name", name="uq_ats_board_normalized_name"),
    )
    op.create_index("ix_ats_boards_company", "ats_boards", ["company"])
    op.create_index("ix_ats_boards_normalized_name", "ats_boards", ["normalized_name"])


def downgrade() -> None:
    op.drop_index("ix_ats_boards_normalized_name", table_name="ats_boards")
    op.drop_index("ix_ats_boards_company", table_name="ats_boards")
    op.drop_table("ats_boards")
