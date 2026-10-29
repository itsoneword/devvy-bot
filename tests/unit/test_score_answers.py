"""Unit tests for scripts/score_answers.py (dv-300d)."""

import json
import sqlite3

import pytest

from scripts.score_answers import (
    _already_scored,
    _fetch_context_turns,
    _fetch_tool_calls,
    _load_session_scope,
    _select_candidates,
    _truncate,
    parse_args,
    run,
)
from src.llm_analytics.db import connect as index_connect
from src.llm_analytics.db import init_db
from src.llm_analytics.rubric import RUBRIC_VERSION


def _bot_db(tmp_path):
    path = tmp_path / "bot.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (session_id TEXT, user_id INTEGER, project_path TEXT);
        CREATE TABLE messages (message_id INTEGER PRIMARY KEY, session_id TEXT,
            user_id INTEGER, timestamp TEXT, prompt TEXT, response TEXT);
        CREATE TABLE tool_calls (id INTEGER, session_id TEXT, message_id INTEGER,
            tool TEXT, started_at TEXT);
        """)
    conn.execute(
        "INSERT INTO sessions VALUES ('s1', 1001, '/srv/devvy/workspaces/chef')"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('s2', 111, '/srv/devvy/workspaces/coach')"
    )
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        [
            (
                1,
                "s1",
                1001,
                "2026-09-19T07:00:00+00:00",
                "prev prompt",
                "prev response",
            ),
            (
                2,
                "s1",
                1001,
                "2026-09-19T07:57:50+00:00",
                "у меня толком ничего нет",
                "x" * 250,  # long enough to pass the 200-char floor
            ),
            (3, "s1", 1001, "2026-09-19T08:00:00+00:00", "short one", "too short"),
            (4, "s2", 111, "2026-09-19T07:57:50+00:00", "coach q", "y" * 250),
        ],
    )
    conn.execute(
        "INSERT INTO tool_calls VALUES (1, 's1', 2, 'WebSearch', '2026-09-19T07:58:00+00:00')"
    )
    conn.commit()
    conn.close()
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    return ro, path


class TestParseArgs:
    def test_backfill_requires_since(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--backfill"])

    def test_backfill_with_since_ok(self) -> None:
        args = parse_args(["--backfill", "--since", "2026-01-01"])
        assert args.since == "2026-01-01"
        assert args.provider == "openrouter"

    def test_message_id_mode(self) -> None:
        args = parse_args(["--message-id", "1720", "--provider", "claude_cli"])
        assert args.message_id == 1720
        assert args.provider == "claude_cli"

    def test_modes_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--recent", "--message-id", "1"])


class TestSelectCandidates:
    def test_message_id_mode(self, tmp_path) -> None:
        conn, _ = _bot_db(tmp_path)
        args = parse_args(["--message-id", "2"])
        rows = list(_select_candidates(conn, {}, args))
        assert [r["message_id"] for r in rows] == [2]

    def test_backfill_filters_by_profile(self, tmp_path) -> None:
        conn, _ = _bot_db(tmp_path)
        scope = _load_session_scope(conn)
        assert scope["s1"][0] == "chef"
        args = parse_args(["--backfill", "--since", "2026-01-01", "--profile", "chef"])
        rows = list(_select_candidates(conn, scope, args))
        assert {r["message_id"] for r in rows} == {1, 2, 3}

    def test_backfill_unknown_profile_yields_nothing(self, tmp_path) -> None:
        conn, _ = _bot_db(tmp_path)
        scope = _load_session_scope(conn)
        args = parse_args(["--backfill", "--since", "2026-01-01", "--profile", "nope"])
        assert list(_select_candidates(conn, scope, args)) == []


class TestContextAndTools:
    def test_fetch_context_turns_excludes_current_and_orders_chronologically(
        self, tmp_path
    ) -> None:
        conn, _ = _bot_db(tmp_path)
        turns = _fetch_context_turns(conn, "s1", "2026-09-19T07:57:50+00:00", 2)
        assert [t.text for t in turns] == ["prev prompt", "prev response"]

    def test_fetch_tool_calls(self, tmp_path) -> None:
        conn, _ = _bot_db(tmp_path)
        assert _fetch_tool_calls(conn, 2) == ["WebSearch"]
        assert _fetch_tool_calls(conn, 1) == []


def test_truncate_appends_marker_only_when_over_cap() -> None:
    assert _truncate("short", 100) == "short"
    truncated = _truncate("x" * 200, 50)
    assert truncated.endswith("[truncated]")
    assert len(truncated) < 200


def test_already_scored_respects_full_key(tmp_path) -> None:
    conn = index_connect(str(tmp_path / "idx.db"))
    init_db(conn)
    conn.execute(
        "INSERT INTO answer_scores VALUES (2,'chef',1,'t','claude-cli:opus',1,'{}',10,'r',NULL,'now')"
    )
    conn.commit()
    assert _already_scored(conn, 2, "claude-cli:opus", 1)
    assert not _already_scored(
        conn, 2, "claude-cli:opus", 2
    )  # different rubric version
    assert not _already_scored(conn, 2, "other-model", 1)


class _FakeJudge:
    model_name = "fake:v1"
    last_cost_usd = 0.001

    async def resolve(self) -> str:
        return self.model_name

    async def complete(self, prompt: str) -> str:
        # Includes the chef overlay keys too since the fixture's session
        # s1 resolves to profile "chef" (parse_judge_output only reads the
        # keys it expects for the given profile, so extras are harmless).
        return json.dumps(
            {
                "criteria": {
                    name: {"score": 3, "rationale": "ok"}
                    for name in (
                        "addresses_the_ask",
                        "grounding",
                        "calibration",
                        "constraint_compliance",
                        "actionability",
                        "chef_anchor_named",
                        "chef_search_performed",
                        "chef_no_blind_correction",
                    )
                },
                "summary": "fine",
                "flags": [],
            }
        )


async def test_run_dry_run_scores_nothing_but_counts(tmp_path, monkeypatch) -> None:
    _, bot_db_path = _bot_db(tmp_path)
    index_db_path = tmp_path / "idx.db"
    monkeypatch.setattr(
        "scripts.score_answers.build_judge", lambda *a, **k: _FakeJudge()
    )

    args = parse_args(
        [
            "--message-id",
            "2",
            "--dry-run",
            "--bot-db",
            str(bot_db_path),
            "--index-db",
            str(index_db_path),
        ]
    )
    stats = await run(args)
    assert stats["scored"] == 1

    conn = index_connect(str(index_db_path))
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM answer_scores").fetchone()[0] == 0


async def test_run_writes_score_and_is_idempotent_on_rerun(
    tmp_path, monkeypatch
) -> None:
    _, bot_db_path = _bot_db(tmp_path)
    index_db_path = tmp_path / "idx.db"
    monkeypatch.setattr(
        "scripts.score_answers.build_judge", lambda *a, **k: _FakeJudge()
    )

    args = parse_args(
        [
            "--message-id",
            "2",
            "--bot-db",
            str(bot_db_path),
            "--index-db",
            str(index_db_path),
        ]
    )
    stats = await run(args)
    assert stats["scored"] == 1
    assert stats["judge_model"] == "fake:v1"
    assert stats["rubric_version"] == RUBRIC_VERSION

    conn = index_connect(str(index_db_path))
    init_db(conn)
    row = conn.execute(
        "SELECT total, profile FROM answer_scores WHERE message_id=2"
    ).fetchone()
    assert row["total"] == 15.0
    assert row["profile"] == "chef"

    # Re-running the same mode must skip the already-scored turn.
    stats2 = await run(args)
    assert stats2["scored"] == 0
    assert stats2["skipped_duplicate"] == 1


async def test_run_skips_short_responses(tmp_path, monkeypatch) -> None:
    _, bot_db_path = _bot_db(tmp_path)
    index_db_path = tmp_path / "idx.db"
    monkeypatch.setattr(
        "scripts.score_answers.build_judge", lambda *a, **k: _FakeJudge()
    )

    args = parse_args(
        [
            "--message-id",
            "3",
            "--bot-db",
            str(bot_db_path),
            "--index-db",
            str(index_db_path),
        ]
    )
    stats = await run(args)
    assert stats["scored"] == 0
    assert stats["skipped_short"] == 1
