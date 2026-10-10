"""Schema of bot.db (DatabaseManager) as SQLAlchemy Core tables (dv-5e4f Phase 1).

Source of truth for Alembic autogenerate (``alembic -n bot revision
--autogenerate``). Queries stay raw SQL; nothing imports these tables to
build statements.

Rules that keep prod compatible (enforced by ``scripts/schema_diff.py`` and
``tests/unit/test_storage/test_alembic_schema.py``):

- Column ORDER equals prod: many sites use ``SELECT *`` / ``Model(*row)``.
- Declared type names render exactly (``TIMESTAMP``/``DATETIME``/``DATE``/
  ``BOOLEAN``/``JSON``): ``detect_types=PARSE_DECLTYPES`` converters key on
  them. Use the uppercase SQLAlchemy types, never generic ``DateTime`` etc.
- Primary-key ``nullable`` is explicit, mirroring prod's NOT NULL flags.
- ``sqlite_autoincrement=True`` wherever prod says AUTOINCREMENT; CHECKs
  and ``server_default`` are spelled out verbatim.

Not modelled here (raw SQL in baseline revision ``0002``): the
``messages_fts``/``memory_facts_fts`` FTS5 tables + their shadow tables, the
6 sync triggers, and the ``daily_stats``/``user_stats`` views.
"""

from sqlalchemy import (
    BOOLEAN,
    DATE,
    INTEGER,
    JSON,
    REAL,
    TEXT,
    TIMESTAMP,
    Column,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Table,
    UniqueConstraint,
    text,
)

metadata = MetaData()


agent_tasks = Table(
    "agent_tasks",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("title", TEXT, nullable=False),
    Column("body", TEXT),
    Column("created_by", TEXT, nullable=False, server_default=text("'user'")),
    Column("assignee", TEXT),
    Column("due_at", TIMESTAMP),
    Column("status", TEXT, nullable=False, server_default=text("'open'")),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Index("idx_agent_tasks_status", "status", "due_at"),
    sqlite_autoincrement=True,
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("event_type", TEXT, nullable=False),
    Column("event_data", JSON),
    Column("success", BOOLEAN, server_default=text("TRUE")),
    Column("timestamp", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("ip_address", TEXT),
    ForeignKeyConstraint(["user_id"], ["users.user_id"]),
    Index("idx_audit_log_timestamp", "timestamp"),
    Index("idx_audit_log_user_id", "user_id"),
    sqlite_autoincrement=True,
)

bot_users = Table(
    "bot_users",
    metadata,
    Column("telegram_id", INTEGER, primary_key=True, nullable=True),
    Column("role", TEXT, nullable=False, server_default=text("'guest'")),
    Column("family_id", INTEGER),
    Column("added_by", INTEGER),
    Column("source", TEXT, nullable=False, server_default=text("'admin'")),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("display_name", TEXT),
    Column("relation", TEXT),
    Column("is_admin", INTEGER, nullable=False, server_default=text("0")),
    ForeignKeyConstraint(["family_id"], ["families.id"]),
    Index("idx_bot_users_family", "family_id"),
)

conversation_windows = Table(
    "conversation_windows",
    metadata,
    Column("user_id", INTEGER, primary_key=True, nullable=False),
    Column("working_dir", TEXT, primary_key=True, nullable=False),
    Column("window_id", INTEGER, nullable=False, server_default=text("1")),
    Column("active_backend", TEXT, nullable=False, server_default=text("'claude'")),
    Column("cumulative_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("last_eval_checkpoint", INTEGER, nullable=False, server_default=text("0")),
    Column(
        "last_checkpoint_message_id", INTEGER, nullable=False, server_default=text("0")
    ),
    Column("updated_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("last_context_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("pending_seed_window", INTEGER),
    Column("force_new_session", INTEGER, nullable=False, server_default=text("0")),
)

cost_tracking = Table(
    "cost_tracking",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("date", DATE, nullable=False),
    Column("daily_cost", REAL, server_default=text("0.0")),
    Column("request_count", INTEGER, server_default=text("0")),
    ForeignKeyConstraint(["user_id"], ["users.user_id"]),
    UniqueConstraint("user_id", "date"),
    Index("idx_cost_tracking_user_date", "user_id", "date"),
    sqlite_autoincrement=True,
)

families = Table(
    "families",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("created_by", INTEGER, nullable=False),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    sqlite_autoincrement=True,
)

item_notes = Table(
    "item_notes",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("item_id", INTEGER, nullable=False),
    Column("author_id", INTEGER),
    Column("kind", TEXT, nullable=False),
    Column("body", TEXT, nullable=False),
    Column("path", TEXT),
    Column("source", TEXT, nullable=False, server_default=text("'manual'")),
    Column("created_at", TEXT, nullable=False),
    ForeignKeyConstraint(["item_id"], ["items.id"]),
    Index("idx_item_notes_item", "item_id", "created_at"),
    sqlite_autoincrement=True,
)

item_seen = Table(
    "item_seen",
    metadata,
    Column("user_id", INTEGER, primary_key=True, nullable=False),
    Column("profile_slug", TEXT, primary_key=True, nullable=False),
    Column("seen_at", TEXT, nullable=False),
)

items = Table(
    "items",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("family_id", INTEGER),
    Column("creator_id", INTEGER, nullable=False),
    Column("assignee_id", INTEGER, nullable=False),
    Column("text", TEXT, nullable=False),
    Column("status", TEXT, nullable=False, server_default=text("'open'")),
    Column("due_at", TEXT),
    Column("recurrence", TEXT),
    Column("tz", TEXT, nullable=False, server_default=text("'UTC'")),
    Column("ends_at", TEXT),
    Column("deliver", TEXT, nullable=False, server_default=text("'message'")),
    Column("profile_slug", TEXT),
    Column("source_profile", TEXT),
    Column("last_fired_at", TEXT),
    Column("done_at", TEXT),
    Column("done_by", INTEGER),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    Column("deadline", TEXT),
    Index("idx_items_assignee", "assignee_id", "status", "due_at"),
    Index("idx_items_creator", "creator_id", "status"),
    sqlite_autoincrement=True,
)

lifecycle_events = Table(
    "lifecycle_events",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("ts", TEXT, nullable=False),
    Column("profile", TEXT, nullable=False),
    Column("event", TEXT, nullable=False),
    Column("detail", TEXT, nullable=False),
    sqlite_autoincrement=True,
)

memory_extractions = Table(
    "memory_extractions",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("user_id", INTEGER, nullable=False),
    Column("prompt_hash", TEXT, nullable=False),
    Column("raw_response", TEXT),
    Column("parsed_insights", JSON),
    Column("validation_status", TEXT, nullable=False, server_default=text("'pending'")),
    Column("target_files", JSON),
    Column("error_message", TEXT),
    Column("cumulative_tokens_at_run", INTEGER),
    Column("timestamp", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("op_stats", JSON),
    ForeignKeyConstraint(["user_id"], ["users.user_id"]),
    Index("idx_memory_extractions_session", "session_id"),
    Index("idx_memory_extractions_timestamp", "timestamp"),
    sqlite_autoincrement=True,
)

memory_facts = Table(
    "memory_facts",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("agent_id", TEXT, nullable=False),
    Column("tier", TEXT, nullable=False, server_default=text("'general'")),
    Column("label", TEXT),
    Column("content", TEXT, nullable=False),
    Column("status", TEXT, nullable=False, server_default=text("'active'")),
    Column("source", TEXT),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("kind", TEXT, nullable=False, server_default=text("'user_fact'")),
    Column("superseded_by", INTEGER),
    Column("valid_from", TIMESTAMP),
    Column("invalid_at", TIMESTAMP),
    Column("recorded_at", TIMESTAMP),
    Column("source_ref", TEXT),
    Column("conflict_group", TEXT),
    Column("invalidated_by", TEXT),
    ForeignKeyConstraint(["superseded_by"], ["memory_facts.id"]),
    Index("idx_memory_facts_agent", "agent_id", "tier", "status"),
    Index("idx_memory_facts_kind", "agent_id", "kind", "invalid_at"),
    sqlite_autoincrement=True,
)

messages = Table(
    "messages",
    metadata,
    Column("message_id", INTEGER, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("user_id", INTEGER, nullable=False),
    Column("timestamp", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("prompt", TEXT, nullable=False),
    Column("response", TEXT),
    Column("cost", REAL, server_default=text("0.0")),
    Column("duration_ms", INTEGER),
    Column("error", TEXT),
    Column("window_id", INTEGER),
    ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
    ForeignKeyConstraint(["user_id"], ["users.user_id"]),
    Index("idx_messages_session_id", "session_id"),
    Index("idx_messages_timestamp", "timestamp"),
    Index("idx_messages_window_id", "window_id"),
    sqlite_autoincrement=True,
)

model_calls = Table(
    "model_calls",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("ts", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("purpose", TEXT, nullable=False),
    Column("kind", TEXT, nullable=False),
    Column("harness", TEXT, nullable=False),
    Column("harness_version", TEXT),
    Column("provider", TEXT, nullable=False),
    Column("model", TEXT),
    Column("attended", INTEGER, nullable=False, server_default=text("1")),
    Column("profile", TEXT),
    Column("user_id", INTEGER),
    Column("session_id", TEXT),
    Column("input_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("output_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cache_read_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cache_creation_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cost_usd", REAL),
    Column("duration_ms", INTEGER, nullable=False, server_default=text("0")),
    Column("outcome", TEXT, nullable=False),
    Column("error", TEXT),
    Index("idx_model_calls_purpose", "purpose", "ts"),
    Index("idx_model_calls_ts", "ts"),
    sqlite_autoincrement=True,
)

pending_notes = Table(
    "pending_notes",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("working_dir", TEXT, nullable=False),
    Column("note", TEXT, nullable=False),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Index("idx_pending_notes_scope", "user_id", "working_dir"),
    sqlite_autoincrement=True,
)

pinned_messages = Table(
    "pinned_messages",
    metadata,
    Column("chat_id", INTEGER, primary_key=True, nullable=False),
    Column("pin_as", TEXT, primary_key=True, nullable=False),
    Column("message_id", INTEGER, nullable=False),
    Column("updated_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
)

project_threads = Table(
    "project_threads",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("project_slug", TEXT, nullable=False),
    Column("chat_id", INTEGER, nullable=False),
    Column("message_thread_id", INTEGER, nullable=False),
    Column("topic_name", TEXT, nullable=False),
    Column("is_active", BOOLEAN, server_default=text("TRUE")),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("chat_id", "project_slug"),
    UniqueConstraint("chat_id", "message_thread_id"),
    Index("idx_project_threads_chat_active", "chat_id", "is_active"),
    Index("idx_project_threads_slug", "project_slug"),
    sqlite_autoincrement=True,
)

scheduled_jobs = Table(
    "scheduled_jobs",
    metadata,
    Column("job_id", TEXT, primary_key=True, nullable=True),
    Column("job_name", TEXT, nullable=False),
    Column("cron_expression", TEXT, nullable=False),
    Column("prompt", TEXT, nullable=False),
    Column("target_chat_ids", TEXT, server_default=text("''")),
    Column("working_directory", TEXT, nullable=False),
    Column("skill_name", TEXT),
    Column("created_by", INTEGER, server_default=text("0")),
    Column("is_active", BOOLEAN, server_default=text("TRUE")),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("delivery", TEXT, nullable=False, server_default=text("'agent'")),
    Column("item_id", INTEGER),
    Column("timezone", TEXT),
    Column("ends_at", TEXT),
    Index("idx_scheduled_jobs_active", "is_active"),
    Index("idx_scheduled_jobs_item", "item_id"),
)

sent_messages = Table(
    "sent_messages",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("telegram_chat_id", INTEGER, nullable=False),
    Column("telegram_message_id", INTEGER, nullable=False),
    Column("kind", TEXT, nullable=False),
    Column("messages_message_id", INTEGER),
    Column("profile_slug", TEXT),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Index(
        "idx_sent_messages_tg", "telegram_chat_id", "telegram_message_id", unique=True
    ),
    sqlite_autoincrement=True,
)

session_tracking = Table(
    "session_tracking",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("user_id", INTEGER, nullable=False),
    Column("input_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("output_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cumulative_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cost", REAL, server_default=text("0.0")),
    Column("timestamp", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
    ForeignKeyConstraint(["user_id"], ["users.user_id"]),
    Index("idx_session_tracking_session", "session_id"),
    Index("idx_session_tracking_timestamp", "timestamp"),
    sqlite_autoincrement=True,
)

sessions = Table(
    "sessions",
    metadata,
    Column("session_id", TEXT, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("project_path", TEXT, nullable=False),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("last_used", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("total_cost", REAL, server_default=text("0.0")),
    Column("total_turns", INTEGER, server_default=text("0")),
    Column("message_count", INTEGER, server_default=text("0")),
    Column("is_active", BOOLEAN, server_default=text("TRUE")),
    Column("deactivated_at", TIMESTAMP),
    Column("deactivated_reason", TEXT),
    Column("backend", TEXT),
    Column("thread_head", INTEGER, nullable=False, server_default=text("0")),
    Column("thread_total_input", INTEGER, nullable=False, server_default=text("0")),
    Column("thread_total_output", INTEGER, nullable=False, server_default=text("0")),
    Column("cumulative_tokens", INTEGER, nullable=False, server_default=text("0")),
    ForeignKeyConstraint(["user_id"], ["users.user_id"]),
    Index("idx_sessions_project_path", "project_path"),
    Index("idx_sessions_user_id", "user_id"),
    Index("idx_sessions_user_project", "user_id", "project_path"),
)

tool_usage = Table(
    "tool_usage",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("message_id", INTEGER),
    Column("tool_name", TEXT, nullable=False),
    Column("tool_input", JSON),
    Column("timestamp", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("success", BOOLEAN, server_default=text("TRUE")),
    Column("error_message", TEXT),
    ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
    ForeignKeyConstraint(["message_id"], ["messages.message_id"]),
    sqlite_autoincrement=True,
)

user_holds = Table(
    "user_holds",
    metadata,
    Column("user_id", INTEGER, primary_key=True, nullable=True),
    Column("held_until", TIMESTAMP, nullable=False),
    Column("reason", TEXT),
    Column("set_by", INTEGER),
    Column("set_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
)

user_preferences = Table(
    "user_preferences",
    metadata,
    Column("user_id", INTEGER, primary_key=True, nullable=True),
    Column("active_profile", TEXT),
    Column("agent_backend", TEXT),
    Column("agent_model", TEXT),
    Column("updated_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("pinned_switch_message_id", INTEGER),
)

user_tokens = Table(
    "user_tokens",
    metadata,
    Column("token_id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("token_hash", TEXT, nullable=False),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("expires_at", TIMESTAMP),
    Column("last_used", TIMESTAMP),
    Column("is_active", BOOLEAN, server_default=text("TRUE")),
    ForeignKeyConstraint(["user_id"], ["users.user_id"]),
    UniqueConstraint("token_hash"),
    sqlite_autoincrement=True,
)

users = Table(
    "users",
    metadata,
    Column("user_id", INTEGER, primary_key=True, nullable=True),
    Column("telegram_username", TEXT),
    Column("first_seen", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("last_active", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("is_allowed", BOOLEAN, server_default=text("FALSE")),
    Column("total_cost", REAL, server_default=text("0.0")),
    Column("message_count", INTEGER, server_default=text("0")),
    Column("session_count", INTEGER, server_default=text("0")),
)

webhook_events = Table(
    "webhook_events",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("event_id", TEXT, nullable=False),
    Column("provider", TEXT, nullable=False),
    Column("event_type", TEXT, nullable=False),
    Column("delivery_id", TEXT),
    Column("payload", JSON),
    Column("processed", BOOLEAN, server_default=text("FALSE")),
    Column("received_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("delivery_id"),
    Index("idx_webhook_events_delivery", "delivery_id"),
    Index("idx_webhook_events_provider", "provider", "received_at"),
    sqlite_autoincrement=True,
)

window_bundles = Table(
    "window_bundles",
    metadata,
    Column("user_id", INTEGER, primary_key=True, nullable=False),
    Column("working_dir", TEXT, primary_key=True, nullable=False),
    Column("window_id", INTEGER, primary_key=True, nullable=False),
    Column("agent_id", TEXT, nullable=False),
    Column("bundle_text", TEXT, nullable=False),
    Column("bundle_sha256", TEXT, nullable=False),
    Column("persona_chars", INTEGER, nullable=False),
    Column("facts_count", INTEGER, nullable=False),
    Column("pointers_count", INTEGER, nullable=False),
    Column("family_count", INTEGER, nullable=False),
    Column("total_chars", INTEGER, nullable=False),
    Column("created_at", TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
)
