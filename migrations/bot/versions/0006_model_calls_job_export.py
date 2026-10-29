"""model_calls.job_name + exported_at

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25

dv-87d8: the Phoenix exporter projects finished ``model_calls`` rows (and their
``tool_calls``) into project "devvy". ``job_name`` names a background job's
trace (a pump stage is ``pump-<task>-<stage>[-pN]``); ``exported_at`` marks a
row as sent. Rows older than Phoenix's 30-day retention are marked at once:
backfilling them would be deleted on arrival.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_calls", sa.Column("job_name", sa.TEXT(), nullable=True))
    op.add_column(
        "model_calls", sa.Column("exported_at", sa.TIMESTAMP(), nullable=True)
    )
    op.create_index("idx_model_calls_exported", "model_calls", ["exported_at", "id"])
    op.execute(
        "UPDATE model_calls SET exported_at = ts "
        "WHERE ts < datetime('now', '-30 days')"
    )


def downgrade() -> None:
    op.drop_index("idx_model_calls_exported", table_name="model_calls")
    op.drop_column("model_calls", "exported_at")
    op.drop_column("model_calls", "job_name")
