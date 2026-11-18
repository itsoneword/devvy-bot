"""message_reactions: judge rows of the design critic (round, variant, ...)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14

dv-925f: pump2 judge rows (``kind='stage_score'``) keep their evidence /
remarks text in ``comment``; the design critic's rows (stage
``design_critic`` / ``design_critic_remark``) also carry the renders
``round`` and ``variant`` and a block remark's ``element`` and
``remark_kind`` (build | data). Plain nullable ADD COLUMNs, no rebuild.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = (
    ("round", sa.INTEGER()),
    ("variant", sa.INTEGER()),
    ("element", sa.TEXT()),
    ("remark_kind", sa.TEXT()),
)


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("message_reactions", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("message_reactions") as b:
        for name, _ in reversed(_COLS):
            b.drop_column(name)
