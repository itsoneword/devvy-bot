import asyncio
import json

import pytest

from src.evaluator.service import EvaluatorService


class _FakeProcess:
    """Stand-in for asyncio.subprocess.Process capturing stdin input."""

    def __init__(self, stdout: bytes = b"[]", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.received_input: bytes | None = None

    async def communicate(self, input: bytes | None = None):  # noqa: A002
        self.received_input = input
        return self._stdout, self._stderr


@pytest.fixture
def service():
    # These tests exercise schema/unwrap/dispatch only; run() deps unused.
    return EvaluatorService(
        memory_repo=None,
        writer=None,
        token_tracker=None,
        db=None,
    )


def test_codex_output_schema_is_object_wrapping_array(service):
    # OpenAI structured outputs reject a top-level array schema; the root must
    # be an object. We wrap the op list under "extractions".
    schema = service._codex_output_schema()
    assert schema["type"] == "object"
    assert "extractions" in schema["properties"]
    # OpenAI strict structured outputs require additionalProperties:false on
    # every object node (verified against the live API).
    assert schema["additionalProperties"] is False
    items = schema["properties"]["extractions"]["items"]
    assert items["additionalProperties"] is False
    assert items["properties"]["op"]["enum"] == [
        "add",
        "supersede",
        "invalidate",
        "flag_conflict",
    ]
    # Nullable: the metadata-only ops (invalidate/flag_conflict) carry no tier.
    assert items["properties"]["tier"]["enum"] == ["pinned", "general", None]
    assert set(items["required"]) == {"op", "tier", "content"}


def test_unwrap_codex_output_extracts_array(service):
    # The object-wrapped Codex response is unwrapped back to the bare array
    # the validator expects.
    wrapped = json.dumps(
        {"extractions": [{"op": "add", "tier": "general", "content": "x"}]}
    )
    out = service._unwrap_codex_output(wrapped)
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed[0]["op"] == "add"


def test_unwrap_codex_output_passes_through_bare_array(service):
    bare = json.dumps([{"op": "add", "tier": "general", "content": "y"}])
    assert json.loads(service._unwrap_codex_output(bare)) == json.loads(bare)


async def test_invoke_cli_dispatches_to_codex(service, monkeypatch):
    calls = {}

    async def fake_codex(prompt):
        calls["codex"] = prompt
        return json.dumps([{"op": "add", "tier": "general", "content": "x"}])

    async def fake_claude(prompt):
        calls["claude"] = prompt
        return "[]"

    monkeypatch.setattr(service, "_invoke_codex_cli", fake_codex)
    monkeypatch.setattr(service, "_invoke_claude_cli", fake_claude)

    out = await service._invoke_cli("PROMPT", backend="openai_codex")
    assert "codex" in calls and "claude" not in calls
    assert json.loads(out)[0]["op"] == "add"


async def test_invoke_claude_cli_pipes_prompt_via_stdin_not_argv(service, monkeypatch):
    """Regression test: a long conversation embedded in the evaluator prompt
    used to be passed as a CLI argument (`claude -p "<prompt>"`), which blows
    past the OS ARG_MAX limit ("Argument list too long") on large
    conversations. The prompt must go via stdin instead, which has no such
    cap."""
    huge_prompt = "x" * 5_000_000  # far past any ARG_MAX, trivial over stdin
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess(stdout=b'{"result": "[]"}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await service._invoke_claude_cli(huge_prompt)

    assert huge_prompt not in captured["args"]  # never placed in argv
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert result == "[]"


async def test_invoke_codex_cli_pipes_prompt_via_stdin_not_argv(service, monkeypatch):
    """Same ARG_MAX regression, codex backend: prompt must go via stdin with
    the `-` positional argument, not as a raw argv element."""
    from pathlib import Path

    huge_prompt = "y" * 5_000_000
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        # Simulate codex writing its --output-schema-constrained result to
        # the -o path before this coroutine returns.
        out_path = Path(args[args.index("-o") + 1])
        out_path.write_text('{"extractions": []}')
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await service._invoke_codex_cli(huge_prompt)

    assert huge_prompt not in captured["args"]
    assert captured["args"][-1] == "-"  # stdin sentinel, not the prompt
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert json.loads(result) == []  # _unwrap_codex_output strips the wrapper
