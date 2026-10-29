from src.agent_backends.codex_cli import map_codex_event


def test_thread_started_yields_thread_id():
    out = map_codex_event({"type": "thread.started", "thread_id": "abc-123"})
    assert out.thread_id == "abc-123"
    assert out.stream_update is None


def test_command_execution_started_yields_bash_tool_call():
    out = map_codex_event(
        {
            "type": "item.started",
            "item": {
                "id": "i0",
                "type": "command_execution",
                "command": "/bin/bash -lc 'echo hi'",
                "status": "in_progress",
            },
        }
    )
    assert out.stream_update is not None
    assert out.stream_update.tool_calls == [
        {"name": "Bash", "input": {"command": "/bin/bash -lc 'echo hi'"}}
    ]
    assert out.stream_update.type != "assistant"  # must not look like commentary


def test_command_execution_completed_is_silent():
    out = map_codex_event(
        {
            "type": "item.completed",
            "item": {
                "id": "i0",
                "type": "command_execution",
                "command": "x",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )
    assert out.stream_update is None


def test_file_change_started_yields_edit_tool_call():
    out = map_codex_event(
        {
            "type": "item.started",
            "item": {
                "id": "i1",
                "type": "file_change",
                "changes": [{"path": "/tmp/x/note.txt", "kind": "add"}],
                "status": "in_progress",
            },
        }
    )
    assert out.stream_update.tool_calls == [
        {"name": "Edit", "input": {"file_path": "/tmp/x/note.txt"}}
    ]


def test_mcp_tool_call_started_yields_named_tool_call():
    out = map_codex_event(
        {
            "type": "item.started",
            "item": {
                "id": "i2",
                "type": "mcp_tool_call",
                "tool": "find_places",
                "arguments": {"q": "sunset"},
                "status": "in_progress",
            },
        }
    )
    assert out.stream_update.tool_calls == [
        {"name": "find_places", "input": {"q": "sunset"}}
    ]


def test_collab_tool_call_is_visible_without_copying_the_child_prompt():
    out = map_codex_event(
        {
            "type": "item.started",
            "item": {
                "id": "i-agent",
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
                "status": "inProgress",
                "model": "gpt-5.6-luna",
                "prompt": "a very large delegated prompt that must not be copied",
                "receiverThreadIds": ["child-1"],
            },
        }
    )
    assert out.stream_update.tool_calls == [
        {
            "name": "Subagent",
            "input": {
                "tool": "spawnAgent",
                "status": "inProgress",
                "model": "gpt-5.6-luna",
                "receiver_thread_ids": ["child-1"],
            },
        }
    ]
    assert "prompt" not in out.stream_update.tool_calls[0]["input"]


def test_context_compaction_is_visible_as_tool_telemetry():
    out = map_codex_event(
        {
            "type": "item.started",
            "item": {"id": "compact-1", "type": "contextCompaction"},
        }
    )
    assert out.stream_update.tool_calls == [
        {"name": "ContextCompaction", "input": {"item_id": "compact-1"}}
    ]


def test_agent_message_is_not_streamed_but_captured_as_final():
    out = map_codex_event(
        {
            "type": "item.completed",
            "item": {"id": "i3", "type": "agent_message", "text": "the answer"},
        }
    )
    assert out.stream_update is None  # never echoed to thinking window
    assert out.final_message == "the answer"  # captured as fallback answer


def test_turn_completed_yields_usage():
    out = map_codex_event(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
    )
    assert out.usage == {"input_tokens": 100, "output_tokens": 20}


def test_unknown_event_type_is_inert():
    out = map_codex_event({"type": "reasoning", "item": {"type": "reasoning"}})
    assert out.stream_update is None and out.thread_id is None and out.usage is None


# dv-c7d1: failed tool items must reach tool_calls as status=error.


def _completed(item):
    return map_codex_event({"type": "item.completed", "item": item})


def test_started_tool_item_carries_its_id():
    out = map_codex_event(
        {
            "type": "item.started",
            "item": {"id": "item_3", "type": "mcp_tool_call", "tool": "t"},
        }
    )
    assert out.item_id == "item_3"


def test_failed_mcp_tool_call_reports_error():
    # Verbatim shape from codex-cli 0.157.1 under the confined profile.
    out = _completed(
        {
            "id": "item_3",
            "type": "mcp_tool_call",
            "server": "probe",
            "tool": "magic_word",
            "arguments": {},
            "result": None,
            "error": {
                "message": "MCP tool call requires approval, but approval policy is never"
            },
            "status": "failed",
        }
    )
    assert out.item_id == "item_3"
    assert out.tool_error == (
        "MCP tool call requires approval, but approval policy is never"
    )
    assert out.stream_update is None


def test_failed_mcp_without_message_still_fails():
    out = _completed({"id": "a", "type": "mcp_tool_call", "status": "failed"})
    assert out.tool_error == "MCP tool call failed"


def test_completed_mcp_tool_call_has_no_error():
    out = _completed(
        {
            "id": "item_2",
            "type": "mcp_tool_call",
            "result": {"content": [{"type": "text", "text": "ok"}]},
            "error": None,
            "status": "completed",
        }
    )
    assert out.tool_error is None


def test_nonzero_command_exit_reports_error():
    out = _completed(
        {
            "id": "c1",
            "type": "command_execution",
            "command": "cat x",
            "aggregated_output": "cat: x: Permission denied\n",
            "exit_code": 1,
            "status": "failed",
        }
    )
    assert out.tool_error == "exit code 1: cat: x: Permission denied"


def test_zero_command_exit_has_no_error():
    out = _completed(
        {"id": "c1", "type": "command_execution", "exit_code": 0, "status": "completed"}
    )
    assert out.tool_error is None
