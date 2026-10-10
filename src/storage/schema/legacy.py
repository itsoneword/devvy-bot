"""FROZEN pre-Alembic schema history (dv-5e4f Phase 1, owner decision 4).

Do not edit. This is the exact DDL the bot shipped before Alembic, kept for
ONE OSS release so a pre-Alembic install can be caught up and then stamped:

- bot.db / data.db: the ``schema_version`` migration chains (bot 1..26,
  data 1..4), replayed from the DB's recorded version.
- llm_analytics.db / channel_intel.db: no version table; their idempotent
  ``CREATE ... IF NOT EXISTS`` init (+ llm_analytics' quality_daily column
  sniff) is replayed as-is.

``src.storage.schema.migrate.ensure_schema`` runs this inside its
``BEGIN IMMEDIATE`` transaction, verifies the result is structurally equal to
the Alembic baseline, then stamps ``alembic_version``. New schema changes go
in ``migrations/<db>/versions/`` -- never here. Delete this module (and the
bridge branch in ``migrate.py``) one release after the Alembic cutover.
"""

import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

BOT_INITIAL_SCHEMA = """
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


BOT_MIGRATIONS: List[Tuple[int, str]] = [
    (1, BOT_INITIAL_SCHEMA),
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
    (
        21,
        """
                -- Session deactivation audit (dv-2a5a). is_active=FALSE is a
                -- soft delete that retires a resume mapping; before this it
                -- left no trace of when or why. On 2026-09-10 a live session
                -- went inactive with no log line and no known writer, and
                -- nothing in the schema could answer the question. Every
                -- writer now stamps both columns.
                ALTER TABLE sessions ADD COLUMN deactivated_at TIMESTAMP;
                ALTER TABLE sessions ADD COLUMN deactivated_reason TEXT;
                """,
    ),
    (
        22,
        """
                -- dv-ebde / dv-2a61: Codex thread bookkeeping moves off
                -- data/codex_sessions.json onto the session row it describes.
                -- backend: NULL = Claude (every row before this migration),
                -- 'openai_codex' = a Codex thread. thread_head = 1: the JSON
                -- entry's pointer, the one Codex thread per (user, dir) to
                -- resume. thread_total_*: the running totals
                -- `turn.completed` reported at the end of the previous turn,
                -- so the next turn can take its delta. cumulative_tokens:
                -- summed per-turn flow. See src/storage/codex_sessions.py.
                -- Additive only: existing rows get NULL / 0.
                ALTER TABLE sessions ADD COLUMN backend TEXT;
                ALTER TABLE sessions ADD COLUMN thread_head
                    INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE sessions ADD COLUMN thread_total_input
                    INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE sessions ADD COLUMN thread_total_output
                    INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE sessions ADD COLUMN cumulative_tokens
                    INTEGER NOT NULL DEFAULT 0;
                CREATE INDEX IF NOT EXISTS idx_sessions_user_project
                    ON sessions(user_id, project_path);

                -- The one live status message NotificationService keeps
                -- pinned per (chat, pin_as), replacing
                -- data/pinned_messages.json. Without it a restart orphans the
                -- pin and the next job pins a second one.
                CREATE TABLE IF NOT EXISTS pinned_messages (
                    chat_id INTEGER NOT NULL,
                    pin_as TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, pin_as)
                );
                """,
    ),
    (
        23,
        """
                -- dv-752a phase A. Family members are stored, not inferred:
                -- a display name and a free-text relation per bot user, and
                -- is_admin for the family creator (admin = family membership
                -- + bot-wide settings; separate from role, both people are
                -- role=owner). display_name is filled from Telegram's
                -- first_name on the member's next message when NULL.
                ALTER TABLE bot_users ADD COLUMN display_name TEXT;
                ALTER TABLE bot_users ADD COLUMN relation TEXT;
                ALTER TABLE bot_users ADD COLUMN is_admin INTEGER NOT NULL
                    DEFAULT 0;
                UPDATE bot_users SET is_admin = 1
                    WHERE telegram_id IN (SELECT created_by FROM families);

                -- ONE source of truth for todos/reminders, visible from
                -- both the creator's and the assignee's side. Times are
                -- ISO-8601 TEXT (UTC instants); recurrence is a 5-field cron
                -- read in `tz` so it does not drift at DST changes.
                -- status: open | fired (one-shot delivered) | done |
                -- cancelled. deliver: message (verbatim) | agent (a turn in
                -- the assignee's workspace) | none (plain todo).
                CREATE TABLE IF NOT EXISTS items (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_id      INTEGER,
                    creator_id     INTEGER NOT NULL,
                    assignee_id    INTEGER NOT NULL,
                    text           TEXT NOT NULL,
                    status         TEXT NOT NULL DEFAULT 'open',
                    due_at         TEXT,
                    recurrence     TEXT,
                    tz             TEXT NOT NULL DEFAULT 'UTC',
                    ends_at        TEXT,
                    deliver        TEXT NOT NULL DEFAULT 'message',
                    profile_slug   TEXT,
                    source_profile TEXT,
                    last_fired_at  TEXT,
                    done_at        TEXT,
                    done_by        INTEGER,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_items_assignee
                    ON items(assignee_id, status, due_at);
                CREATE INDEX IF NOT EXISTS idx_items_creator
                    ON items(creator_id, status);

                -- A job fires an item: the handler reads the LIVE item (text
                -- edits need no re-register) and runs as its assignee.
                -- timezone/ends_at let any cron job run in local time.
                ALTER TABLE scheduled_jobs ADD COLUMN item_id INTEGER;
                ALTER TABLE scheduled_jobs ADD COLUMN timezone TEXT;
                ALTER TABLE scheduled_jobs ADD COLUMN ends_at TEXT;
                CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_item
                    ON scheduled_jobs(item_id);

                -- Turn-start injection cursor: item activity newer than
                -- seen_at is injected into the next turn of that agent.
                CREATE TABLE IF NOT EXISTS item_seen (
                    user_id      INTEGER NOT NULL,
                    profile_slug TEXT NOT NULL,
                    seen_at      TEXT NOT NULL,
                    PRIMARY KEY (user_id, profile_slug)
                );
                """,
    ),
    (
        24,
        """
                -- dv-5f95 phase 1. deadline = when the item is DUE (UTC ISO
                -- instant, like the other times); due_at/recurrence stay the
                -- reminder time, so "due Friday, nudge Wednesday" fits.
                ALTER TABLE items ADD COLUMN deadline TEXT;

                -- Comments, research results and status changes attached to
                -- an item. kind: comment | research | status_change. body is
                -- short text (long content lives in a file, `path` points at
                -- it). author_id NULL = automated note. source: manual | auto.
                CREATE TABLE IF NOT EXISTS item_notes (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id    INTEGER NOT NULL REFERENCES items(id),
                    author_id  INTEGER,
                    kind       TEXT NOT NULL,
                    body       TEXT NOT NULL,
                    path       TEXT,
                    source     TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_item_notes_item
                    ON item_notes(item_id, created_at);
                """,
    ),
    (
        25,
        """
                -- dv-11a5. One row per model call, written by the model
                -- router (src/model_router). kind: turn (tools, session) |
                -- plain (prompt in, text out). harness: claude |
                -- openai_codex. harness_version: the CLI's own version string
                -- at call time (the CLIs update themselves). cost_usd NULL =
                -- the harness reports no USD figure. outcome: ok | error |
                -- timeout. attended = 0 marks runs nobody is watching.
                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    purpose TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    harness_version TEXT,
                    provider TEXT NOT NULL,
                    model TEXT,
                    attended INTEGER NOT NULL DEFAULT 1,
                    profile TEXT,
                    user_id INTEGER,
                    session_id TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_model_calls_ts
                    ON model_calls(ts);
                CREATE INDEX IF NOT EXISTS idx_model_calls_purpose
                    ON model_calls(purpose, ts);
                """,
    ),
    (
        26,
        """
                -- dv-5e4f Phase 0. lifecycle_events previously lived outside
                -- the migration chain entirely: src/observability/lifecycle.py
                -- ran this exact CREATE TABLE IF NOT EXISTS itself, on every
                -- boot, via its own ensure_schema(). Moving it here gives the
                -- table one owner (this migration chain) before Phase 1
                -- (SQLAlchemy models + Alembic) needs every table to have
                -- exactly one. Schema is byte-identical to the old _SCHEMA.
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                """,
    ),
]


# data.db schema v1 (dv-5f3a plan §3). String columns stay TEXT so the CSV
# migration is lossless: train reps holds range strings like "10-12" — never
# fabricate per-set numbers. imported_from = source CSV path (NULL = live MCP
# write); it is both the verification key and the revert lever.
USER_DATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs_sleep (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    bed TEXT, wake TEXT,
    sleep_min REAL, onset_min REAL, score REAL, hrv REAL, rhr REAL,
    weight_kg REAL, wellbeing TEXT, alcohol_u REAL, thc TEXT,
    room_temp REAL, out_night REAL, out_day REAL,
    imported_from TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(user_id, date)
);

CREATE TABLE IF NOT EXISTS logs_food (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    meal TEXT NOT NULL,
    time TEXT,
    kcal REAL, protein REAL, fat REAL, carb REAL,
    confidence TEXT CHECK (confidence IN ('high','est') OR confidence IS NULL),
    source TEXT CHECK (source IN ('photo','text') OR source IS NULL),
    imported_from TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_food_user_date
    ON logs_food(user_id, date);

CREATE TABLE IF NOT EXISTS logs_train (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    session TEXT,
    exercise TEXT NOT NULL,
    set_n TEXT, weight_kg TEXT, reps TEXT, rpe REAL,
    imported_from TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_train_user_date
    ON logs_train(user_id, date);
CREATE INDEX IF NOT EXISTS idx_logs_train_user_exercise
    ON logs_train(user_id, exercise);

CREATE TABLE IF NOT EXISTS logs_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    duration_min REAL,
    intensity TEXT,
    imported_from TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_activity_user_date
    ON logs_activity(user_id, date);
-- kcal_active/kcal_total/exercise_min/stand_h/steps/distance_km land with
-- migration 3 below (dv-63df) — Apple Watch ring data has no home in v1.

-- Shared across users BY DESIGN (data scope, not access control): nutrition
-- data is universal. provenance is the load-bearing column — an estimate
-- must never harden into a stated fact (dv-7a3c/dv-1106 failure class).
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('product','dish')),
    kcal_100g REAL, protein REAL, fat REAL, carb REAL, salt REAL,
    provenance TEXT NOT NULL
        CHECK (provenance IN ('label_photo','official','estimate')),
    confirmed_on TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(name, brand)
);

CREATE TABLE IF NOT EXISTS portions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    phrase TEXT NOT NULL,
    grams REAL NOT NULL,
    learned_from TEXT,
    updated_on TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(user_id, phrase)
);

-- Consumers land with dv-b9c7 (issue_open/issue_update/issue_list tools);
-- the table ships here so the CSV/issues.md migration has a target.
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','RESOLVED','CHRONIC')),
    started_on TEXT, resolved_on TEXT,
    symptom TEXT, restrictions TEXT, escalation TEXT, notes TEXT,
    imported_from TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_user_status
    ON issues(user_id, status);

-- Free-form prose, verbatim (notes/YYYY-MM.md, program.md migrate here;
-- single storage — owner decision 3, files frozen after verification).
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('note','program')),
    period TEXT,
    title TEXT,
    content TEXT NOT NULL,
    imported_from TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_user_kind
    ON docs(user_id, kind, period);

PRAGMA journal_mode=WAL;
"""


# Migration 2: drop the confidence/source CHECKs on logs_food. The plan
# specced them as enums, but the real CSVs disagree — confidence has 'low'
# (owner scope) and the wife's source column holds free-text meal composition
# notes, which are worth keeping verbatim (lossless migration beats a tidy
# enum; the CHECK failed the first real --apply). SQLite cannot drop a CHECK,
# so: rebuild + copy + rename (same pattern as bot.db migration 8).
LOGS_FOOD_RELAX_CHECKS = """
CREATE TABLE logs_food_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    meal TEXT NOT NULL,
    time TEXT,
    kcal REAL, protein REAL, fat REAL, carb REAL,
    confidence TEXT,
    source TEXT,
    imported_from TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO logs_food_new
    SELECT id, user_id, date, meal, time, kcal, protein, fat, carb,
           confidence, source, imported_from, created_at, updated_at
    FROM logs_food;
DROP TABLE logs_food;
ALTER TABLE logs_food_new RENAME TO logs_food;
CREATE INDEX IF NOT EXISTS idx_logs_food_user_date
    ON logs_food(user_id, date);
"""

# Migration 3 (dv-c388): provenance is a required column, not a convention.
# Coach fabricated 5 "backfill" activity rows indistinguishable from real
# data at query time — the only disclosure lived in a free-text `type`
# string that no aggregate reads. `source` mirrors `products.provenance`:
# NOT NULL, enum-checked, no default, so a write can never skip choosing it.
# Existing rows backfill to 'user_reported' (a human vouched for the number
# existing) — never 'measured', which would be the same overclaim this
# migration exists to stop. SQLite can't add a NOT NULL/CHECK column to a
# populated table, so: rebuild + copy + rename (same pattern as migration 2).
LOGS_ACTIVITY_ADD_SOURCE = """
CREATE TABLE logs_activity_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    duration_min REAL,
    intensity TEXT,
    source TEXT NOT NULL
        CHECK (source IN ('user_reported','measured','estimated')),
    imported_from TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO logs_activity_new
    SELECT id, user_id, date, type, duration_min, intensity,
           'user_reported', imported_from, created_at, updated_at
    FROM logs_activity;
DROP TABLE logs_activity;
ALTER TABLE logs_activity_new RENAME TO logs_activity;
CREATE INDEX IF NOT EXISTS idx_logs_activity_user_date
    ON logs_activity(user_id, date);
"""


# Migration 4 (dv-63df): the dv-3668 CSV migration only read activity.csv
# (type/duration/intensity), so Apple Watch ring data — Move/Exercise/Stand,
# steps, distance, day total — that lived in training_log.md prose had no
# column to land in and was dropped. All nullable, no default: empty stays
# empty (see module docstring), and SQLite forbids ADD COLUMN with a
# non-constant default anyway.
LOGS_ACTIVITY_WATCH_FIELDS = """
ALTER TABLE logs_activity ADD COLUMN kcal_active REAL;
ALTER TABLE logs_activity ADD COLUMN kcal_total REAL;
ALTER TABLE logs_activity ADD COLUMN exercise_min REAL;
ALTER TABLE logs_activity ADD COLUMN stand_h REAL;
ALTER TABLE logs_activity ADD COLUMN steps REAL;
ALTER TABLE logs_activity ADD COLUMN distance_km REAL;
"""


DATA_MIGRATIONS: List[Tuple[int, str]] = [
    (1, USER_DATA_SCHEMA),
    (2, LOGS_FOOD_RELAX_CHECKS),
    (3, LOGS_ACTIVITY_ADD_SOURCE),
    (4, LOGS_ACTIVITY_WATCH_FIELDS),
]


LLM_ANALYTICS_SCHEMA = """
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
    user_messages INTEGER NOT NULL DEFAULT 0,
    recipe_turns INTEGER NOT NULL DEFAULT 0,
    recipe_turns_unsearched INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_quality_daily_date ON quality_daily(date);
CREATE TABLE IF NOT EXISTS bot_windows (
    user_id INTEGER NOT NULL, working_dir TEXT NOT NULL, window_id INTEGER NOT NULL,
    active_backend TEXT, cumulative_tokens INTEGER, last_eval_checkpoint INTEGER,
    last_checkpoint_message_id INTEGER, updated_at TEXT,
    PRIMARY KEY (user_id, working_dir)
);
CREATE TABLE IF NOT EXISTS answer_scores (
    message_id INTEGER NOT NULL,
    profile TEXT,
    user_id INTEGER,
    ts TEXT,
    judge_model TEXT NOT NULL,
    rubric_version INTEGER NOT NULL,
    scores_json TEXT NOT NULL,
    total REAL NOT NULL,
    rationale TEXT,
    flags TEXT,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (message_id, judge_model, rubric_version)
);
CREATE INDEX IF NOT EXISTS idx_answer_scores_profile_ts
    ON answer_scores(profile, ts);
"""

# Columns added to quality_daily after its initial release (dv-37e9). A
# fresh DB gets them from CREATE TABLE above; an existing one needs ALTER
# TABLE, which SQLite has no "IF NOT EXISTS" spelling for.
LLM_ANALYTICS_QUALITY_DAILY_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("recipe_turns", "INTEGER NOT NULL DEFAULT 0"),
    ("recipe_turns_unsearched", "INTEGER NOT NULL DEFAULT 0"),
)


CHANNEL_INTEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    name TEXT NOT NULL,
    type TEXT,
    location TEXT,
    lat REAL, lon REAL,
    travel_time_from_home TEXT,
    price TEXT,
    opening_hours TEXT,
    features TEXT,
    status TEXT NOT NULL DEFAULT 'idea',
    liked INTEGER,
    my_notes TEXT,
    source TEXT,
    enriched_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS place_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    place_id INTEGER,
    file_path TEXT NOT NULL,
    caption TEXT,
    tags TEXT,
    vision_model TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    FOREIGN KEY (place_id) REFERENCES places(id)
);
CREATE TABLE IF NOT EXISTS place_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    source_channel TEXT,
    text TEXT,
    image_refs TEXT,
    tags TEXT,
    notes TEXT,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_places (
    reference_id INTEGER NOT NULL,
    place_id INTEGER NOT NULL,
    PRIMARY KEY (reference_id, place_id),
    FOREIGN KEY (reference_id) REFERENCES place_references(id),
    FOREIGN KEY (place_id) REFERENCES places(id)
);
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    place_id INTEGER NOT NULL,
    date TEXT,
    activities TEXT,
    costs TEXT,
    narrative TEXT,
    photo_refs TEXT,
    created_at TIMESTAMP NOT NULL,
    FOREIGN KEY (place_id) REFERENCES places(id)
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    place_ids TEXT,
    body TEXT,
    selected_image_refs TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    owner TEXT PRIMARY KEY,
    liked_features TEXT,
    liked_place_ids TEXT,
    budget_norms TEXT,
    home_location TEXT,
    default_radius TEXT
);
CREATE TABLE IF NOT EXISTS kb_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    tg_user_id INTEGER, username TEXT, display_name TEXT, first_seen TIMESTAMP
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    user_id INTEGER, post_ref TEXT, text TEXT,
    topics TEXT, sentiment TEXT, created_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES kb_users(id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(name, my_notes, location, content='places', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5(caption, tags, content='place_images', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS refs_fts USING fts5(text, notes, content='place_references', content_rowid='id');
-- places_fts sync
CREATE TRIGGER IF NOT EXISTS places_ai AFTER INSERT ON places BEGIN
  INSERT INTO places_fts(rowid, name, my_notes, location)
  VALUES (new.id, new.name, new.my_notes, new.location);
END;
CREATE TRIGGER IF NOT EXISTS places_ad AFTER DELETE ON places BEGIN
  INSERT INTO places_fts(places_fts, rowid, name, my_notes, location)
  VALUES ('delete', old.id, old.name, old.my_notes, old.location);
END;
CREATE TRIGGER IF NOT EXISTS places_au AFTER UPDATE ON places BEGIN
  INSERT INTO places_fts(places_fts, rowid, name, my_notes, location)
  VALUES ('delete', old.id, old.name, old.my_notes, old.location);
  INSERT INTO places_fts(rowid, name, my_notes, location)
  VALUES (new.id, new.name, new.my_notes, new.location);
END;
-- images_fts sync
CREATE TRIGGER IF NOT EXISTS place_images_ai AFTER INSERT ON place_images BEGIN
  INSERT INTO images_fts(rowid, caption, tags)
  VALUES (new.id, new.caption, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS place_images_ad AFTER DELETE ON place_images BEGIN
  INSERT INTO images_fts(images_fts, rowid, caption, tags)
  VALUES ('delete', old.id, old.caption, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS place_images_au AFTER UPDATE ON place_images BEGIN
  INSERT INTO images_fts(images_fts, rowid, caption, tags)
  VALUES ('delete', old.id, old.caption, old.tags);
  INSERT INTO images_fts(rowid, caption, tags)
  VALUES (new.id, new.caption, new.tags);
END;
-- refs_fts sync
CREATE TRIGGER IF NOT EXISTS place_references_ai AFTER INSERT ON place_references BEGIN
  INSERT INTO refs_fts(rowid, text, notes)
  VALUES (new.id, new.text, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS place_references_ad AFTER DELETE ON place_references BEGIN
  INSERT INTO refs_fts(refs_fts, rowid, text, notes)
  VALUES ('delete', old.id, old.text, old.notes);
END;
CREATE TRIGGER IF NOT EXISTS place_references_au AFTER UPDATE ON place_references BEGIN
  INSERT INTO refs_fts(refs_fts, rowid, text, notes)
  VALUES ('delete', old.id, old.text, old.notes);
  INSERT INTO refs_fts(rowid, text, notes)
  VALUES (new.id, new.text, new.notes);
END;
"""


# --- frozen runner -----------------------------------------------------------

# Last legacy version per versioned DB: what the Alembic baseline reproduces.
LEGACY_HEAD = {"bot": 26, "data": 4}

VERSIONED: Dict[str, List[Tuple[int, str]]] = {
    "bot": BOT_MIGRATIONS,
    "data": DATA_MIGRATIONS,
}


def split_statements(script: str) -> List[str]:
    """Split a SQL script into single statements, trigger bodies intact.

    ``executescript`` (what the legacy runner used) COMMITs any open
    transaction first, which would break the runner's ``BEGIN IMMEDIATE``;
    so statements run one by one instead. ``PRAGMA journal_mode`` is dropped:
    it is a silent no-op inside a transaction, and ``migrate.py`` sets WAL on
    every DB before opening one.
    """
    out: List[str] = []
    buf = ""
    for ch in script:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())

    def code_of(stmt: str) -> str:
        return "\n".join(
            line for line in stmt.splitlines() if not line.strip().startswith("--")
        ).strip()

    return [
        s
        for s in out
        if code_of(s) not in ("", ";")
        and not re.match(r"(?i)^PRAGMA\s+journal_mode", code_of(s))
    ]


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    for stmt in split_statements(script):
        conn.execute(stmt)


def has_schema_version(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    return row is not None


def schema_version(conn: sqlite3.Connection) -> int:
    """Recorded legacy version, 0 when the table is missing or empty."""
    if not has_schema_version(conn):
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] else 0


def run_legacy(
    conn: sqlite3.Connection, db_name: str, upto: Optional[int] = None
) -> Tuple[int, int]:
    """Replay the frozen legacy schema history on ``conn``.

    bot/data: every migration above the recorded ``schema_version`` up to
    ``upto`` (default: the last one), recording each version as before.
    llm_analytics/channel_intel: the idempotent init. The CALLER owns the
    transaction. Returns (version before, version after); (0, 0) for the
    unversioned DBs.
    """
    if db_name in VERSIONED:
        # Same text the legacy runner used (sqlite_master keeps it verbatim).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ( version INTEGER PRIMARY KEY )"
        )
        before = schema_version(conn)
        after = before
        for version, script in VERSIONED[db_name]:
            if version > before and (upto is None or version <= upto):
                _run_script(conn, script)
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )
                after = version
        return before, after
    if db_name == "llm_analytics":
        _run_script(conn, LLM_ANALYTICS_SCHEMA)
        existing = {r[1] for r in conn.execute("PRAGMA table_info(quality_daily)")}
        for name, ddl in LLM_ANALYTICS_QUALITY_DAILY_ADDED_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE quality_daily ADD COLUMN {name} {ddl}")
        return 0, 0
    if db_name == "channel_intel":
        _run_script(conn, CHANNEL_INTEL_SCHEMA)
        return 0, 0
    raise ValueError(f"unknown db {db_name!r}")


def build_legacy(
    path: Union[str, Path], db_name: str, upto: Optional[int] = None
) -> None:
    """Build or advance a pre-Alembic DB file. For tests and the bridge
    proof only -- the app goes through ``migrate.ensure_schema``."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        run_legacy(conn, db_name, upto)
        conn.execute("COMMIT")
    finally:
        conn.close()
