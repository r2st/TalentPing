"""Ghost-job signals: repost count and risk on postings, a bar on saved searches

Roughly a fifth of scraped postings are ghosts — expired, never budgeted, or a
resume-pipeline farm. ``app.services.ghost_job`` scores them from columns the
fetchers already fill; this migration gives that score somewhere to live.

``job_postings.ghost_risk`` is nullable and means *never assessed*, which is not
the same as zero. The feed's filter admits nulls for exactly that reason, on the
same argument as the salary floor admitting postings that published no band.

Existing rows are backfilled here rather than left null, so a posting found
before the deploy and one found after it read identically in the feed — the same
promise migration ``c9a4e7b21d68`` made for salary bands. The backfill can only
see age, wording and source (``repost_count`` starts at zero for everyone, since
the scans that would have counted a repost happened before we were counting), so
a backfilled row scores at or below what a freshly-ingested one would. Under-
calling a ghost is the safe direction: it shows the posting.

Revision ID: e3b8c1a75d24
Revises: c9a4e7b21d68
Create Date: 2026-07-31
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "e3b8c1a75d24"
down_revision: str | None = "c9a4e7b21d68"
branch_labels = None
depends_on = None

_BATCH = 500


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column("repost_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("job_postings", sa.Column("ghost_risk", sa.Integer(), nullable=True))
    op.add_column("job_postings", sa.Column("ghost_reasons", sa.JSON(), nullable=True))
    op.add_column(
        "job_searches",
        sa.Column("max_ghost_risk", sa.Integer(), nullable=False, server_default="85"),
    )

    # Backfill through the same assessor the ingest path uses.
    from app.services import ghost_job

    bind = op.get_bind()
    postings = sa.table(
        "job_postings",
        sa.column("id", sa.Integer),
        sa.column("title", sa.String),
        sa.column("description", sa.Text),
        sa.column("source", sa.String),
        sa.column("posted_at", sa.DateTime(timezone=True)),
        sa.column("source_urls", sa.JSON),
        sa.column("ghost_risk", sa.Integer),
        sa.column("ghost_reasons", sa.JSON),
    )

    class _Row:
        """The structural shape :func:`ghost_job.assess` reads."""

        def __init__(self, title, description, source, posted_at):
            self.title = title
            self.description = description
            self.source = source
            self.posted_at = posted_at

    offset = 0
    while True:
        rows = bind.execute(
            sa.select(
                postings.c.id,
                postings.c.title,
                postings.c.description,
                postings.c.source,
                postings.c.posted_at,
                postings.c.source_urls,
            )
            .order_by(postings.c.id)
            .limit(_BATCH)
            .offset(offset)
        ).all()
        if not rows:
            break
        for row_id, title, description, source, posted_at, source_urls in rows:
            assessment = ghost_job.assess(
                _Row(title, description, source, posted_at),
                source_count=max(1, len(source_urls or [])),
            )
            bind.execute(
                sa.update(postings)
                .where(postings.c.id == row_id)
                .values(
                    ghost_risk=assessment.risk,
                    ghost_reasons=list(assessment.reasons),
                )
            )
        offset += _BATCH


def downgrade() -> None:
    op.drop_column("job_searches", "max_ghost_risk")
    op.drop_column("job_postings", "ghost_reasons")
    op.drop_column("job_postings", "repost_count")
    op.drop_column("job_postings", "ghost_risk")
