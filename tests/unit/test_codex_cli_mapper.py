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
