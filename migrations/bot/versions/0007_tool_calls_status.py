"""tool_calls: tool / input / status

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25

0005 (the first dv-2260 cut, already applied to the live bot.db) recorded a
call as ``tool_name`` + ``input_short`` + ``ok`` + ``detached``. The trace
reads one ``status`` instead (ok / error / detached / unfinished), ``input``
as JSON, and links rows to ``messages`` / ``model_calls``. SQLite cannot
change columns in place, so the table is rebuilt and every row copied.

``input_short`` was a truncated JSON dump; it is copied as is, so a long old
input stays unparseable (its readers already skip those).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_INDEXES = (
    "idx_tool_calls_model_call",
    "idx_tool_calls_message",
    "idx_tool_calls_session",
    "idx_tool_calls_parent",
    "idx_tool_calls_started",
)


def _drop_indexes() -> None:
    # Index names are global in SQLite: free them before the new table
    # takes them.
    for name in _OLD_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def upgrade() -> None:
    _drop_indexes()
    op.rename_table("tool_calls", "tool_calls_0005")
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("trace_id", sa.TEXT(), nullable=True),
        sa.Column("model_call_id", sa.INTEGER(), nullable=True),
        sa.Column("message_id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=True),
        sa.Column("seq", sa.INTEGER(), nullable=False),
        sa.Column("tool_use_id", sa.TEXT(), nullable=True),
        sa.Column("parent_tool_use_id", sa.TEXT(), nullable=True),
        sa.Column("tool", sa.TEXT(), nullable=False),
        sa.Column("subagent_type", sa.TEXT(), nullable=True),
        sa.Column("input", sa.JSON(), nullable=True),
        sa.Column("status", sa.TEXT(), nullable=False),
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
        sa.ForeignKeyConstraint(["message_id"], ["messages.message_id"]),
        sa.ForeignKeyConstraint(["model_call_id"], ["model_calls.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "tool_use_id"),
        sqlite_autoincrement=True,
    )
    # seq was NULL for the rows 0005 copied from tool_usage; id keeps their
    # order. ok NULL = never paired with a result = unfinished.
    op.execute(
        "INSERT INTO tool_calls (id, trace_id, model_call_id, message_id,"
        " session_id, seq, tool_use_id, parent_tool_use_id, tool, subagent_type,"
        " input, status, error, started_at, duration_ms, result_bytes,"
        " result_preview) "
        "SELECT id, trace_id, model_call_id, message_id, session_id,"
        " COALESCE(seq, id), tool_use_id, parent_tool_use_id, tool_name,"
        " subagent_type, input_short,"
        " CASE WHEN detached THEN 'detached' WHEN ok = 1 THEN 'ok'"
        " WHEN ok = 0 THEN 'error' ELSE 'unfinished' END,"
        " error, started_at, duration_ms, result_bytes, result_preview "
        "FROM tool_calls_0005 ORDER BY id"
    )
    op.drop_table("tool_calls_0005")
    op.create_index("idx_tool_calls_model_call", "tool_calls", ["model_call_id", "seq"])
    op.create_index("idx_tool_calls_message", "tool_calls", ["message_id"])
    op.create_index("idx_tool_calls_parent", "tool_calls", ["parent_tool_use_id"])
    op.create_index("idx_tool_calls_started", "tool_calls", ["started_at"])


def downgrade() -> None:
    _drop_indexes()
    op.rename_table("tool_calls", "tool_calls_0007")
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
        sa.Column(
            "detached", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "tool_use_id"),
        sqlite_autoincrement=True,
    )
    op.execute(
        "INSERT INTO tool_calls (id, trace_id, model_call_id, message_id,"
        " session_id, seq, tool_use_id, parent_tool_use_id, tool_name,"
        " subagent_type, input_short, ok, error, started_at, duration_ms,"
        " result_bytes, result_preview, detached) "
        "SELECT id, trace_id, model_call_id, message_id, session_id, seq,"
        " tool_use_id, parent_tool_use_id, tool, subagent_type,"
        " substr(input, 1, 300),"
        " CASE status WHEN 'ok' THEN 1 WHEN 'error' THEN 0 END,"
        " error, started_at, duration_ms, result_bytes, result_preview,"
        " status = 'detached' "
        "FROM tool_calls_0007 ORDER BY id"
    )
    op.drop_table("tool_calls_0007")
    op.create_index("idx_tool_calls_model_call", "tool_calls", ["model_call_id", "seq"])
    op.create_index("idx_tool_calls_message", "tool_calls", ["message_id"])
    op.create_index("idx_tool_calls_session", "tool_calls", ["session_id"])
    op.create_index("idx_tool_calls_parent", "tool_calls", ["parent_tool_use_id"])
    op.create_index("idx_tool_calls_started", "tool_calls", ["started_at"])
