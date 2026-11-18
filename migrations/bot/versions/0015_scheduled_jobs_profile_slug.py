"""scheduled_jobs.profile_slug

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-12

dv-f6c4: the profile whose turn created a scheduled job, so its reply is
delivered as that profile's message (its topic, else the DM with its badge).
Nullable; existing rows keep NULL and derive the profile from
working_directory. Additive; no rebuild.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scheduled_jobs", sa.Column("profile_slug", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_jobs", "profile_slug")
