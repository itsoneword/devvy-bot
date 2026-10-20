"""context-size accounting: tool input/result sizes + context_items (dv-aeca)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TOOL_COLUMNS = ("input_chars", "result_chars", "result_images", "result_tokens_est")


def upgrade() -> None:
    for name in _TOOL_COLUMNS:
        op.add_column("tool_calls", sa.Column(name, sa.INTEGER(), nullable=True))
    op.create_table(
        "context_items",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("agent_id", sa.TEXT(), nullable=True),
        sa.Column("file_id", sa.INTEGER(), nullable=False),
        sa.Column("entry_offset", sa.INTEGER(), nullable=False),
        sa.Column("seq", sa.INTEGER(), nullable=False),
        sa.Column("ts", sa.TEXT(), nullable=False),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("name", sa.TEXT(), nullable=False),
        sa.Column("chars", sa.INTEGER(), nullable=False),
        sa.Column("request_id", sa.TEXT(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id", "entry_offset", "seq"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_context_items_session",
        "context_items",
        ["session_id", "agent_id", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_context_items_session", table_name="context_items")
    op.drop_table("context_items")
    with op.batch_alter_table("tool_calls") as batch:
        for name in reversed(_TOOL_COLUMNS):
            batch.drop_column(name)
