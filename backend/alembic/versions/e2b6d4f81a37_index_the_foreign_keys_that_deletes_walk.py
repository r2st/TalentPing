"""Index the foreign keys that deletes walk

Every ``ON DELETE`` rule is a query the database runs on the *child* table, and
it runs it once per parent row. When the child's foreign-key column has no
index that query is a sequential scan, so removing one parent row costs a walk
of the whole child table — and the cost is a function of how much data the
*deployment* holds, not how much the deleting user does.

Six columns were missing that index. Measured on Postgres 17 against a seeded
database (80,000 emails, 40,000 follow-ups, 40,000 bounce records), deleting
200 emails::

    Trigger for constraint follow_ups_email_id_fkey:            279.6 ms
    Trigger for constraint email_bounces_email_id_fkey:         284.7 ms
    Trigger for constraint recruiter_emails_reply_email_id_fkey:  1.5 ms
    Trigger for constraint email_events_email_id_fkey:            0.6 ms
    Trigger for constraint email_attachments_email_id_fkey:       0.8 ms
    Execution Time: 567 ms

The three fast triggers are the ones whose columns were already indexed. After
this migration the same delete is::

    Trigger for constraint follow_ups_email_id_fkey:              1.3 ms
    Trigger for constraint email_bounces_email_id_fkey:           1.3 ms
    Execution Time: 4.1 ms

The path that pays this today is account erasure. ``DELETE FROM users``
cascades to ``applications`` to ``email_threads`` to ``emails``, and every one
of those emails fires both unindexed triggers — inside one transaction, in a
request, holding a write lock on ``emails`` throughout. It is quadratic: ten
times the mail costs a hundred times the delete. Deleting a resume is the same
shape against ``job_searches``, ``form_applications`` and
``autopilot_preferences``, and that one is a routine button in the UI rather
than a once-per-account event.

None of this is visible in the test suite, because SQLite answers a foreign-key
check by scanning either way and the fixtures hold four rows.

Two redundant indexes go with them, both the same shape: a plain ``(user_id)``
tree standing beside a composite that already leads with ``user_id``. Postgres
uses a leading prefix of a composite index for exactly the lookups the narrow
one served, so the narrow one answers no query the wide one cannot and is
maintained on every insert regardless. ``ix_notifications_user_id`` was also
what made ``tests/test_schema_drift.py::test_no_two_indexes_cover_the_same_columns``
fail on ``main``, though not for the reason the failure gave: it named
``ix_notifications_user_unread`` as the duplicate, which is partial and
therefore holds a different set of rows. That check now keys on the predicate
as well as the columns.

``ix_applications_user_id_id`` is a different repair on the read side. Nothing
but ``applications`` connects a user to their mail — ``email_threads`` has no
``user_id`` — so every inbox request begins by collecting this user's
application ids. Under the plain ``(user_id)`` index that collection is an
index scan plus a heap fetch per row (1,000 applications, 209 buffers) because
the index does not carry ``id``. Appending it makes the lookup index-only: 6
buffers, no heap fetches. ``(user_id)`` is a prefix of the new index, so every
query the old one served is still served, and keeping both would only add write
cost.

Index creation takes a write lock for its duration. ``emails`` and ``resumes``
are the parents here, but the locks are taken on the *children* —
``follow_ups``, ``email_bounces``, ``job_searches``, ``form_applications``,
``autopilot_preferences`` — plus ``applications``. If any of those grows large
enough for the lock to be felt, these become ``CREATE INDEX CONCURRENTLY``
outside a transaction.

**Deploying this needs ``--allow-destructive``.** The two ``drop_index`` calls
below trip ``scripts/deploy.sh``'s gate, which classifies dropping an index as
risky on the grounds that queries relying on it degrade rather than fail — so
the damage arrives as latency, not as an error anybody is paged for. That
reasoning is right in general and does not apply here: both dropped indexes are
redundant prefixes of composites created in the same migration, so nothing
loses cover. Read the gate's output, then re-run with the flag.

Revision ID: e2b6d4f81a37
Revises: c4f8a1d09b27
Create Date: 2026-08-26
"""
from collections.abc import Sequence

from alembic import op

revision: str = "e2b6d4f81a37"
down_revision: str | None = "c4f8a1d09b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ``(index name, table, column)`` for every foreign key that had no index.
#: All six are ``ON DELETE SET NULL``; the names match what SQLAlchemy's
#: ``index=True`` generates, so the models and the migration agree and
#: ``tests/test_schema_drift.py`` stays quiet.
_FK_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_autopilot_preferences_campaign_id", "autopilot_preferences", "campaign_id"),
    ("ix_autopilot_preferences_resume_id", "autopilot_preferences", "resume_id"),
    ("ix_email_bounces_email_id", "email_bounces", "email_id"),
    ("ix_follow_ups_email_id", "follow_ups", "email_id"),
    ("ix_form_applications_resume_id", "form_applications", "resume_id"),
    ("ix_job_searches_resume_id", "job_searches", "resume_id"),
)


def upgrade() -> None:
    for name, table, column in _FK_INDEXES:
        op.create_index(name, table, [column], unique=False)

    op.create_index(
        "ix_applications_user_id_id", "applications", ["user_id", "id"], unique=False
    )
    # Redundant now: its column is the leading column of the index above.
    op.drop_index("ix_applications_user_id", table_name="applications")

    # Redundant against ``ix_notifications_user_created`` (user_id, created_at),
    # which has always been there. Not against ``ix_notifications_user_unread``,
    # which is partial.
    op.drop_index("ix_notifications_user_id", table_name="notifications")


def downgrade() -> None:
    op.create_index(
        "ix_notifications_user_id", "notifications", ["user_id"], unique=False
    )
    op.create_index("ix_applications_user_id", "applications", ["user_id"], unique=False)
    op.drop_index("ix_applications_user_id_id", table_name="applications")

    for name, table, _column in reversed(_FK_INDEXES):
        op.drop_index(name, table_name=table)
