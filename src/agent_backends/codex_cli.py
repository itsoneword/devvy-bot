"""Codex CLI backend: wraps `codex exec --json` for full agent parity."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

import structlog

from src.bot.window_seed import (
    SEED_TAIL_TURNS,
    SeedTail,
    build_seed_preamble,
    select_tail,
)
from src.claude.prompt_composer import PromptComposer
from src.claude.sdk_integration import ClaudeResponse, StreamUpdate
from src.claude.turn_setup import (
    compose_persona,
    composer_for,
    current_time_context,
    image_roots_env,
    prepend_items_context,
    resolve_turn_context,
)
from src.config.settings import Settings
from src.model_router.trace import current_trace
from src.storage.codex_sessions import CodexSessionStore, InMemoryCodexSessionStore
from src.storage.repositories import MessageRepository
from src.security.bash_box import BashBox, KeyEnvError, load_key_env
from src.utils.agent_env import (
    AGENT_CLI_CREDENTIAL_VARS,
    agent_child_env,
    is_env_name,
    split_mcp_env,
)

from .codex_execpolicy import ensure_rules as ensure_money_guard_rules
from .codex_sandbox import sandbox_args
from .codex_usage import CodexUsageClient

if TYPE_CHECKING:
    from src.agent_backends.protocol import BackendSession
    from src.claude.session import SessionManager
    from src.claude.tool_context import TurnContext
    from src.claude.window_bundle import WindowBundleProvider
    from src.storage.memory_repository import MemoryRetriever

logger = structlog.get_logger()

# Per-stream buffer for the codex subprocess. Well above any single line the
# CLI is known to emit (~250KB worst case observed) so readline() on stdout
# cannot raise, while still bounding memory if the CLI ever goes haywire.
# The PreToolUse hook Codex runs before every tool call (dv-1601).
_POLICY_HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent / "agent_bridge" / "codex_policy_hook.py"
)

STREAM_LIMIT = 8 * 1024 * 1024

# What `codex exec resume <id>` says when the thread is gone. Verified on
# codex-cli 0.152.0, 2026-09-06, with an unknown UUID: exit 1, NO stdout
# events at all (not even thread.started), and this single stderr line:
#   Error: thread/resume: thread/resume failed: no rollout found for thread
#   id 00000000-0000-0000-0000-000000000000 (code -32600)
# The match is deliberately narrow. The Claude facade retries on ANY resume
# error because its CLI only gives a bare exit 1; here the CLI names the
# failure, so an auth/model/API error on a resumed thread must NOT wipe the
# stored thread and re-run (dv-fbf1 was exactly that class of bug).
RESUME_FAILURE_MARKERS = ("thread/resume", "no rollout found")


class CodexResumeError(RuntimeError):
    """`codex exec resume <thread_id>` refused because the thread is gone."""

    def __init__(self, thread_id: str, message: str) -> None:
        super().__init__(message)
        self.thread_id = thread_id


def is_resume_failure(resume_id: Optional[str], reason: str) -> bool:
    """True when a failed turn was a resume the CLI could not find.

    Only meaningful when a resume was attempted; a fresh turn failing with the
    same words (it never does) is still a plain failure.
    """
    if not resume_id:
        return False
    lowered = reason.lower()
    return any(marker in lowered for marker in RESUME_FAILURE_MARKERS)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process group, then reap.

    ``proc.kill()`` alone is not enough: `codex` is a Node shim that execs a
    vendored Rust binary as a child, so SIGKILL to the shim orphans the binary
    (observed 2026-08-30, still alive 30 minutes after its turn timed out).
    The child is spawned with ``start_new_session=True`` so its pgid is its own
    pid and this cannot reach the bot itself. Reaping avoids a zombie.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


@dataclass
class CodexEventOutcome:
    """Result of interpreting one `codex exec --json` event line."""

    stream_update: Optional[StreamUpdate] = None
    thread_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    final_message: Optional[str] = None
    # Codex item id of a started/completed tool item, to pair the two.
    item_id: Optional[str] = None
    # Set on item.completed when that tool item failed (dv-c7d1).
    tool_error: Optional[str] = None


def _tool_item_error(item: Dict[str, Any]) -> Optional[str]:
    """Error text for a failed MCP / shell item, or None if it succeeded."""
    itype = item.get("type")
    status = item.get("status")
    if itype == "mcp_tool_call":
        err = item.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        if err or status == "failed":
            return str(err or "MCP tool call failed")
        return None
    if itype == "command_execution":
        code = item.get("exit_code")
        if status == "declined":
            return "command declined"
        if (isinstance(code, int) and code != 0) or status == "failed":
            tail = str(item.get("aggregated_output") or "").strip()[-200:]
            msg = f"exit code {code}" if code is not None else "command failed"
            return f"{msg}: {tail}" if tail else msg
    return None


def map_codex_event(event: Dict[str, Any]) -> CodexEventOutcome:
    """Map one Codex JSONL event to a CodexEventOutcome.

    Tool activity -> StreamUpdate.tool_calls (shown in the progress window).
    Agent messages are NOT streamed (would pollute the thinking window); the
    final answer is captured here as a fallback and authoritatively via -o.
    """
    etype = event.get("type")
    if etype == "thread.started":
        return CodexEventOutcome(thread_id=event.get("thread_id"))
    if etype == "turn.completed":
        return CodexEventOutcome(usage=event.get("usage") or {})
    if etype == "item.started":
        outcome = _map_started_item(event.get("item") or {})
        outcome.item_id = (event.get("item") or {}).get("id")
        return outcome
    if etype == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            return CodexEventOutcome(final_message=item.get("text"))
        return CodexEventOutcome(
            item_id=item.get("id"), tool_error=_tool_item_error(item)
        )
    return CodexEventOutcome()


def _feed_codex_trace(
    trace: Any, event: Dict[str, Any], outcome: "CodexEventOutcome"
) -> None:
    """Feed one Codex event to the harness trace. Codex streams a flat list
    of items: no caller tree (every call is the main agent's) and no subagent
    results; durations are wall clock between item.started and completed."""
    try:
        etype = event.get("type")
        item = event.get("item") or {}
        item_id = outcome.item_id or item.get("id")
        if etype == "item.started" and outcome.stream_update:
            calls = outcome.stream_update.tool_calls or []
            if len(calls) == 1:
                # An item without an id cannot be paired with its completion;
                # it still gets a row (left "no result" at the end).
                use_id = str(item_id or f"codex-{len(trace.rows()) + 1}")
                trace.start(use_id, calls[0]["name"], calls[0].get("input"))
        elif etype == "item.completed" and item_id:
            text = (
                item.get("aggregated_output")
                or item.get("output")
                or item.get("result")
                or ""
            )
            trace.finish(
                str(item_id),
                text if isinstance(text, str) else str(text),
                error=outcome.tool_error,
            )
    except Exception as exc:  # tracing must never break a turn
        logger.warning("codex trace feed failed", error=str(exc))


def _map_started_item(item: Dict[str, Any]) -> CodexEventOutcome:
    """Progress entry for a tool item that just started."""
    itype = item.get("type")
    if itype == "command_execution":
        cmd = str(item.get("command", ""))
        # A keyed turn's shell runs in the redaction wrapper (dv-28f2): the
        # progress view and the trace show what the model asked for.
        cmd = BashBox.unwrap(cmd) or cmd
        return CodexEventOutcome(
            stream_update=StreamUpdate(
                type="system",
                tool_calls=[{"name": "Bash", "input": {"command": cmd}}],
            )
        )
    if itype == "file_change":
        changes = item.get("changes") or [{}]
        path = str(changes[0].get("path", ""))
        return CodexEventOutcome(
            stream_update=StreamUpdate(
                type="system",
                tool_calls=[{"name": "Edit", "input": {"file_path": path}}],
            )
        )
    if itype == "mcp_tool_call":
        name = str(item.get("tool") or item.get("name") or "mcp_tool")
        args = item.get("arguments") or {}
        # Codex has sent arguments as a JSON *string* as well as an
        # object. The stream callback that intercepts send_image_to_user
        # does `input.get("file_path")` on this, so a string would raise
        # inside the callback and take the whole turn's progress with it.
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {"raw": args}
        return CodexEventOutcome(
            stream_update=StreamUpdate(
                type="system",
                tool_calls=[{"name": name, "input": args}],
            )
        )
    if itype in {"collabAgentToolCall", "collab_agent_tool_call"}:
        # App Server calls this a CollabAgentToolCallThreadItem.  The
        # exec JSONL adapter has used both camelCase and snake_case item
        # names across releases, so accept both.  Keep the persisted
        # input deliberately small: the child prompt is already part of
        # the rollout and copying it into tool_usage would recreate the
        # context-amplification problem this telemetry is meant to expose.
        details = {
            "tool": item.get("tool"),
            "status": item.get("status"),
            "model": item.get("model"),
            "receiver_thread_ids": item.get("receiverThreadIds")
            or item.get("receiver_thread_ids")
            or [],
        }
        return CodexEventOutcome(
            stream_update=StreamUpdate(
                type="system",
                tool_calls=[{"name": "Subagent", "input": details}],
            )
        )
    if itype in {"contextCompaction", "context_compaction"}:
        return CodexEventOutcome(
            stream_update=StreamUpdate(
                type="system",
                tool_calls=[
                    {
                        "name": "ContextCompaction",
                        "input": {"item_id": item.get("id")},
                    }
                ],
            )
        )
    return CodexEventOutcome()


_MCP_SERVER_KEY = re.compile(r"^mcp_servers\.([A-Za-z0-9_]+)\.")


def _with_mcp_auto_approval(overrides: List[str]) -> List[str]:
    """Append ``default_tools_approval_mode="approve"`` for every MCP server
    that ``overrides`` registers (dv-c7d1).

    Under the confined permission profile (dv-e38e) codex-cli 0.157 asks for
    approval before each MCP tool call, and ``codex exec`` runs with approval
    policy ``never``, so every call failed with "MCP tool call requires
    approval, but approval policy is never". Only the bot's own servers are
    listed here (agent bridge, config/mcp.json stdio servers, the profile
    seam); they enforce their own auth, and codex runs them outside the
    command sandbox anyway, so pre-approving them does not widen the
    filesystem confinement. Values: auto | prompt | writes | approve.
    """
    names: List[str] = []
    for arg in overrides:
        m = _MCP_SERVER_KEY.match(arg)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    out = list(overrides)
    for name in names:
        out += ["-c", f'mcp_servers.{name}.default_tools_approval_mode="approve"']
    return out


def _scrubber(values: Sequence[str]) -> Callable[[str], str]:
    """Replace every key value (raw and JSON-escaped) with ``<redacted>``.

    The backstop behind the redaction wrapper (dv-28f2): a JSONL line that
    still carries a key value never reaches the progress view, the trace, the
    reply or a log.
    """
    forms = sorted(
        {f for v in values if v for f in (v, json.dumps(v)[1:-1])},
        key=len,
        reverse=True,
    )

    def scrub(text: str) -> str:
        for form in forms:
            text = text.replace(form, "<redacted>")
        return text

    return scrub


def _strip_provider_prefix(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def codex_home() -> Path:
    """Where the Codex CLI keeps its state (`$CODEX_HOME`, default `~/.codex`)."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def read_rollout_token_info(
    thread_id: str, home: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """The last `token_count` payload of a thread's rollout file, or None.

    `codex exec --json` hands the turn ONE usage number, on `turn.completed`,
    and it is the thread's `total_token_usage`: the SUM of every model
    call's prompt since the thread started, tool-loop iterations included.
    That is flow. The level -- what the model actually saw on its last call
    -- exists only in the rollout the CLI writes under
    `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`, as
    `event_msg`/`token_count` lines whose `info` carries `last_token_usage`
    next to `total_token_usage` and `model_context_window`. Verified on
    codex-cli 0.152.0, 2026-09-05: a 6-call general-profile turn reported
    175,970 on `turn.completed` while its last call was 35,604 (rollout
    01a071c6), and the window rolled on every turn (docs/FINDINGS.md).

    The last `token_count` line wins. The CLI appends it before the process
    exits, so once `proc.wait()` has returned it is on disk. A missing or
    unparsable file yields None; the caller then reports "unknown" and the
    window keeps its last known level rather than inventing one.
    """
    root = (home or codex_home()) / "sessions"
    if not root.is_dir():
        return None
    matches = list(root.rglob(f"rollout-*-{thread_id}.jsonl"))
    if not matches:
        return None
    path = max(matches, key=lambda p: p.stat().st_mtime)
    info: Optional[Dict[str, Any]] = None
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if b'"token_count"' not in raw:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                payload = line.get("payload") if isinstance(line, dict) else None
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "token_count"
                    and isinstance(payload.get("info"), dict)
                ):
                    info = payload["info"]
    except OSError:
        return None
    return info


class CodexCliIntegration:
    """Full-agent Codex backend that drives the Codex CLI (`codex exec`).

    Satisfies ``src.agent_backends.protocol.AgentBackend`` (dv-5368), the
    same surface as ``ClaudeIntegration``.
    """

    backend_id = "openai_codex"

    def __init__(
        self,
        config: Settings,
        session_store: CodexSessionStore | InMemoryCodexSessionStore,
        codex_bin: str = "codex",
        prompt_composer: Optional[PromptComposer] = None,
        profile_registry: Any = None,
        memory_retriever: Optional["MemoryRetriever"] = None,
        bundle_provider: Optional["WindowBundleProvider"] = None,
        session_manager: Optional["SessionManager"] = None,
        agent_bridge: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.store = session_store
        self.codex_bin = codex_bin
        self.account_usage = CodexUsageClient(codex_bin=codex_bin)
        self.prompt_composer = prompt_composer
        self.profile_registry = profile_registry
        self.memory_retriever = memory_retriever
        self.bundle_provider = bundle_provider
        # Optional ItemService (dv-752a): per-turn <family_reminders>.
        self.items_service: Any = None
        # The `sessions` table is the FK parent of `messages`, `tool_usage`
        # and every downstream reader (evaluator conversation text, window
        # seeding, /sessions). Codex threads used to live ONLY in the JSON
        # store above, so `save_claude_interaction` inserted a message row
        # pointing at a session id that did not exist and SQLite rejected the
        # whole turn with "FOREIGN KEY constraint failed" (dv-0c01, live
        # 2026-09-03 21:16). Sharing the Claude session manager makes a Codex
        # thread a first-class session row.
        self.session_manager = session_manager
        # Optional["AgentBridge"] (src/agent_bridge): the loopback HTTP MCP
        # server that gives this out-of-process backend the bot's own tools.
        # None -> the turn runs with no agent tools, exactly as before
        # dv-ffcb.
        self.agent_bridge = agent_bridge
        self._profile_composers: Dict[str, PromptComposer] = {}
        # In-flight `codex exec` children by (user, profile slug) so a scoped
        # /stop can reach them; `_stopped_procs` marks the ones /stop killed.
        self._active_procs: Dict[
            Tuple[int, str], List["asyncio.subprocess.Process"]
        ] = {}
        self._stopped_procs: set = set()

    async def stop_turns(self, user_id: int, profile: str) -> int:
        """Scoped /stop: kill ``user_id``'s in-flight turn(s) on ``profile``.
        Returns the number of turns stopped."""
        procs = list(self._active_procs.get((user_id, profile), ()))
        for proc in procs:
            self._stopped_procs.add(proc.pid)
            await _terminate(proc)
        logger.info(
            "Turn stopped", user_id=user_id, profile=profile, stopped=len(procs)
        )
        return len(procs)

    def attach_agent_bridge(self, bridge: Any) -> None:
        """Wire the agent-tools bridge (post-startup: it needs the scheduler)."""
        self.agent_bridge = bridge
        logger.info("Codex backend: agent bridge attached", url=bridge.url)

    async def read_account_limits(self) -> Dict[str, Any]:
        """Read provider limits without starting a model turn."""
        return await self.account_usage.read()

    # --- AgentBackend surface (src/agent_backends/protocol.py) ---

    def attach_tools(self, services: Any, bridge: Any = None) -> None:
        """Out-of-process backend: only the HTTP bridge can carry the tools."""
        if bridge is not None:
            self.attach_agent_bridge(bridge)

    async def resumable_session_for(
        self, user_id: int, working_directory: Path
    ) -> Optional[str]:
        """The thread ``_exec_turn`` would resume when no session id is passed."""
        return await self.store.thread_for(user_id, working_directory)

    async def sessions_for_user(self, user_id: int) -> List["BackendSession"]:
        """Active Codex threads for ``user_id`` as backend-neutral records."""
        from datetime import datetime, timezone

        from .protocol import BackendSession

        out: List[BackendSession] = []
        for e in await self.store.sessions_for_user(user_id):
            raw_ts = e.get("updated_at")
            try:
                last_used = datetime.fromisoformat(str(raw_ts))
            except (TypeError, ValueError):
                last_used = datetime.fromtimestamp(0, tz=timezone.utc)
            out.append(
                BackendSession(
                    session_id=str(e.get("session_id") or ""),
                    working_dir=Path(str(e.get("working_dir", ""))),
                    last_used=last_used,
                    cumulative_tokens=int(e.get("cumulative_tokens", 0) or 0),
                    backend_id=self.backend_id,
                )
            )
        return out

    def default_model(self) -> str:
        """The model a turn runs on when the caller passes no ``model=``.

        The orchestrator always passes one (``_model_override`` in
        ``orchestration/base.py`` — the single source of the user's pick for
        both backends, validated against the Codex catalog there). This is
        only the fallback for direct callers such as tests. No per-user state
        lives on this object: the old ``set_user_model`` side channel was
        per-process, lost on restart, and raced across chats (dv-4207).
        """
        return self.config.agent_model or "openai-codex/gpt-5.6-sol"

    def _build_argv(
        self,
        *,
        model: str,
        cwd: Path,
        out_file: Path,
        resume_id: Optional[str],
        images: Optional[List[Path]] = None,
        user_id: Optional[int] = None,
        redact_env: Sequence[str] = (),
    ) -> List[str]:
        # Exec-level options must precede the `resume` subcommand — `codex exec
        # resume` does not accept them as its own args (it rejects e.g. `-C`).
        base = [self.codex_bin, "exec"]
        base += [
            "-c",
            "agents.max_concurrent_threads_per_session=2",
            "--json",
            "-C",
            str(cwd),
            "-m",
            _strip_provider_prefix(model),
            *self._sandbox_args(cwd, user_id),
            "--skip-git-repo-check",
            "-o",
            str(out_file),
        ]
        for img in images or []:
            base += ["-i", str(img)]
        base += _with_mcp_auto_approval(self._mcp_overrides(cwd))
        base += self._policy_hook_args()
        base += self._shell_env_args(redact_env)
        if resume_id:
            base += ["resume", resume_id]
        return base

    def _policy_hook_args(self) -> List[str]:
        """Mount the PreToolUse hook that asks the bot before every tool call.

        The same rules the Claude hook enforces (src/security/policy.py),
        reached over the agent bridge (dv-1601). Verified on codex-cli
        0.157.1: the hook fires before shell commands, apply_patch and MCP
        calls even under ``danger-full-access``, and a deny blocks the call.
        The hook is passed per turn, so it needs the trust bypass; nothing
        under ``~/.codex`` is touched. The timeout leaves room for the
        owner's confirmation window (``confirmation_timeout_seconds``).
        """
        if not getattr(self.agent_bridge, "policy_url", None):
            return []
        command = f"{sys.executable} {_POLICY_HOOK_SCRIPT}"
        hook_timeout = self.config.confirmation_hook_timeout_seconds
        return [
            "--dangerously-bypass-hook-trust",
            "-c",
            'hooks.PreToolUse=[{matcher="",hooks=[{type="command",'
            f"command={json.dumps(command)},timeout={hook_timeout}}}]}}]",
        ]

    @staticmethod
    def _shell_env_args(names: Sequence[str]) -> List[str]:
        """Shell env lockdown for a keyed turn (dv-28f2); [] for any other.

        The shell snapshot re-sources the codex child's whole env and bypasses
        ``shell_environment_policy`` (research dv-0277 trap 1), so it goes off;
        ``include_only`` then hands the shell the allowlisted basics plus the
        key names, never the bridge token, policy URL, seam or MCP secrets.
        Hooks still get the full codex env. Only key profiles pay for this
        (owner, 2026-10-24); every other turn's argv is unchanged.
        """
        if not names:
            return []
        allowed = {k for k in agent_child_env() if k not in AGENT_CLI_CREDENTIAL_VARS}
        allowed |= {"TZ", *names}
        return [
            "-c",
            "features.shell_snapshot=false",
            "-c",
            'shell_environment_policy.inherit="all"',
            "-c",
            "shell_environment_policy.ignore_default_excludes=true",
            "-c",
            f"shell_environment_policy.include_only={json.dumps(sorted(allowed))}",
        ]

    def _sandbox_keys_env(self, cwd: Path) -> "Tuple[Dict[str, str], List[str]]":
        """(key env, values to redact) for this turn's profile, or empty.

        The Codex half of ``sandbox_keys`` (dv-0277, dv-28f2): the same store
        read the bash box launcher does (:func:`bash_box.load_key_env`), once
        per turn. Only a sandboxed profile, as on the Claude path (a
        full-access profile gets no box there either), and only with the
        policy hook mounted: without it nothing rewrites the shell into the
        redaction wrapper, and the raw value would reach the model.
        """
        profile = self._profile_for(cwd)
        services = tuple(getattr(profile, "sandbox_keys", ()) or ())
        if not services or self._full_access_for(cwd):
            return {}, []
        if not self._policy_hook_args():
            logger.warning(
                "Codex turn without a policy hook gets no sandbox keys",
                profile=getattr(profile, "slug", None),
            )
            return {}, []
        try:
            env, values = load_key_env(services)
        except KeyEnvError as exc:
            logger.warning(
                "Codex sandbox keys unavailable for this turn",
                profile=getattr(profile, "slug", None),
                error=str(exc),
            )
            return {}, []
        return {k: v for k, v in env.items() if is_env_name(k)}, values

    def _sandbox_args(self, cwd: Path, user_id: Optional[int]) -> List[str]:
        """Per-profile sandbox (dv-e38e): full access only for ``unrestricted``
        profiles; everything else, including a cwd no profile owns, runs in
        the confined permission profile (see codex_sandbox.py)."""
        profile = self._profile_for(cwd)
        return sandbox_args(
            unrestricted=bool(getattr(profile, "unrestricted", False)),
            full_access=self._full_access_for(cwd),
            cwd=cwd,
            extra_dirs=getattr(profile, "extra_dirs", ()) or (),
            registry=self.profile_registry,
            user_id=user_id,
        )

    def _profile_for(self, cwd: Path) -> Any:
        return (
            self.profile_registry.get_by_working_dir(cwd)
            if self.profile_registry
            else None
        )

    def _profile_unrestricted(self, cwd: Path) -> bool:
        return bool(getattr(self._profile_for(cwd), "unrestricted", False))

    def _full_access_for(self, cwd: Path) -> bool:
        """Unsandboxed profile (devops): same rule as the Claude SDK path."""
        sandbox = getattr(self._profile_for(cwd), "sandbox", None)
        if sandbox is None:
            sandbox = self.config.sandbox_enabled
        return not sandbox

    def _stdio_mcp_overrides(self, working_directory: Path) -> List[str]:
        """Mount ``config/mcp.json``'s stdio servers for this Codex turn.

        This is how ``send_image_to_user`` reaches Codex (dv-ffcb wave 3). It
        is a stdio MCP server, not an in-process tool, so it cannot ride the
        bridge — but it is also the *only* implementation, and duplicating it
        as a bridge ToolSpec would put two tools with the same name in front of
        the Claude backend, which already mounts this one. So Codex gets the
        same server the Claude path gets, over the transport it was written
        for: ``ClaudeAgentOptions.mcp_servers`` there
        (``sdk_integration._load_mcp_config`` + ``_inject_image_roots``),
        ``-c mcp_servers.<name>.command/args/env`` here.

        ``TELEGRAM_IMAGE_ROOTS`` comes from the same
        :func:`turn_setup.image_roots_env` ``_inject_image_roots`` uses — the
        tool refuses a path outside those roots itself, so the model is told
        the send failed instead of being told "queued" for an image the bot's
        intercept then silently drops.

        HTTP entries in that file are skipped: the profile ``mcp`` seam below
        owns those, and mounting one twice under two names would double the
        tool list.
        """
        overrides: List[str] = []
        for key, command, args, env in self._stdio_mcp_servers(working_directory):
            # json.dumps renders a TOML basic string / array; `-c` parses its
            # value as TOML, so quoting has to survive the shell-free argv.
            overrides += ["-c", f"mcp_servers.{key}.command={json.dumps(str(command))}"]
            if args:
                overrides += ["-c", f"mcp_servers.{key}.args={json.dumps(args)}"]
            # argv is world-readable (`ps`): only non-secret values go inline.
            # Secret ones travel in the child env and are forwarded by NAME
            # (`env_vars`, codex-cli 0.157.1); see _stdio_mcp_secret_env.
            safe, routed = split_mcp_env(env, self.config.secret_values())
            for var, value in safe.items():
                overrides += ["-c", f"mcp_servers.{key}.env.{var}={json.dumps(value)}"]
            if routed:
                overrides += [
                    "-c",
                    f"mcp_servers.{key}.env_vars={json.dumps(sorted(routed))}",
                ]
        return overrides

    def _stdio_mcp_servers(
        self, working_directory: Path
    ) -> List["tuple[str, str, List[str], Dict[str, str]]"]:
        """``config/mcp.json`` stdio servers as (key, command, args, env)."""
        from src.bot.utils.image_extractor import IMAGE_ROOTS_ENV

        path = self.config.mcp_config_path
        if not (self.config.enable_mcp and path):
            return []
        try:
            servers = json.loads(Path(path).read_text()).get("mcpServers") or {}
        except (OSError, ValueError):
            logger.warning("Failed to read MCP config for Codex", path=str(path))
            return []
        if not isinstance(servers, dict):
            return []
        out: List["tuple[str, str, List[str], Dict[str, str]]"] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            command = cfg.get("command")
            if not command:
                continue
            key = str(name).replace("-", "_")
            env = {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
            env = {k: v for k, v in env.items() if is_env_name(k)}
            if name == "telegram":
                env[IMAGE_ROOTS_ENV] = image_roots_env(
                    self.config.approved_directory,
                    working_directory,
                    getattr(self._profile_for(working_directory), "extra_dirs", ())
                    or (),
                )
            args = [str(a) for a in (cfg.get("args") or [])]
            out.append((key, str(command), args, env))
        return out

    def _stdio_mcp_secret_env(self, working_directory: Path) -> Dict[str, str]:
        """The secret stdio-MCP env values kept out of argv (see above)."""
        secrets = self.config.secret_values()
        routed_all: Dict[str, str] = {}
        for _, _, _, env in self._stdio_mcp_servers(working_directory):
            routed_all.update(split_mcp_env(env, secrets)[1])
        return routed_all

    def _seam_env(self, working_directory: Path) -> Dict[str, str]:
        """``{auth_env: token}`` for this profile's HTTP MCP seam, or {}.

        Codex reads the bearer from the named var (``bearer_token_env_var``),
        so the child needs it; the value comes from Settings, since the bot
        scrubbed its own env at startup (dv-2976).
        """
        if not self.profile_registry:
            return {}
        profile = self.profile_registry.get_by_working_dir(working_directory)
        seam_path = getattr(profile, "mcp", None) if profile else None
        if not seam_path:
            return {}
        try:
            seam = json.loads(Path(seam_path).read_text())
        except (OSError, ValueError):
            return {}
        auth_env = str(seam.get("auth_env") or "") if isinstance(seam, dict) else ""
        if not is_env_name(auth_env):
            return {}
        token = self.config.secret_value(auth_env) or os.environ.get(auth_env)
        return {auth_env: token} if token else {}

    def _child_env(
        self, working_directory: Path, extra: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Allowlisted env for this turn's codex child (dv-2976): PATH/HOME/
        CODEX_* and friends, plus exactly the secrets this turn needs.

        ``TZ`` is set explicitly to the configured user timezone (dv-ca94),
        not merely allowed through: the host runs UTC, so without this the
        CLI's own ``environment_context`` and any of its builtin time tools
        report ``Etc/UTC`` regardless of where the user actually is.
        """
        return agent_child_env(
            {
                "TZ": self.config.user_timezone,
                **self._seam_env(working_directory),
                **self._stdio_mcp_secret_env(working_directory),
                **(extra or {}),
            }
        )

    def _mcp_overrides(self, working_directory: Path) -> List[str]:
        """Return `-c` overrides registering this turn's MCP servers.

        Three seams, all mounted when available:

        * the **agent bridge** (dv-ffcb) — the bot's own tools (memory, the
          user's data log, platform, jobs) over loopback HTTP, on every
          profile. Its bearer token is per-turn and lives only in the child's
          environment, so the env var name is all that goes on the command
          line.
        * the **stdio servers in `config/mcp.json`** — `send_image_to_user`.
          See :meth:`_stdio_mcp_overrides`.
        * the **profile `mcp` seam** — only profiles that declare one (e.g.
          channel) get it; devops/general have none. The seam JSON
          ({type:http, url, auth_env}) is translated to Codex
          `mcp_servers.<name>` config overrides, and Codex reads that bearer
          token from the named env var, inherited from the bot's environment.
        """
        overrides: List[str] = []
        if self.agent_bridge is not None:
            from src.agent_bridge import TOKEN_ENV_VAR

            overrides += [
                "-c",
                f'mcp_servers.agent.url="{self.agent_bridge.url}"',
                "-c",
                f'mcp_servers.agent.bearer_token_env_var="{TOKEN_ENV_VAR}"',
            ]
        overrides += self._stdio_mcp_overrides(working_directory)
        if not self.profile_registry:
            return overrides
        profile = self.profile_registry.get_by_working_dir(working_directory)
        seam_path = getattr(profile, "mcp", None) if profile else None
        if not seam_path:
            return overrides
        try:
            seam = json.loads(Path(seam_path).read_text())
        except (OSError, ValueError):
            logger.warning("Failed to read MCP seam", path=str(seam_path))
            return overrides
        if seam.get("type") != "http" or not seam.get("url"):
            return overrides
        name = Path(seam_path).stem.replace("-", "_")
        overrides += ["-c", f'mcp_servers.{name}.url="{seam["url"]}"']
        auth_env = seam.get("auth_env")
        if auth_env:
            overrides += [
                "-c",
                f'mcp_servers.{name}.bearer_token_env_var="{auth_env}"',
            ]
        return overrides

    def _turn_context(
        self,
        working_directory: Path,
        user_id: Optional[int],
        requester_chat_id: Optional[int],
        family_id: Optional[int],
        role: Optional[str],
    ) -> "TurnContext":
        """This turn's identity, resolved exactly as the SDK path resolves it.

        The same :func:`turn_setup.resolve_turn_context` call
        ``ClaudeSDKManager._turn_context_for`` makes (dv-a342), so a tool
        called over the bridge operates on precisely the scope this turn
        reads from (dv-6dbc: the model never supplies any of it).
        """
        return resolve_turn_context(
            self.profile_registry,
            working_directory,
            user_id=user_id,
            requester_chat_id=requester_chat_id,
            family_id=family_id,
            role=role,
        )

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], Any]] = None,
        force_new: bool = False,
        images: Optional[List[Path]] = None,
        # Model ref for this turn (``openai-codex/<name>``; the provider
        # prefix is stripped for ``-m``). Same kwarg ClaudeIntegration takes;
        # None -> :meth:`default_model`.
        model: Optional[str] = None,
        # The Telegram chat driving this turn: server context for the agent
        # tools reached over the bridge (notify/schedule_add), never a tool
        # parameter.
        requester_chat_id: Optional[int] = None,
        # Requesting user's family (if any): adds the shared family fact
        # scope to persona composition, mirroring the Claude SDK path.
        family_id: Optional[int] = None,
        # Gates the owner-only tools on the bridge, as on the SDK path.
        role: Optional[str] = None,
    ) -> ClaudeResponse:
        """Run one Codex turn, with the bot's own tools mounted for its life.

        The bridge token is minted here and revoked in the ``finally``: it is
        valid for exactly this turn, and only the child process ever sees it.
        A crashed turn that never reaches the revoke is covered by the
        registry's TTL (src/agent_bridge/tokens.py).
        """
        token: Optional[str] = None
        # sandbox_keys (dv-28f2): values to the child env only, never argv;
        # the NAMES ride the TurnContext so the policy gate wraps the shell.
        key_env, key_values = (
            self._sandbox_keys_env(working_directory)
            if self.agent_bridge is not None
            else ({}, [])
        )
        redact_env = tuple(sorted(key_env))
        turn_vars: Dict[str, str] = dict(key_env)
        if self.agent_bridge is not None:
            from dataclasses import replace

            from src.agent_bridge import TOKEN_ENV_VAR

            ctx = self._turn_context(
                working_directory, user_id, requester_chat_id, family_id, role
            )
            if redact_env:
                ctx = replace(ctx, redact_env=redact_env)
            token = self.agent_bridge.mint(ctx)
            # The ONLY place the token goes: this child's environment. Not the
            # command line (world-readable in /proc), not a config file, not a
            # log line.
            turn_vars[TOKEN_ENV_VAR] = token
            policy_url = getattr(self.agent_bridge, "policy_url", None)
            if policy_url:
                from src.agent_bridge import POLICY_URL_ENV_VAR

                turn_vars[POLICY_URL_ENV_VAR] = policy_url
                # The hook's HTTP wait must outlive the owner's tap but end
                # before Codex kills the hook (hook timeout is +margin).
                turn_vars["DEVVY_POLICY_TIMEOUT_SECONDS"] = str(
                    self.config.confirmation_hook_timeout_seconds - 10
                )
            elif self._profile_unrestricted(working_directory):
                logger.warning(
                    "Codex turn on an unrestricted profile has no policy hook",
                    working_directory=str(working_directory),
                )
        # Never the bot's whole env: an allowlist plus this turn's vars.
        child_env = self._child_env(working_directory, turn_vars)
        try:
            return await self._run_turn(
                prompt=prompt,
                working_directory=working_directory,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                model=model or self.default_model(),
                family_id=family_id,
                child_env=child_env,
                redact_env=redact_env,
                redact_values=key_values,
            )
        finally:
            if token is not None and self.agent_bridge is not None:
                self.agent_bridge.revoke(token)

    async def _run_turn(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], Any]] = None,
        force_new: bool = False,
        images: Optional[List[Path]] = None,
        model: Optional[str] = None,
        family_id: Optional[int] = None,
        child_env: Optional[Dict[str, str]] = None,
        redact_env: Sequence[str] = (),
        redact_values: Sequence[str] = (),
    ) -> ClaudeResponse:
        """One turn, with the resume-failed fallback the Claude facade has.

        When `codex exec resume <id>` reports the thread gone
        (:func:`is_resume_failure`), the stored thread for this (user, dir) is
        dropped, the turn re-runs on a fresh thread seeded with the dead
        thread's stored tail (same `build_seed_preamble(..., reason=
        "resume_failed")` as `facade.run_command`), and the response carries
        `session_restarted=True` so the orchestration footer reads the same
        for both backends. Any other failure -- including the fresh retry
        itself failing -- propagates unchanged.
        """
        try:
            return await self._exec_turn(
                prompt=prompt,
                working_directory=working_directory,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                model=model,
                family_id=family_id,
                child_env=child_env,
                redact_env=redact_env,
                redact_values=redact_values,
            )
        except CodexResumeError as resume_error:
            logger.warning(
                "Codex thread resume failed, starting fresh thread",
                failed_thread_id=resume_error.thread_id,
                user_id=user_id,
                working_directory=str(working_directory),
                error=str(resume_error),
            )
            # Seed BEFORE clearing anything, and never let a storage hiccup
            # turn the recoverable retry into a hard failure (facade parity).
            retry_prompt = prompt
            seed_tail: Optional[SeedTail] = None
            try:
                seed_tail = await self._seed_tail_for(resume_error.thread_id)
                retry_prompt = build_seed_preamble(
                    seed_tail, prompt, reason="resume_failed"
                )
            except Exception as seed_error:
                logger.warning(
                    "Resume-failed seed build failed; retrying unseeded",
                    failed_thread_id=resume_error.thread_id,
                    error=str(seed_error),
                )
            # Only the resume index is dropped. The `sessions` row stays: it
            # is the FK parent of the dead thread's `messages`, which are the
            # history recall_conversation and the seed above read from.
            await self.store.clear(user_id, working_directory)
            logger.info(
                "Codex fresh thread after resume failure",
                failed_thread_id=resume_error.thread_id,
                seeded=bool(seed_tail.text) if seed_tail else False,
                tail_messages=seed_tail.shown_turns if seed_tail else 0,
            )
            response = await self._exec_turn(
                prompt=retry_prompt,
                working_directory=working_directory,
                user_id=user_id,
                session_id=None,
                on_stream=on_stream,
                force_new=True,
                images=images,
                model=model,
                family_id=family_id,
                child_env=child_env,
                redact_env=redact_env,
                redact_values=redact_values,
            )
            response.session_restarted = True
            return response

    async def _seed_tail_for(self, thread_id: str) -> SeedTail:
        """The dead thread's last stored turns, shaped for the seed preamble.

        Reads the same `messages` rows the Claude facade's `_recent_pairs` /
        `_count_session_messages` read: a Codex thread is a first-class
        `sessions` row (dv-0c01), so its turns are keyed on the thread id.
        Without a session manager there is no history to read; an empty tail
        makes `build_seed_preamble` return the prompt unchanged.
        """
        db_manager = (
            getattr(self.session_manager.storage, "db_manager", None)
            if self.session_manager is not None
            else None
        )
        if db_manager is None:
            return select_tail([], total_turns=None)
        messages = MessageRepository(db_manager)
        pairs = await messages.get_recent_pairs(thread_id, SEED_TAIL_TURNS)
        total = await messages.count_session_messages(thread_id)
        return select_tail(pairs, total_turns=total)

    async def _exec_turn(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], Any]] = None,
        force_new: bool = False,
        images: Optional[List[Path]] = None,
        model: Optional[str] = None,
        family_id: Optional[int] = None,
        child_env: Optional[Dict[str, str]] = None,
        redact_env: Sequence[str] = (),
        redact_values: Sequence[str] = (),
    ) -> ClaudeResponse:
        """One `codex exec` subprocess, start to finish. No retry logic."""
        start = time.time()
        # Every turn, resumed or fresh (dv-ca94) — unlike the persona bundle
        # below, this must never be cached into the saved thread transcript.
        prompt = f"{current_time_context(self.config.user_timezone)}\n\n{prompt}"
        prompt = await prepend_items_context(
            self.items_service,
            self.profile_registry,
            prompt,
            working_directory,
            user_id,
        )
        resume_id: Optional[str] = None
        if not force_new:
            resume_id = session_id or await self.store.thread_for(
                user_id, working_directory
            )

        # A resumed Codex thread already contains the frozen bundle in its
        # first user turn.  Re-prepending it here permanently appends another
        # 7-10k tokens to the saved transcript on every Telegram message.
        # Fresh threads (including resume-failed retries and window rolls)
        # still receive the full bundle exactly once.
        if resume_id is None:
            prompt = await self._with_persona(
                working_directory, prompt, family_id, user_id
            )

        # Hard-forbid the real-money exchange scripts on every Codex turn
        # (dv-4fc2): no Approve/Deny gate exists here. Raises -> no turn.
        ensure_money_guard_rules(codex_home())

        out_file = working_directory / ".codex_last_message.txt"
        argv = self._build_argv(
            model=model or self.default_model(),
            cwd=working_directory,
            out_file=out_file,
            resume_id=resume_id,
            images=images,
            user_id=user_id,
            redact_env=redact_env,
        )
        argv.append(prompt)
        scrub = _scrubber(redact_values)

        thread_id: Optional[str] = resume_id
        usage: Dict[str, Any] = {}
        final_fallback: Optional[str] = None
        # Tool activity, in the ClaudeResponse shape. Codex only reports it as
        # `item.started` stream events, so without this the turn persists no
        # `tool_usage` rows and the save-honesty check (which reads
        # response.tools_used) sees every Codex turn as tool-less and flags
        # any "I saved it" as a mismatch.
        tools_used: List[Dict[str, Any]] = []
        tool_items: Dict[str, Dict[str, Any]] = {}
        # Harness trace (dv-2260): the router's collector, fed per item.
        trace = current_trace()
        error_events: List[str] = []

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group: `codex` is a Node wrapper around a vendored
            # Rust binary, so killing the direct child leaves the binary
            # running forever. Observed 2026-08-30 -- a timed-out turn left an
            # orphan alive for 30+ minutes.
            start_new_session=True,
            # Allowlisted (run_command -> _child_env); None only for direct
            # _exec_turn callers, which then inherit the scrubbed bot env.
            env=child_env,
            # StreamReader's default line limit is 64KiB, and readline() RAISES
            # on a longer line rather than returning it. codex emits single
            # lines far past that on both streams (a models-parse error carries
            # a ~250KB body; a JSON event can carry a large tool result), so
            # the default turns a big line into a dead reader and a hung turn.
            limit=STREAM_LIMIT,
        )
        assert proc.stdout is not None and proc.stderr is not None
        proc_key = (
            user_id,
            self._agent_id_for(working_directory, user_id).split("#")[0],
        )
        self._active_procs.setdefault(proc_key, []).append(proc)
        # stderr MUST be drained concurrently. It is a 64KiB pipe, and the CLI
        # writes far more than that to it: a models-list parse failure dumps
        # the entire ~250KB response body, twice per turn. Reading only stdout
        # fills the stderr buffer, blocks the child mid-write, and the turn
        # hangs until agent_timeout_seconds. See docs/FINDINGS.md 2026-08-30.
        stderr_tail: Deque[str] = deque(maxlen=20)

        async def _drain_stderr() -> None:
            """Read stderr forever so the child can never block writing to it.

            Fixed-size reads, not readline(): line-oriented reading is what
            broke the first attempt at this fix. It must also never raise --
            a drainer that dies silently reintroduces the exact deadlock, and
            an un-awaited task's exception is invisible.
            """
            assert proc.stderr is not None
            pending = ""
            try:
                while True:
                    chunk = await proc.stderr.read(65536)
                    if not chunk:
                        break
                    pending += chunk.decode(errors="replace")
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        if line.strip():
                            stderr_tail.append(line.strip()[:2000])
                    if len(pending) > STREAM_LIMIT:
                        pending = pending[-2000:]  # one absurd unterminated line
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as exc:  # never let the drainer die quietly
                logger.warning("codex stderr drain stopped", error=str(exc))

        drainer = asyncio.create_task(_drain_stderr())
        try:
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=self.config.agent_idle_limit_seconds,
                )
                if not line:
                    break
                try:
                    text = line.decode().strip()
                    if redact_values:
                        text = scrub(text)
                    event = json.loads(text)
                except (ValueError, UnicodeDecodeError):
                    continue
                # `codex exec --json` reports API/turn failures HERE, on
                # stdout, and leaves stderr empty (FINDINGS 2026-09-03).
                # Capture them or a failed turn reports "exit 1: " and nothing.
                if isinstance(event, dict):
                    if event.get("type") == "error":
                        error_events.append(str(event.get("message", "")))
                    elif event.get("type") == "turn.failed":
                        _err = event.get("error") or {}
                        if isinstance(_err, dict):
                            error_events.append(str(_err.get("message", "")))
                outcome = map_codex_event(event)
                if trace is not None and isinstance(event, dict):
                    _feed_codex_trace(trace, event, outcome)
                if outcome.thread_id:
                    thread_id = outcome.thread_id
                if outcome.usage:
                    usage = outcome.usage
                if outcome.final_message:
                    final_fallback = outcome.final_message
                if outcome.tool_error and outcome.item_id in tool_items:
                    # dv-c7d1: a failed MCP call / non-zero shell exit is
                    # stored as success=0 with this message in tool_usage.
                    tool_items[outcome.item_id]["error"] = outcome.tool_error
                if outcome.stream_update:
                    calls = outcome.stream_update.tool_calls or []
                    tools_used.extend(calls)
                    if outcome.item_id and len(calls) == 1:
                        tool_items[outcome.item_id] = calls[0]
                    if on_stream:
                        await on_stream(outcome.stream_update)
            await asyncio.wait_for(
                proc.wait(), timeout=self.config.agent_idle_limit_seconds
            )
        except asyncio.TimeoutError:
            await _terminate(proc)
            if stderr_tail:
                logger.warning(
                    "Codex CLI timed out; stderr tail",
                    stderr="\n".join(stderr_tail),
                )
            raise RuntimeError("Codex CLI timed out.")
        finally:
            drainer.cancel()
            group = self._active_procs.get(proc_key, [])
            if proc in group:
                group.remove(proc)
            if not group:
                self._active_procs.pop(proc_key, None)

        stopped = proc.pid in self._stopped_procs
        self._stopped_procs.discard(proc.pid)

        answer = ""
        try:
            if out_file.exists():
                answer = out_file.read_text().strip()
                out_file.unlink()
        except OSError:
            pass
        answer = scrub(answer or (final_fallback or ""))

        if proc.returncode != 0 and not answer and not stopped:
            # NOT `proc.stderr.read()`: the drainer above already consumed the
            # whole stream, so re-reading it here returns b"" and the operator
            # gets "exit 1: " with no reason. Report what was actually
            # captured — the stdout error events first, since that is where
            # `codex exec --json` puts API failures.
            reason = " | ".join(dict.fromkeys(e for e in error_events if e))
            if not reason:
                reason = scrub("\n".join(stderr_tail))
            message = (
                f"Codex CLI failed (exit {proc.returncode}): "
                f"{(reason or '(no output)')[-500:]}"
            )
            if resume_id and is_resume_failure(resume_id, reason):
                raise CodexResumeError(resume_id, message)
            raise RuntimeError(message)

        # `turn.completed.usage` is the thread's running TOTAL, not this
        # turn's (see read_rollout_token_info). This turn's flow is the delta
        # against the total stored at the end of the previous turn on the
        # same thread; a fresh thread starts from zero. Recording the raw
        # number, as this used to, summed a sum: the general chain read 3.7M
        # tokens after a handful of chat turns (the old codex_sessions.json,
        # 2026-09-05).
        total_in = int(usage.get("input_tokens", 0) or 0)
        total_out = int(usage.get("output_tokens", 0) or 0)
        prev_in, prev_out = (
            await self.store.thread_totals(user_id, working_directory, thread_id)
            if thread_id
            else (0, 0)
        )
        in_tok = max(total_in - prev_in, 0)
        out_tok = max(total_out - prev_out, 0)

        # The LEVEL comes from the rollout, never from turn.completed. 0 means
        # unknown: ClaudeResponse.context_tokens stays falsy, the orchestrator
        # passes None, and the window holds its last measured level (the
        # backend is in the repository's level-reporting set).
        level = 0
        if thread_id:
            info = await asyncio.to_thread(read_rollout_token_info, thread_id)
            last = (info or {}).get("last_token_usage") or {}
            level = int(last.get("input_tokens", 0) or 0)
            if not level:
                logger.warning(
                    "Codex rollout gave no context level; window keeps its last",
                    thread_id=thread_id,
                    rollout_found=info is not None,
                )

        # cost=None, not 0.0 (dv-0722): the CLI reports no USD figure, the
        # catalog (`codex debug models`) carries no prices, and the account is
        # a ChatGPT subscription. 0.0 told the rate limiter, the watchdog and
        # /usage that Codex was free; None makes them budget the token delta
        # above instead (RateLimiter.record_tokens).
        response = ClaudeResponse(
            content=answer
            or ("(Stopped.)" if stopped else "(No response from Codex.)"),
            error_type="interrupted" if stopped else None,
            session_id=thread_id or "",
            cost=None,
            duration_ms=int((time.time() - start) * 1000),
            num_turns=1,
            input_tokens=in_tok,
            output_tokens=out_tok,
            peak_context_tokens=level,
            tools_used=tools_used,
        )
        await self._persist_session(user_id, working_directory, response)
        # After _persist_session: the Codex columns live on the same
        # `sessions` row (dv-ebde), whose `users` FK parent that call ensures.
        if thread_id:
            await self.store.record(
                user_id,
                working_directory,
                thread_id,
                in_tok,
                out_tok,
                thread_total_input=total_in,
                thread_total_output=total_out,
            )
        return response

    async def _persist_session(
        self,
        user_id: int,
        working_directory: Path,
        response: ClaudeResponse,
    ) -> None:
        """Write/refresh the `sessions` row for this Codex thread.

        Runs BEFORE run_command returns, because the caller
        (`_run_turn` -> `storage.save_claude_interaction`) inserts a `messages`
        row keyed on this session id immediately afterwards and that FK must
        already resolve. Mirrors the Claude facade, which persists through the
        same `session_manager.update_session` at the same point in its turn.

        A storage failure is logged, never raised: losing the history row is
        bad, losing the user's answer on top of it is worse.
        """
        if self.session_manager is None or not response.session_id:
            return
        from datetime import datetime, timezone

        from src.claude.session import ClaudeSession

        try:
            session = await self.session_manager.storage.load_session(
                response.session_id, user_id
            )
            if session is None:
                now = datetime.now(timezone.utc)
                session = ClaudeSession(
                    session_id="",
                    user_id=user_id,
                    project_path=Path(working_directory),
                    created_at=now,
                    last_used=now,
                    is_new_session=True,
                )
            await self.session_manager.update_session(session, response)
        except Exception as exc:  # storage is best-effort for a live turn
            logger.warning(
                "Failed to persist Codex session row",
                session_id=response.session_id,
                user_id=user_id,
                error=str(exc),
            )

    # --- persona injection ---
    def _composer_for(self, working_directory: Path) -> Optional[PromptComposer]:
        """Pick the per-profile composer for a working dir, else the default."""
        return composer_for(
            self.profile_registry,
            self.prompt_composer,
            self._profile_composers,
            working_directory,
        )

    def _agent_id_for(
        self, working_directory: Path, user_id: Optional[int] = None
    ) -> str:
        """Profile slug is the memory scope key; default profile is 'devops'.

        ``user_id`` applies per-user scoping on ``personal_memory`` profiles.
        """
        from src.profiles.registry import agent_id_for_working_dir

        return agent_id_for_working_dir(
            self.profile_registry, working_directory, user_id
        )

    async def _with_persona(
        self,
        working_directory: Path,
        prompt: str,
        family_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> str:
        """Prepend the active profile's composed persona to a fresh thread.

        Codex reads a project's own ``AGENTS.md`` from cwd as developer
        instructions, so the bot persona must NOT be written there: the root
        ``AGENTS.md`` is human-authored, dev-only, and no agent may clobber it.
        Codex has no system-prompt flag, so the bundle lives in the first user
        message and is retained by the saved thread.  Callers must not invoke
        this for resume turns: doing so duplicates the frozen bundle in the
        permanent conversation history.

        ``user_id`` resolves per-user memory scope on ``personal_memory``
        profiles, mirroring the Claude SDK path.
        """
        try:
            persona = await self._persona_text(
                working_directory, prompt, user_id=user_id, family_id=family_id
            )
        except OSError:
            logger.warning("Failed to compose persona", cwd=str(working_directory))
            return prompt
        if not persona:
            return prompt
        return f"{persona}\n\n<user_request>\n{prompt}\n</user_request>"

    async def _persona_text(
        self,
        working_directory: Path,
        prompt: str,
        *,
        user_id: Optional[int] = None,
        family_id: Optional[int] = None,
    ) -> Optional[str]:
        """The composed persona for this turn, or None without a composer.

        Frozen per-window bundle (dv-5e2a) first: a Codex turn in the same
        window must inject the same frozen text as the Claude SDK path, or
        the bundle un-freezes whenever the backend switches. Live compose is
        the fallback only. Both come from :func:`turn_setup.compose_persona`,
        the same call ``ClaudeSDKManager._persona_text`` makes (dv-a342);
        only the destination differs — see :meth:`_with_persona`.
        """
        return await compose_persona(
            self._turn_context(working_directory, user_id, None, family_id, None),
            prompt=prompt,
            composer=self._composer_for(working_directory),
            memory_retriever=self.memory_retriever,
            bundle_provider=self.bundle_provider,
            family_id=family_id,
            # Without it a devops Codex turn is told to stay in its cwd, and a
            # window Codex opens freezes that line for Claude too (dv-6a9b).
            full_access=self._full_access_for(Path(working_directory)),
        )
