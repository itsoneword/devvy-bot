"""Parser unit tests: synthetic skip, summaries, linkage regexes, seeds."""

from typing import Any

from src.llm_analytics.parser import (
    is_seeded_text,
    normalize_ts,
    parse_entry,
    summarize_input,
)


def _assistant_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "assistant",
        "timestamp": "2026-08-01T12:25:02.999Z",
        "uuid": "uuid-1",
        "requestId": "req_1",
        "message": {
            "model": "claude-fable-5",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 10,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 200,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 200,
                    "ephemeral_5m_input_tokens": 0,
                },
            },
            "content": [],
        },
    }
    entry.update(overrides)
    return entry


class TestAssistant:
    def test_real_call_parsed(self) -> None:
        parsed = parse_entry(_assistant_entry(), kind="session")
        assert parsed is not None and parsed.api_call is not None
        call = parsed.api_call
        assert call.request_id == "req_1"
        assert call.ts == "2026-08-01T12:25:02.999Z"
        assert (call.input_tokens, call.output_tokens) == (2, 10)
        assert (call.cache_read_tokens, call.cache_creation_tokens) == (100, 200)
        assert (call.cache_5m_tokens, call.cache_1h_tokens) == (0, 200)

    def test_synthetic_model_skipped(self) -> None:
        entry = _assistant_entry()
        entry["message"]["model"] = "<synthetic>"
        assert parse_entry(entry, kind="session") is None

    def test_missing_request_id_skipped(self) -> None:
        entry = _assistant_entry()
        del entry["requestId"]
        assert parse_entry(entry, kind="session") is None

    def test_api_error_message_skipped(self) -> None:
        entry = _assistant_entry(isApiErrorMessage=True)
        assert parse_entry(entry, kind="session") is None

    def test_tool_use_blocks_collected(self) -> None:
        entry = _assistant_entry()
        entry["message"]["content"] = [
            {"type": "text", "text": "thinking"},
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "Bash",
                "input": {"command": "ls -la", "description": "x" * 400},
            },
        ]
        parsed = parse_entry(entry, kind="session")
        assert parsed is not None and len(parsed.tool_uses) == 1
        tool_use = parsed.tool_uses[0]
        assert tool_use.name == "Bash"
        assert tool_use.input_summary == "x" * 300  # description wins, capped

    def test_agent_tool_input_captured(self) -> None:
        entry = _assistant_entry()
        entry["message"]["content"] = [
            {
                "type": "tool_use",
                "id": "toolu_02",
                "name": "Agent",
                "input": {
                    "description": "Fix bug",
                    "subagent_type": "Explore",
                    "prompt": "long prompt",
                },
            },
        ]
        parsed = parse_entry(entry, kind="session")
        assert parsed is not None
        agent_input = parsed.tool_uses[0].agent_input
        assert agent_input is not None
        assert agent_input.description == "Fix bug"
        assert agent_input.subagent_type == "Explore"
        assert agent_input.model is None


class TestUser:
    def test_tool_result_and_agent_link(self) -> None:
        entry: dict[str, Any] = {
            "type": "user",
            "timestamp": "2026-08-01T12:25:04.100Z",
            "toolUseResult": {"description": "Fix bug", "agentId": "a1b2"},
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_02",
                        "is_error": False,
                        "content": [
                            {"type": "text", "text": "launched\nagentId: a1b2c3d4e5"},
                        ],
                    }
                ],
            },
        }
        parsed = parse_entry(entry, kind="session")
        assert parsed is not None
        assert parsed.tool_results[0].tool_use_id == "toolu_02"
        assert parsed.tool_results[0].is_error == 0
        assert parsed.spawn_links[0].agent_id == "a1b2c3d4e5"
        assert parsed.spawn_links[0].tool_use_id == "toolu_02"
        assert parsed.spawn_links[0].description == "Fix bug"
        assert parsed.turn is None

    def test_plain_text_cli_turn(self) -> None:
        entry: dict[str, Any] = {
            "type": "user",
            "timestamp": "2026-08-01T12:25:04.100Z",
            "entrypoint": "cli",
            "message": {"role": "user", "content": "hello " + "y" * 300},
        }
        parsed = parse_entry(entry, kind="session")
        assert parsed is not None and parsed.turn is not None
        assert len(parsed.turn.excerpt or "") == 200
        assert parsed.user_text is not None

    def test_sdk_py_user_entry_makes_no_turn(self) -> None:
        entry: dict[str, Any] = {
            "type": "user",
            "timestamp": "2026-08-01T12:25:04.100Z",
            "entrypoint": "sdk-py",
            "message": {"role": "user", "content": "bot prompt"},
        }
        parsed = parse_entry(entry, kind="session")
        assert parsed is not None and parsed.turn is None
        assert parsed.user_text == "bot prompt"  # still usable for seed check

    def test_meta_entry_skipped(self) -> None:
        entry: dict[str, Any] = {
            "type": "user",
            "isMeta": True,
            "timestamp": "2026-08-01T12:25:04.100Z",
            "message": {"role": "user", "content": "caveat"},
        }
        assert parse_entry(entry, kind="session") is None


class TestQueueOperation:
    def test_task_notification_linkage(self) -> None:
        content = (
            "<task-notification>\n<task-id>a628626a9d1fc60cf</task-id>\n"
            "<tool-use-id>toolu_01HX3ayfVS2odufSka5Dd81e</tool-use-id>\n"
            "<status>completed</status>\n</task-notification>\ntail"
        )
        entry = {"type": "queue-operation", "operation": "enqueue", "content": content}
        parsed = parse_entry(entry, kind="session")
        assert parsed is not None
        assert parsed.spawn_links[0].agent_id == "a628626a9d1fc60cf"
        assert parsed.spawn_links[0].tool_use_id == "toolu_01HX3ayfVS2odufSka5Dd81e"

    def test_plain_queue_operation_ignored(self) -> None:
        entry = {"type": "queue-operation", "operation": "enqueue", "content": "hi"}
        assert parse_entry(entry, kind="session") is None


class TestHelpers:
    def test_normalize_ts_forms(self) -> None:
        assert normalize_ts("2026-08-01T12:24:49.351Z") == "2026-08-01T12:24:49.351Z"
        assert (
            normalize_ts("2026-03-07T11:26:46.254284+00:00")
            == "2026-03-07T11:26:46.254Z"
        )
        assert normalize_ts("2026-03-07 11:26:46") == "2026-03-07T11:26:46.000Z"

    def test_seed_markers(self) -> None:
        assert is_seeded_text("<session_notes>\n...")
        assert is_seeded_text("<recent_conversation>...")
        assert not is_seeded_text("regular first prompt")

    def test_summarize_input_priority_and_cap(self) -> None:
        assert summarize_input({"command": "ls", "file_path": "/x"}) == "ls"
        assert summarize_input({"prompt": "p" * 500}) == "p" * 300
        assert summarize_input({"content": "never stored"}) is None
