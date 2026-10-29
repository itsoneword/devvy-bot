"""Unit tests for the Tier-1 quality signal computation (dv-37e9)."""

import sqlite3

from src.llm_analytics.quality import (
    _CLAIM_RE,
    _COMPLAINT_RE,
    agent_parts,
    compute_quality_daily,
    is_log_tool,
    looks_like_recipe,
)


class TestPatterns:
    def test_complaints_match(self) -> None:
        for text in (
            "я не просил тебя это делать",
            "Я же тебя НЕ ПРОСИЛ",
            "не пиши так больше",
            "зачем ты удалил файл?",
            "убери этот блок",
            "не надо было так",
            "я же говорил про граммы",
            "я же тебе говорила",
        ):
            assert _COMPLAINT_RE.search(text), text

    def test_complaints_no_false_positive(self) -> None:
        for text in ("запиши обед 500 ккал", "спасибо, отлично", "what's up"):
            assert not _COMPLAINT_RE.search(text), text

    def test_claims(self) -> None:
        assert _CLAIM_RE.search("Записал: обед 640 ккал")
        assert _CLAIM_RE.search("записано в дневник")
        assert _CLAIM_RE.search("Logged your workout")
        assert not _CLAIM_RE.search("I blogged about it")
        assert not _CLAIM_RE.search("сейчас запишу")

    def test_is_log_tool(self) -> None:
        assert is_log_tool("mcp__agent__log_food")
        assert is_log_tool("log_sleep")
        assert not is_log_tool("Bash")
        assert not is_log_tool("mcp__agent__dialog_reset")

    def test_agent_parts(self) -> None:
        assert agent_parts("coach#u111111111") == ("coach", 111111111)
        assert agent_parts("chef") == ("chef", None)
        assert agent_parts("f1:chef") == ("f1:chef", None)
        assert agent_parts("general#weird") == ("general", None)

    def test_looks_like_recipe_positive(self) -> None:
        response = "1. 200 г муки\n2. 300 мл молока\n3. 2 яйца, взбить"
        assert looks_like_recipe(response)

    def test_looks_like_recipe_needs_list_and_quantities(self) -> None:
        # Quantities but no list structure.
        assert not looks_like_recipe("Возьми 200 г муки и 300 мл молока, смешай.")
        # List structure but no unit-qualified quantities.
        assert not looks_like_recipe("1. Разогрей духовку\n2. Смешай ингредиенты")
        # One quantity only isn't enough.
        assert not looks_like_recipe("1. 200 г муки\n2. немного соли по вкусу")


def _bot_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE sessions (session_id TEXT, user_id INTEGER,
            project_path TEXT);
        CREATE TABLE messages (message_id INTEGER, session_id TEXT,
            user_id INTEGER, timestamp TEXT, prompt TEXT, response TEXT);
        CREATE TABLE tool_calls (id INTEGER, session_id TEXT,
            message_id INTEGER, tool TEXT, started_at TEXT,
            parent_tool_use_id TEXT);
        CREATE TABLE memory_facts (id INTEGER, agent_id TEXT, tier TEXT,
            created_at TEXT);
        """)
    conn.execute("INSERT INTO sessions VALUES ('s1', 7, '/w/workspaces/coach/users/7')")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        [
            (1, "s1", 7, "2026-08-10T09:00:00+00:00", "обед 500", "Записал."),
            (2, "s1", 7, "2026-08-10T10:00:00+00:00", "ужин", "записано!"),
            (3, "s1", 7, "2026-08-11T09:00:00+00:00", "зачем ты убрал это", "ок"),
            (
                4,
                "s1",
                7,
                "2026-08-10T11:00:00+00:00",
                "рецепт блинов",
                "1. 200 г муки\n2. 300 мл молока\n3. 2 яйца",
            ),
            (
                5,
                "s1",
                7,
                "2026-08-10T12:00:00+00:00",
                "рецепт омлета",
                "1. 150 г яиц\n2. 50 мл молока\n3. соль",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO tool_calls (id, session_id, message_id, tool, started_at)"
        " VALUES (?,?,?,?,?)",
        [
            (1, "s1", 1, "mcp__agent__log_food", "2026-08-10T09:00:01+00:00"),
            (2, "s1", 3, "Bash", "2026-08-11T09:00:01+00:00"),
            (3, "s1", 5, "WebSearch", "2026-08-10T12:00:01+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO memory_facts VALUES (1, 'coach#u7', 'pinned',"
        " '2026-08-11 12:00:00')"
    )
    return conn


def test_compute_quality_daily() -> None:
    rows = compute_quality_daily(_bot_db())
    # (date, profile, user_id, pinned, log_calls, claimed_not_logged,
    #  complaints, user_messages, recipe_turns, recipe_turns_unsearched,
    #  verification_calls, verified_turns, deep_research_turns,
    #  program_turns, program_turns_unchecked)
    assert rows == [
        ("2026-08-10", "coach", 7, 0, 1, 1, 0, 4, 2, 1, 1, 1, 0, 0, 0),
        ("2026-08-11", "coach", 7, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0),
    ]


def test_missing_tables_tolerated() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert compute_quality_daily(conn) == []
