"""Unit tests for the index DB schema (dv-300d: answer_scores + the
quality_daily column migration for pre-existing DBs)."""

import sqlite3

from src.llm_analytics.db import init_db


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_init_db_creates_answer_scores_table() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    assert _cols(conn, "answer_scores") == {
        "message_id",
        "profile",
        "user_id",
        "ts",
        "judge_model",
        "rubric_version",
        "scores_json",
        "total",
        "rationale",
        "flags",
        "scored_at",
    }


def test_answer_scores_primary_key_is_idempotency_key() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    row = (
        1,
        "chef",
        7,
        "2026-09-19T00:00:00Z",
        "openrouter:qwen",
        1,
        "{}",
        15.0,
        "ok",
        None,
        "now",
    )
    conn.execute("INSERT INTO answer_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
    conn.execute(
        "INSERT OR IGNORE INTO answer_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)", row
    )
    count = conn.execute("SELECT COUNT(*) FROM answer_scores").fetchone()[0]
    assert count == 1

    # A different judge_model or rubric_version is a distinct row.
    other = (
        1,
        "chef",
        7,
        "2026-09-19T00:00:00Z",
        "claude-cli:opus",
        1,
        "{}",
        10.0,
        "ok",
        None,
        "now",
    )
    conn.execute("INSERT INTO answer_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)", other)
    assert conn.execute("SELECT COUNT(*) FROM answer_scores").fetchone()[0] == 2


def test_init_db_migrates_preexisting_quality_daily_missing_new_columns() -> None:
    conn = sqlite3.connect(":memory:")
    # Simulate a DB created before dv-300d: the old 8-column quality_daily.
    conn.executescript("""
        CREATE TABLE quality_daily (
            date TEXT NOT NULL, profile TEXT, user_id INTEGER,
            pinned_facts INTEGER NOT NULL DEFAULT 0,
            log_calls INTEGER NOT NULL DEFAULT 0,
            claimed_not_logged INTEGER NOT NULL DEFAULT 0,
            complaint_hits INTEGER NOT NULL DEFAULT 0,
            user_messages INTEGER NOT NULL DEFAULT 0
        );
        """)
    conn.execute("INSERT INTO quality_daily VALUES ('2026-08-10','chef',7,0,1,1,0,2)")
    init_db(conn)
    assert {"recipe_turns", "recipe_turns_unsearched"} <= _cols(conn, "quality_daily")
    row = conn.execute(
        "SELECT recipe_turns, recipe_turns_unsearched FROM quality_daily"
    ).fetchone()
    assert row == (0, 0)  # backfilled default, existing row preserved


def test_init_db_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    init_db(conn)  # must not raise on a second call
    assert _cols(conn, "answer_scores")
