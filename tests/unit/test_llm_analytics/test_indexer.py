"""Indexer integration tests on tiny synthetic fixtures: dedupe, offset
resume (partial lines left unconsumed), truncation rescan."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src.llm_analytics.config import AnalyticsConfig
from src.llm_analytics.db import connect, init_db
from src.llm_analytics.indexer import Indexer
from src.llm_analytics.pricing import PricingTable

PRICING = {
    "default": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    "rules": [],
}


def _entry(request_id: str, ts: str, output_tokens: int = 10) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "uuid": f"uuid-{request_id}",
            "requestId": request_id,
            "cwd": "/home/u/proj",
            "entrypoint": "cli",
            "version": "2.1.220",
            "gitBranch": "main",
            "message": {
                "model": "claude-fable-5",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 60,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 60,
                        "ephemeral_5m_input_tokens": 0,
                    },
                },
                "content": [],
            },
        }
    )


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Any]:
    projects = tmp_path / "projects" / "-home-u-proj"
    projects.mkdir(parents=True)
    session = projects / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    lines = [
        _entry("req_1", "2026-08-01T10:00:00.000Z"),
        _entry("req_1", "2026-08-01T10:00:01.000Z"),  # streamed duplicate
        _entry("req_2", "2026-08-01T10:00:05.000Z"),
    ]
    session.write_text("\n".join(lines) + "\n")
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(json.dumps(PRICING))
    config = AnalyticsConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "index.db",
        projects_dir=tmp_path / "projects",
        bot_db_path=tmp_path / "missing-bot.db",  # absent: mirror is skipped
        pricing_path=pricing_path,
        scan_interval=30,
    )
    conn = connect(config.db_path)
    init_db(conn)
    indexer = Indexer(conn, config, PricingTable.load(pricing_path))
    return {"conn": conn, "indexer": indexer, "session": session}


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


class TestIndexer:
    def test_dedupe_by_request_id(self, env: dict[str, Any]) -> None:
        env["indexer"].scan_once()
        assert _count(env["conn"], "SELECT COUNT(*) FROM api_calls") == 2
        row = env["conn"].execute("SELECT * FROM sessions").fetchone()
        assert row["api_call_count"] == 2
        assert row["output_tokens"] == 20
        assert row["bot_formula_tokens"] == 2 * (3 + 10)  # excludes cache tokens

    def test_second_scan_is_noop(self, env: dict[str, Any]) -> None:
        env["indexer"].scan_once()
        stats = env["indexer"].scan_once()
        assert stats.files_scanned == 0
        assert stats.files_skipped == 1
        assert stats.lines_consumed == 0

    def test_partial_line_not_consumed(self, env: dict[str, Any]) -> None:
        env["indexer"].scan_once()
        offset_before = (
            env["conn"].execute("SELECT byte_offset FROM files").fetchone()[0]
        )
        complete = _entry("req_3", "2026-08-01T10:00:10.000Z") + "\n"
        partial = _entry("req_4", "2026-08-01T10:00:15.000Z")[:40]  # no newline
        with env["session"].open("a") as handle:
            handle.write(complete + partial)
        env["indexer"].scan_once()
        conn = env["conn"]
        assert _count(conn, "SELECT COUNT(*) FROM api_calls") == 3  # req_4 absent
        offset_after = conn.execute("SELECT byte_offset FROM files").fetchone()[0]
        assert offset_after == offset_before + len(complete)  # stops at partial
        # Completing the line later consumes exactly the straddled entry.
        with env["session"].open("a") as handle:
            handle.write(_entry("req_4", "2026-08-01T10:00:15.000Z")[40:] + "\n")
        env["indexer"].scan_once()
        assert _count(conn, "SELECT COUNT(*) FROM api_calls") == 4

    def test_truncation_wipes_scope_and_rescans(self, env: dict[str, Any]) -> None:
        env["indexer"].scan_once()
        env["session"].write_text(_entry("req_9", "2026-08-01T11:00:00.000Z") + "\n")
        stats = env["indexer"].scan_once()
        assert stats.files_truncated == 1
        conn = env["conn"]
        rows = conn.execute("SELECT request_id FROM api_calls").fetchall()
        assert [r["request_id"] for r in rows] == ["req_9"]  # no duplicates/stale
        assert conn.execute("SELECT byte_offset FROM files").fetchone()[0] > 0
        session = conn.execute("SELECT * FROM sessions").fetchone()
        assert session["api_call_count"] == 1

    def test_subagent_file_and_rollup(self, env: dict[str, Any]) -> None:
        subdir = (
            env["session"].parent / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" / "subagents"
        )
        subdir.mkdir(parents=True)
        (subdir / "agent-a1b2c3.jsonl").write_text(
            _entry("req_sub", "2026-08-01T10:01:00.000Z") + "\n"
        )
        env["indexer"].scan_once()
        conn = env["conn"]
        run = conn.execute("SELECT * FROM subagent_runs").fetchone()
        assert run["agent_id"] == "a1b2c3"
        assert run["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert run["api_call_count"] == 1
        call = conn.execute(
            "SELECT agent_id FROM api_calls WHERE request_id='req_sub'"
        ).fetchone()
        assert call["agent_id"] == "a1b2c3"
        session = conn.execute("SELECT * FROM sessions").fetchone()
        assert session["subagent_count"] == 1
        # first_call_context comes from the main thread only.
        assert session["first_call_context"] == 3 + 50 + 60

    def test_workflow_subagent_glob(self, env: dict[str, Any]) -> None:
        # Second layout: <session>/subagents/workflows/wf_*/agent-*.jsonl.
        wf_dir = (
            env["session"].parent
            / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            / "subagents"
            / "workflows"
            / "wf_12345678-9ab"
        )
        wf_dir.mkdir(parents=True)
        (wf_dir / "agent-w1w2w3.jsonl").write_text(
            _entry("req_wf", "2026-08-01T10:02:00.000Z") + "\n"
        )
        # journal.jsonl must stay ignored (not an agent transcript).
        (wf_dir / "journal.jsonl").write_text('{"type": "journal"}\n')
        env["indexer"].scan_once()
        conn = env["conn"]
        files = conn.execute(
            "SELECT path, kind, session_id, agent_id FROM files"
            " WHERE kind='subagent'"
        ).fetchall()
        assert len(files) == 1
        assert files[0]["path"].endswith("wf_12345678-9ab/agent-w1w2w3.jsonl")
        assert files[0]["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert files[0]["agent_id"] == "w1w2w3"
        run = conn.execute(
            "SELECT * FROM subagent_runs WHERE agent_id='w1w2w3'"
        ).fetchone()
        assert run["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert run["api_call_count"] == 1
        # Second scan is still a no-op (both files unchanged).
        stats = env["indexer"].scan_once()
        assert stats.files_scanned == 0
        assert stats.files_skipped == 2
