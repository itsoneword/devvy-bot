"""Database connection and initialization.

Features:
- Connection pooling
- Automatic migrations
- Health checks
- Schema versioning
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, List, Tuple

import aiosqlite
import structlog

logger = structlog.get_logger()


# Python 3.12+: sqlite3's default datetime adapter is deprecated.
# Register explicit adapters/converters once at import time to avoid warnings
# and keep consistent ISO-8601 persistence for datetime values.
sqlite3.register_adapter(datetime, lambda value: value.isoformat())
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_converter("DATETIME", lambda b: datetime.fromisoformat(b.decode()))
# Keep DATE columns as raw ISO strings (matches existing model expectations).
sqlite3.register_converter("DATE", lambda b: b.decode())

# Initial schema migration
INITIAL_SCHEMA = """
-- Core Tables

-- Users table
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    telegram_username TEXT,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_allowed BOOLEAN DEFAULT FALSE,
    total_cost REAL DEFAULT 0.0,
    message_count INTEGER DEFAULT 0,
    session_count INTEGER DEFAULT 0
);

-- Sessions table
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    project_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_cost REAL DEFAULT 0.0,
    total_turns INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Messages table
CREATE TABLE messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    prompt TEXT NOT NULL,
    response TEXT,
    cost REAL DEFAULT 0.0,
    duration_ms INTEGER,
    error TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Tool usage table
CREATE TABLE tool_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_id INTEGER,
    tool_name TEXT NOT NULL,
    tool_input JSON,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    success BOOLEAN DEFAULT TRUE,
    error_message TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id)
);

-- Audit log table
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_data JSON,
    success BOOLEAN DEFAULT TRUE,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip_address TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- User tokens table (for token auth)
CREATE TABLE user_tokens (
    token_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    last_used TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Cost tracking table
CREATE TABLE cost_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date DATE NOT NULL,
    daily_cost REAL DEFAULT 0.0,
    request_count INTEGER DEFAULT 0,
    UNIQUE(user_id, date),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Indexes for performance
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_project_path ON sessions(project_path);
CREATE INDEX idx_messages_session_id ON messages(session_id);
CREATE INDEX idx_messages_timestamp ON messages(timestamp);
CREATE INDEX idx_audit_log_user_id ON audit_log(user_id);
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX idx_cost_tracking_user_date ON cost_tracking(user_id, date);
"""


class DatabaseManager:
    """Manage database connections and initialization."""

    def __init__(self, database_url: str):
        """Initialize database manager."""
        self.database_path = self._parse_database_url(database_url)
        self._connection_pool = []
        self._pool_size = 5
        self._pool_lock = asyncio.Lock()

    def _parse_database_url(self, database_url: str) -> Path:
        """Parse database URL to path."""
        if database_url.startswith("sqlite:///"):
            return Path(database_url[10:])
        elif database_url.startswith("sqlite://"):
            return Path(database_url[9:])
        else:
            return Path(database_url)

    async def initialize(self):
        """Initialize database and run migrations."""
        logger.info("Initializing database", path=str(self.database_path))

        # Ensure directory exists
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        # Run migrations
        await self._run_migrations()

        # Initialize connection pool
        await self._init_pool()

        logger.info("Database initialization complete")

    async def _run_migrations(self):
        """Run database migrations."""
        async with aiosqlite.connect(
            self.database_path, detect_types=sqlite3.PARSE_DECLTYPES
        ) as conn:
            conn.row_factory = aiosqlite.Row

            # Enable foreign keys
            await conn.execute("PRAGMA foreign_keys = ON")

            # Get current version
            current_version = await self._get_schema_version(conn)
            logger.info("Current schema version", version=current_version)

            # Run migrations
            migrations = self._get_migrations()
            for version, migration in migrations:
                if version > current_version:
                    logger.info("Running migration", version=version)
                    await conn.executescript(migration)
                    await self._set_schema_version(conn, version)

            await conn.commit()

    async def _get_schema_version(self, conn: aiosqlite.Connection) -> int:
        """Get current schema version."""
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """
        )

        cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0

    async def _set_schema_version(self, conn: aiosqlite.Connection, version: int):
        """Set schema version."""
        await conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (version,)
        )

    def _get_migrations(self) -> List[Tuple[int, str]]:
        """Get migration scripts."""
        return [
            (1, INITIAL_SCHEMA),
            (
                2,
                """
                -- Add analytics views
                CREATE VIEW IF NOT EXISTS daily_stats AS
                SELECT
                    date(timestamp) as date,
                    COUNT(DISTINCT user_id) as active_users,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(duration_ms) as avg_duration
                FROM messages
                GROUP BY date(timestamp);

                CREATE VIEW IF NOT EXISTS user_stats AS
                SELECT
                    u.user_id,
                    u.telegram_username,
                    COUNT(DISTINCT s.session_id) as total_sessions,
                    COUNT(m.message_id) as total_messages,
                    SUM(m.cost) as total_cost,
                    MAX(m.timestamp) as last_activity
                FROM users u
                LEFT JOIN sessions s ON u.user_id = s.user_id
                LEFT JOIN messages m ON u.user_id = m.user_id
                GROUP BY u.user_id;
                """,
            ),
            (
                3,
                """
                -- Agentic platform tables

                -- Scheduled jobs for recurring agent tasks
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    cron_expression TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    target_chat_ids TEXT DEFAULT '',
                    working_directory TEXT NOT NULL,
                    skill_name TEXT,
                    created_by INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                -- Webhook events for deduplication and audit
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    delivery_id TEXT UNIQUE,
                    payload JSON,
                    processed BOOLEAN DEFAULT FALSE,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_webhook_events_delivery
                    ON webhook_events(delivery_id);
                CREATE INDEX IF NOT EXISTS idx_webhook_events_provider
                    ON webhook_events(provider, received_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_active
                    ON scheduled_jobs(is_active);

                -- Enable WAL mode for better concurrent write performance
                PRAGMA journal_mode=WAL;
                """,
            ),
            (
                4,
                """
                -- Project thread mapping for strict forum-topic routing
                CREATE TABLE IF NOT EXISTS project_threads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_slug TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_thread_id INTEGER NOT NULL,
                    topic_name TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, project_slug),
                    UNIQUE(chat_id, message_thread_id)
                );

                CREATE INDEX IF NOT EXISTS idx_project_threads_chat_active
                    ON project_threads(chat_id, is_active);
                CREATE INDEX IF NOT EXISTS idx_project_threads_slug
                    ON project_threads(project_slug);
                """,
            ),
            (
                5,
                """
                -- Session token tracking (per-turn)
                CREATE TABLE IF NOT EXISTS session_tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cumulative_tokens INTEGER NOT NULL DEFAULT 0,
                    cost REAL DEFAULT 0.0,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_session_tracking_session
                    ON session_tracking(session_id);
                CREATE INDEX IF NOT EXISTS idx_session_tracking_timestamp
                    ON session_tracking(timestamp);

                -- Memory extraction records
                CREATE TABLE IF NOT EXISTS memory_extractions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    raw_response TEXT,
                    parsed_insights JSON,
                    validation_status TEXT NOT NULL DEFAULT 'pending',
                    target_files JSON,
                    error_message TEXT,
                    cumulative_tokens_at_run INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_extractions_session
                    ON memory_extractions(session_id);
                CREATE INDEX IF NOT EXISTS idx_memory_extractions_timestamp
                    ON memory_extractions(timestamp);
                """,
            ),
            (
                6,
                """
                -- Backend-portable conversation windows (per user+working_dir)
                ALTER TABLE messages ADD COLUMN window_id INTEGER;

                CREATE TABLE IF NOT EXISTS conversation_windows (
                    user_id INTEGER NOT NULL,
                    working_dir TEXT NOT NULL,
                    window_id INTEGER NOT NULL DEFAULT 1,
                    active_backend TEXT NOT NULL DEFAULT 'claude',
                    cumulative_tokens INTEGER NOT NULL DEFAULT 0,
                    last_eval_checkpoint INTEGER NOT NULL DEFAULT 0,
                    last_checkpoint_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, working_dir)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_window_id
                    ON messages(window_id);
                """,
            ),
            (
                7,
                """
                -- Per-user persistent selection (profile + backend/model)
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id        INTEGER PRIMARY KEY,
                    active_profile TEXT,
                    agent_backend  TEXT,
                    agent_model    TEXT,
                    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
            ),
            (
                8,
                """
                -- Drop the stale session_id->sessions FK on memory_extractions.
                -- BPC keys evaluator runs by a synthetic window id
                -- (user:working_dir:window), which is not a row in `sessions`,
                -- so the FK rejected every audit insert. Rebuild without it
                -- (keep the user_id FK).
                CREATE TABLE memory_extractions_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    raw_response TEXT,
                    parsed_insights JSON,
                    validation_status TEXT NOT NULL DEFAULT 'pending',
                    target_files JSON,
                    error_message TEXT,
                    cumulative_tokens_at_run INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                INSERT INTO memory_extractions_new
                    SELECT id, session_id, user_id, prompt_hash, raw_response,
                           parsed_insights, validation_status, target_files,
                           error_message, cumulative_tokens_at_run, timestamp
                    FROM memory_extractions;
                DROP TABLE memory_extractions;
                ALTER TABLE memory_extractions_new RENAME TO memory_extractions;
                CREATE INDEX IF NOT EXISTS idx_memory_extractions_session
                    ON memory_extractions(session_id);
                CREATE INDEX IF NOT EXISTS idx_memory_extractions_timestamp
                    ON memory_extractions(timestamp);
                """,
            ),
            (
                9,
                """
                -- DB-backed agent memory (replaces evaluator file tree).
                -- agent_id-only scope (single-user);
                -- multi-user later = ADD COLUMN user_id.
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id    TEXT NOT NULL,
                    tier        TEXT NOT NULL DEFAULT 'general',  -- 'pinned'|'general'
                    label       TEXT,                            -- optional grouping
                    content     TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'active',   -- 'active'|'superseded'
                    source      TEXT,                            -- 'evaluator'|'manual'
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_memory_facts_agent
                    ON memory_facts(agent_id, tier, status);

                -- FTS5 over fact content for general-tier keyword retrieval.
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts
                    USING fts5(content, content='memory_facts', content_rowid='id');
                CREATE TRIGGER IF NOT EXISTS memory_facts_ai
                AFTER INSERT ON memory_facts BEGIN
                    INSERT INTO memory_facts_fts(rowid, content)
                    VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_facts_ad
                AFTER DELETE ON memory_facts BEGIN
                    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_facts_au
                AFTER UPDATE ON memory_facts BEGIN
                    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                    INSERT INTO memory_facts_fts(rowid, content)
                    VALUES (new.id, new.content);
                END;
                """,
            ),
            (
                10,
                """
                -- Shared task board: any profile (or the user) can create
                -- tasks visible to all agents. assignee is a profile slug
                -- or 'user'.
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT NOT NULL,
                    body        TEXT,
                    created_by  TEXT NOT NULL DEFAULT 'user',
                    assignee    TEXT,
                    due_at      TIMESTAMP,
                    status      TEXT NOT NULL DEFAULT 'open',  -- 'open'|'done'
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_status
                    ON agent_tasks(status, due_at);
                """,
            ),
            (
                11,
                """
                -- Users & families v1: DB-backed bot access + family grouping.
                -- Owners still bootstrap from ALLOWED_USERS env; this table
                -- holds runtime-managed users (guests) and family membership.
                CREATE TABLE IF NOT EXISTS families (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by  INTEGER NOT NULL,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS bot_users (
                    telegram_id INTEGER PRIMARY KEY,
                    role        TEXT NOT NULL DEFAULT 'guest',  -- 'owner'|'guest'
                    family_id   INTEGER REFERENCES families(id),
                    added_by    INTEGER,
                    source      TEXT NOT NULL DEFAULT 'admin',  -- 'env'|'admin'|'family'
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_bot_users_family
                    ON bot_users(family_id);
                """,
            ),
            (
                12,
                """
                -- Track the pinned "switched profile" confirmation message so
                -- the next switch can unpin exactly that one (leaving any
                -- other pins the user made untouched) before pinning the new
                -- confirmation.
                ALTER TABLE user_preferences
                    ADD COLUMN pinned_switch_message_id INTEGER;
                """,
            ),
            (
                13,
                """
                -- dv-9f31. Two fixes to conversation windows:
                --
                -- 1. `cumulative_tokens` is a running SUM of per-turn tokens,
                --    but the thing the rotation threshold cares about is a
                --    LEVEL: how big the conversation is right now. Add the
                --    level as its own column instead of overloading the sum.
                -- 2. The roll's "start a fresh backend session next turn"
                --    intent lived in PTB user_data (in-memory, no
                --    persistence), so a service restart between the roll and
                --    the user's next message silently resumed the session the
                --    bot had just retired. Park it next to the window it
                --    belongs to.
                ALTER TABLE conversation_windows
                    ADD COLUMN last_context_tokens INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE conversation_windows
                    ADD COLUMN pending_seed_window INTEGER;
                ALTER TABLE conversation_windows
                    ADD COLUMN force_new_session INTEGER NOT NULL DEFAULT 0;
                """,
            ),
            (
                14,
                """
                -- dv-r3c1. FTS5 index over raw messages for the
                -- `recall_conversation` agent tool: the conversation is
                -- infinite, the context window is not, so the agent needs a
                -- way to search what rotated out. External-content table
                -- (mirrors memory_facts_fts): the FTS index stores only the
                -- searchable text, `messages` stays the source of truth.
                --
                -- prefix='2 3 4' + query-time trailing '*' (see
                -- MessageRepository._fts_prefix_query) compensate for
                -- unicode61 having no Cyrillic stemmer -- обед/обеда/обедом
                -- are distinct tokens otherwise.
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    prompt, response,
                    content='messages', content_rowid='message_id',
                    prefix='2 3 4'
                );
                CREATE TRIGGER IF NOT EXISTS messages_fts_ai
                AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, prompt, response)
                    VALUES (new.message_id, new.prompt, COALESCE(new.response, ''));
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_ad
                AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, prompt, response)
                    VALUES ('delete', old.message_id, old.prompt, COALESCE(old.response, ''));
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_au
                AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, prompt, response)
                    VALUES ('delete', old.message_id, old.prompt, COALESCE(old.response, ''));
                    INSERT INTO messages_fts(rowid, prompt, response)
                    VALUES (new.message_id, new.prompt, COALESCE(new.response, ''));
                END;

                -- One-time backfill. This migration itself only ever runs
                -- once per database (guarded by schema_version), so this
                -- INSERT cannot double-index existing rows on a later boot.
                INSERT INTO messages_fts(rowid, prompt, response)
                    SELECT message_id, prompt, COALESCE(response, '') FROM messages;
                """,
            ),
            (
                15,
                """
                -- dv-8254. Fact lifecycle: two kinds, metadata-only
                -- invalidation. A fact is an atomic verbatim dated statement;
                -- when state changes the old row is CLOSED (invalid_at +
                -- superseded_by), never rewritten -- the supersede chain is
                -- the history. `status` stays maintained ('active' <=>
                -- invalid_at IS NULL) so existing readers keep working while
                -- the epic lands; new readers use invalid_at IS NULL.
                --
                -- SQLite forbids ADD COLUMN with a non-constant default, so
                -- timestamps are NULL-default + explicit backfill; new rows
                -- get them from the repository.
                ALTER TABLE memory_facts ADD COLUMN kind TEXT NOT NULL
                    DEFAULT 'user_fact';  -- 'user_fact'|'conversation_result'
                ALTER TABLE memory_facts ADD COLUMN superseded_by INTEGER
                    REFERENCES memory_facts(id);
                ALTER TABLE memory_facts ADD COLUMN valid_from TIMESTAMP;
                ALTER TABLE memory_facts ADD COLUMN invalid_at TIMESTAMP;
                ALTER TABLE memory_facts ADD COLUMN recorded_at TIMESTAMP;
                -- Workspace file holding the full text of a
                -- conversation_result (the injected pointer targets it).
                ALTER TABLE memory_facts ADD COLUMN source_ref TEXT;
                -- Safety conflicts keep BOTH rows active, tagged with the
                -- same group value (lowest fact id of the pair, as text) so
                -- one predicate fetches all conflicted rows and GROUP BY
                -- pairs them. Extensible past pairs.
                ALTER TABLE memory_facts ADD COLUMN conflict_group TEXT;
                -- Who closed the row ('evaluator'|'agent_tool'|
                -- 'user_request'|'consolidation'|...): the history view's
                -- "who decided this stopped being true".
                ALTER TABLE memory_facts ADD COLUMN invalidated_by TEXT;

                -- Deterministic backfill. Kind is a heuristic (>800 ch =
                -- research/diagnosis blob, demoted to pointer-only), applied
                -- live by owner decision D3; the dv-4c6f owner-reviewed pass
                -- corrects individual rows afterwards.
                UPDATE memory_facts
                    SET valid_from = created_at, recorded_at = created_at;
                UPDATE memory_facts SET invalid_at = updated_at
                    WHERE status = 'superseded';
                UPDATE memory_facts SET kind = 'conversation_result'
                    WHERE length(content) > 800;

                CREATE INDEX IF NOT EXISTS idx_memory_facts_kind
                    ON memory_facts(agent_id, kind, invalid_at);
                """,
            ),
            (
                16,
                """
                -- dv-7783. Ledger of every Telegram message THIS BOT sent,
                -- keyed by (chat, telegram message id). When a user replies
                -- to one of our messages, the quoted message resolves here:
                -- an agent turn reply (joinable to `messages` via
                -- messages_message_id) vs a system notification (evaluator
                -- notify, scheduler output) that the agent never wrote.
                -- Without this, the reply-context envelope cannot attribute
                -- origin, and text like "Memory updated: 2 fact(s)" gets
                -- fed back to the agent as if it had said it.
                CREATE TABLE IF NOT EXISTS sent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id    INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,            -- 'turn_reply'|'system_notify'
                    messages_message_id INTEGER,   -- messages.message_id or NULL
                    profile_slug TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sent_messages_tg
                    ON sent_messages(telegram_chat_id, telegram_message_id);
                """,
            ),
            (
                17,
                """
                -- dv-5e2a. Frozen per-window context bundle: the exact system
                -- prompt injected on EVERY turn of a conversation window,
                -- materialized once (get-or-create on first turn) and keyed
                -- like conversation_windows -- window_id is only unique
                -- within a (user_id, working_dir) pair. Persisting the text
                -- (not just metadata) is what makes freezing survive a
                -- restart: reload, never recompose, or a mid-window persona
                -- edit + restart would mutate the live prompt. Old windows'
                -- rows are kept as an audit trail.
                CREATE TABLE IF NOT EXISTS window_bundles (
                    user_id       INTEGER NOT NULL,
                    working_dir   TEXT NOT NULL,
                    window_id     INTEGER NOT NULL,
                    agent_id      TEXT NOT NULL,
                    bundle_text   TEXT NOT NULL,
                    bundle_sha256 TEXT NOT NULL,
                    persona_chars INTEGER NOT NULL,
                    facts_count   INTEGER NOT NULL,
                    pointers_count INTEGER NOT NULL,
                    family_count  INTEGER NOT NULL,
                    total_chars   INTEGER NOT NULL,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, working_dir, window_id)
                );
                """,
            ),
            (
                18,
                """
                -- dv-c5d9. Per-run breakdown of what the evaluator pipeline
                -- actually did: {found, already_saved, added, superseded,
                -- invalidated, conflicts_flagged, dropped_duplicate,
                -- slice_saved_matches, reconcile, verdicts}. This is what
                -- makes the user-facing "N found — X already saved, Y new"
                -- report checkable by query instead of a claim (criterion 4;
                -- same honesty class as dv-7783). Timeout failures carry an
                -- error_message prefixed 'timeout' for the same reason.
                ALTER TABLE memory_extractions ADD COLUMN op_stats JSON;
                """,
            ),
            (
                19,
                """
                -- Verbatim scheduled reminders. 'agent' = fire an LLM turn
                -- with the stored prompt (original behavior); 'message' =
                -- deliver the stored text to Telegram as-is, no LLM turn.
                ALTER TABLE scheduled_jobs ADD COLUMN delivery TEXT NOT NULL
                    DEFAULT 'agent';

                -- One-shot notes injected into the NEXT agent turn in a
                -- workspace (e.g. "a scheduled reminder was delivered while
                -- you weren't looking"). Consumed (deleted) on read; keyed
                -- like conversation_windows so a note armed for coach can
                -- never leak into a chef turn.
                CREATE TABLE IF NOT EXISTS pending_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    working_dir TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_pending_notes_scope
                    ON pending_notes(user_id, working_dir);
                """,
            ),
            (
                20,
                """
                -- Session watchdog holds (dv-8bb5): a temporary suspension
                -- applied by an admin from the watchdog alert's hold
                -- buttons. Separate from bot_users (the auth whitelist) on
                -- purpose -- that table is on the hot path for every
                -- message's identity check, and mixing in a second concern
                -- (temporary block) risks regressing it later. One row per
                -- user; a new hold overwrites the old one.
                CREATE TABLE IF NOT EXISTS user_holds (
                    user_id INTEGER PRIMARY KEY,
                    held_until TIMESTAMP NOT NULL,
                    reason TEXT,
                    set_by INTEGER,
                    set_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """,
            ),
        ]

    async def _new_connection(self) -> aiosqlite.Connection:
        """Open a configured aiosqlite connection on a daemon thread.

        The connection backing thread is marked daemon (set before the thread
        starts, on await) so a process can always exit even if close() is
        missed -- e.g. tests that forget teardown, or an ungraceful shutdown.
        SQLite keeps committed data durable and rolls back incomplete txns.
        """
        conn = aiosqlite.connect(
            self.database_path, detect_types=sqlite3.PARSE_DECLTYPES
        )
        conn.daemon = True  # must be set before the thread starts (on await)
        conn = await conn
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def _init_pool(self):
        """Initialize connection pool."""
        logger.info("Initializing connection pool", size=self._pool_size)

        async with self._pool_lock:
            for _ in range(self._pool_size):
                self._connection_pool.append(await self._new_connection())

    @asynccontextmanager
    async def get_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get database connection from pool."""
        async with self._pool_lock:
            if self._connection_pool:
                conn = self._connection_pool.pop()
            else:
                conn = await self._new_connection()

        try:
            yield conn
        finally:
            async with self._pool_lock:
                if len(self._connection_pool) < self._pool_size:
                    self._connection_pool.append(conn)
                else:
                    await conn.close()

    async def close(self):
        """Close all connections in pool."""
        logger.info("Closing database connections")

        async with self._pool_lock:
            for conn in self._connection_pool:
                await conn.close()
            self._connection_pool.clear()

    async def health_check(self) -> bool:
        """Check database health."""
        try:
            async with self.get_connection() as conn:
                await conn.execute("SELECT 1")
                return True
        except Exception as e:
            logger.error("Database health check failed", error=str(e))
            return False
