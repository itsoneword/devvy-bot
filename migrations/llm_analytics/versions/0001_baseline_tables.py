"""baseline tables

Revision ID: 0001
Revises:
Create Date: 2026-10-10

Review before committing (docs/database-migrations.md):
- Column order / declared type names must match the model exactly.
- A batch_alter_table on a table with FTS5 sync triggers (bot: messages,
  memory_facts; channel_intel: places, place_images, place_references)
  rebuilds it and DROPS its triggers -- recreate them in this revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "answer_scores",
        sa.Column("message_id", sa.INTEGER(), nullable=False),
        sa.Column("profile", sa.TEXT(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column("ts", sa.TEXT(), nullable=True),
        sa.Column("judge_model", sa.TEXT(), nullable=False),
        sa.Column("rubric_version", sa.INTEGER(), nullable=False),
        sa.Column("scores_json", sa.TEXT(), nullable=False),
        sa.Column("total", sa.REAL(), nullable=False),
        sa.Column("rationale", sa.TEXT(), nullable=True),
        sa.Column("flags", sa.TEXT(), nullable=True),
        sa.Column("scored_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("message_id", "judge_model", "rubric_version"),
    )
    op.create_index(
        "idx_answer_scores_profile_ts", "answer_scores", ["profile", "ts"], unique=False
    )

    op.create_table(
        "api_calls",
        sa.Column("request_id", sa.TEXT(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("agent_id", sa.TEXT(), nullable=True),
        sa.Column("ts", sa.TEXT(), nullable=False),
        sa.Column("model", sa.TEXT(), nullable=False),
        sa.Column("input_tokens", sa.INTEGER(), nullable=False),
        sa.Column("output_tokens", sa.INTEGER(), nullable=False),
        sa.Column("cache_read_tokens", sa.INTEGER(), nullable=False),
        sa.Column("cache_creation_tokens", sa.INTEGER(), nullable=False),
        sa.Column(
            "cache_5m_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cache_1h_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("est_cost_usd", sa.REAL(), nullable=False),
        sa.Column("pricing_rule", sa.TEXT(), nullable=False),
        sa.Column("entry_uuid", sa.TEXT(), nullable=True),
        sa.Column("file_id", sa.INTEGER(), nullable=False),
        sa.Column("entry_offset", sa.INTEGER(), nullable=False),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "idx_calls_session", "api_calls", ["session_id", "agent_id", "ts"], unique=False
    )
    op.create_index("idx_calls_ts", "api_calls", ["ts"], unique=False)

    op.create_table(
        "bot_messages",
        sa.Column("message_id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("ts", sa.TEXT(), nullable=False),
        sa.Column("cost", sa.REAL(), nullable=True),
        sa.Column("duration_ms", sa.INTEGER(), nullable=True),
        sa.Column("window_id", sa.INTEGER(), nullable=True),
        sa.Column("prompt_chars", sa.INTEGER(), nullable=True),
        sa.Column("response_chars", sa.INTEGER(), nullable=True),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index(
        "idx_bot_messages_chain", "bot_messages", ["user_id", "window_id"], unique=False
    )
    op.create_index(
        "idx_bot_messages_session", "bot_messages", ["session_id"], unique=False
    )

    op.create_table(
        "bot_sessions",
        sa.Column("session_id", sa.TEXT(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("project_path", sa.TEXT(), nullable=False),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("last_used", sa.TEXT(), nullable=True),
        sa.Column("total_cost", sa.REAL(), nullable=True),
        sa.Column("total_turns", sa.INTEGER(), nullable=True),
        sa.Column("message_count", sa.INTEGER(), nullable=True),
        sa.Column("is_active", sa.INTEGER(), nullable=True),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "bot_windows",
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("working_dir", sa.TEXT(), nullable=False),
        sa.Column("window_id", sa.INTEGER(), nullable=False),
        sa.Column("active_backend", sa.TEXT(), nullable=True),
        sa.Column("cumulative_tokens", sa.INTEGER(), nullable=True),
        sa.Column("last_eval_checkpoint", sa.INTEGER(), nullable=True),
        sa.Column("last_checkpoint_message_id", sa.INTEGER(), nullable=True),
        sa.Column("updated_at", sa.TEXT(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", "working_dir"),
    )
    op.create_table(
        "derived_events",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("agent_id", sa.TEXT(), nullable=True),
        sa.Column("ts", sa.TEXT(), nullable=False),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("detail", sa.TEXT(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('cache_expiry','partial_cache_hit','context_cliff')"
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_events_session",
        "derived_events",
        ["session_id", "agent_id", "ts"],
        unique=False,
    )

    op.create_table(
        "files",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("path", sa.TEXT(), nullable=False),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("agent_id", sa.TEXT(), nullable=True),
        sa.Column(
            "byte_offset", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("mtime", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column("size", sa.INTEGER(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_scanned_at", sa.TEXT(), nullable=True),
        sa.Column("missing_since", sa.TEXT(), nullable=True),
        sa.CheckConstraint("kind IN ('session','subagent')"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "meta",
        sa.Column("key", sa.TEXT(), nullable=True),
        sa.Column("value", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "quality_daily",
        sa.Column("date", sa.TEXT(), nullable=False),
        sa.Column("profile", sa.TEXT(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column(
            "pinned_facts", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "log_calls", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "claimed_not_logged",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "complaint_hits", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "user_messages", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "recipe_turns", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "recipe_turns_unsearched",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_index("idx_quality_daily_date", "quality_daily", ["date"], unique=False)

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.TEXT(), nullable=True),
        sa.Column("cwd", sa.TEXT(), nullable=True),
        sa.Column("profile", sa.TEXT(), nullable=True),
        sa.Column("entrypoint", sa.TEXT(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column("git_branch", sa.TEXT(), nullable=True),
        sa.Column("cli_version", sa.TEXT(), nullable=True),
        sa.Column("first_ts", sa.TEXT(), nullable=True),
        sa.Column("last_ts", sa.TEXT(), nullable=True),
        sa.Column(
            "api_call_count", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "input_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "output_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cache_read_tokens",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cache_creation_tokens",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "est_cost_usd", sa.REAL(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "tool_call_count", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "subagent_count", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "turn_count", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "in_bot_db", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("first_call_context", sa.INTEGER(), nullable=True),
        sa.Column("bot_formula_tokens", sa.INTEGER(), nullable=True),
        sa.Column(
            "seeded_start", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "subagent_runs",
        sa.Column("agent_id", sa.TEXT(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("spawn_tool_use_id", sa.TEXT(), nullable=True),
        sa.Column("description", sa.TEXT(), nullable=True),
        sa.Column("subagent_type", sa.TEXT(), nullable=True),
        sa.Column("model_override", sa.TEXT(), nullable=True),
        sa.Column("started_ts", sa.TEXT(), nullable=True),
        sa.Column("ended_ts", sa.TEXT(), nullable=True),
        sa.Column(
            "api_call_count", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "input_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "output_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cache_read_tokens",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cache_creation_tokens",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "est_cost_usd", sa.REAL(), server_default=sa.text("0"), nullable=False
        ),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "tool_calls",
        sa.Column("tool_use_id", sa.TEXT(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("agent_id", sa.TEXT(), nullable=True),
        sa.Column("name", sa.TEXT(), nullable=False),
        sa.Column("ts", sa.TEXT(), nullable=False),
        sa.Column("request_id", sa.TEXT(), nullable=True),
        sa.Column("assistant_uuid", sa.TEXT(), nullable=True),
        sa.Column("result_ts", sa.TEXT(), nullable=True),
        sa.Column("duration_ms", sa.INTEGER(), nullable=True),
        sa.Column("is_error", sa.INTEGER(), nullable=True),
        sa.Column("input_summary", sa.TEXT(), nullable=True),
        sa.PrimaryKeyConstraint("tool_use_id"),
    )
    op.create_index(
        "idx_tools_session",
        "tool_calls",
        ["session_id", "agent_id", "ts"],
        unique=False,
    )

    op.create_table(
        "turns",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("ts", sa.TEXT(), nullable=False),
        sa.Column("source", sa.TEXT(), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column("prompt_excerpt", sa.TEXT(), nullable=True),
        sa.Column("duration_ms", sa.INTEGER(), nullable=True),
        sa.Column("cost", sa.REAL(), nullable=True),
        sa.CheckConstraint("source IN ('bot','transcript')"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "ts", "source"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("turns")
    op.drop_index("idx_tools_session", table_name="tool_calls")

    op.drop_table("tool_calls")
    op.drop_table("subagent_runs")
    op.drop_table("sessions")
    op.drop_index("idx_quality_daily_date", table_name="quality_daily")

    op.drop_table("quality_daily")
    op.drop_table("meta")
    op.drop_table("files")
    op.drop_index("idx_events_session", table_name="derived_events")

    op.drop_table("derived_events")
    op.drop_table("bot_windows")
    op.drop_table("bot_sessions")
    op.drop_index("idx_bot_messages_session", table_name="bot_messages")
    op.drop_index("idx_bot_messages_chain", table_name="bot_messages")

    op.drop_table("bot_messages")
    op.drop_index("idx_calls_ts", table_name="api_calls")
    op.drop_index("idx_calls_session", table_name="api_calls")

    op.drop_table("api_calls")
    op.drop_index("idx_answer_scores_profile_ts", table_name="answer_scores")

    op.drop_table("answer_scores")
