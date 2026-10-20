"""Context-growth accounting (dv-aeca): parser sizes, the growth ledger, the
indexer's one-shot re-read, and the window endpoint. Synthetic content only."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.llm_analytics.db import connect
from src.llm_analytics.growth import build_rows, summarize
from src.llm_analytics.indexer import PARSER_VERSION, Indexer
from src.llm_analytics.parser import estimate_tokens, parse_entry
from src.llm_analytics.pricing import PricingTable
from src.llm_analytics.server import build_app
from tests.unit.test_llm_analytics.test_server import PROJ, S1, USER, _build_fixture

T0 = "2026-08-01T10:00:00.000Z"


def _assistant(
    rid: str, ts: str, cache_read: int, out: int, blocks: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": ts,
        "uuid": f"u-{rid}",
        "requestId": rid,
        "message": {
            "model": "claude-fable-5",
            "usage": {
                "input_tokens": 2,
                "output_tokens": out,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": 0,
            },
            "content": blocks,
        },
    }


def _tool_result(ts: str, tool_use_id: str, content: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            ],
        },
    }


class TestParserSizes:
    def test_tool_result_chars_and_images(self) -> None:
        text = parse_entry(_tool_result(T0, "t1", "x" * 230), kind="session")
        assert text is not None
        assert (
            text.tool_results[0].result_chars,
            text.tool_results[0].result_images,
        ) == (
            230,
            0,
        )
        blocks = [{"type": "text", "text": "abc"}, {"type": "image", "source": {}}]
        mixed = parse_entry(_tool_result(T0, "t2", blocks), kind="session")
        assert mixed is not None
        assert (
            mixed.tool_results[0].result_chars,
            mixed.tool_results[0].result_images,
        ) == (
            3,
            1,
        )

    def test_estimate_tokens(self) -> None:
        assert estimate_tokens(230) == 100
        assert estimate_tokens(0, images=2) == 3000

    def test_tool_use_input_chars_and_output_items(self) -> None:
        entry = _assistant(
            "r1",
            T0,
            100,
            50,
            [
                {"type": "thinking", "thinking": "", "signature": "s" * 40},
                {"type": "text", "text": "hello"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
        )
        parsed = parse_entry(entry, kind="session")
        assert parsed is not None
        assert parsed.tool_uses[0].input_chars == len(json.dumps({"command": "ls"}))
        assert [(i.kind, i.chars, i.request_id) for i in parsed.context_items] == [
            ("thinking", 40, "r1"),
            ("assistant_text", 5, "r1"),
        ]

    def test_user_text_split_into_bot_sections(self) -> None:
        text = (
            "<family_reminders>aa</family_reminders>\n"
            "<current_time>bbb</current_time>\n"
            "<session_notes>cccc</session_notes>\nthe question"
        )
        entry = {
            "type": "user",
            "timestamp": T0,
            "entrypoint": "sdk-py",
            "message": {"role": "user", "content": text},
        }
        parsed = parse_entry(entry, kind="session")
        assert parsed is not None
        got = {(i.kind, i.name): i.chars for i in parsed.context_items}
        assert got[("turn_prefix", "family_reminders")] == len(
            "<family_reminders>aa</family_reminders>"
        )
        assert got[("seed", "session_notes")] == len(
            "<session_notes>cccc</session_notes>"
        )
        assert got[("user_text", "user")] == len("the question")

    def test_attachment_and_shape(self) -> None:
        reminder = {
            "type": "attachment",
            "timestamp": T0,
            "attachment": {"type": "total_tokens_reminder"},
            "rendered": [{"content": "<system-reminder>1</system-reminder>"}],
        }
        parsed = parse_entry(reminder, kind="session")
        assert parsed is not None
        assert (parsed.context_items[0].kind, parsed.context_items[0].name) == (
            "reminder",
            "total_tokens_reminder",
        )
        shape = {
            "type": "api-request-shape",
            "timestamp": T0,
            "shape": {
                "system": [
                    {
                        "type": "text",
                        "text": "head\n<agent_identity>idid</agent_identity>",
                    }
                ],
                "tools": [{"name": "Bash", "description": "d"}],
            },
        }
        parsed = parse_entry(shape, kind="session")
        assert parsed is not None
        got = {(i.kind, i.name): i.chars for i in parsed.context_items}
        assert got[("shape_system", "agent_identity")] == len(
            "<agent_identity>idid</agent_identity>"
        )
        assert got[("shape_system", "base")] == 5
        assert ("shape_tools", "Bash") in got


def test_shape_ignores_tag_names_mentioned_in_prose() -> None:
    text = (
        "<shared_capabilities>\nsee <constraints> below\n</shared_capabilities>\n"
        "<constraints>\nreal\n</constraints>"
    )
    shape = {
        "type": "api-request-shape",
        "timestamp": T0,
        "shape": {"system": [{"type": "text", "text": text}], "tools": []},
    }
    parsed = parse_entry(shape, kind="session")
    assert parsed is not None
    got = {i.name: i.chars for i in parsed.context_items}
    assert got["constraints"] == len("<constraints>\nreal\n</constraints>")
    assert got["shared_capabilities"] == len(
        "<shared_capabilities>\nsee <constraints> below\n</shared_capabilities>"
    )


def _call(rid: str, ts: str, ctx: int, out: int) -> dict[str, Any]:
    return {
        "request_id": rid,
        "ts": ts,
        "input_tokens": 2,
        "cache_read_tokens": ctx - 2,
        "cache_creation_tokens": 0,
        "output_tokens": out,
    }


def _item(
    kind: str, name: str, chars: int, ts: str, rid: str | None = None
) -> dict[str, Any]:
    return {"kind": kind, "name": name, "chars": chars, "ts": ts, "request_id": rid}


class TestGrowthLedger:
    def test_rows_attribute_measured_growth(self) -> None:
        calls = [
            _call("r1", "2026-08-01T10:00:10.000Z", 1000, 100),
            _call("r2", "2026-08-01T10:00:20.000Z", 1900, 30),
            _call("r3", "2026-08-01T10:00:30.000Z", 1930, 10),
        ]
        tools = [
            {
                "tool_use_id": "t1",
                "name": "Read",
                "request_id": "r1",
                "result_ts": "2026-08-01T10:00:15.000Z",
                "result_chars": 300,
                "result_images": 0,
                "input_chars": 20,
            },
            {
                "tool_use_id": "t2",
                "name": "Agent",
                "request_id": "r1",
                "result_ts": "2026-08-01T10:00:16.000Z",
                "result_chars": 100,
                "result_images": 0,
                "input_chars": 20,
            },
        ]
        items = [
            _item("shape_system", "base", 600, "2026-08-01T10:00:05.000Z"),
            _item("shape_tools", "Read", 200, "2026-08-01T10:00:05.000Z"),
            _item("user_text", "user", 200, "2026-08-01T10:00:01.000Z"),
            _item("reminder", "total_tokens_reminder", 100, "2026-08-01T10:00:17.000Z"),
            _item("assistant_text", "text", 50, "2026-08-01T10:00:10.000Z", "r1"),
        ]
        rows = build_rows(calls, tools, items)
        # Row 0 splits the first context by size: 600/200/200 of 1000.
        base = {(e["category"], e["name"]): e["tokens"] for e in rows[0]["entries"]}
        assert base == {
            ("base_system", "base"): 600,
            ("base_tools", "Read"): 200,
            ("user_text", "user"): 200,
        }
        # Step 1 grew by exactly 900 = 100 output + 800 of new input.
        assert rows[1]["delta"] == 900
        assert sum(e["tokens"] for e in rows[1]["entries"]) == 900
        by_cat: dict[str, int] = {}
        for e in rows[1]["entries"]:
            by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["tokens"]
        assert by_cat["assistant_text"] + by_cat["tool_use"] == 100
        assert (
            by_cat["tool_result"] + by_cat["subagent_result"] + by_cat["reminder"]
            == 800
        )
        # 300 : 100 : 100 chars → 480 / 160 / 160 tokens.
        assert by_cat["tool_result"] == 480
        assert by_cat["subagent_result"] == 160
        assert by_cat["reminder"] == 160

    def test_shrink_and_summary(self) -> None:
        calls = [
            _call("r1", "2026-08-01T10:00:10.000Z", 5000, 10),
            _call("r2", "2026-08-01T10:00:20.000Z", 2000, 10),
        ]
        rows = build_rows(calls, [], [])
        assert rows[1]["entries"][-1]["category"] == "context_shrink"
        summary = summarize(rows)
        assert summary["context_end"] == 2000
        assert summary["calls"] == 2

    def test_summary_tool_percentiles(self) -> None:
        rows = [
            {
                "ts": f"t{i}",
                "context": 0,
                "entries": [
                    {
                        "category": "tool_result",
                        "name": "Bash",
                        "tokens": tokens,
                        "chars": tokens * 2,
                        "tool_use_id": None,
                    }
                ],
            }
            for i, tokens in enumerate([10, 20, 30, 1000])
        ]
        summary = summarize(rows, top=1)
        tool = summary["tools"][0]
        assert (tool["calls"], tool["tokens"], tool["max"], tool["p95"]) == (
            4,
            1060,
            1000,
            1000,
        )
        assert summary["top_items"][0]["tokens"] == 1000

    def test_no_shape_marks_unmeasured_base(self) -> None:
        calls = [_call("r1", "2026-08-01T10:00:10.000Z", 1000, 10)]
        items = [_item("user_text", "user", 230, "2026-08-01T10:00:01.000Z")]
        entries = build_rows(calls, [], items)[0]["entries"]
        got = {e["category"]: e["tokens"] for e in entries}
        assert got == {"user_text": 100, "base_unknown": 900}


def _extend_s1(tmp_path: Path) -> None:
    """Append a tool round-trip with distinct usage to the fixture's S1."""
    path = tmp_path / "projects" / "-home-u-proj" / f"{S1}.jsonl"
    lines = [
        json.dumps(_tool_result("2026-08-01T10:01:30.000Z", "toolu_01", "y" * 460)),
        json.dumps(
            {
                "type": "attachment",
                "timestamp": "2026-08-01T10:01:31.000Z",
                "attachment": {"type": "total_tokens_reminder"},
                "rendered": [{"content": "r" * 46}],
            }
        ),
        json.dumps(_assistant("req_s1_3", "2026-08-01T10:02:00.000Z", 400, 5, [])),
    ]
    with path.open("a") as handle:
        handle.write("\n".join(lines) + "\n")


class TestIndexerAndEndpoint:
    def test_sizes_stored_and_window_breakdown(self, tmp_path: Path) -> None:
        config = _build_fixture(tmp_path)
        _extend_s1(tmp_path)
        conn = connect(config.db_path)
        Indexer(conn, config, PricingTable.load(config.pricing_path)).scan_once()
        row = conn.execute(
            "SELECT input_chars, result_chars, result_tokens_est FROM tool_calls"
            " WHERE tool_use_id = 'toolu_01'"
        ).fetchone()
        assert tuple(row) == (
            len(json.dumps({"command": "ls", "description": "list"})),
            460,
            200,
        )
        kinds = {r[0] for r in conn.execute("SELECT kind FROM context_items")}
        assert "reminder" in kinds
        conn.close()
        with TestClient(build_app(config)) as client:
            body = client.get(
                "/api/chains/window",
                params={"user_id": USER, "working_dir": PROJ, "window_id": 1},
            ).json()
            assert body["indexed_sizes"] is True
            assert body["calls"] == 3
            tool = next(t for t in body["tools"] if t["name"] == "Bash")
            # Call 3's context (402) minus call 2's (113) minus its 10 output
            # tokens, shared 460:46 between the result and the reminder.
            assert tool["tokens"] == round((402 - 113 - 10) * 460 / 506)
            detail = client.get(
                "/api/chains/detail", params={"user_id": USER, "working_dir": PROJ}
            ).json()
            last_in_s1 = [p for p in detail["series"] if p["session_id"] == S1][-1]
            assert last_in_s1["delta"] == 402 - 113
            assert last_in_s1["tools"][0][:2] == ["Bash", 460]
            missing = client.get(
                "/api/chains/window",
                params={"user_id": 999, "working_dir": "/nope", "window_id": 1},
            )
            assert missing.status_code == 404

    def test_parser_upgrade_rereads_existing_files(self, tmp_path: Path) -> None:
        config = _build_fixture(tmp_path)
        _extend_s1(tmp_path)
        conn = connect(config.db_path)
        indexer = Indexer(conn, config, PricingTable.load(config.pricing_path))
        indexer.scan_once()
        # Simulate a DB indexed before the upgrade: sizes missing, old version.
        conn.execute("UPDATE tool_calls SET result_chars = NULL, input_chars = NULL")
        conn.execute("DELETE FROM context_items")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'parser_version'")
        conn.commit()
        indexer.scan_once()
        assert (
            conn.execute(
                "SELECT result_chars FROM tool_calls WHERE tool_use_id = 'toolu_01'"
            ).fetchone()[0]
            == 460
        )
        assert conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0] > 0
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key = 'parser_version'"
            ).fetchone()[0]
            == PARSER_VERSION
        )
        # Re-reading is idempotent: no duplicate calls or items.
        count = conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0]
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'parser_version'")
        conn.commit()
        indexer.scan_once()
        assert conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0] == count
        conn.close()


def test_db_is_plain_sqlite(tmp_path: Path) -> None:
    """Guard: the migration leaves a normally openable DB at head."""
    config = _build_fixture(tmp_path)
    check = sqlite3.connect(config.db_path)
    cols = {r[1] for r in check.execute("PRAGMA table_info(tool_calls)")}
    assert {"input_chars", "result_chars", "result_images", "result_tokens_est"} <= cols
    check.close()
