"""Schema of llm_analytics.db (sync sqlite3 indexer) as SQLAlchemy Core tables (dv-5e4f Phase 1).

Source of truth for Alembic autogenerate (``alembic -n llm_analytics revision
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
"""

from sqlalchemy import (
    INTEGER,
    REAL,
    TEXT,
    CheckConstraint,
    Column,
    Index,
    MetaData,
    Table,
    UniqueConstraint,
    text,
)

metadata = MetaData()


answer_scores = Table(
    "answer_scores",
    metadata,
    Column("message_id", INTEGER, primary_key=True, nullable=False),
    Column("profile", TEXT),
    Column("user_id", INTEGER),
    Column("ts", TEXT),
    Column("judge_model", TEXT, primary_key=True, nullable=False),
    Column("rubric_version", INTEGER, primary_key=True, nullable=False),
    Column("scores_json", TEXT, nullable=False),
    Column("total", REAL, nullable=False),
    Column("rationale", TEXT),
    Column("flags", TEXT),
    Column("scored_at", TEXT, nullable=False),
    Index("idx_answer_scores_profile_ts", "profile", "ts"),
)

api_calls = Table(
    "api_calls",
    metadata,
    Column("request_id", TEXT, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("agent_id", TEXT),
    Column("ts", TEXT, nullable=False),
    Column("model", TEXT, nullable=False),
    Column("input_tokens", INTEGER, nullable=False),
    Column("output_tokens", INTEGER, nullable=False),
    Column("cache_read_tokens", INTEGER, nullable=False),
    Column("cache_creation_tokens", INTEGER, nullable=False),
    Column("cache_5m_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cache_1h_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("est_cost_usd", REAL, nullable=False),
    Column("pricing_rule", TEXT, nullable=False),
    Column("entry_uuid", TEXT),
    Column("file_id", INTEGER, nullable=False),
    Column("entry_offset", INTEGER, nullable=False),
    Index("idx_calls_session", "session_id", "agent_id", "ts"),
    Index("idx_calls_ts", "ts"),
)

bot_messages = Table(
    "bot_messages",
    metadata,
    Column("message_id", INTEGER, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("user_id", INTEGER, nullable=False),
    Column("ts", TEXT, nullable=False),
    Column("cost", REAL),
    Column("duration_ms", INTEGER),
    Column("window_id", INTEGER),
    Column("prompt_chars", INTEGER),
    Column("response_chars", INTEGER),
    Index("idx_bot_messages_chain", "user_id", "window_id"),
    Index("idx_bot_messages_session", "session_id"),
)

bot_sessions = Table(
    "bot_sessions",
    metadata,
    Column("session_id", TEXT, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("project_path", TEXT, nullable=False),
    Column("created_at", TEXT, nullable=False),
    Column("last_used", TEXT),
    Column("total_cost", REAL),
    Column("total_turns", INTEGER),
    Column("message_count", INTEGER),
    Column("is_active", INTEGER),
)

bot_windows = Table(
    "bot_windows",
    metadata,
    Column("user_id", INTEGER, primary_key=True, nullable=False),
    Column("working_dir", TEXT, primary_key=True, nullable=False),
    Column("window_id", INTEGER, nullable=False),
    Column("active_backend", TEXT),
    Column("cumulative_tokens", INTEGER),
    Column("last_eval_checkpoint", INTEGER),
    Column("last_checkpoint_message_id", INTEGER),
    Column("updated_at", TEXT),
)

context_items = Table(
    "context_items",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("agent_id", TEXT),
    Column("file_id", INTEGER, nullable=False),
    Column("entry_offset", INTEGER, nullable=False),
    Column("seq", INTEGER, nullable=False),
    Column("ts", TEXT, nullable=False),
    Column("kind", TEXT, nullable=False),
    Column("name", TEXT, nullable=False),
    Column("chars", INTEGER, nullable=False),
    Column("request_id", TEXT),
    UniqueConstraint("file_id", "entry_offset", "seq"),
    Index("idx_context_items_session", "session_id", "agent_id", "ts"),
    sqlite_autoincrement=True,
)

derived_events = Table(
    "derived_events",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("agent_id", TEXT),
    Column("ts", TEXT, nullable=False),
    Column("kind", TEXT, nullable=False),
    Column("detail", TEXT, nullable=False),
    CheckConstraint("kind IN ('cache_expiry','partial_cache_hit','context_cliff')"),
    Index("idx_events_session", "session_id", "agent_id", "ts"),
    sqlite_autoincrement=True,
)

files = Table(
    "files",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("path", TEXT, nullable=False),
    Column("kind", TEXT, nullable=False),
    Column("session_id", TEXT, nullable=False),
    Column("agent_id", TEXT),
    Column("byte_offset", INTEGER, nullable=False, server_default=text("0")),
    Column("mtime", REAL, nullable=False, server_default=text("0")),
    Column("size", INTEGER, nullable=False, server_default=text("0")),
    Column("last_scanned_at", TEXT),
    Column("missing_since", TEXT),
    CheckConstraint("kind IN ('session','subagent')"),
    UniqueConstraint("path"),
    sqlite_autoincrement=True,
)

meta = Table(
    "meta",
    metadata,
    Column("key", TEXT, primary_key=True, nullable=True),
    Column("value", TEXT, nullable=False),
)

quality_daily = Table(
    "quality_daily",
    metadata,
    Column("date", TEXT, nullable=False),
    Column("profile", TEXT),
    Column("user_id", INTEGER),
    Column("pinned_facts", INTEGER, nullable=False, server_default=text("0")),
    Column("log_calls", INTEGER, nullable=False, server_default=text("0")),
    Column("claimed_not_logged", INTEGER, nullable=False, server_default=text("0")),
    Column("complaint_hits", INTEGER, nullable=False, server_default=text("0")),
    Column("user_messages", INTEGER, nullable=False, server_default=text("0")),
    Column("recipe_turns", INTEGER, nullable=False, server_default=text("0")),
    Column(
        "recipe_turns_unsearched", INTEGER, nullable=False, server_default=text("0")
    ),
    Column("verification_calls", INTEGER, nullable=False, server_default=text("0")),
    Column("verified_turns", INTEGER, nullable=False, server_default=text("0")),
    Column("deep_research_turns", INTEGER, nullable=False, server_default=text("0")),
    Column("program_turns", INTEGER, nullable=False, server_default=text("0")),
    Column(
        "program_turns_unchecked", INTEGER, nullable=False, server_default=text("0")
    ),
    Index("idx_quality_daily_date", "date"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("session_id", TEXT, primary_key=True, nullable=True),
    Column("cwd", TEXT),
    Column("profile", TEXT),
    Column("entrypoint", TEXT),
    Column("user_id", INTEGER),
    Column("git_branch", TEXT),
    Column("cli_version", TEXT),
    Column("first_ts", TEXT),
    Column("last_ts", TEXT),
    Column("api_call_count", INTEGER, nullable=False, server_default=text("0")),
    Column("input_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("output_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cache_read_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cache_creation_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("est_cost_usd", REAL, nullable=False, server_default=text("0")),
    Column("tool_call_count", INTEGER, nullable=False, server_default=text("0")),
    Column("subagent_count", INTEGER, nullable=False, server_default=text("0")),
    Column("turn_count", INTEGER, nullable=False, server_default=text("0")),
    Column("in_bot_db", INTEGER, nullable=False, server_default=text("0")),
    Column("first_call_context", INTEGER),
    Column("bot_formula_tokens", INTEGER),
    Column("seeded_start", INTEGER, nullable=False, server_default=text("0")),
    # Session attribution (dv-d2a3, src/llm_analytics/attribution.py). The
    # raw cwd label stays in `profile`.
    Column("attr_group", TEXT),
    Column("attr_key", TEXT),
    Column("attr_detail", TEXT),
    Column("attr_archived", INTEGER, nullable=False, server_default=text("0")),
    Index("idx_sessions_attr", "attr_group", "attr_key"),
)

subagent_runs = Table(
    "subagent_runs",
    metadata,
    Column("agent_id", TEXT, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("spawn_tool_use_id", TEXT),
    Column("description", TEXT),
    Column("subagent_type", TEXT),
    Column("model_override", TEXT),
    Column("started_ts", TEXT),
    Column("ended_ts", TEXT),
    Column("api_call_count", INTEGER, nullable=False, server_default=text("0")),
    Column("input_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("output_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cache_read_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("cache_creation_tokens", INTEGER, nullable=False, server_default=text("0")),
    Column("est_cost_usd", REAL, nullable=False, server_default=text("0")),
)

tool_calls = Table(
    "tool_calls",
    metadata,
    Column("tool_use_id", TEXT, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("agent_id", TEXT),
    Column("name", TEXT, nullable=False),
    Column("ts", TEXT, nullable=False),
    Column("request_id", TEXT),
    Column("assistant_uuid", TEXT),
    Column("result_ts", TEXT),
    Column("duration_ms", INTEGER),
    Column("is_error", INTEGER),
    Column("input_summary", TEXT),
    Column("input_chars", INTEGER),
    Column("result_chars", INTEGER),
    Column("result_images", INTEGER),
    Column("result_tokens_est", INTEGER),
    Index("idx_tools_session", "session_id", "agent_id", "ts"),
)

turns = Table(
    "turns",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("session_id", TEXT, nullable=False),
    Column("ts", TEXT, nullable=False),
    Column("source", TEXT, nullable=False),
    Column("user_id", INTEGER),
    Column("prompt_excerpt", TEXT),
    Column("duration_ms", INTEGER),
    Column("cost", REAL),
    CheckConstraint("source IN ('bot','transcript')"),
    UniqueConstraint("session_id", "ts", "source"),
    sqlite_autoincrement=True,
)
