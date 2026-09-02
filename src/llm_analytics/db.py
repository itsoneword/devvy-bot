"""Index DB: own SQLite file, WAL, sync sqlite3 (indexer runs off-thread).

The read API (milestone 3) will add an aiosqlite connection; the indexer's
parse/write path is deliberately sync per the plan.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('session','subagent')),
    session_id TEXT NOT NULL,
    agent_id TEXT,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    last_scanned_at TEXT,
    missing_since TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    cwd TEXT, profile TEXT, entrypoint TEXT,
    user_id INTEGER,
    git_branch TEXT, cli_version TEXT,
    first_ts TEXT, last_ts TEXT,
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    est_cost_usd REAL NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    subagent_count INTEGER NOT NULL DEFAULT 0,
    turn_count INTEGER NOT NULL DEFAULT 0,
    in_bot_db INTEGER NOT NULL DEFAULT 0,
    first_call_context INTEGER,
    bot_formula_tokens INTEGER,
    seeded_start INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_calls (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL, cache_creation_tokens INTEGER NOT NULL,
    cache_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_1h_tokens INTEGER NOT NULL DEFAULT 0,
    est_cost_usd REAL NOT NULL,
    pricing_rule TEXT NOT NULL,
    entry_uuid TEXT, file_id INTEGER NOT NULL,
    entry_offset INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_session ON api_calls(session_id, agent_id, ts);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON api_calls(ts);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL, agent_id TEXT,
    name TEXT NOT NULL, ts TEXT NOT NULL,
    request_id TEXT, assistant_uuid TEXT,
    result_ts TEXT, duration_ms INTEGER, is_error INTEGER,
    input_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id, agent_id, ts);

CREATE TABLE IF NOT EXISTS subagent_runs (
    agent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    spawn_tool_use_id TEXT,
    description TEXT, subagent_type TEXT, model_override TEXT,
    started_ts TEXT, ended_ts TEXT,
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    est_cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL, ts TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('bot','transcript')),
    user_id INTEGER, prompt_excerpt TEXT,
    duration_ms INTEGER, cost REAL,
    UNIQUE(session_id, ts, source)
);

CREATE TABLE IF NOT EXISTS derived_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL, agent_id TEXT, ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('cache_expiry','partial_cache_hit','context_cliff')),
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session
    ON derived_events(session_id, agent_id, ts);

CREATE TABLE IF NOT EXISTS bot_sessions (
    session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, project_path TEXT NOT NULL,
    created_at TEXT NOT NULL, last_used TEXT, total_cost REAL, total_turns INTEGER,
    message_count INTEGER, is_active INTEGER
);
CREATE TABLE IF NOT EXISTS bot_messages (
    message_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, user_id INTEGER NOT NULL,
    ts TEXT NOT NULL, cost REAL, duration_ms INTEGER, window_id INTEGER,
    prompt_chars INTEGER, response_chars INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bot_messages_session ON bot_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_bot_messages_chain ON bot_messages(user_id, window_id);
CREATE TABLE IF NOT EXISTS quality_daily (
    date TEXT NOT NULL, profile TEXT, user_id INTEGER,
    pinned_facts INTEGER NOT NULL DEFAULT 0,
    log_calls INTEGER NOT NULL DEFAULT 0,
    claimed_not_logged INTEGER NOT NULL DEFAULT 0,
    complaint_hits INTEGER NOT NULL DEFAULT 0,
    user_messages INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_quality_daily_date ON quality_daily(date);
CREATE TABLE IF NOT EXISTS bot_windows (
    user_id INTEGER NOT NULL, working_dir TEXT NOT NULL, window_id INTEGER NOT NULL,
    active_backend TEXT, cumulative_tokens INTEGER, last_eval_checkpoint INTEGER,
    last_checkpoint_message_id INTEGER, updated_at TEXT,
    PRIMARY KEY (user_id, working_dir)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the index DB with WAL mode; rows come back as sqlite3.Row."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all index tables. Idempotent (IF NOT EXISTS everywhere)."""
    conn.executescript(SCHEMA)
    conn.commit()
