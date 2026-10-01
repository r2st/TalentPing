"""Recruiter scan runs: count the autoresponders that were refused

Adds ``recruiter_scan_runs.skipped_auto_reply``.

``inbound_scanner`` now refuses a message whose headers declare it automatically
generated (RFC 3834 ``Auto-Submitted`` and the pre-standard headers every
installed Exchange still sends) before it is ever stored, because a reply to an
out-of-office notice is a message sent to a robot that answers it again.

Every other filter in that scanner has a column here — the table's promise is to
be the ``ScanResult`` dict persisted, and ``skipped_total`` sums the buckets, so
a new filter without a column would quietly make "listed = detected + skipped"
stop adding up on the stats page.

Purely additive, defaulted to zero; the downgrade is a clean reversal. Existing
rows read zero, which is the truth about them: the filter did not exist.

Revision ID: b3d7e15c9a24
Revises: e2b6d4f81a37
Create Date: 2026-08-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3d7e15c9a24"
down_revision: str | None = "e2b6d4f81a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recruiter_scan_runs",
        sa.Column(
            "skipped_auto_reply",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("recruiter_scan_runs", "skipped_auto_reply")
