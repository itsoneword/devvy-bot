"""Plain Codex calls through ``codex exec`` (dv-ef90).

A plain call reads a prompt and emits text, so it runs read-only in a
throwaway directory, with the allowlisted child env, always on an explicit
model, and its whole process group is killed on timeout.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.agent_env import agent_child_env

from .types import CallRequest, CallResult, ModelCallError, ModelCallTimeout

_MODEL_PREFIX = "openai-codex/"


class CodexPlainCaller:
    """Runs one :class:`CallRequest` on the Codex CLI and returns its text."""

    def __init__(self, codex_bin: str = "codex") -> None:
        self._codex_bin = codex_bin

    def argv(self, request: CallRequest, workdir: Path) -> List[str]:
        argv = [
            self._codex_bin,
            "exec",
            "--json",
            "-C",
            str(workdir),
            "-m",
            request.model.removeprefix(_MODEL_PREFIX),
            "-s",
            "read-only",
            "--skip-git-repo-check",
        ]
        if request.json_schema is not None:
            schema_path = workdir / "schema.json"
            schema_path.write_text(json.dumps(request.json_schema))
            argv += ["--output-schema", str(schema_path)]
        # The prompt goes over stdin ("-"): it can exceed ARG_MAX.
        return argv + ["-o", str(workdir / "out.txt"), "-"]

    async def __call__(self, request: CallRequest) -> CallResult:
        prompt = request.prompt
        if request.system_prompt:
            prompt = f"{request.system_prompt}\n\n{prompt}"
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            process = await asyncio.create_subprocess_exec(
                *self.argv(request, workdir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=agent_child_env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=prompt.encode("utf-8")),
                    timeout=request.timeout_seconds,
                )
            except asyncio.TimeoutError:
                await _kill_group(process)
                raise ModelCallTimeout(
                    f"timeout after {request.timeout_seconds:g}s (codex)"
                )
            except BaseException:
                await _kill_group(process)
                raise
            if process.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                err = err or codex_stdout_error(stdout) or "(no output)"
                raise ModelCallError(
                    f"Codex CLI failed (exit {process.returncode}): {err[-500:]}"
                )
            out_path = workdir / "out.txt"
            text = (
                out_path.read_text(encoding="utf-8").strip()
                if out_path.exists()
                else ""
            )
        usage = _usage(stdout)
        return CallResult(
            text=text,
            model=request.model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cached_input_tokens") or 0),
        )


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    """Kill the child's process group and reap it.

    `codex` is a Node shim that starts the real binary as its child, so
    killing the shim alone leaves the binary running. The child has its own
    session, so the group never includes the bot.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


def _events(stdout: bytes) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _usage(stdout: bytes) -> Dict[str, Any]:
    usage: Optional[Dict[str, Any]] = None
    for event in _events(stdout):
        if event.get("type") == "turn.completed" and isinstance(
            event.get("usage"), dict
        ):
            usage = event["usage"]
    return usage or {}


def codex_stdout_error(stdout: bytes) -> str:
    """Pull the failure reason out of a `codex exec --json` stdout stream.

    `codex exec` reports API and turn failures as JSONL `error` /
    `turn.failed` events on STDOUT and leaves stderr completely empty, so
    reporting stderr alone produced `Codex CLI failed (exit 1): ` with nothing
    after the colon (dv-0c01).
    """
    messages: List[str] = []
    for event in _events(stdout):
        if event.get("type") == "error":
            messages.append(str(event.get("message", "")))
        elif event.get("type") == "turn.failed":
            err = event.get("error") or {}
            if isinstance(err, dict):
                messages.append(str(err.get("message", "")))
    # turn.failed usually restates the preceding error event verbatim.
    deduped: List[str] = []
    for msg in messages:
        if msg and msg not in deduped:
            deduped.append(msg)
    return " | ".join(deduped)
