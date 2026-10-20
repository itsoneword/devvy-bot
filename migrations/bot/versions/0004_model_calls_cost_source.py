"""model_calls.cost_source

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17

Whether a row's cost_usd is the provider's billed figure ('exact') or tokens
x price table ('estimate'); NULL where the harness's own figure is kept
(dv-d2f6). Additive; no rebuild.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_calls", sa.Column("cost_source", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_calls", "cost_source")
