"""Salary filtering: parsed bands on postings, a floor on saved searches

``job_postings.salary_text`` is what the employer wrote ("$120k – $160k", "Up to
£90,000", "competitive"). Filtering on it needs numbers, and parsing prose in a
WHERE clause is not a thing, so the band is parsed once at ingest into
``salary_min``/``salary_max``.

Null means *the employer published nothing*, which is the common case and is
never the same thing as zero — see ``app.services.salary_service.meets_floor``
for why an unpublished band always clears a floor.

Existing rows are backfilled here rather than left null. Without it a user who
set a floor would see their feed behave differently for postings found before
and after the deploy, with nothing on screen to explain the difference.

Revision ID: c9a4e7b21d68
Revises: b6f4a9c72e15
Create Date: 2026-07-31
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c9a4e7b21d68"
down_revision: str | None = "b6f4a9c72e15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("salary_min", sa.Integer(), nullable=True))
    op.add_column("job_postings", sa.Column("salary_max", sa.Integer(), nullable=True))
    op.add_column("job_searches", sa.Column("min_salary", sa.Integer(), nullable=True))

    # Backfill through the same parser the ingest path uses, so a row written
    # before this migration and a row written after it read identically.
    from app.services.salary_service import parse_offered

    bind = op.get_bind()
    postings = sa.table(
        "job_postings",
        sa.column("id", sa.Integer),
        sa.column("salary_text", sa.String),
        sa.column("salary_min", sa.Integer),
        sa.column("salary_max", sa.Integer),
    )
    rows = bind.execute(
        sa.select(postings.c.id, postings.c.salary_text).where(
            postings.c.salary_text.is_not(None)
        )
    ).all()
    for row_id, salary_text in rows:
        low, high = parse_offered(salary_text)
        if low is None and high is None:
            continue
        bind.execute(
            sa.update(postings)
            .where(postings.c.id == row_id)
            .values(salary_min=low, salary_max=high)
        )


def downgrade() -> None:
    op.drop_column("job_searches", "min_salary")
    op.drop_column("job_postings", "salary_max")
    op.drop_column("job_postings", "salary_min")
