"""Record tasks that failed for the last time

Adds ``dead_letter_jobs``. ``task_ignore_result=True`` is set globally, so a
Celery task that exhausts its retries currently leaves a traceback on one
worker's stderr and nothing else — the deployment cannot answer "what has been
failing?", and there is no way to run the work again once the cause is fixed.

``fingerprint`` carries the index rather than a unique constraint. The capture
path collapses a repeating failure by looking for an open row with the same
fingerprint and incrementing its counter, but two workers can lose that race and
both insert. Two rows saying the same thing is a cosmetic problem; a unique
constraint would make the loser's INSERT raise *inside the handler that exists
to record failures*, which is the one thing this table may never do.

No foreign key on ``user_id`` for the same reason: it is copied out of a task
argument on the failure path, so its value is whatever the caller passed —
including a stale id. See :mod:`app.models.dead_letter`.

``server_default`` on the NOT NULL columns because rows are also created by
fixtures and by hand.

Revision ID: f2c7a9e4b310
Revises: d1e5b8c3a927
Create Date: 2026-08-08
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2c7a9e4b310"
down_revision: str | None = "d1e5b8c3a927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dead_letter_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_name", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("queue", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("args_json", sa.Text(), nullable=True),
        sa.Column("kwargs_json", sa.Text(), nullable=True),
        sa.Column(
            "reason", sa.String(length=16), nullable=False, server_default="failed"
        ),
        sa.Column("exception_type", sa.String(length=255), nullable=True),
        sa.Column("exception_message", sa.Text(), nullable=True),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "replayable", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replayed_task_id", sa.String(length=64), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dead_letter_jobs_task_name", "dead_letter_jobs", ["task_name"]
    )
    op.create_index("ix_dead_letter_jobs_task_id", "dead_letter_jobs", ["task_id"])
    op.create_index("ix_dead_letter_jobs_user_id", "dead_letter_jobs", ["user_id"])
    op.create_index("ix_dead_letter_jobs_status", "dead_letter_jobs", ["status"])
    op.create_index(
        "ix_dead_letter_jobs_fingerprint_status",
        "dead_letter_jobs",
        ["fingerprint", "status"],
    )
    op.create_index(
        "ix_dead_letter_jobs_status_last_failed",
        "dead_letter_jobs",
        ["status", "last_failed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dead_letter_jobs_status_last_failed", "dead_letter_jobs")
    op.drop_index("ix_dead_letter_jobs_fingerprint_status", "dead_letter_jobs")
    op.drop_index("ix_dead_letter_jobs_status", "dead_letter_jobs")
    op.drop_index("ix_dead_letter_jobs_user_id", "dead_letter_jobs")
    op.drop_index("ix_dead_letter_jobs_task_id", "dead_letter_jobs")
    op.drop_index("ix_dead_letter_jobs_task_name", "dead_letter_jobs")
    op.drop_table("dead_letter_jobs")
