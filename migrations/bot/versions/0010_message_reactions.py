"""message_reactions: Telegram reactions as answer-quality labels

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-06

dv-e1a7: a 👎/👍/❤/🔥 reaction on a bot turn reply, stored against the turn
(``messages_message_id`` via the ``sent_messages`` ledger). One row per
chat + message + user; changing the reaction updates it, removing deletes it.
``score``: 👎 -1, 👍 +1, ❤ +1, 🔥 +2.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_reactions",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("telegram_chat_id", sa.INTEGER(), nullable=False),
        sa.Column("telegram_message_id", sa.INTEGER(), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("messages_message_id", sa.INTEGER(), nullable=False),
        sa.Column("profile_slug", sa.TEXT(), nullable=True),
        sa.Column("emoji", sa.TEXT(), nullable=False),
        sa.Column("score", sa.INTEGER(), nullable=False),
        sa.Column("reacted_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_chat_id", "telegram_message_id", "user_id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_message_reactions_profile",
        "message_reactions",
        ["profile_slug", "reacted_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_message_reactions_profile", table_name="message_reactions")
    op.drop_table("message_reactions")
