"""message_reactions: owner feature feedback; feedback_feature_cards

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-07

dv-a42c: the owner's after-testing rating of a whole feature (a chain of
linked cards) lives in the same table as the dv-e1a7 turn reactions, told
apart by ``kind``:

- ``kind``: ``turn_reaction`` (every existing row) | ``feature_feedback``;
- ``feature_id``: the chain (``src/pump2/feedback.py``); ``comment``: the raw
  text or voice transcript; ``has_bugs`` / ``bugs_text``; ``tested``
  (1 / 0 = "не тестил" / NULL); ``pros`` / ``cons`` / ``aspects`` (comma list
  of ui, function, speed, other) from the stateless extraction call;
  ``score_source`` (``button`` | ``extracted``);
- ``messages_message_id`` and ``score`` become nullable (a feedback row has
  no turn; a voice-only or skipped answer may have no score).

``feedback_feature_cards``: one row per card of a rated feature, with how
the card was done (``exec_mode`` pump2 / job / chat, ``stages``, ``returns``).

SQLite cannot relax NOT NULL in place: the table is rebuilt and every row
copied (as 0007 did for tool_calls).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_COLS = (
    "id, telegram_chat_id, telegram_message_id, user_id, messages_message_id,"
    " profile_slug, emoji, score, reacted_at"
)


def _create(nullable_new: bool) -> None:
    cols = [
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("telegram_chat_id", sa.INTEGER(), nullable=False),
        sa.Column("telegram_message_id", sa.INTEGER(), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("messages_message_id", sa.INTEGER(), nullable=nullable_new),
        sa.Column("profile_slug", sa.TEXT(), nullable=True),
        sa.Column("emoji", sa.TEXT(), nullable=False),
        sa.Column("score", sa.INTEGER(), nullable=nullable_new),
        sa.Column("reacted_at", sa.TIMESTAMP(), nullable=False),
    ]
    if nullable_new:
        cols += [
            sa.Column(
                "kind",
                sa.TEXT(),
                server_default=sa.text("'turn_reaction'"),
                nullable=False,
            ),
            sa.Column("feature_id", sa.TEXT(), nullable=True),
            sa.Column("comment", sa.TEXT(), nullable=True),
            sa.Column("has_bugs", sa.INTEGER(), nullable=True),
            sa.Column("bugs_text", sa.TEXT(), nullable=True),
            sa.Column("tested", sa.INTEGER(), nullable=True),
            sa.Column("pros", sa.TEXT(), nullable=True),
            sa.Column("cons", sa.TEXT(), nullable=True),
            sa.Column("aspects", sa.TEXT(), nullable=True),
            sa.Column("score_source", sa.TEXT(), nullable=True),
        ]
    op.create_table(
        "message_reactions",
        *cols,
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_chat_id", "telegram_message_id", "user_id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_message_reactions_profile",
        "message_reactions",
        ["profile_slug", "reacted_at"],
    )
    if nullable_new:
        op.create_index(
            "idx_message_reactions_feature", "message_reactions", ["feature_id"]
        )


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_message_reactions_profile")
    op.rename_table("message_reactions", "message_reactions_0010")
    _create(nullable_new=True)
    op.execute(
        f"INSERT INTO message_reactions ({_OLD_COLS}, kind)"
        f" SELECT {_OLD_COLS}, 'turn_reaction' FROM message_reactions_0010"
    )
    op.drop_table("message_reactions_0010")
    op.create_table(
        "feedback_feature_cards",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("feature_id", sa.TEXT(), nullable=False),
        sa.Column("board", sa.TEXT(), nullable=False),
        sa.Column("card_id", sa.TEXT(), nullable=False),
        sa.Column("title", sa.TEXT(), nullable=True),
        sa.Column("merge_sha", sa.TEXT(), nullable=True),
        sa.Column("merged_at", sa.TEXT(), nullable=True),
        sa.Column("exec_mode", sa.TEXT(), nullable=False),
        sa.Column("stages", sa.TEXT(), nullable=True),
        sa.Column("returns", sa.INTEGER(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feature_id", "board", "card_id"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("feedback_feature_cards")
    op.drop_index("idx_message_reactions_feature", table_name="message_reactions")
    op.drop_index("idx_message_reactions_profile", table_name="message_reactions")
    op.rename_table("message_reactions", "message_reactions_0011")
    _create(nullable_new=False)
    op.execute(
        f"INSERT INTO message_reactions ({_OLD_COLS}) SELECT {_OLD_COLS}"
        " FROM message_reactions_0011 WHERE kind = 'turn_reaction'"
    )
    op.drop_table("message_reactions_0011")
