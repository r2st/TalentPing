"""Drop the eight indexes that duplicate a unique constraint, column for column.

Postgres implements a ``UNIQUE`` constraint with a unique btree index. So a
table that carries both ``UniqueConstraint("domain")`` and a separate
``index=True`` on ``domain`` ends up with *two* btrees over the same single
column — one unique, one not. Every insert, update and delete on such a row
maintains both, and both occupy disk and cache. The non-unique one buys nothing:
it covers exactly the same column in the same order, and the unique index is
strictly the more selective of the pair, so the planner can serve from it every
read the redundant index could have served.

Eight tables were in that state. Six got there by declaring an explicit named
``UniqueConstraint`` in ``__table_args__`` *and* ``index=True`` on the column —
two spellings of the same intent that SQLAlchemy dutifully rendered as two
objects. Two (``gmail_accounts.email``, ``autopilot_preferences.user_id``) got
there because the hand-written migration spelled the uniqueness as a constraint
while the model spelled it ``unique=True, index=True``, which renders as a
unique *index* — so the migration created the constraint, then created the plain
index alongside it, and the model and the database have disagreed about the
shape of that uniqueness ever since.

Only exact duplicates are dropped here: same table, same column list, and the
survivor is unique. That is the case with no counter-argument. This schema also
has twelve *prefix*-redundant indexes — a single-column index whose column is
the leading column of a wider index, such as ``ix_recruiters_user_id`` under
``uq_recruiter_user_email(user_id, email)``. Those are usually worth dropping
too, but a narrow index is genuinely cheaper to scan than a wide one, so that is
a judgement call per index and per query rather than a mechanical one. They are
left alone, and ``tests/test_schema_drift.py`` deliberately does not fail on
them.

The models are updated in the same commit to stop declaring the redundant index,
so ``Base.metadata`` and the migrated database agree afterwards — which is what
the new drift test checks.

Also declares ``ix_job_postings_screened_out_at`` on the model. That index has
existed in production since ``c1f7b3a25e94`` and is load-bearing (the autopilot
candidate query filters ``screened_out_at IS NULL`` every run), but no model ever
mentioned it, so it read as an orphan to any comparison. Nothing changes in the
database for it — only the model catches up.

Revision ID: c5a9e63b17d4
Revises: b2d7f4c81a95
Create Date: 2026-08-02
"""
from collections.abc import Sequence

from alembic import op

revision: str = "c5a9e63b17d4"
down_revision: str | None = "b2d7f4c81a95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (index to drop, its table, the column it covered). The column is carried so
# the downgrade can put the index back exactly as it was.
_REDUNDANT: tuple[tuple[str, str, str], ...] = (
    ("ix_ats_boards_normalized_name", "ats_boards", "normalized_name"),
    ("ix_autopilot_preferences_user_id", "autopilot_preferences", "user_id"),
    ("ix_company_profiles_normalized_name", "company_profiles", "normalized_name"),
    ("ix_form_apply_profiles_user_id", "form_apply_profiles", "user_id"),
    ("ix_gmail_accounts_email", "gmail_accounts", "email"),
    ("ix_gmail_watches_gmail_account_id", "gmail_watches", "gmail_account_id"),
    ("ix_linkedin_accounts_user_id", "linkedin_accounts", "user_id"),
    ("ix_recruiter_cache_domain", "recruiter_cache", "domain"),
)


def upgrade() -> None:
    for index, table, _column in _REDUNDANT:
        # ``if_exists`` because two of these were created by a model-driven
        # ``create_all`` in some environments and by an explicit
        # ``op.create_index`` in others; a database that only ever had the
        # unique constraint should not fail the deploy.
        op.drop_index(index, table_name=table, if_exists=True)


def downgrade() -> None:
    for index, table, column in reversed(_REDUNDANT):
        op.create_index(index, table, [column], unique=False, if_not_exists=True)
