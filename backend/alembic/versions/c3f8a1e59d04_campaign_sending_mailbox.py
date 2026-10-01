"""campaigns.gmail_account_id: which mailbox a campaign argues from

One additive nullable column with an index and a ``SET NULL`` foreign key.

``e6b2d9a41f70`` gave threads and profiles a mailbox of their own, which covers
autopilot: a posting matches a profile, and the profile names the address. It
left the campaigns a user builds by hand resolving to ``primary_gmail``
unconditionally, because a campaign has no profile to ask — there is no
``campaigns.profile_id``. This column is that missing statement, and it is
checked before the profile because it is the more specific one.

Nullable and unbackfilled on purpose. Null does not mean "no mailbox", it means
"decide the usual way" — the resolver already falls back to the profile and then
the primary, and every campaign written before this column existed was sent from
the primary anyway. Writing that account id into every historical row would
freeze a past default as a present instruction: the user would move their sending
identity and find their old campaigns still pinned to the old address.

``SET NULL`` for the same reason it is used on ``email_threads`` — disconnecting
a mailbox must not delete the campaigns sent from it and cascade away every
application underneath them.

Revision ID: c3f8a1e59d04
Revises: b1c4e70d3a92
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c3f8a1e59d04"
down_revision: str | None = "b1c4e70d3a92"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns", sa.Column("gmail_account_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_campaigns_gmail_account_id", "campaigns", ["gmail_account_id"]
    )
    # SQLite (dev, tests) cannot ADD CONSTRAINT, so the FK goes on through
    # batch_alter_table, which rebuilds the table. Postgres (prod) takes the
    # plain path.
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("campaigns") as batch:
            batch.create_foreign_key(
                "fk_campaigns_gmail_account_id",
                "gmail_accounts",
                ["gmail_account_id"],
                ["id"],
                ondelete="SET NULL",
            )
        return
    op.create_foreign_key(
        "fk_campaigns_gmail_account_id",
        "campaigns",
        "gmail_accounts",
        ["gmail_account_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("campaigns") as batch:
            batch.drop_constraint("fk_campaigns_gmail_account_id", type_="foreignkey")
            batch.drop_index("ix_campaigns_gmail_account_id")
            batch.drop_column("gmail_account_id")
        return
    op.drop_constraint(
        "fk_campaigns_gmail_account_id", "campaigns", type_="foreignkey"
    )
    op.drop_index("ix_campaigns_gmail_account_id", table_name="campaigns")
    op.drop_column("campaigns", "gmail_account_id")
