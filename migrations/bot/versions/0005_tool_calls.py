"""tool_calls replaces tool_usage

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20

The harness trace (dv-2260): one row per tool call of any model turn, with its
caller (main agent / subagent), outcome, duration and a short result preview.
``tool_usage``'s rows are copied in (no result fields, no trace) and the table
is dropped; its readers read ``tool_calls`` now.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("trace_id", sa.TEXT(), nullable=True),
        sa.Column("model_call_id", sa.INTEGER(), nullable=True),
        sa.Column("message_id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=True),
        sa.Column("seq", sa.INTEGER(), nullable=True),
        sa.Column("tool_use_id", sa.TEXT(), nullable=True),
        sa.Column("parent_tool_use_id", sa.TEXT(), nullable=True),
        sa.Column("tool_name", sa.TEXT(), nullable=False),
        sa.Column("subagent_type", sa.TEXT(), nullable=True),
        sa.Column("input_short", sa.TEXT(), nullable=True),
        sa.Column("ok", sa.INTEGER(), nullable=True),
        sa.Column("error", sa.TEXT(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("duration_ms", sa.INTEGER(), nullable=True),
        sa.Column("result_bytes", sa.INTEGER(), nullable=True),
        sa.Column("result_preview", sa.TEXT(), nullable=True),
        sa.Column("detached", sa.INTEGER(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "tool_use_id"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_tool_calls_model_call", "tool_calls", ["model_call_id", "seq"])
    op.create_index("idx_tool_calls_message", "tool_calls", ["message_id"])
    op.create_index("idx_tool_calls_session", "tool_calls", ["session_id"])
    op.create_index("idx_tool_calls_parent", "tool_calls", ["parent_tool_use_id"])
    op.create_index("idx_tool_calls_started", "tool_calls", ["started_at"])
    op.execute(
        "INSERT INTO tool_calls (session_id, message_id, tool_name, input_short,"
        " ok, error, started_at) "
        "SELECT session_id, message_id, tool_name, substr(tool_input, 1, 300),"
        " CASE WHEN success THEN 1 ELSE 0 END, error_message, timestamp "
        "FROM tool_usage ORDER BY id"
    )
    op.drop_table("tool_usage")


def downgrade() -> None:
    raise NotImplementedError("tool_usage is gone for good (dv-2260)")
