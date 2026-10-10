"""The Codex plain-call adapter (dv-ef90): `codex exec`, read-only, explicit
model, prompt over stdin, group kill on timeout."""

import asyncio
import json
import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.model_router import CallRequest, ModelCallError, ModelCallTimeout
from src.model_router.codex_plain import CodexPlainCaller


class _FakeProcess:
    pid = 4242

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.received_input: bytes | None = None

    async def communicate(self, input: bytes | None = None):  # noqa: A002
        self.received_input = input
        return self._stdout, self._stderr


def _request(**overrides) -> CallRequest:
    fields = dict(
        purpose="extraction",
        harness="openai_codex",
        prompt="PROMPT",
        model="openai-codex/gpt-6-sol",
        system_prompt="",
        json_schema={"type": "object"},
    )
    fields.update(overrides)
    return CallRequest(**fields)


async def test_prompt_goes_over_stdin_and_the_answer_comes_from_the_out_file(
    monkeypatch,
):
    """A conversation embedded in the prompt blows past ARG_MAX as an
    argument; it must go over stdin with the `-` sentinel."""
    huge_prompt = "y" * 5_000_000
    captured = {}
    usage = {"input_tokens": 11, "cached_input_tokens": 4, "output_tokens": 2}
    process = _FakeProcess(
        stdout=json.dumps({"type": "turn.completed", "usage": usage}).encode()
    )

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        Path(args[args.index("-o") + 1]).write_text('{"extractions": []}')
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await CodexPlainCaller()(_request(prompt=huge_prompt))

    args = captured["args"]
    assert huge_prompt not in args
    assert args[-1] == "-"
    assert process.received_input == huge_prompt.encode()
    assert result.text == '{"extractions": []}'
    assert result.input_tokens == 11 and result.output_tokens == 2
    assert result.cache_read_tokens == 4


async def test_runs_read_only_in_a_throwaway_dir_on_an_explicit_model(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        schema = Path(args[args.index("--output-schema") + 1])
        captured["schema"] = json.loads(schema.read_text())
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")

    result = await CodexPlainCaller()(_request())

    args = captured["args"]
    assert args[args.index("-s") + 1] == "read-only"
    assert "danger-full-access" not in args
    assert args[args.index("-m") + 1] == "gpt-6-sol"
    workdir = Path(args[args.index("-C") + 1])
    assert workdir == Path(args[args.index("-o") + 1]).parent
    assert workdir != Path.cwd()
    assert captured["schema"] == {"type": "object"}
    # Own process group, and the allowlisted env rather than the bot's.
    assert captured["kwargs"]["start_new_session"] is True
    assert "TELEGRAM_BOT_TOKEN" not in captured["kwargs"]["env"]
    assert result.text == ""


async def test_reports_the_error_it_finds_on_stdout(monkeypatch):
    """`codex exec --json` puts API failures on STDOUT as `error` /
    `turn.failed` events and leaves stderr empty."""
    stdout = b"\n".join(
        [
            b'{"type":"thread.started","thread_id":"th-1"}',
            b'{"type":"error","message":"400 invalid_json_schema: Missing label."}',
            b'{"type":"turn.failed","error":'
            b'{"message":"400 invalid_json_schema: Missing label."}}',
        ]
    )

    async def fake_exec(*args, **kwargs):
        return _FakeProcess(stdout=stdout, stderr=b"", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ModelCallError) as excinfo:
        await CodexPlainCaller()(_request())
    message = str(excinfo.value)
    assert "exit 1" in message
    # turn.failed restates the error event; say it once.
    assert message.count("invalid_json_schema") == 1


async def test_never_reports_an_empty_reason(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProcess(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ModelCallError, match=r"exit 1\): \(no output\)"):
        await CodexPlainCaller()(_request())


async def test_timeout_kills_the_process_group_and_is_labelled(monkeypatch):
    process = MagicMock()
    process.pid = 4242

    async def hang(input=None):  # noqa: A002
        await asyncio.sleep(30)

    process.communicate = hang
    process.wait = AsyncMock()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    killed = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    with pytest.raises(ModelCallTimeout, match="^timeout after"):
        await CodexPlainCaller()(_request(timeout_seconds=0.01))

    assert killed == [(4242, signal.SIGKILL)]
    process.wait.assert_awaited()
