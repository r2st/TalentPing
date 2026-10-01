"""Scout re-rank, cross-board dedup, salary benchmarks, company profiles

Four additive changes behind the market-intelligence work:

* ``job_postings.llm_fit_score`` / ``llm_reasoning`` — Scout's second opinion on
  the shortlist. Deliberately separate columns: the deterministic ``fit_score``
  the feed sorts and reports on is never rewritten by a model.
* ``job_postings.duplicate_of_id`` / ``source_urls`` — the same role carried by
  several boards collapses onto one canonical row, which keeps a link to every
  board it was seen on. A self-referential FK, nulled rather than cascaded on
  delete so removing a canonical row promotes its copies back into the feed
  instead of deleting jobs the candidate never chose to lose.
* ``salary_benchmarks`` — market bands per role family × seniority × market,
  global rather than per user.
* ``company_profiles`` — cached company research keyed on the normalized company
  name, refreshed weekly.

Purely additive; the downgrade is a clean reversal.

Revision ID: b7c5d1e93f42
Revises: f3a6b1c8d920
Create Date: 2026-07-25
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c5d1e93f42"
down_revision: str | None = "f3a6b1c8d920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- Scout's re-rank + cross-board dedup on existing postings -----------
    op.add_column("job_postings", sa.Column("llm_fit_score", sa.Float(), nullable=True))
    op.add_column("job_postings", sa.Column("llm_reasoning", sa.Text(), nullable=True))
    op.add_column("job_postings", sa.Column("duplicate_of_id", sa.Integer(), nullable=True))
    op.add_column(
        "job_postings",
        sa.Column("source_urls", sa.JSON(), nullable=True, server_default="[]"),
    )
    op.create_index(
        "ix_job_postings_duplicate_of_id", "job_postings", ["duplicate_of_id"]
    )
    op.create_foreign_key(
        "fk_job_postings_duplicate_of_id",
        "job_postings",
        "job_postings",
        ["duplicate_of_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- Market salary bands -------------------------------------------------
    op.create_table(
        "salary_benchmarks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("role_family", sa.String(length=64), nullable=False),
        sa.Column("seniority", sa.String(length=16), nullable=False),
        sa.Column("location_key", sa.String(length=64), nullable=False),
        sa.Column("role_label", sa.String(length=120), nullable=True),
        sa.Column("location_label", sa.String(length=120), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("salary_min", sa.Integer(), nullable=False),
        sa.Column("salary_median", sa.Integer(), nullable=False),
        sa.Column("salary_max", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="modelled"),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "role_family",
            "seniority",
            "location_key",
            name="uq_salary_benchmark_role_seniority_location",
        ),
    )
    op.create_index("ix_salary_benchmarks_role_family", "salary_benchmarks", ["role_family"])
    op.create_index("ix_salary_benchmarks_location_key", "salary_benchmarks", ["location_key"])

    # ---- Cached company research --------------------------------------------
    op.create_table(
        "company_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("size", sa.String(length=32), nullable=True),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("founded_year", sa.Integer(), nullable=True),
        sa.Column("headquarters", sa.String(length=255), nullable=True),
        sa.Column("industry", sa.String(length=120), nullable=True),
        sa.Column("funding_stage", sa.String(length=32), nullable=True),
        sa.Column("funding_total", sa.String(length=64), nullable=True),
        sa.Column("glassdoor_rating", sa.Float(), nullable=True),
        sa.Column("tech_stack", sa.JSON(), nullable=True, server_default="[]"),
        sa.Column("news", sa.JSON(), nullable=True, server_default="[]"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="heuristic"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ok"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("researched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
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
        sa.UniqueConstraint("normalized_name", name="uq_company_profile_normalized_name"),
    )
    op.create_index(
        "ix_company_profiles_normalized_name", "company_profiles", ["normalized_name"]
    )


def downgrade() -> None:
    op.drop_index("ix_company_profiles_normalized_name", table_name="company_profiles")
    op.drop_table("company_profiles")

    op.drop_index("ix_salary_benchmarks_location_key", table_name="salary_benchmarks")
    op.drop_index("ix_salary_benchmarks_role_family", table_name="salary_benchmarks")
    op.drop_table("salary_benchmarks")

    op.drop_constraint("fk_job_postings_duplicate_of_id", "job_postings", type_="foreignkey")
    op.drop_index("ix_job_postings_duplicate_of_id", table_name="job_postings")
    op.drop_column("job_postings", "source_urls")
    op.drop_column("job_postings", "duplicate_of_id")
    op.drop_column("job_postings", "llm_reasoning")
    op.drop_column("job_postings", "llm_fit_score")
