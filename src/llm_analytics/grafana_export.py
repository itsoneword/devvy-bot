"""Restricted bot.db export for the Grafana console (dv-e005).

Grafana OSS has no per-datasource permissions: any Viewer can POST its own
rawSql to a datasource. So this file IS the scope limit for the devvy
datasource. It holds only ``model_calls``, ``tool_calls``,
``message_reactions`` and ``feedback_feature_cards``, and only the columns in the allowlists below — no free
text (``error``, ``input``, ``result_preview``), no ``user_id``, no raw chat
id. ``has_error`` replaces the error text; ``chat_label`` the chat id.

Runs in the existing bot.db mirror cycle (``Indexer.scan_once``, throttled
to :data:`EXPORT_INTERVAL_S`), not as a second sync job. The source is opened
strictly read-only; the output is built in a temp file next to the target and
swapped in with ``os.replace``, so Grafana never sees a half-written file.
Rollback journal, not WAL: Grafana mounts the dir read-only and could not
create ``-shm``/``-wal`` files.
"""

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

EXPORT_INTERVAL_S = 300.0

# (column, sqlite type, source expression). Source expressions only ever name
# allowlisted columns; error text collapses into a 0/1 flag.
_HAS_ERROR = "CASE WHEN COALESCE(error, '') = '' THEN 0 ELSE 1 END"

MODEL_CALLS_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY", "id"),
    ("ts", "TEXT", "ts"),
    ("purpose", "TEXT", "purpose"),
    ("kind", "TEXT", "kind"),
    ("harness", "TEXT", "harness"),
    ("harness_version", "TEXT", "harness_version"),
    ("provider", "TEXT", "provider"),
    ("model", "TEXT", "model"),
    ("attended", "INTEGER", "attended"),
    ("profile", "TEXT", "profile"),
    ("session_id", "TEXT", "session_id"),
    ("input_tokens", "INTEGER", "input_tokens"),
    ("output_tokens", "INTEGER", "output_tokens"),
    ("cache_read_tokens", "INTEGER", "cache_read_tokens"),
    ("cache_creation_tokens", "INTEGER", "cache_creation_tokens"),
    ("cost_usd", "REAL", "cost_usd"),
    ("duration_ms", "INTEGER", "duration_ms"),
    ("outcome", "TEXT", "outcome"),
    ("cost_source", "TEXT", "cost_source"),
    ("has_error", "INTEGER", _HAS_ERROR),
)

# tool_calls carries one ``status`` since dv-2260 (0007); the export keeps the
# ok / detached columns the dashboards query (NULL ok = never finished).
_OK = "CASE status WHEN 'ok' THEN 1 WHEN 'error' THEN 0 END"

TOOL_CALLS_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY", "id"),
    ("model_call_id", "INTEGER", "model_call_id"),
    ("session_id", "TEXT", "session_id"),
    ("seq", "INTEGER", "seq"),
    ("tool_use_id", "TEXT", "tool_use_id"),
    ("parent_tool_use_id", "TEXT", "parent_tool_use_id"),
    ("tool_name", "TEXT", "tool"),
    ("subagent_type", "TEXT", "subagent_type"),
    ("ok", "INTEGER", _OK),
    ("started_at", "TEXT", "started_at"),
    ("duration_ms", "INTEGER", "duration_ms"),
    ("result_bytes", "INTEGER", "result_bytes"),
    ("detached", "INTEGER", "(status = 'detached')"),
    ("has_error", "INTEGER", _HAS_ERROR),
)


def chat_label(chat_id: object) -> str:
    """``'group'`` for a group chat (negative id), else ``'private:'`` + a
    short stable hash: panels can tell chats apart without the raw id."""
    if not isinstance(chat_id, int):
        return "-"
    if chat_id < 0:
        return "group"
    return "private:" + hashlib.sha256(str(chat_id).encode()).hexdigest()[:8]


# Turn reactions as answer-quality labels (dv-e1a7) and the owner's feature
# feedback (dv-a42c). No user_id, no chat id, no raw ``comment`` (the
# transcript); the extracted short pros / cons / bugs_text do go out.
MESSAGE_REACTIONS_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("ts", "TEXT", "reacted_at"),
    ("profile", "TEXT", "profile_slug"),
    ("chat_label", "TEXT", "chat_label(telegram_chat_id)"),
    ("emoji", "TEXT", "emoji"),
    ("score", "INTEGER", "score"),
    ("messages_message_id", "INTEGER", "messages_message_id"),
    ("kind", "TEXT", "kind"),
    ("feature_id", "TEXT", "feature_id"),
    ("score_source", "TEXT", "score_source"),
    ("tested", "INTEGER", "tested"),
    ("has_bugs", "INTEGER", "has_bugs"),
    ("bugs_text", "TEXT", "bugs_text"),
    ("pros", "TEXT", "pros"),
    ("cons", "TEXT", "cons"),
    ("aspects", "TEXT", "aspects"),
    # kind='stage_score' (dv-3b99): a pump2 judge stage's score per criterion
    ("board", "TEXT", "board"),
    ("card_id", "TEXT", "card_id"),
    ("stage", "TEXT", "stage"),
    ("criterion", "TEXT", "criterion"),
    ("card_size", "TEXT", "card_size"),
    ("card_kind", "TEXT", "card_kind"),
)

# The cards of each rated feature and how each was done (dv-a42c).
FEEDBACK_FEATURE_CARDS_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("feature_id", "TEXT", "feature_id"),
    ("board", "TEXT", "board"),
    ("card_id", "TEXT", "card_id"),
    ("title", "TEXT", "title"),
    ("merge_sha", "TEXT", "merge_sha"),
    ("merged_at", "TEXT", "merged_at"),
    ("exec_mode", "TEXT", "exec_mode"),
    ("stages", "TEXT", "stages"),
    ("returns", "INTEGER", "returns"),
)

_TABLES = {
    "model_calls": MODEL_CALLS_COLUMNS,
    "tool_calls": TOOL_CALLS_COLUMNS,
    "message_reactions": MESSAGE_REACTIONS_COLUMNS,
    "feedback_feature_cards": FEEDBACK_FEATURE_CARDS_COLUMNS,
}

# Panels filter and group by time; tool panels join on model_call_id.
_INDEXES = (
    "CREATE INDEX idx_model_calls_ts ON model_calls(ts)",
    "CREATE INDEX idx_tool_calls_started ON tool_calls(started_at)",
    "CREATE INDEX idx_tool_calls_model_call ON tool_calls(model_call_id)",
    "CREATE INDEX idx_message_reactions_ts ON message_reactions(ts)",
    "CREATE INDEX idx_feedback_cards_feature ON feedback_feature_cards(feature_id)",
)


def export_usage(bot_db_path: Path, out_path: Path) -> bool:
    """Write ``out_path`` from ``bot_db_path``. Returns False (and writes
    nothing) if bot.db or the output dir is absent: no Grafana installed, or
    a fresh box — an empty export would make panels silently show zero."""
    if not bot_db_path.exists():
        logger.warning("grafana_export_no_bot_db", path=str(bot_db_path))
        return False
    if not out_path.parent.is_dir():
        logger.debug("grafana_export_no_dir", path=str(out_path.parent))
        return False

    src = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
    src.create_function("chat_label", 1, chat_label, deterministic=True)
    try:
        rows = {
            table: src.execute(
                f"SELECT {', '.join(expr for _, _, expr in cols)} FROM {table}"
            ).fetchall()
            for table, cols in _TABLES.items()
        }
    finally:
        src.close()

    fd, tmp_name = tempfile.mkstemp(
        dir=out_path.parent, prefix=f".{out_path.name}.", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        dst = sqlite3.connect(tmp)
        try:
            dst.execute("PRAGMA journal_mode=DELETE")
            for table, cols in _TABLES.items():
                dst.execute(
                    f"CREATE TABLE {table} ("
                    + ", ".join(f"{name} {kind}" for name, kind, _ in cols)
                    + ")"
                )
                dst.executemany(
                    f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})",
                    rows[table],
                )
            for stmt in _INDEXES:
                dst.execute(stmt)
            dst.commit()
        finally:
            dst.close()
        # Grafana runs as uid 472; the file holds no secrets by construction.
        tmp.chmod(0o644)
        os.replace(tmp, out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info(
        "grafana_export_written",
        path=str(out_path),
        model_calls=len(rows["model_calls"]),
        tool_calls=len(rows["tool_calls"]),
        message_reactions=len(rows["message_reactions"]),
        feedback_feature_cards=len(rows["feedback_feature_cards"]),
    )
    return True
