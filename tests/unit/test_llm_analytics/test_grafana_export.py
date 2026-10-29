"""Restricted Grafana export of bot.db (dv-e005 phase 1).

Grafana OSS has no per-datasource permissions: any Viewer can POST its own
rawSql. So the exported file IS the scope limit — it must hold only
``model_calls`` and ``tool_calls``, and only an explicit column allowlist
(no free text, no user ids). These tests build a bot.db with the real
migrated schema, fill the sensitive tables with marker text, and check that
none of it crosses into the export.
"""

import dataclasses
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from src.llm_analytics.config import AnalyticsConfig
from src.llm_analytics.db import connect, init_db
from src.llm_analytics.indexer import Indexer
from src.llm_analytics.pricing import PricingTable
from src.storage.schema.migrate import migrate_connection

SECRET = "sk-SECRET-MARKER-7f3a"
PROMPT_TEXT = "PRIVATE-PROMPT-MARKER please remember my address"


def export_usage(bot_db_path: Path, out_path: Path) -> Any:
    """Imported per call so one missing module fails each test, not collection."""
    from src.llm_analytics.grafana_export import export_usage as _export

    return _export(bot_db_path, out_path)


MODEL_CALLS_COLUMNS = {
    "id",
    "ts",
    "purpose",
    "kind",
    "harness",
    "harness_version",
    "provider",
    "model",
    "attended",
    "profile",
    "session_id",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "duration_ms",
    "outcome",
    "cost_source",
    "has_error",
}
TOOL_CALLS_COLUMNS = {
    "id",
    "model_call_id",
    "session_id",
    "seq",
    "tool_use_id",
    "parent_tool_use_id",
    "tool_name",
    "subagent_type",
    "ok",
    "started_at",
    "duration_ms",
    "result_bytes",
    "detached",
    "has_error",
}
FORBIDDEN_COLUMNS = {
    "input_short",
    "result_preview",
    "error",
    "user_id",
    "trace_id",
    "message_id",
}


def _insert_model_call(
    conn: sqlite3.Connection, call_id: int, *, error: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO model_calls (id, ts, purpose, kind, harness, harness_version,"
        " provider, model, attended, profile, user_id, session_id, input_tokens,"
        " output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd,"
        " duration_ms, outcome, error, cost_source)"
        " VALUES (?, '2026-10-25T10:00:00+00:00', 'chat', 'turn', 'claude',"
        " '2.1.0', 'anthropic', 'claude-opus', 1, 'devops', 386000111, 'sess-1',"
        " 10, 20, 30, 40, 0.5, 1200, ?, ?, 'reported')",
        (call_id, "error" if error else "ok", error),
    )


@pytest.fixture()
def bot_db(tmp_path: Path) -> Path:
    """bot.db with the real migrated schema and marker text in every
    sensitive place: messages, user_tokens, model_calls.error,
    tool_calls.input / result_preview / error."""
    path = tmp_path / "db" / "bot.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    migrate_connection("bot", conn)
    conn.execute(
        "INSERT INTO users (user_id, telegram_username) VALUES (386000111, 'alice')"
    )
    conn.execute(
        "INSERT INTO messages (session_id, user_id, prompt, response, timestamp)"
        " VALUES ('sess-1', 386000111, ?, ?, '2026-10-25T10:00:00')",
        (PROMPT_TEXT, f"reply with {SECRET}"),
    )
    conn.execute(
        "INSERT INTO user_tokens (user_id, token_hash, created_at)"
        " VALUES (386000111, ?, '2026-10-25T10:00:00')",
        (SECRET,),
    )
    _insert_model_call(conn, 1)
    _insert_model_call(conn, 2, error=f"failed reading {SECRET} from /home/x/.env")
    conn.execute(
        "INSERT INTO tool_calls (id, trace_id, model_call_id, message_id, session_id,"
        " seq, tool_use_id, parent_tool_use_id, tool, subagent_type,"
        " input, status, error, started_at, duration_ms, result_bytes,"
        " result_preview)"
        " VALUES (1, 'tr-1', 1, 1, 'sess-1', 0, 'toolu_1', NULL, 'Bash', NULL,"
        " ?, 'ok', NULL, '2026-10-25T10:00:01+00:00', 50, 120, ?)",
        (f"curl -H 'Authorization: {SECRET}'", PROMPT_TEXT),
    )
    conn.execute(
        "INSERT INTO tool_calls (id, trace_id, model_call_id, message_id, session_id,"
        " seq, tool_use_id, parent_tool_use_id, tool, subagent_type,"
        " input, status, error, started_at, duration_ms, result_bytes,"
        " result_preview)"
        " VALUES (2, 'tr-1', 2, 1, 'sess-1', 1, 'toolu_2', 'toolu_1', 'Read',"
        " 'Explore', '/home/x/.env', 'error', ?, '2026-10-25T10:00:02+00:00', 5, 0,"
        " NULL)",
        (f"permission denied {SECRET}",),
    )
    conn.commit()
    conn.close()
    return path


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            " AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


class TestExportScope:
    """Scenario: Grafana sees only model_calls/tool_calls; messages unreachable."""

    def test_export_has_only_model_calls_and_tool_calls(
        self, bot_db: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "grafana" / "devvy.db"
        out.parent.mkdir()
        export_usage(bot_db, out)
        conn = _open_ro(out)
        try:
            assert _tables(conn) == {"model_calls", "tool_calls"}
            assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 2
        finally:
            conn.close()

    def test_select_from_messages_on_export_fails(
        self, bot_db: Path, tmp_path: Path
    ) -> None:
        """What a Viewer's arbitrary rawSql would try first."""
        out = tmp_path / "grafana" / "devvy.db"
        out.parent.mkdir()
        export_usage(bot_db, out)
        conn = _open_ro(out)
        try:
            for table in ("messages", "user_tokens", "users", "memory_facts"):
                with pytest.raises(sqlite3.OperationalError, match="no such table"):
                    conn.execute(f"SELECT * FROM {table}").fetchall()
            # The export must not ATTACH-chain or point back at bot.db either.
            assert [r[1] for r in conn.execute("PRAGMA database_list")] == ["main"]
        finally:
            conn.close()


class TestExportColumns:
    """Scenario: export has no input_short, result_preview, error text or user_id."""

    def test_columns_are_exactly_the_allowlist(
        self, bot_db: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "grafana" / "devvy.db"
        out.parent.mkdir()
        export_usage(bot_db, out)
        conn = _open_ro(out)
        try:
            mc = _columns(conn, "model_calls")
            tc = _columns(conn, "tool_calls")
        finally:
            conn.close()
        assert mc == MODEL_CALLS_COLUMNS
        assert tc == TOOL_CALLS_COLUMNS
        assert not (mc | tc) & FORBIDDEN_COLUMNS

    def test_no_marker_text_anywhere_in_the_file(
        self, bot_db: Path, tmp_path: Path
    ) -> None:
        """Dropped columns must be gone from the bytes too (no free pages,
        no leftover copy), not just hidden from PRAGMA table_info."""
        out = tmp_path / "grafana" / "devvy.db"
        out.parent.mkdir()
        export_usage(bot_db, out)
        raw = out.read_bytes()
        for marker in (SECRET, "PRIVATE-PROMPT-MARKER", "386000111", "/home/x/.env"):
            assert marker.encode() not in raw, marker

    def test_has_error_flag_replaces_error_text(
        self, bot_db: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "grafana" / "devvy.db"
        out.parent.mkdir()
        export_usage(bot_db, out)
        conn = _open_ro(out)
        try:
            mc = dict(conn.execute("SELECT id, has_error FROM model_calls"))
            tc = dict(conn.execute("SELECT id, has_error FROM tool_calls"))
            usage = conn.execute(
                "SELECT profile, purpose, input_tokens, output_tokens, cost_usd"
                " FROM model_calls WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert {k: bool(v) for k, v in mc.items()} == {1: False, 2: True}
        assert {k: bool(v) for k, v in tc.items()} == {1: False, 2: True}
        assert tuple(usage) == ("devops", "chat", 10, 20, 0.5)


class TestExportFile:
    def test_rerun_replaces_atomically_with_new_rows(
        self, bot_db: Path, tmp_path: Path
    ) -> None:
        """Existing export + new bot.db rows: the next run swaps in a full new
        file (os.replace), leaves no tmp/journal/WAL files for Grafana to trip on."""
        out_dir = tmp_path / "grafana"
        out_dir.mkdir()
        out = out_dir / "devvy.db"
        export_usage(bot_db, out)
        first_inode = out.stat().st_ino

        conn = sqlite3.connect(bot_db)
        _insert_model_call(conn, 3)
        conn.commit()
        conn.close()

        export_usage(bot_db, out)
        assert out.stat().st_ino != first_inode  # replaced, not edited in place
        check = _open_ro(out)
        try:
            assert check.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 3
            assert check.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
        finally:
            check.close()
        assert sorted(p.name for p in out_dir.iterdir()) == ["devvy.db"]
        assert stat.S_IMODE(out.stat().st_mode) & 0o044 == 0o044  # uid 472 reads it

    def test_source_is_opened_read_only(self, bot_db: Path, tmp_path: Path) -> None:
        """The export must never need write access to bot.db."""
        conn = sqlite3.connect(bot_db)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        before = bot_db.read_bytes()
        bot_db.chmod(0o444)
        try:
            out = tmp_path / "grafana" / "devvy.db"
            out.parent.mkdir()
            export_usage(bot_db, out)
            assert out.exists()
        finally:
            bot_db.chmod(0o644)
        assert bot_db.read_bytes() == before

    def test_missing_bot_db_writes_nothing(self, tmp_path: Path) -> None:
        """added: fresh install, bot.db absent — no crash, no empty export
        that would make Grafana panels silently show zero."""
        out = tmp_path / "grafana" / "devvy.db"
        out.parent.mkdir()
        export_usage(tmp_path / "db" / "missing.db", out)
        assert not out.exists()
        assert list(out.parent.iterdir()) == []


# --- the hook in the existing mirror cycle ---------------------------------


def _indexer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    """Indexer whose config comes from the real from_env (so the export path
    and its env override are whatever the build named them), with the data
    root pointed at tmp_path."""
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    for var in (
        "LLM_ANALYTICS_DB_PATH",
        "LLM_ANALYTICS_BOT_DB_PATH",
        "LLM_ANALYTICS_GRAFANA_EXPORT",
    ):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(
        '{"default": {"input": 5.0, "output": 25.0, "cache_read": 0.5,'
        ' "cache_write_5m": 6.25, "cache_write_1h": 10.0}, "rules": []}'
    )
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    config = dataclasses.replace(
        AnalyticsConfig.from_env(),
        projects_dir=projects,
        pricing_path=pricing_path,
    )
    assert config.bot_db_path == tmp_path / "db" / "bot.db"
    conn = connect(config.db_path)
    init_db(conn)
    return Indexer(conn, config, PricingTable.load(pricing_path))


class TestIndexerHook:
    def test_scan_writes_export_under_data_root_grafana(
        self, bot_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "grafana").mkdir()
        indexer = _indexer(tmp_path, monkeypatch)
        indexer.scan_once()
        out = tmp_path / "grafana" / "devvy.db"
        assert out.exists()
        conn = _open_ro(out)
        try:
            assert _tables(conn) == {"model_calls", "tool_calls"}
        finally:
            conn.close()

    def test_second_scan_within_300s_does_not_rewrite(
        self, bot_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "grafana").mkdir()
        indexer = _indexer(tmp_path, monkeypatch)
        indexer.scan_once()
        out = tmp_path / "grafana" / "devvy.db"
        inode = out.stat().st_ino

        conn = sqlite3.connect(bot_db)
        _insert_model_call(conn, 3)
        conn.commit()
        conn.close()

        indexer.scan_once()  # 30 s cycle, export throttled to 300 s
        assert out.stat().st_ino == inode
        check = _open_ro(out)
        try:
            assert check.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 2
        finally:
            check.close()

    def test_empty_env_override_turns_export_off(
        self, bot_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "grafana").mkdir()
        indexer = _indexer(tmp_path, monkeypatch, LLM_ANALYTICS_GRAFANA_EXPORT="")
        indexer.scan_once()
        assert list((tmp_path / "grafana").iterdir()) == []


def test_data_layout_has_grafana_dir() -> None:
    from src.utils.paths import data_layout

    assert data_layout(Path("/d")).grafana_dir == Path("/d/grafana")


def test_export_dir_is_not_the_db_dir() -> None:
    """Grafana mounts grafana_dir; it must never be (or contain) db_dir."""
    from src.utils.paths import data_layout

    layout = data_layout(Path("/d"))
    grafana = layout.grafana_dir
    assert grafana != layout.db_dir
    assert layout.db_dir not in (grafana, *grafana.parents)
    assert grafana not in (layout.db_dir, *layout.db_dir.parents)
    assert os.path.commonpath([grafana, layout.bot_db]) != str(grafana)
