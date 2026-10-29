"""Background jobs on the router (dv-ef90): one agent turn in its own process.

`scripts/bgjob` (and taskpump through it) and the server monitor used to run
the CLI in print mode with every permission bypassed and no checks. They run
outside the bot process, so the bot's in-process hook cannot reach them. This
module gives them the same turn through the Agent SDK with:

* the one tool-call policy (``src/security/policy.py``), evaluated here, so
  allow/deny holds even when the bot is down;
* an ``ask`` sent to the bot's internal API, which shows the owner the usual
  Telegram button and waits up to :data:`ASK_TIMEOUT_SECONDS`; no bot, no
  answer, or a connection error all mean deny;
* a ``model_calls`` row written through the bot (the job's sandbox cannot
  write the database itself), carrying the job's harness trace -- its
  ``tool_calls`` rows (dv-2260) -- in the same POST. A bot that is down
  loses both; the job's answer and exit code do not change.

``scripts/agentjob`` is the command-line entry point.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
)
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    McpStdioServerConfig,
    SettingSource,
)

from src.security import policy
from src.utils.constants import DEFAULT_CLAUDE_MAX_BUFFER_SIZE
from src.utils.paths import bot_claude_md_excludes

from .harness import CLAUDE_ACCOUNT_ENV, claude_program
from .quota import QUOTA_EXIT, parse_limit, quota_line, usage_line
from .routes import Route, cost_for, openrouter_key, provider_env
from .trace import TraceCollector
from .types import CLAUDE_HARNESS, CODEX_HARNESS, PURPOSES

logger = structlog.get_logger()

# Owner decision 2026-10-04: an unattended run that hits an ask rule waits for
# the owner's button up to one hour, then is denied.
ASK_TIMEOUT_SECONDS = 3600.0
# How long the hook may run; the SDK kills it otherwise.
_HOOK_TIMEOUT_SECONDS = int(ASK_TIMEOUT_SECONDS) + 120
# Any Task-tool subagents a job spawns stay on sonnet whatever the job's model.
SUBAGENT_MODEL = "sonnet"

ASK_PATH = "/internal/policy/ask"
MODEL_CALLS_PATH = "/internal/model_calls"

# dv-b6dd: the job ends (and everything it started is killed) when its turn
# ends. Background work still live then gets this many follow-up turns asking
# the agent to wait for it; still live after them -> exit INCOMPLETE_EXIT.
# Env BGJOB_MAX_NUDGES overrides (0 = mark only, no extra turn).
MAX_NUDGES = 2
INCOMPLETE_EXIT = 3
# QUOTA_EXIT (4, src/model_router/quota.py): the turn ended on a usage limit.


@dataclass(frozen=True)
class JobSpec:
    purpose: str
    name: str
    prompt: str
    model: str
    working_directory: Path
    full_access: bool
    unrestricted: bool
    profile: Optional[str] = None
    cli_path: Optional[str] = None
    # dv-58db: the pump worktree the job may delete in without asking.
    free_root: Optional[Path] = None
    # dv-781f: task_id / task_title / stage for the ask explainer, fixed at
    # launch (`agentjob --ask-context`); profile and job name are added here.
    ask_context: Optional[Dict[str, str]] = None
    # Which settings the CLI reads (dv-fe02, owner 3.2). bgjob and the server
    # monitor keep the owner's user settings; pump stages pass ("project",)
    # so they get no plugin skills/agents and no deviz SessionStart hook,
    # whose "claim the task" contradicts the stage's "do not edit .deviz/".
    setting_sources: Tuple[SettingSource, ...] = ("user", "project")
    # Where to write the turn's session id (bgjob: <jobdir>/session_id), so
    # LLM Analytics links the job to its transcript exactly (dv-d2a3).
    session_file: Optional[Path] = None
    # dv-d63d: the job route (``CallRoutes.resolve_job``) the job runs on;
    # ``model`` is its model. None = the Claude subscription, as before.
    route: Optional[Route] = None
    # dv-d63d: the profile's ``extra_dirs`` a confined Codex job may write
    # (the Claude job gets them from the bgjob box alone).
    extra_dirs: Tuple[Path, ...] = ()

    def ask_context_body(self) -> Dict[str, str]:
        """The ask's ``context``: profile, job name, then the launch's keys."""
        body = {"profile": self.profile, "job": self.name}
        body.update(self.ask_context or {})
        return {k: v for k, v in body.items() if isinstance(v, str) and v}


# The launch-time keys `agentjob --ask-context` takes; profile and job name
# come from the spec itself.
ASK_CONTEXT_KEYS = ("task_id", "task_title", "stage")


def parse_ask_context(text: Optional[str]) -> Optional[Dict[str, str]]:
    """`--ask-context` JSON -> its known str keys; ``None`` on anything else.

    A bad value costs the explainer its context, never the job.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        logger.warning("ask context is not JSON, ignored")
        return None
    if not isinstance(data, dict):
        return None
    kept = {
        k: v for k, v in data.items() if k in ASK_CONTEXT_KEYS and isinstance(v, str)
    }
    return kept or None


def pump_worktree_root(cwd: Path) -> Optional[Path]:
    """*cwd* when it is a pump worktree, else ``None`` (dv-58db).

    A pump worktree is ``$PUMP_WORKTREES/<id>`` or ``.../<board>/<id>``
    (default ``<repo>/../pump-worktrees``, as in ``scripts/taskpump``) whose
    ``.git`` is a file, i.e. a linked worktree. Resolved first, so a symlink
    into the dir from outside, or out of it, does not count. Not the job's
    cwd in general: a non-pump job runs in the live checkout.
    """
    default = Path(__file__).resolve().parents[2].parent / "pump-worktrees"
    worktrees = Path(os.environ.get("PUMP_WORKTREES") or default).resolve()
    resolved = Path(cwd).resolve()
    try:
        parts = resolved.relative_to(worktrees).parts
    except ValueError:
        return None
    if len(parts) not in (1, 2) or not (resolved / ".git").is_file():
        return None
    if len(parts) == 2 and (worktrees / parts[0] / ".git").exists():
        return None  # a subdir of a worktree, not a board's worktree
    return resolved


def _write_session_file(path: Optional[Path], session_id: Any) -> None:
    if path is None or not isinstance(session_id, str) or not session_id:
        return
    try:
        path.write_text(session_id + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("session_file_write_failed", path=str(path), error=str(exc))


class BotLink:
    """The job's line to the bot: the internal API with the shared secret."""

    def __init__(self, base_url: str, secret: Optional[str]) -> None:
        self._base = base_url.rstrip("/")
        self._secret = secret

    def post(self, path: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        if not self._secret:
            raise RuntimeError("no bot API secret available")
        request = urllib.request.Request(
            self._base + path,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._secret}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode() or "{}")
        return data if isinstance(data, dict) else {}

    async def ask(
        self,
        reason: str,
        preview: str,
        source: str,
        context: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Owner's answer to an ask rule; False on any failure."""
        body: Dict[str, Any] = {"reason": reason, "preview": preview, "source": source}
        if context:
            body["context"] = context
        try:
            answer = await asyncio.to_thread(
                self.post, ASK_PATH, body, ASK_TIMEOUT_SECONDS + 60
            )
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            logger.warning("ask could not reach the bot -> denied", error=str(exc))
            return False
        return bool(answer.get("approved"))

    async def record(self, row: Dict[str, Any]) -> bool:
        try:
            await asyncio.to_thread(self.post, MODEL_CALLS_PATH, row, 15)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            logger.warning("model call not recorded", error=str(exc))
            return False
        return True


class RemoteModelCallRecorder:
    """A ``ModelCallRepository`` stand-in for processes whose sandbox makes
    bot.db read-only (dv-e690): each row goes to the bot's
    ``/internal/model_calls``, which writes it. Plugs into ``ModelRouter``
    as its recorder; a bot that is down loses the row, never the call."""

    def __init__(self, link: BotLink) -> None:
        self._link = link

    async def record(self, call: Any) -> None:
        await self._link.record(dataclasses.asdict(call))


def install_job_router(repo_root: Path) -> Any:
    """Make ``get_router()`` in a job process a router that records through
    the bot. Call once at the top of a job script that makes router calls
    (``decide`` / ``complete``). Returns the router."""
    from .router import ModelRouter, set_router

    router = ModelRouter(
        recorder=RemoteModelCallRecorder(bot_link_from_env(repo_root))  # type: ignore[arg-type]
    )
    set_router(router)
    return router


async def job_tool_decision(
    tool_name: str,
    tool_input: Dict[str, Any],
    turn: policy.TurnPolicy,
    link: BotLink,
    source: str,
    ask_context: Optional[Dict[str, str]] = None,
    backend: str = "job",
) -> Optional[str]:
    """One tool call of a job: ``None`` = run it, else the deny reason.

    The one rule set (``policy.decide``); an ask goes to the owner through
    the bot. Shared by the Claude job's hook and the Codex job's policy
    endpoint (dv-d63d), so neither harness has a rule path of its own."""
    decision = policy.decide(tool_name, tool_input, turn)
    if decision.action == policy.DENY:
        logger.warning(
            "PreToolUse hook denied tool call",
            backend=backend,
            tool_name=tool_name,
            reason=decision.reason,
        )
        return decision.reason
    if decision.action == policy.ASK:
        approved = await link.ask(
            decision.reason, decision.preview, source, context=ask_context
        )
        if not approved:
            logger.warning(
                "Destructive op denied by confirmation gate",
                backend=backend,
                tool_name=tool_name,
                reason=decision.reason,
            )
            return f"Denied by owner (confirmation gate): {decision.reason}"
    return None


def policy_hook(
    turn: policy.TurnPolicy,
    link: BotLink,
    source: str,
    ask_context: Optional[Dict[str, str]] = None,
) -> Any:
    """The job's PreToolUse hook: same rules as chat, asks through the bot.

    ``ask_context`` goes with every ask, for the explainer (dv-781f)."""

    async def pre_tool_use(
        input_data: Dict[str, Any], tool_use_id: Optional[str], context: Any
    ) -> Dict[str, Any]:
        tool_name = str(input_data.get("tool_name") or "")
        tool_input = input_data.get("tool_input") or {}
        reason = await job_tool_decision(
            tool_name, tool_input, turn, link, source, ask_context
        )
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return pre_tool_use


def job_turn_policy(spec: JobSpec) -> policy.TurnPolicy:
    """The policy flags of the job's profile, for either harness."""
    return policy.TurnPolicy(
        working_directory=spec.working_directory,
        full_access=spec.full_access,
        unrestricted=spec.unrestricted,
        free_root=spec.free_root,
    )


_IMAGE_SERVER = Path(__file__).resolve().parent.parent / "mcp" / "job_image_server.py"


def job_image_server(spec: JobSpec) -> McpStdioServerConfig:
    """The stdio ``agent`` server of a job: ``view_image`` only (dv-275f).

    One config for both harnesses. Roots are the job's workspace and extra
    dirs; ``sys.executable`` is the bot's venv (Pillow lives there)."""
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(_IMAGE_SERVER)],
        "env": {
            "JOB_IMAGE_WORKDIR": str(spec.working_directory),
            "JOB_IMAGE_EXTRA_DIRS": os.pathsep.join(str(d) for d in spec.extra_dirs),
        },
    }


def job_options(spec: JobSpec, link: BotLink) -> ClaudeAgentOptions:
    turn = job_turn_policy(spec)
    return ClaudeAgentOptions(
        model=spec.model,
        cwd=str(spec.working_directory),
        cli_path=claude_program(spec.cli_path),
        # The user's and the project's settings (CLAUDE.md, skills, MCP servers
        # of the working dir), but NOT `.claude/settings.local.json`: that file
        # is the owner's interactive session's permission cache. In the devops
        # dir it says `defaultMode: dontAsk` with an ask list, and the CLI
        # applies it even under bypassPermissions, so a job's `rm` was denied
        # right after the owner approved it (2026-10-06; print mode had the
        # same problem). Pump stages narrow it to the project (JobSpec).
        setting_sources=list(spec.setting_sources),
        # Neither the owner's global CLAUDE.md nor the install dir's (dv-fe02):
        # the upward walk would load them from any cwd under $HOME.
        settings=json.dumps({"claudeMdExcludes": list(bot_claude_md_excludes())}),
        # The job has nobody at a prompt; the hook is the gate.
        permission_mode="bypassPermissions",
        env=_job_env(spec),
        mcp_servers={"agent": job_image_server(spec)},
        hooks={
            "PreToolUse": [
                HookMatcher(
                    hooks=[
                        policy_hook(
                            turn,
                            link,
                            source=spec.name,
                            ask_context=spec.ask_context_body(),
                        )
                    ],
                    timeout=_HOOK_TIMEOUT_SECONDS,
                )
            ]
        },
        stderr=lambda line: print(line, file=sys.stderr, flush=True),
        # Same ceiling as bot turns: a job reading a screenshot died at 1 MiB.
        max_buffer_size=DEFAULT_CLAUDE_MAX_BUFFER_SIZE,
    )


def _job_env(spec: JobSpec) -> Dict[str, str]:
    """The CLI env: the subscription account, subagents on sonnet; on a
    third-party route (OpenRouter) ``provider_env`` on top, which also keeps
    subagents and every tier alias on the route's model."""
    env = {**CLAUDE_ACCOUNT_ENV, "CLAUDE_CODE_SUBAGENT_MODEL": SUBAGENT_MODEL}
    if spec.route is not None and spec.route.is_third_party:
        env.update(provider_env(spec.route, openrouter_key()))
    return env


class BackgroundTasks:
    """The CLI's background tasks of this job (dv-b6dd): Bash
    run_in_background, a foreground Bash the CLI moved to the background at
    its timeout, background agents, monitors. Fed every stream message; a
    task is live from ``task_started`` until a terminal status arrives in
    either a ``task_notification`` or a ``task_updated`` (the SDK warns some
    terminal states come only as the latter)."""

    def __init__(self) -> None:
        self._live: Dict[str, str] = {}

    def feed(self, message: Any) -> None:
        if isinstance(message, TaskStartedMessage):
            self._live[message.task_id] = message.description or (
                message.task_type or "task"
            )
        elif isinstance(message, TaskNotificationMessage):
            if message.status in TERMINAL_TASK_STATUSES:
                self._live.pop(message.task_id, None)
        elif isinstance(message, TaskUpdatedMessage):
            status = message.status or (message.patch or {}).get("status")
            if status in TERMINAL_TASK_STATUSES:
                self._live.pop(message.task_id, None)

    def live(self) -> List[Tuple[str, str]]:
        """``[(task_id, description)]`` still running."""
        return list(self._live.items())


def _ppid(proc_root: Path, pid: int) -> Optional[int]:
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
        # The comm field may hold spaces/parens: the state and ppid follow
        # the LAST ')'.
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _cmdline(proc_root: Path, pid: int) -> str:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return "?"
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()[:120] or "?"


def orphans(
    proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup")
) -> List[Tuple[int, str]]:
    """Processes left in this job's systemd unit outside its own process
    tree (dv-b6dd): a ``nohup … &`` or ``setsid`` the CLI does not know
    about, which systemd kills when the job ends. Self, its ancestors
    (run.sh, bwrap) and its descendants (the CLI, MCP servers, tracked
    tasks, foreground testrun leaves) are not orphans.

    Scans only inside a ``bgjob-*.service`` unit (the whole unit, so the
    delegated ``box/testrun-*`` leaves count); anywhere else, e.g. agentjob
    run from a login shell, it returns [] rather than report the session's
    unrelated processes. Returns ``[(pid, cmdline[:120])]``."""
    me = os.getpid()
    try:
        lines = (proc_root / str(me) / "cgroup").read_text().splitlines()
    except OSError:
        return []
    path = next((ln[3:] for ln in lines if ln.startswith("0::")), "")
    parts = [p for p in path.strip("/").split("/") if p]
    unit = next(
        (
            i
            for i, p in enumerate(parts)
            if p.startswith("bgjob-") and p.endswith(".service")
        ),
        None,
    )
    if unit is None:
        return []
    root = cgroup_root.joinpath(*parts[: unit + 1])
    pids: List[int] = []
    for procs in sorted(root.rglob("cgroup.procs")):
        try:
            pids += [int(x) for x in procs.read_text().split() if x.isdigit()]
        except OSError:
            continue

    ancestors = set()
    pid: Optional[int] = _ppid(proc_root, me)
    while pid and pid not in ancestors:
        ancestors.add(pid)
        pid = _ppid(proc_root, pid)

    found: List[Tuple[int, str]] = []
    for candidate in sorted(set(pids)):
        if candidate == me or candidate in ancestors:
            continue
        seen = set()
        pid = _ppid(proc_root, candidate)
        if pid is None:  # gone since the cgroup was read
            continue
        while pid and pid != me and pid not in seen:
            seen.add(pid)
            pid = _ppid(proc_root, pid)
        if pid != me:
            found.append((candidate, _cmdline(proc_root, candidate)))
    return found


def _max_nudges() -> int:
    try:
        return max(0, int(os.environ.get("BGJOB_MAX_NUDGES", MAX_NUDGES)))
    except ValueError:
        return MAX_NUDGES


def _live_work(tasks: List[Tuple[str, str]], procs: List[Tuple[int, str]]) -> List[str]:
    return [f"task {tid} ({desc})" for tid, desc in tasks] + [
        f"pid {pid} ({cmd})" for pid, cmd in procs
    ]


def nudge_text(work: List[str]) -> str:
    """The follow-up turn when the agent answered with work still running."""
    listed = "\n".join(f"- {w}" for w in work)
    return (
        "Your turn ended, but work you started is still running:\n"
        f"{listed}\n"
        "This is an unattended job: it ends when you answer, and everything "
        "still running is killed then. No notification will reach you later. "
        "Wait for this work now, in the foreground (block on the task's "
        "output, or a foreground wait loop of at most 600000 ms per Bash "
        "call), finish what it was for, then write your final answer again "
        "with its <summary>. If the work is not needed, stop it first."
    )


async def run_job(spec: JobSpec, link: BotLink, out: Any = None) -> int:
    """Run the job; print its answer to ``out`` (stdout); return an exit code.

    Also writes the ``model_calls`` row and the job's tool rows through the
    bot.
    """
    if spec.purpose not in PURPOSES:
        raise ValueError(f"unknown model call purpose: {spec.purpose!r}")
    out = out if out is not None else sys.stdout
    route = spec.route
    if route is not None and route.harness == CODEX_HARNESS:
        from .job_codex import run_codex_job

        return await run_codex_job(spec, link, out)
    if route is not None and route.harness != CLAUDE_HARNESS:
        # Fail loud, never run the job somewhere else (no fallback, owner
        # 2026-10-26).
        print(
            f"agentjob failed: job harness {route.harness!r} is not supported",
            file=sys.stderr,
            flush=True,
        )
        return 2
    try:
        options = job_options(spec, link)
    except RuntimeError as exc:  # provider_env: no OpenRouter key in the store
        print(f"agentjob failed: {exc}", file=sys.stderr, flush=True)
        return 1
    client = ClaudeSDKClient(options)
    started = time.monotonic()
    version: Optional[str] = None
    model: Optional[str] = spec.model
    final: Optional[ResultMessage] = None
    texts: List[str] = []
    error: Optional[str] = None
    # The same collector as a bot turn; no sink: the rows travel with the
    # model_calls record at the end.
    trace = TraceCollector()
    # One call however many turns: usage is per turn and summed; the CLI's
    # total_cost_usd is already the session's running total (dv-4517).
    cost: Optional[float] = None
    usage_sum = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    tasks = BackgroundTasks()
    left: List[str] = []
    # dv-55ad: the last usage event (raw info, seen at); printed at the end.
    latest_usage: Optional[Tuple[Any, datetime]] = None
    try:
        await client.connect()
        prompt = spec.prompt
        nudges = _max_nudges()
        for attempt in range(nudges + 1):
            await client.query(prompt)
            # Each turn's own result decides: an error in a nudge turn must
            # not fall back to the earlier turn's success.
            final, texts = None, []
            async for message in client.receive_response():
                trace.feed_sdk(message)
                tasks.feed(message)
                if isinstance(message, RateLimitEvent):
                    latest_usage = (message.rate_limit_info.raw, datetime.now(UTC))
                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    version = message.data.get("claude_code_version") or version
                    model = message.data.get("model") or model
                    # Written at init so a job that dies mid-turn is linked too.
                    _write_session_file(
                        spec.session_file, message.data.get("session_id")
                    )
                elif isinstance(message, AssistantMessage):
                    texts = [
                        block.text
                        for block in message.content
                        if isinstance(block, TextBlock) and block.text
                    ]
                elif isinstance(message, ResultMessage):
                    final = message
                    cost = message.total_cost_usd or cost
                    for key in usage_sum:
                        usage_sum[key] += int((message.usage or {}).get(key) or 0)
            if final is None or final.is_error:
                break
            left = _live_work(tasks.live(), orphans())
            if not left or attempt == nudges:
                break
            logger.info("job left background work running, nudging", work=left)
            prompt = nudge_text(left)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    # The box cannot write the shared record: bgjob summarize records this
    # line, whatever the exit (dv-55ad). A third-party route sends no windows.
    line = usage_line(*latest_usage) if latest_usage is not None else None
    if line is not None and not (route is not None and route.is_third_party):
        print(line, file=sys.stderr, flush=True)

    trace.close(completed=final is not None and not final.is_error)

    if final is not None:
        _write_session_file(spec.session_file, final.session_id)

    if final is not None and not final.is_error:
        answer = final.result if final.result is not None else "\n".join(texts)
        print(answer, file=out, flush=True)
        if left:
            reason = f"{len(left)} background task(s) still running: " + "; ".join(left)
            print(f"agentjob incomplete: {reason}", file=sys.stderr, flush=True)
            outcome, code, error = "incomplete", INCOMPLETE_EXIT, reason[:500]
        else:
            outcome, code = "ok", 0
    else:
        reason = error or (
            str(final.result or final.subtype) if final is not None else "no result"
        )
        print(f"agentjob failed: {reason}", file=sys.stderr, flush=True)
        outcome, code, error = "error", 1, reason[:500]
        # dv-7ad9: a usage limit is not a failure of the work. Exit
        # QUOTA_EXIT with the reset time on stderr; the pump pauses on it.
        limit = parse_limit(reason)
        if limit is not None:
            print(quota_line(limit), file=sys.stderr, flush=True)
            code = QUOTA_EXIT

    provider: Optional[str] = None
    if route is not None and route.is_third_party:
        # The CLI prices on the Anthropic table: wrong off the subscription.
        provider = route.provider
        cost = cost_for(route, model=model, usage=usage_sum)

    await link.record(
        {
            "purpose": spec.purpose,
            "kind": "turn",
            "harness": CLAUDE_HARNESS,
            "provider": provider,
            "harness_version": version,
            "model": model,
            "attended": False,
            "profile": spec.profile,
            "session_id": final.session_id if final is not None else None,
            "input_tokens": usage_sum["input_tokens"],
            "output_tokens": usage_sum["output_tokens"],
            "cache_read_tokens": usage_sum["cache_read_input_tokens"],
            "cache_creation_tokens": usage_sum["cache_creation_input_tokens"],
            "cost_usd": cost,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outcome": outcome,
            "error": error,
            "trace_id": trace.trace_id,
            "tools": _wire_rows(trace.rows()),
            "job_name": spec.name,
        }
    )
    return code


def _wire_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tool rows as JSON: datetimes to ISO strings."""
    return [
        {k: v.isoformat() if isinstance(v, datetime) else v for k, v in row.items()}
        for row in rows
    ]


def bot_link_from_env(repo_root: Path) -> BotLink:
    """The bot's internal API as the scripts know it: host/port from the
    config env, secret from the secrets file (never logged)."""
    from src.utils.paths import read_bot_secret

    dotenv = repo_root / ".env"
    env = _dotenv_values(dotenv)
    host = (
        os.environ.get("API_SERVER_HOST") or env.get("API_SERVER_HOST") or "127.0.0.1"
    )
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = os.environ.get("API_SERVER_PORT") or env.get("API_SERVER_PORT") or "8080"
    secret = read_bot_secret("WEBHOOK_API_SECRET", dotenv=dotenv)
    return BotLink(f"http://{host}:{port}", secret)


def _dotenv_values(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip("\"'")
    return values
