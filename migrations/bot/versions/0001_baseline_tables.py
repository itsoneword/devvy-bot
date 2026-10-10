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
        "agent_tasks",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("title", sa.TEXT(), nullable=False),
        sa.Column("body", sa.TEXT(), nullable=True),
        sa.Column(
            "created_by", sa.TEXT(), server_default=sa.text("'user'"), nullable=False
        ),
        sa.Column("assignee", sa.TEXT(), nullable=True),
        sa.Column("due_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "status", sa.TEXT(), server_default=sa.text("'open'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_agent_tasks_status", "agent_tasks", ["status", "due_at"], unique=False
    )

    op.create_table(
        "conversation_windows",
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("working_dir", sa.TEXT(), nullable=False),
        sa.Column(
            "window_id", sa.INTEGER(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "active_backend",
            sa.TEXT(),
            server_default=sa.text("'claude'"),
            nullable=False,
        ),
        sa.Column(
            "cumulative_tokens",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "last_eval_checkpoint",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "last_checkpoint_message_id",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "last_context_tokens",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("pending_seed_window", sa.INTEGER(), nullable=True),
        sa.Column(
            "force_new_session",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "working_dir"),
    )
    op.create_table(
        "families",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("created_by", sa.INTEGER(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "item_seen",
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("profile_slug", sa.TEXT(), nullable=False),
        sa.Column("seen_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "profile_slug"),
    )
    op.create_table(
        "items",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("family_id", sa.INTEGER(), nullable=True),
        sa.Column("creator_id", sa.INTEGER(), nullable=False),
        sa.Column("assignee_id", sa.INTEGER(), nullable=False),
        sa.Column("text", sa.TEXT(), nullable=False),
        sa.Column(
            "status", sa.TEXT(), server_default=sa.text("'open'"), nullable=False
        ),
        sa.Column("due_at", sa.TEXT(), nullable=True),
        sa.Column("recurrence", sa.TEXT(), nullable=True),
        sa.Column("tz", sa.TEXT(), server_default=sa.text("'UTC'"), nullable=False),
        sa.Column("ends_at", sa.TEXT(), nullable=True),
        sa.Column(
            "deliver", sa.TEXT(), server_default=sa.text("'message'"), nullable=False
        ),
        sa.Column("profile_slug", sa.TEXT(), nullable=True),
        sa.Column("source_profile", sa.TEXT(), nullable=True),
        sa.Column("last_fired_at", sa.TEXT(), nullable=True),
        sa.Column("done_at", sa.TEXT(), nullable=True),
        sa.Column("done_by", sa.INTEGER(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.Column("deadline", sa.TEXT(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_items_assignee", "items", ["assignee_id", "status", "due_at"], unique=False
    )
    op.create_index(
        "idx_items_creator", "items", ["creator_id", "status"], unique=False
    )

    op.create_table(
        "lifecycle_events",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("ts", sa.TEXT(), nullable=False),
        sa.Column("profile", sa.TEXT(), nullable=False),
        sa.Column("event", sa.TEXT(), nullable=False),
        sa.Column("detail", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "memory_facts",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("agent_id", sa.TEXT(), nullable=False),
        sa.Column(
            "tier", sa.TEXT(), server_default=sa.text("'general'"), nullable=False
        ),
        sa.Column("label", sa.TEXT(), nullable=True),
        sa.Column("content", sa.TEXT(), nullable=False),
        sa.Column(
            "status", sa.TEXT(), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("source", sa.TEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "kind", sa.TEXT(), server_default=sa.text("'user_fact'"), nullable=False
        ),
        sa.Column("superseded_by", sa.INTEGER(), nullable=True),
        sa.Column("valid_from", sa.TIMESTAMP(), nullable=True),
        sa.Column("invalid_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("recorded_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("source_ref", sa.TEXT(), nullable=True),
        sa.Column("conflict_group", sa.TEXT(), nullable=True),
        sa.Column("invalidated_by", sa.TEXT(), nullable=True),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["memory_facts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_memory_facts_agent",
        "memory_facts",
        ["agent_id", "tier", "status"],
        unique=False,
    )
    op.create_index(
        "idx_memory_facts_kind",
        "memory_facts",
        ["agent_id", "kind", "invalid_at"],
        unique=False,
    )

    op.create_table(
        "model_calls",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column(
            "ts",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("purpose", sa.TEXT(), nullable=False),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("harness", sa.TEXT(), nullable=False),
        sa.Column("harness_version", sa.TEXT(), nullable=True),
        sa.Column("provider", sa.TEXT(), nullable=False),
        sa.Column("model", sa.TEXT(), nullable=True),
        sa.Column(
            "attended", sa.INTEGER(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("profile", sa.TEXT(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=True),
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
        sa.Column("cost_usd", sa.REAL(), nullable=True),
        sa.Column(
            "duration_ms", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("outcome", sa.TEXT(), nullable=False),
        sa.Column("error", sa.TEXT(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_model_calls_purpose", "model_calls", ["purpose", "ts"], unique=False
    )
    op.create_index("idx_model_calls_ts", "model_calls", ["ts"], unique=False)

    op.create_table(
        "pending_notes",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("working_dir", sa.TEXT(), nullable=False),
        sa.Column("note", sa.TEXT(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_pending_notes_scope",
        "pending_notes",
        ["user_id", "working_dir"],
        unique=False,
    )

    op.create_table(
        "pinned_messages",
        sa.Column("chat_id", sa.INTEGER(), nullable=False),
        sa.Column("pin_as", sa.TEXT(), nullable=False),
        sa.Column("message_id", sa.INTEGER(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("chat_id", "pin_as"),
    )
    op.create_table(
        "project_threads",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("project_slug", sa.TEXT(), nullable=False),
        sa.Column("chat_id", sa.INTEGER(), nullable=False),
        sa.Column("message_thread_id", sa.INTEGER(), nullable=False),
        sa.Column("topic_name", sa.TEXT(), nullable=False),
        sa.Column(
            "is_active", sa.BOOLEAN(), server_default=sa.text("(TRUE)"), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "project_slug"),  # order = sqlite_autoindex_1
        sa.UniqueConstraint("chat_id", "message_thread_id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_project_threads_chat_active",
        "project_threads",
        ["chat_id", "is_active"],
        unique=False,
    )
    op.create_index(
        "idx_project_threads_slug", "project_threads", ["project_slug"], unique=False
    )

    op.create_table(
        "scheduled_jobs",
        sa.Column("job_id", sa.TEXT(), nullable=True),
        sa.Column("job_name", sa.TEXT(), nullable=False),
        sa.Column("cron_expression", sa.TEXT(), nullable=False),
        sa.Column("prompt", sa.TEXT(), nullable=False),
        sa.Column(
            "target_chat_ids", sa.TEXT(), server_default=sa.text("('')"), nullable=True
        ),
        sa.Column("working_directory", sa.TEXT(), nullable=False),
        sa.Column("skill_name", sa.TEXT(), nullable=True),
        sa.Column(
            "created_by", sa.INTEGER(), server_default=sa.text("0"), nullable=True
        ),
        sa.Column(
            "is_active", sa.BOOLEAN(), server_default=sa.text("(TRUE)"), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "delivery", sa.TEXT(), server_default=sa.text("'agent'"), nullable=False
        ),
        sa.Column("item_id", sa.INTEGER(), nullable=True),
        sa.Column("timezone", sa.TEXT(), nullable=True),
        sa.Column("ends_at", sa.TEXT(), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(
        "idx_scheduled_jobs_active", "scheduled_jobs", ["is_active"], unique=False
    )
    op.create_index(
        "idx_scheduled_jobs_item", "scheduled_jobs", ["item_id"], unique=False
    )

    op.create_table(
        "sent_messages",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("telegram_chat_id", sa.INTEGER(), nullable=False),
        sa.Column("telegram_message_id", sa.INTEGER(), nullable=False),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("messages_message_id", sa.INTEGER(), nullable=True),
        sa.Column("profile_slug", sa.TEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_sent_messages_tg",
        "sent_messages",
        ["telegram_chat_id", "telegram_message_id"],
        unique=True,
    )

    op.create_table(
        "user_holds",
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column("held_until", sa.TIMESTAMP(), nullable=False),
        sa.Column("reason", sa.TEXT(), nullable=True),
        sa.Column("set_by", sa.INTEGER(), nullable=True),
        sa.Column(
            "set_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column("active_profile", sa.TEXT(), nullable=True),
        sa.Column("agent_backend", sa.TEXT(), nullable=True),
        sa.Column("agent_model", sa.TEXT(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("pinned_switch_message_id", sa.INTEGER(), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "users",
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column("telegram_username", sa.TEXT(), nullable=True),
        sa.Column(
            "first_seen",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "last_active",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "is_allowed", sa.BOOLEAN(), server_default=sa.text("(FALSE)"), nullable=True
        ),
        sa.Column(
            "total_cost", sa.REAL(), server_default=sa.text("(0.0)"), nullable=True
        ),
        sa.Column(
            "message_count", sa.INTEGER(), server_default=sa.text("0"), nullable=True
        ),
        sa.Column(
            "session_count", sa.INTEGER(), server_default=sa.text("0"), nullable=True
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("event_id", sa.TEXT(), nullable=False),
        sa.Column("provider", sa.TEXT(), nullable=False),
        sa.Column("event_type", sa.TEXT(), nullable=False),
        sa.Column("delivery_id", sa.TEXT(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "processed", sa.BOOLEAN(), server_default=sa.text("(FALSE)"), nullable=True
        ),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_webhook_events_delivery", "webhook_events", ["delivery_id"], unique=False
    )
    op.create_index(
        "idx_webhook_events_provider",
        "webhook_events",
        ["provider", "received_at"],
        unique=False,
    )

    op.create_table(
        "window_bundles",
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("working_dir", sa.TEXT(), nullable=False),
        sa.Column("window_id", sa.INTEGER(), nullable=False),
        sa.Column("agent_id", sa.TEXT(), nullable=False),
        sa.Column("bundle_text", sa.TEXT(), nullable=False),
        sa.Column("bundle_sha256", sa.TEXT(), nullable=False),
        sa.Column("persona_chars", sa.INTEGER(), nullable=False),
        sa.Column("facts_count", sa.INTEGER(), nullable=False),
        sa.Column("pointers_count", sa.INTEGER(), nullable=False),
        sa.Column("family_count", sa.INTEGER(), nullable=False),
        sa.Column("total_chars", sa.INTEGER(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("user_id", "working_dir", "window_id"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("event_type", sa.TEXT(), nullable=False),
        sa.Column("event_data", sa.JSON(), nullable=True),
        sa.Column(
            "success", sa.BOOLEAN(), server_default=sa.text("(TRUE)"), nullable=True
        ),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("ip_address", sa.TEXT(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_audit_log_timestamp", "audit_log", ["timestamp"], unique=False)
    op.create_index("idx_audit_log_user_id", "audit_log", ["user_id"], unique=False)

    op.create_table(
        "bot_users",
        sa.Column("telegram_id", sa.INTEGER(), nullable=True),
        sa.Column("role", sa.TEXT(), server_default=sa.text("'guest'"), nullable=False),
        sa.Column("family_id", sa.INTEGER(), nullable=True),
        sa.Column("added_by", sa.INTEGER(), nullable=True),
        sa.Column(
            "source", sa.TEXT(), server_default=sa.text("'admin'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("display_name", sa.TEXT(), nullable=True),
        sa.Column("relation", sa.TEXT(), nullable=True),
        sa.Column(
            "is_admin", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["families.id"],
        ),
        sa.PrimaryKeyConstraint("telegram_id"),
    )
    op.create_index("idx_bot_users_family", "bot_users", ["family_id"], unique=False)

    op.create_table(
        "cost_tracking",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("date", sa.DATE(), nullable=False),
        sa.Column(
            "daily_cost", sa.REAL(), server_default=sa.text("(0.0)"), nullable=True
        ),
        sa.Column(
            "request_count", sa.INTEGER(), server_default=sa.text("0"), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "date"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_cost_tracking_user_date",
        "cost_tracking",
        ["user_id", "date"],
        unique=False,
    )

    op.create_table(
        "item_notes",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("item_id", sa.INTEGER(), nullable=False),
        sa.Column("author_id", sa.INTEGER(), nullable=True),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("body", sa.TEXT(), nullable=False),
        sa.Column("path", sa.TEXT(), nullable=True),
        sa.Column(
            "source", sa.TEXT(), server_default=sa.text("'manual'"), nullable=False
        ),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_item_notes_item", "item_notes", ["item_id", "created_at"], unique=False
    )

    op.create_table(
        "memory_extractions",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("prompt_hash", sa.TEXT(), nullable=False),
        sa.Column("raw_response", sa.TEXT(), nullable=True),
        sa.Column("parsed_insights", sa.JSON(), nullable=True),
        sa.Column(
            "validation_status",
            sa.TEXT(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("target_files", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.TEXT(), nullable=True),
        sa.Column("cumulative_tokens_at_run", sa.INTEGER(), nullable=True),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("op_stats", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_memory_extractions_session",
        "memory_extractions",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "idx_memory_extractions_timestamp",
        "memory_extractions",
        ["timestamp"],
        unique=False,
    )

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.TEXT(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("project_path", sa.TEXT(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "last_used",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "total_cost", sa.REAL(), server_default=sa.text("(0.0)"), nullable=True
        ),
        sa.Column(
            "total_turns", sa.INTEGER(), server_default=sa.text("0"), nullable=True
        ),
        sa.Column(
            "message_count", sa.INTEGER(), server_default=sa.text("0"), nullable=True
        ),
        sa.Column(
            "is_active", sa.BOOLEAN(), server_default=sa.text("(TRUE)"), nullable=True
        ),
        sa.Column("deactivated_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("deactivated_reason", sa.TEXT(), nullable=True),
        sa.Column("backend", sa.TEXT(), nullable=True),
        sa.Column(
            "thread_head", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "thread_total_input",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "thread_total_output",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cumulative_tokens",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "idx_sessions_project_path", "sessions", ["project_path"], unique=False
    )
    op.create_index("idx_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_index(
        "idx_sessions_user_project",
        "sessions",
        ["user_id", "project_path"],
        unique=False,
    )

    op.create_table(
        "user_tokens",
        sa.Column("token_id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("token_hash", sa.TEXT(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("last_used", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "is_active", sa.BOOLEAN(), server_default=sa.text("(TRUE)"), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
        ),
        sa.PrimaryKeyConstraint("token_id"),
        sa.UniqueConstraint("token_hash"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "messages",
        sa.Column("message_id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("prompt", sa.TEXT(), nullable=False),
        sa.Column("response", sa.TEXT(), nullable=True),
        sa.Column("cost", sa.REAL(), server_default=sa.text("(0.0)"), nullable=True),
        sa.Column("duration_ms", sa.INTEGER(), nullable=True),
        sa.Column("error", sa.TEXT(), nullable=True),
        sa.Column("window_id", sa.INTEGER(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
        ),
        sa.PrimaryKeyConstraint("message_id"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_messages_session_id", "messages", ["session_id"], unique=False)
    op.create_index("idx_messages_timestamp", "messages", ["timestamp"], unique=False)
    op.create_index("idx_messages_window_id", "messages", ["window_id"], unique=False)

    op.create_table(
        "session_tracking",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column(
            "input_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "output_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cumulative_tokens",
            sa.INTEGER(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("cost", sa.REAL(), server_default=sa.text("(0.0)"), nullable=True),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_session_tracking_session", "session_tracking", ["session_id"], unique=False
    )
    op.create_index(
        "idx_session_tracking_timestamp",
        "session_tracking",
        ["timestamp"],
        unique=False,
    )

    op.create_table(
        "tool_usage",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("session_id", sa.TEXT(), nullable=False),
        sa.Column("message_id", sa.INTEGER(), nullable=True),
        sa.Column("tool_name", sa.TEXT(), nullable=False),
        sa.Column("tool_input", sa.JSON(), nullable=True),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column(
            "success", sa.BOOLEAN(), server_default=sa.text("(TRUE)"), nullable=True
        ),
        sa.Column("error_message", sa.TEXT(), nullable=True),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.message_id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("tool_usage")
    op.drop_index("idx_session_tracking_timestamp", table_name="session_tracking")
    op.drop_index("idx_session_tracking_session", table_name="session_tracking")

    op.drop_table("session_tracking")
    op.drop_index("idx_messages_window_id", table_name="messages")
    op.drop_index("idx_messages_timestamp", table_name="messages")
    op.drop_index("idx_messages_session_id", table_name="messages")

    op.drop_table("messages")
    op.drop_table("user_tokens")
    op.drop_index("idx_sessions_user_project", table_name="sessions")
    op.drop_index("idx_sessions_user_id", table_name="sessions")
    op.drop_index("idx_sessions_project_path", table_name="sessions")

    op.drop_table("sessions")
    op.drop_index("idx_memory_extractions_timestamp", table_name="memory_extractions")
    op.drop_index("idx_memory_extractions_session", table_name="memory_extractions")

    op.drop_table("memory_extractions")
    op.drop_index("idx_item_notes_item", table_name="item_notes")

    op.drop_table("item_notes")
    op.drop_index("idx_cost_tracking_user_date", table_name="cost_tracking")

    op.drop_table("cost_tracking")
    op.drop_index("idx_bot_users_family", table_name="bot_users")

    op.drop_table("bot_users")
    op.drop_index("idx_audit_log_user_id", table_name="audit_log")
    op.drop_index("idx_audit_log_timestamp", table_name="audit_log")

    op.drop_table("audit_log")
    op.drop_table("window_bundles")
    op.drop_index("idx_webhook_events_provider", table_name="webhook_events")
    op.drop_index("idx_webhook_events_delivery", table_name="webhook_events")

    op.drop_table("webhook_events")
    op.drop_table("users")
    op.drop_table("user_preferences")
    op.drop_table("user_holds")
    op.drop_index("idx_sent_messages_tg", table_name="sent_messages")

    op.drop_table("sent_messages")
    op.drop_index("idx_scheduled_jobs_item", table_name="scheduled_jobs")
    op.drop_index("idx_scheduled_jobs_active", table_name="scheduled_jobs")

    op.drop_table("scheduled_jobs")
    op.drop_index("idx_project_threads_slug", table_name="project_threads")
    op.drop_index("idx_project_threads_chat_active", table_name="project_threads")

    op.drop_table("project_threads")
    op.drop_table("pinned_messages")
    op.drop_index("idx_pending_notes_scope", table_name="pending_notes")

    op.drop_table("pending_notes")
    op.drop_index("idx_model_calls_ts", table_name="model_calls")
    op.drop_index("idx_model_calls_purpose", table_name="model_calls")

    op.drop_table("model_calls")
    op.drop_index("idx_memory_facts_kind", table_name="memory_facts")
    op.drop_index("idx_memory_facts_agent", table_name="memory_facts")

    op.drop_table("memory_facts")
    op.drop_table("lifecycle_events")
    op.drop_index("idx_items_creator", table_name="items")
    op.drop_index("idx_items_assignee", table_name="items")

    op.drop_table("items")
    op.drop_table("item_seen")
    op.drop_table("families")
    op.drop_table("conversation_windows")
    op.drop_index("idx_agent_tasks_status", table_name="agent_tasks")

    op.drop_table("agent_tasks")
