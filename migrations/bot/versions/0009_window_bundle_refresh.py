"""window_bundles: refresh on pinned/core change; sessions.bundle_sha256

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05

dv-ef59 amends dv-5e2a: a pinned or core fact added, corrected or forgotten
mid-window re-freezes that window's bundle on the next turn (general facts
still wait for the roll).

- ``window_bundles.family_agent_id``: the family scope (``f{id}:{slug}``)
  whose facts the bundle includes, so a family-scope write finds every
  member's bundle. NULL = no family facts.
- ``window_bundles.stale_at``: set by the memory writer; a stale row is a
  miss on the next lookup and the re-materialization replaces it in place.
- ``window_bundles.revision``: 0 for the first text of a window, +1 per
  refresh (the sha audit of dv-5e2a can tell the two apart).
- ``sessions.bundle_sha256``: the bundle sha a Codex thread last received,
  so a resumed thread gets the changed standing facts exactly once.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Plain ADD / DROP COLUMN (SQLite >= 3.35), not batch_alter_table: a batch
# rebuild of ``sessions`` trips over the ``user_stats`` view that reads it.


def upgrade() -> None:
    op.add_column("window_bundles", sa.Column("family_agent_id", sa.TEXT()))
    op.add_column("window_bundles", sa.Column("stale_at", sa.TIMESTAMP()))
    op.add_column(
        "window_bundles",
        sa.Column(
            "revision", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column("sessions", sa.Column("bundle_sha256", sa.TEXT()))


def downgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN bundle_sha256")
    op.execute("ALTER TABLE window_bundles DROP COLUMN revision")
    op.execute("ALTER TABLE window_bundles DROP COLUMN stale_at")
    op.execute("ALTER TABLE window_bundles DROP COLUMN family_agent_id")
