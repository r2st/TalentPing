"""Two indexes for the queries that run whether or not anyone is reading

Both of these back reads that are already correct and already bounded. What
neither had was an index shaped like the query, so each one paid a cost that
grew with the age of the account rather than with anything the user did.

``ix_notifications_user_unread``
    ``GET /notifications/unread`` is polled by the app shell every thirty
    seconds, on every route, for the whole life of every session, and returns
    one integer. Its filter is ``user_id = ? AND read_at IS NULL AND
    dismissed_at IS NULL``. The only index that could serve it was
    ``ix_notifications_user_created``, which knows nothing about either null
    test — so the count walked every notification the user had ever been sent.
    That set never shrinks: a dismissed row stays on file to keep its
    ``dedupe_key`` claimed. Partial, so it holds only the rows the poll asks
    about (on a settled account, almost none) and the rest cost nothing to keep
    it current.

``ix_recruiter_emails_user_received``
    Every page of the Recruiter Inbox orders by ``coalesce(received_at,
    created_at) DESC, id DESC`` — when the recruiter sent it, falling back to
    when we noticed, with the id breaking ties so two pages of a same-second
    batch cannot repeat a row and drop another. ``ix_recruiter_emails_user_status``
    locates the user's rows and stops there; the sort was then run over all of
    them to return twenty. An expression index because the sort key is an
    expression: a plain ``(user_id, received_at)`` cannot match it, and
    ``received_at`` is null on exactly the rows the coalesce exists for.

Index creation takes a write lock on the table for its duration. Both tables are
small enough for that to be unnoticeable at current sizes; if that stops being
true, these become ``CREATE INDEX CONCURRENTLY`` outside a transaction.

Revision ID: b3d7c1f92e64
Revises: f4a7b2c19e05
Create Date: 2026-08-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3d7c1f92e64"
down_revision: str | None = "f4a7b2c19e05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL AND dismissed_at IS NULL"),
        sqlite_where=sa.text("read_at IS NULL AND dismissed_at IS NULL"),
    )
    op.create_index(
        "ix_recruiter_emails_user_received",
        "recruiter_emails",
        [
            sa.text("user_id"),
            sa.text("coalesce(received_at, created_at) DESC"),
            sa.text("id DESC"),
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_recruiter_emails_user_received", table_name="recruiter_emails")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
