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
    # OpenAI strict mode also requires EVERY property to be listed in
    # `required`; optionality is expressed by the "null" type member alone.
    # Listing only {op, tier, content} made the server answer 400
    # invalid_json_schema ("Missing 'label'") and every Codex evaluator run
    # died with `Codex CLI failed (exit 1):` and an empty stderr (dv-0c01).
    assert set(items["required"]) == set(items["properties"])


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


def test_codex_schema_every_op_field_is_nullable_except_op(service):
    """Strict mode forces every field into `required`, so every field that a
    metadata-only op does not carry MUST accept null — otherwise requiring
    them would make `invalidate`/`flag_conflict` unrepresentable."""
    items = service._codex_output_schema()["properties"]["extractions"]["items"]
    for name, spec in items["properties"].items():
        if name == "op":
            assert spec["type"] == "string"
            continue
        assert "null" in spec["type"], name


async def test_invoke_codex_cli_reports_the_error_it_finds_on_stdout(
    service, monkeypatch
):
    """`codex exec --json` puts API failures on STDOUT as `error`/`turn.failed`
    events and leaves stderr empty. Reporting stderr alone produced the
    useless live line `Evaluator failed: Codex CLI failed (exit 1): `."""
    stdout = b"\n".join(
        [
            b'{"type":"thread.started","thread_id":"th-1"}',
            b'{"type":"error","message":"400 invalid_json_schema: Missing \'label\'."}',
            b'{"type":"turn.failed","error":'
            b'{"message":"400 invalid_json_schema: Missing \'label\'."}}',
        ]
    )

    async def fake_exec(*args, **kwargs):
        return _FakeProcess(stdout=stdout, stderr=b"", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError) as excinfo:
        await service._invoke_codex_cli("PROMPT")
    message = str(excinfo.value)
    assert "exit 1" in message
    assert "invalid_json_schema" in message
    # turn.failed restates the error event; say it once.
    assert message.count("invalid_json_schema") == 1


async def test_invoke_codex_cli_never_reports_an_empty_reason(service, monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProcess(stdout=b"", stderr=b"", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match=r"exit 1\): \(no output\)"):
        await service._invoke_codex_cli("PROMPT")


async def test_invoke_codex_cli_runs_read_only_outside_the_bot_cwd(
    service, monkeypatch
):
    """This call reads a prompt and emits JSON. It used to ask for
    danger-full-access in the bot's own working directory, which bought
    nothing and pointed a shell-capable agent at the repository."""
    from pathlib import Path

    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        Path(args[args.index("-o") + 1]).write_text('{"extractions": []}')
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await service._invoke_codex_cli("PROMPT")

    args = captured["args"]
    assert args[args.index("-s") + 1] == "read-only"
    assert "danger-full-access" not in args
    # -C points at the same throwaway temp dir the schema/out files live in.
    workdir = Path(args[args.index("-C") + 1])
    assert workdir == Path(args[args.index("-o") + 1]).parent
