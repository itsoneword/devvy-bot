"""quality_daily verification-effort counters (dv-9f41)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    "verification_calls",
    "verified_turns",
    "deep_research_turns",
    "program_turns",
    "program_turns_unchecked",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "quality_daily",
            sa.Column(
                name, sa.INTEGER(), server_default=sa.text("0"), nullable=False
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("quality_daily") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
