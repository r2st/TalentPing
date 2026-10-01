"""One autopilot cycle per user at a time

Adds ``autopilot_preferences.running_since``, the lease a cycle holds while it
runs.

Three dispatchers aim one cycle at the same user: the hourly
``run_all_autopilots`` beat sweep, switching autopilot on, and
``POST /autopilot/run-now`` — which a user can press twice, or press while the
beat tick's cycle is still crawling. Nothing stopped two from running together.

They do not merely duplicate work. ``run_user_autopilot`` reads its budget once
— ``send_headroom`` and ``send_policy.remaining_allowance`` — and then applies to
that many postings; neither run has committed anything when the other reads, so
both see a full budget and each spends it. The daily cap and the unreviewed
auto-send allowance are numbers the candidate chose themselves, and two
overlapping cycles double both.

Nullable with no default: NULL means "no cycle running", which is the correct
state for every existing row, including any row belonging to a user whose cycle
was in flight when this migration ran. The worst case there is one cycle that
outlives the deploy and is not covered by a lease it started before the column
existed — the same exposure the code had before, for one run.

A timestamp rather than a boolean so the lease can expire: a worker killed
mid-cycle (SIGKILL, OOM) never reaches the ``finally`` that clears it, and a
boolean would strand that user's autopilot permanently. See
``auto_apply_service._CYCLE_LEASE_SECONDS``.

Revision ID: b7d3e8f4c210
Revises: a4e9c2b71f83
Create Date: 2026-08-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d3e8f4c210"
down_revision: str | None = "a4e9c2b71f83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "autopilot_preferences",
        sa.Column("running_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("autopilot_preferences", "running_since")
