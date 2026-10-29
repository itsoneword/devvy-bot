"""session attribution: sessions.attr_* by real source (dv-d2a3)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TEXT_COLUMNS = ("attr_group", "attr_key", "attr_detail")


def upgrade() -> None:
    # Additive only: sessions.profile keeps the raw cwd label. The indexer
    # fills these on its next cycle (ATTRIBUTION_VERSION backfill).
    for name in _TEXT_COLUMNS:
        op.add_column("sessions", sa.Column(name, sa.TEXT(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column(
            "attr_archived", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_index(
        "idx_sessions_attr", "sessions", ["attr_group", "attr_key"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_sessions_attr", table_name="sessions")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("attr_archived")
        for name in reversed(_TEXT_COLUMNS):
            batch.drop_column(name)
