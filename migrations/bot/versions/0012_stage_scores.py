"""message_reactions: pump2 stage scores (kind='stage_score')

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-08

dv-3b99 (C7): a review or custom stage report may carry ``## Scores``
(``C1: 8 | evidence``). pump2 stores one row per criterion in the same table
dv-a42c reads, told apart by ``kind='stage_score'``: ``board``, ``card_id``,
``stage``, ``criterion``, ``score`` (1-10), the analyze-set ``card_size`` /
``card_kind``. Plain nullable ADD COLUMNs, no table rebuild.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = ("board", "card_id", "stage", "criterion", "card_size", "card_kind")


def upgrade() -> None:
    for c in _COLS:
        op.add_column("message_reactions", sa.Column(c, sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM message_reactions WHERE kind = 'stage_score'")
    with op.batch_alter_table("message_reactions") as b:
        for c in reversed(_COLS):
            b.drop_column(c)
