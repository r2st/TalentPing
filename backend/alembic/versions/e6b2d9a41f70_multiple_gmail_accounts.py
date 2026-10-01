"""multiple gmail accounts: per-thread and per-profile mailbox, per-mailbox scan

Three additive nullable columns, a backfill, and one pre-existing data-loss fix.

The columns are what let the product stop assuming ``user.primary_gmail``:
``email_threads.gmail_account_id`` says which mailbox a conversation lives in
(a Gmail thread id means nothing outside the mailbox holding it), and
``profiles.gmail_account_id`` says which address new outreach for that profile
argues from.

The FK change on ``recruiter_emails`` is independent of the feature and was
always wrong: ``ondelete="CASCADE"`` on a nullable column meant disconnecting a
mailbox deleted every inbound recruiter message ever detected in it. Its sibling
``recruiter_scan_runs.gmail_account_id`` has been ``SET NULL`` for exactly this
reason. It is fixed here because multi-mailbox turns "remove a mailbox" from a
rare, terminal act into a routine one.

Revision ID: e6b2d9a41f70
Revises: d4a8f2e61c93
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e6b2d9a41f70"
down_revision = "d4a8f2e61c93"
branch_labels = None
depends_on = None

# Postgres named this itself at table-creation time; SQLite (dev, tests) has no
# name for it at all, which is why the two dialects are handled apart below.
_PG_RECRUITER_FK = "recruiter_emails_gmail_account_id_fkey"
_FK_NAME = "fk_recruiter_emails_gmail_account_id"


def upgrade() -> None:
    op.add_column(
        "email_threads", sa.Column("gmail_account_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_email_threads_gmail_account_id", "email_threads", ["gmail_account_id"]
    )
    op.create_foreign_key(
        "fk_email_threads_gmail_account_id",
        "email_threads",
        "gmail_accounts",
        ["gmail_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "profiles", sa.Column("gmail_account_id", sa.Integer(), nullable=True)
    )
    op.create_index("ix_profiles_gmail_account_id", "profiles", ["gmail_account_id"])
    op.create_foreign_key(
        "fk_profiles_gmail_account_id",
        "profiles",
        "gmail_accounts",
        ["gmail_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Per-mailbox inbound scan bookkeeping. The debounce used to be keyed on
    # RecruiterReplyPreference, which is one row per user — with several
    # mailboxes the first one scanned in a cycle suppressed all the others.
    op.add_column(
        "gmail_accounts",
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "gmail_accounts",
        sa.Column(
            "detected_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )

    # Every existing thread belongs to whatever mailbox was primary for its
    # owner. With one mailbox per user today this is exact, not a guess — and a
    # user with none simply leaves the column null, which resolves to "no Gmail
    # connected" exactly as it did before this column existed.
    op.execute(
        """
        UPDATE email_threads SET gmail_account_id = (
            SELECT ga.id FROM gmail_accounts ga
            JOIN applications a ON a.user_id = ga.user_id
            WHERE a.id = email_threads.application_id
              AND ga.status = 'connected'
            ORDER BY ga.is_primary DESC, ga.id
            LIMIT 1
        )
        """
    )

    # Seed the per-mailbox scan clock from the per-user one so the first beat
    # tick after deploy does not re-read every mailbox at once.
    op.execute(
        """
        UPDATE gmail_accounts SET last_scan_at = (
            SELECT p.last_scan_at FROM recruiter_reply_preferences p
            WHERE p.user_id = gmail_accounts.user_id
        )
        """
    )

    _repoint_recruiter_email_fk(ondelete="SET NULL")


def downgrade() -> None:
    _repoint_recruiter_email_fk(ondelete="CASCADE")

    op.drop_column("gmail_accounts", "detected_count")
    op.drop_column("gmail_accounts", "last_scan_at")

    op.drop_constraint("fk_profiles_gmail_account_id", "profiles", type_="foreignkey")
    op.drop_index("ix_profiles_gmail_account_id", table_name="profiles")
    op.drop_column("profiles", "gmail_account_id")

    op.drop_constraint(
        "fk_email_threads_gmail_account_id", "email_threads", type_="foreignkey"
    )
    op.drop_index("ix_email_threads_gmail_account_id", table_name="email_threads")
    op.drop_column("email_threads", "gmail_account_id")


def _repoint_recruiter_email_fk(*, ondelete: str) -> None:
    """Rewrite recruiter_emails.gmail_account_id's delete rule.

    SQLite cannot ALTER a constraint, so it goes through ``batch_alter_table``,
    which rebuilds the table. There the original FK is anonymous — there is no
    name to drop — so the rebuild is driven by the naming convention alone and
    a missing constraint is not an error.
    """
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("recruiter_emails") as batch:
            batch.create_foreign_key(
                _FK_NAME,
                "gmail_accounts",
                ["gmail_account_id"],
                ["id"],
                ondelete=ondelete,
            )
        return

    # Both names are module constants, not input: the anonymous name Postgres
    # gave the original FK, and the one the naming convention produces.
    existing = bind.exec_driver_sql(
        f"""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'recruiter_emails'::regclass
          AND contype = 'f'
          AND conname IN ('{_PG_RECRUITER_FK}', '{_FK_NAME}')
        """
    ).scalar()
    if existing:
        op.drop_constraint(existing, "recruiter_emails", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        "recruiter_emails",
        "gmail_accounts",
        ["gmail_account_id"],
        ["id"],
        ondelete=ondelete,
    )
