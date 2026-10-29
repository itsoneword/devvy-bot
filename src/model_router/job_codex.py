"""A background job on Codex (dv-d63d): ``codex exec`` instead of the Claude SDK.

``run_job`` dispatches here when the job route's harness is ``openai_codex``.
Same contract as the Claude job in ``job.py``:

* the one tool-call policy: the bot's own ``codex_policy_hook.py`` is mounted
  unchanged (``policy_hook_args``) and POSTs each call to a job-local
  loopback endpoint, which answers with :func:`job_tool_decision` -- the
  ``decide()`` + owner ask of the Claude job's hook. The bot's agent bridge
  cannot serve it: its tokens are minted in the bot process only;
* one ``model_calls`` row through the bot: harness ``openai_codex``, tokens
  from the last ``turn.completed`` (the thread's running total), session id
  = the codex thread id, the shell calls as its ``tool_calls`` rows;
* background work still live after the answer -> up to ``MAX_NUDGES``
  follow-up turns via ``codex exec ... resume <thread> -`` (dv-b6dd).

Codex reads ``AGENTS.md``; the job points it at the repo's ``CLAUDE.md``
(``project_doc_fallback_filenames``). Not on Codex jobs: the bot's MCP tools
and Task subagents (owner, triage 3.1) -- file and shell tools only.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog

from src.agent_backends.codex_cli import (
    STREAM_LIMIT,
    _feed_codex_trace,
    _strip_provider_prefix,
    _terminate,
    map_codex_event,
    policy_hook_args,
)
from src.agent_backends.codex_sandbox import sandbox_args
from src.agent_bridge.policy_gate import POLICY_PATH, POLICY_URL_ENV_VAR
from src.security import policy
from src.utils.agent_env import agent_child_env

from .harness import HarnessVersions, codex_program
from .job import (
    _HOOK_TIMEOUT_SECONDS,
    ASK_TIMEOUT_SECONDS,
    INCOMPLETE_EXIT,
    BotLink,
    JobSpec,
    _live_work,
    _max_nudges,
    _wire_rows,
    _write_session_file,
    job_image_server,
    job_tool_decision,
    job_turn_policy,
    nudge_text,
    orphans,
)
from .trace import TraceCollector
from .types import CODEX_HARNESS

logger = structlog.get_logger()

# What the hook script reads (src/agent_bridge/codex_policy_hook.py).
_TOKEN_ENV = "AGENT_BRIDGE_TOKEN"
_TIMEOUT_ENV = "DEVVY_POLICY_TIMEOUT_SECONDS"
# The hook's HTTP wait: the owner's hour plus margin, below codex's own hook
# timeout so the hook answers (deny) before codex kills it.
_POLICY_REQUEST_TIMEOUT = int(ASK_TIMEOUT_SECONDS) + 60
# Codex reads AGENTS.md; this repo (and the pump worktrees) carry CLAUDE.md.
PROJECT_DOC_ARGS = ["-c", 'project_doc_fallback_filenames=["CLAUDE.md"]']


class JobPolicyEndpoint:
    """Loopback HTTP endpoint the mounted hook asks before every tool call.

    Random port, random bearer token, one job. Speaks just enough HTTP/1.1
    for ``urllib``: one POST per connection, JSON in, JSON out. Anything it
    cannot read is a deny (the hook also fails closed when it gets no answer).
    """

    def __init__(self, spec: JobSpec, link: BotLink) -> None:
        self._spec = spec
        self._link = link
        self._turn = job_turn_policy(spec)
        self.token = secrets.token_urlsafe(32)
        self._server: Optional[asyncio.AbstractServer] = None
        self.url = ""
        # Codex can fire the hook twice for one tool call (policy_gate.py):
        # the second gets the first one's answer, not a second owner button.
        self._calls: Dict[str, "asyncio.Task[Dict[str, Any]]"] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}{POLICY_PATH}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def child_env(self) -> Dict[str, str]:
        return {
            POLICY_URL_ENV_VAR: self.url,
            _TOKEN_ENV: self.token,
            _TIMEOUT_ENV: str(_POLICY_REQUEST_TIMEOUT),
        }

    async def check(self, body: Dict[str, Any]) -> Dict[str, Any]:
        tool_use_id = str(body.get("tool_use_id") or "")
        if not tool_use_id:
            return await self._check(body)
        task = self._calls.get(tool_use_id)
        if task is None:
            task = asyncio.ensure_future(self._check(body))
            self._calls[tool_use_id] = task
        return await asyncio.shield(task)

    async def _check(self, body: Dict[str, Any]) -> Dict[str, Any]:
        tool_name = str(body.get("tool_name") or "")
        tool_input = body.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        try:
            reason = await job_tool_decision(
                tool_name,
                tool_input,
                self._turn,
                self._link,
                self._spec.name,
                self._spec.ask_context_body(),
                backend="codex_job",
            )
        except Exception as exc:
            logger.warning("job policy check failed -> denied", error=str(exc))
            reason = "the policy check failed"
        if reason is None:
            return {"decision": policy.ALLOW, "reason": ""}
        return {"decision": policy.DENY, "reason": reason}

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status, answer = 200, {"decision": policy.DENY, "reason": "bad request"}
        try:
            request_line = (await reader.readline()).decode("latin-1").split()
            headers: Dict[str, str] = {}
            while True:
                line = (await reader.readline()).decode("latin-1").strip()
                if not line:
                    break
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            raw = await reader.readexactly(length) if length else b""
            if headers.get("authorization") != f"Bearer {self.token}":
                status = 401
                answer = {"decision": policy.DENY, "reason": "unknown token"}
            elif len(request_line) < 2 or request_line[:2] != ["POST", POLICY_PATH]:
                status = 404
                answer = {"decision": policy.DENY, "reason": "not found"}
            else:
                body = json.loads(raw.decode() or "{}")
                if isinstance(body, dict):
                    answer = await self.check(body)
        except Exception as exc:  # unreadable request: deny, never crash
            logger.warning("job policy endpoint: bad request", error=str(exc))
        data = json.dumps(answer).encode()
        reason = {200: "OK", 401: "Unauthorized", 404: "Not Found"}[status]
        try:
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\n"
                "Connection: close\r\n\r\n".encode() + data
            )
            await writer.drain()
        finally:
            writer.close()


def _image_server_args(spec: JobSpec) -> List[str]:
    """Mount the job's ``view_image`` server (dv-275f) and switch Codex's own
    off: it never sees our roots. Pre-approved: the policy hook is the gate."""
    cfg = job_image_server(spec)
    args = [
        "mcp_servers.agent.command=" + json.dumps(cfg["command"]),
        "mcp_servers.agent.args=" + json.dumps(cfg["args"]),
        'mcp_servers.agent.default_tools_approval_mode="approve"',
        "features.view_image=false",
    ]
    args += [
        f"mcp_servers.agent.env.{var}={json.dumps(value)}"
        for var, value in cfg["env"].items()
    ]
    return [x for a in args for x in ("-c", a)]


def codex_job_argv(
    spec: JobSpec,
    program: str,
    out_file: Path,
    resume_id: Optional[str],
) -> List[str]:
    """``codex exec`` for one turn of the job; the prompt goes on stdin.

    Exec-level options precede ``resume`` (``codex exec resume`` rejects
    them as its own args), as in the chat turn's ``_build_argv``."""
    model = spec.route.model if spec.route is not None else None
    argv = [program, "exec", "--json", "-C", str(spec.working_directory)]
    if model:
        argv += ["-m", _strip_provider_prefix(model)]
    argv += sandbox_args(
        unrestricted=spec.unrestricted,
        full_access=spec.full_access,
        cwd=spec.working_directory,
        extra_dirs=spec.extra_dirs,
    )
    argv += ["--skip-git-repo-check", "-o", str(out_file)]
    argv += PROJECT_DOC_ARGS
    argv += policy_hook_args(_HOOK_TIMEOUT_SECONDS)
    argv += _image_server_args(spec)
    if resume_id:
        argv += ["resume", resume_id]
    return argv + ["-"]


class _Turn:
    """What one ``codex exec`` run left behind."""

    def __init__(self) -> None:
        self.rc: Optional[int] = None
        self.thread_id: Optional[str] = None
        self.usage: Optional[Dict[str, Any]] = None
        self.message: Optional[str] = None
        self.errors: List[str] = []
        self.stderr_tail: List[str] = []


async def _exec(
    argv: List[str],
    prompt: str,
    env: Dict[str, str],
    cwd: Path,
    trace: TraceCollector,
    session_file: Optional[Path],
) -> _Turn:
    turn = _Turn()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        # Own process group: codex is a Node shim around a Rust binary
        # (_terminate in codex_cli.py).
        start_new_session=True,
        limit=STREAM_LIMIT,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    async def drain_stderr() -> None:
        # Must never stop reading, or a chatty codex blocks (FINDINGS
        # 2026-08-30). The job's stderr is its log.
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            sys.stderr.write(text)
            turn.stderr_tail = (turn.stderr_tail + text.splitlines())[-20:]

    async def read_stdout() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            outcome = map_codex_event(event)
            _feed_codex_trace(trace, event, outcome)
            if outcome.thread_id:
                turn.thread_id = outcome.thread_id
                _write_session_file(session_file, outcome.thread_id)
            if outcome.usage is not None:
                turn.usage = outcome.usage
            if outcome.final_message:
                turn.message = outcome.final_message
            if event.get("type") in ("error", "turn.failed"):
                err = event.get("message") or (event.get("error") or {})
                if isinstance(err, dict):
                    err = err.get("message")
                turn.errors.append(str(err or event.get("type")))

    stderr_task = asyncio.create_task(drain_stderr())
    try:
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # codex exited before reading: its exit code says why
        await read_stdout()
        turn.rc = await proc.wait()
    except BaseException:
        await _terminate(proc)
        raise
    finally:
        try:
            await asyncio.wait_for(stderr_task, 10)
        except (asyncio.TimeoutError, Exception):
            stderr_task.cancel()
    return turn


def _read_answer(out_file: Path, turn: _Turn) -> str:
    try:
        text = out_file.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return text.strip() or (turn.message or "")


async def run_codex_job(spec: JobSpec, link: BotLink, out: Any = None) -> int:
    """``run_job`` on Codex: print the answer to ``out``, return the exit code
    (0 ok, 1 failed, 2 no codex here, 3 work still running), record one row through the bot.
    """
    out = out if out is not None else sys.stdout
    program = codex_program()
    if not program:
        # Same exit as an unsupported harness: this host cannot run the
        # route; never run the job somewhere else (no fallback, owner).
        print("agentjob failed: codex not found on PATH", file=sys.stderr, flush=True)
        return 2
    started = time.monotonic()
    version = await HarnessVersions().get(CODEX_HARNESS)
    trace = TraceCollector()
    endpoint = JobPolicyEndpoint(spec, link)
    error: Optional[str] = None
    turn: Optional[_Turn] = None
    thread_id: Optional[str] = None
    usage: Dict[str, Any] = {}
    answer = ""
    left: List[str] = []
    try:
        await endpoint.start()
        env = agent_child_env(endpoint.child_env())
        prompt = spec.prompt
        nudges = _max_nudges()
        with tempfile.TemporaryDirectory(prefix="codex-job-") as tmp:
            for attempt in range(nudges + 1):
                out_file = Path(tmp) / f"last-{attempt}.txt"
                argv = codex_job_argv(spec, program, out_file, thread_id)
                turn = await _exec(
                    argv,
                    prompt,
                    env,
                    spec.working_directory,
                    trace,
                    spec.session_file,
                )
                thread_id = turn.thread_id or thread_id
                usage = turn.usage or usage
                if turn.rc != 0 or turn.usage is None:
                    break
                answer = _read_answer(out_file, turn)
                left = _live_work([], orphans())
                if not left or attempt == nudges:
                    break
                logger.info("job left background work running, nudging", work=left)
                prompt = nudge_text(left)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        await endpoint.close()

    succeeded = error is None and turn is not None and turn.rc == 0
    succeeded = succeeded and turn is not None and turn.usage is not None
    trace.close(completed=succeeded)

    if succeeded:
        print(answer, file=out, flush=True)
        if left:
            reason = f"{len(left)} background task(s) still running: " + "; ".join(left)
            print(f"agentjob incomplete: {reason}", file=sys.stderr, flush=True)
            outcome, code, error = "incomplete", INCOMPLETE_EXIT, reason[:500]
        else:
            outcome, code = "ok", 0
    else:
        reason = error or _failure_reason(turn)
        print(f"agentjob failed: {reason}", file=sys.stderr, flush=True)
        outcome, code, error = "error", 1, reason[:500]

    in_tok, out_tok, cached = _usage_totals(usage)
    route = spec.route
    await link.record(
        {
            "purpose": spec.purpose,
            "kind": "turn",
            "harness": CODEX_HARNESS,
            "provider": route.provider if route is not None else None,
            "harness_version": version,
            "model": (route.model if route is not None else None) or spec.model,
            "attended": False,
            "profile": spec.profile,
            "session_id": thread_id,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_tokens": cached,
            "cache_creation_tokens": 0,
            # cost None, not 0.0 (dv-0722): a ChatGPT subscription, the CLI
            # reports no USD; the chat Codex rows do the same.
            "cost_usd": None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outcome": outcome,
            "error": error,
            "trace_id": trace.trace_id,
            "tools": _wire_rows(trace.rows()),
            "job_name": spec.name,
        }
    )
    return code


def _usage_totals(usage: Dict[str, Any]) -> Tuple[int, int, int]:
    """(input, output, cached input) of the job. ``turn.completed.usage`` is
    the thread's running total (codex_cli ``_run_turn``) and a job is one
    thread, so the last one is the job's; input as codex counts it, cached
    included, as ``codex_plain`` records it."""
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cached_input_tokens") or 0),
    )


def _failure_reason(turn: Optional[_Turn]) -> str:
    if turn is None:
        return "codex did not run"
    if turn.errors:
        return turn.errors[-1]
    tail = "\n".join(turn.stderr_tail[-5:]).strip()
    if turn.rc != 0:
        return f"codex exited {turn.rc}" + (f": {tail}" if tail else "")
    return "codex gave no result" + (f": {tail}" if tail else "")
