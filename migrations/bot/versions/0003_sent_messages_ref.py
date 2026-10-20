"""sent_messages.ref

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17

Nullable tag on a ledgered message ("job:<id>"), so a reply to a job
notification can be mapped back to the job (dv-c41e). Additive; no rebuild.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sent_messages", sa.Column("ref", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("sent_messages", "ref")
