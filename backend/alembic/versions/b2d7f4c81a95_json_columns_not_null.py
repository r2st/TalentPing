"""Make the JSON list/dict columns NOT NULL, as the models have always claimed.

Every column touched here is declared on its model with a non-optional
annotation — ``Mapped[list[str]]``, not ``Mapped[list[str] | None]`` — plus a
``default=list`` (or ``default=dict``). SQLAlchemy reads that annotation and
emits ``NOT NULL``, so ``Base.metadata.create_all`` builds them NOT NULL, and
the test suite has therefore *never* seen one of these columns hold NULL.

The migrations that actually built production said something else. Each of
these columns arrived through an ``op.add_column(..., nullable=True)``, which is
the only safe way to add a column to a populated table — and none of them was
ever tightened afterwards. So production has spent the whole life of the schema
able to store NULL in 38 columns that every reader is typed to receive a list or
a dict from. Rows that predate each ``add_column`` hold NULL *right now*: for the
32 columns with no server default, Postgres filled the existing rows with NULL,
and nothing has rewritten them since.

That gap is invisible from inside the suite by construction, and the readers are
not uniformly defensive about it. Some are — ``ghost_job`` says
``len(posting.source_urls or [])`` — but ``inbound_reply`` slices
``targeting.skills[:12]`` and ``ai_composer`` forwards ``resume.skills``
untouched. On a NULL those raise ``TypeError`` deep inside a Celery task, for
one user, on one old row.

So: backfill, then tighten, per column. The fill value is the model's own
Python-side default, read off ``Base.metadata`` rather than guessed — 35 of the
38 default to ``list`` and three (``fit_scores.notes``,
``form_apply_profiles.custom_answers``, ``recruiter_emails.extracted``) default
to ``dict``.

Deliberately **not** adding a server default to the columns that lack one. The
models do not declare one, and adding it here would open a fresh drift in the
direction this migration exists to close. Every write to these tables goes
through the ORM, which supplies the default.

``ALTER TABLE ... SET NOT NULL`` takes ACCESS EXCLUSIVE and scans the table to
verify. At this schema's size that is milliseconds; the UPDATE that precedes it
is the more expensive half and is bounded by the number of NULL rows, which for
most of these columns is zero.

Revision ID: b2d7f4c81a95
Revises: a7f3c9d21e64
Create Date: 2026-08-02
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2d7f4c81a95"
down_revision: str | None = "a7f3c9d21e64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, column, empty-value factory). The factory *is* the model's own
# ``default=`` — ``list`` or ``dict`` — rather than a hand-copied ``"[]"``, so
# the fill value cannot drift from what the ORM would have written. Kept as data
# so the downgrade can walk the same list backwards and so
# ``tests/test_schema_drift.py`` can assert the set is exactly the set of
# columns whose model default this migration is entitled to write.
_COLUMNS: tuple[tuple[str, str, type], ...] = (
    ("autopilot_preferences", "target_roles", list),
    ("autopilot_preferences", "target_industries", list),
    ("autopilot_preferences", "locations", list),
    ("campaigns", "target_companies", list),
    ("company_profiles", "tech_stack", list),
    ("company_profiles", "news", list),
    ("cover_letters", "company_research", list),
    ("cover_letters", "highlights", list),
    ("cover_letters", "missing_keywords", list),
    ("fit_scores", "matched_skills", list),
    ("fit_scores", "missing_skills", list),
    ("fit_scores", "notes", dict),
    ("form_applications", "filled_fields", list),
    ("form_applications", "answers", list),
    ("form_applications", "steps", list),
    ("form_applications", "unanswered", list),
    ("form_apply_profiles", "custom_answers", dict),
    ("job_postings", "source_urls", list),
    ("job_postings", "ghost_reasons", list),
    ("job_searches", "roles", list),
    ("job_searches", "keywords", list),
    ("profiles", "target_roles", list),
    ("profiles", "target_industries", list),
    ("profiles", "skills", list),
    ("profiles", "location_preferences", list),
    ("recruiter_cache", "emails", list),
    ("recruiter_cache", "contacts", list),
    ("recruiter_emails", "extracted", dict),
    ("resumes", "skills", list),
    ("resumes", "target_roles", list),
    ("resumes", "target_industries", list),
    ("resumes", "experience", list),
    ("resumes", "education", list),
    ("resumes", "links", list),
    ("tailored_resumes", "ordered_skills", list),
    ("tailored_resumes", "highlighted_experience", list),
    ("tailored_resumes", "matched_keywords", list),
    ("tailored_resumes", "missing_keywords", list),
)


def upgrade() -> None:
    for table, column, empty in _COLUMNS:
        # Backfill first. Without this the SET NOT NULL below fails outright on
        # any table that has ever held a row from before the column existed —
        # which is the whole reason this migration is necessary.
        #
        # The parameter is bound as `sa.JSON()` rather than as a string: the
        # column is `json`, and Postgres will not implicitly coerce a `varchar`
        # into it ("You will need to rewrite or cast the expression"). Binding
        # the Python value through the JSON type lets the driver do the cast,
        # and keeps the statement dialect-agnostic.
        op.execute(
            sa.text(
                f'UPDATE {table} SET "{column}" = :empty WHERE "{column}" IS NULL'
            ).bindparams(sa.bindparam("empty", value=empty(), type_=sa.JSON()))
        )
        op.alter_column(table, column, existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    # Widening back to NULL-able always succeeds and never rewrites the table.
    # The backfilled ``[]`` values are left in place: they are indistinguishable
    # from what the application would have written anyway, and there is no
    # record of which rows were NULL before.
    for table, column, _empty in reversed(_COLUMNS):
        op.alter_column(table, column, existing_type=sa.JSON(), nullable=True)
