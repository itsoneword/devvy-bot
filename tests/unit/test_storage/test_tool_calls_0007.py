"""dv-2260 migration 0007: a bot.db already at 0005 (the first trace cut:
tool_name / input_short / ok / detached — the live DB's shape) is converted to
tool / input / status without losing a row."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from src.storage.schema import migrate
from src.storage.schema.migrate import ensure_schema
from src.storage.schema.structure import diff, snapshot


def _db_at_0005(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        migrate._run_alembic(conn, "bot", command.upgrade, "0005")
        conn.execute("COMMIT")
        conn.executemany(
            "INSERT INTO tool_calls (trace_id, seq, tool_use_id, parent_tool_use_id,"
            " tool_name, input_short, ok, error, detached, result_preview)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # copied from tool_usage by 0005: no trace, no seq
                (
                    None,
                    None,
                    None,
                    None,
                    "Read",
                    '{"file_path": "/a"}',
                    1,
                    None,
                    0,
                    None,
                ),
                ("t", 1, "u1", None, "Bash", '{"command": "false"}', 0, "boom", 0, "x"),
                ("t", 2, "u2", None, "Agent", '{"prompt": "go…', None, None, 1, None),
                ("t", 3, "u3", "u2", "Grep", '{"pattern": "p"}', None, None, 0, None),
            ],
        )
    finally:
        conn.close()


def test_0005_rows_converted_to_status_and_input(tmp_path):
    path = tmp_path / "bot.db"
    _db_at_0005(path)

    assert ensure_schema("bot", path) == "upgraded"

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT id, seq, tool, input, status, error, parent_tool_use_id,"
            " result_preview FROM tool_calls ORDER BY id"
        ).fetchall()
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert rows == [
        (1, 1, "Read", '{"file_path": "/a"}', "ok", None, None, None),
        (2, 1, "Bash", '{"command": "false"}', "error", "boom", None, "x"),
        (3, 2, "Agent", '{"prompt": "go…', "detached", None, None, None),
        (4, 3, "Grep", '{"pattern": "p"}', "unfinished", None, "u2", None),
    ]
    assert not any(t.startswith("tool_calls_") for t in tables)
    fresh = tmp_path / "fresh" / "bot.db"
    ensure_schema("bot", fresh)
    assert diff(snapshot(fresh), snapshot(path)) == []


def test_0007_downgrade_restores_0005_shape(tmp_path):
    path = tmp_path / "bot.db"
    _db_at_0005(path)
    ensure_schema("bot", path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        migrate._run_alembic(conn, "bot", command.downgrade, "0005")
        conn.execute("COMMIT")
        rows = conn.execute(
            "SELECT tool_name, ok, detached FROM tool_calls ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("Read", 1, 0),
        ("Bash", 0, 0),
        ("Agent", None, 1),
        ("Grep", None, 0),
    ]
