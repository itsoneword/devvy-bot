"""Codex CLI backend: wraps `codex exec --json` for full agent parity."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional, Tuple

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
    image_roots_env,
    resolve_turn_context,
)
from src.config.settings import Settings

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
        item = event.get("item") or {}
        itype = item.get("type")
        if itype == "command_execution":
            cmd = str(item.get("command", ""))
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
        return CodexEventOutcome()
    if etype == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            return CodexEventOutcome(final_message=item.get("text"))
        return CodexEventOutcome()
    return CodexEventOutcome()


class CodexSessionStore:
    """Persist Codex thread ids per (user, working_dir) as JSON.

    Mirrors the Claude session-manager's resume-by-directory behaviour so Codex
    turns continue a conversation and `/sessions` can report them. Datetimes are
    timezone-aware UTC ISO strings.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _key(user_id: int, working_dir: Path) -> str:
        return f"{user_id}::{Path(working_dir)}"

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text())
                if isinstance(raw, dict):
                    self._data = raw
        except (OSError, ValueError):
            logger.warning("Failed to load Codex session store", path=str(self.path))
            self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)

    def record(
        self,
        user_id: int,
        working_dir: Path,
        thread_id: str,
        input_tokens: int,
        output_tokens: int,
        thread_total_input: Optional[int] = None,
        thread_total_output: Optional[int] = None,
    ) -> None:
        """Add one turn's flow (`input_tokens` + `output_tokens`, PER TURN) to
        the (user, dir) counter and remember the thread's running totals as
        the CLI reported them, so the next turn can take its delta."""
        from datetime import datetime, timezone

        key = self._key(user_id, working_dir)
        entry = self._data.get(key, {"cumulative_tokens": 0})
        entry["session_id"] = thread_id
        entry["working_dir"] = str(Path(working_dir))
        entry["user_id"] = user_id
        entry["cumulative_tokens"] = (
            int(entry.get("cumulative_tokens", 0))
            + int(input_tokens)
            + int(output_tokens)
        )
        if thread_total_input is not None:
            entry["thread_total_input"] = int(thread_total_input)
        if thread_total_output is not None:
            entry["thread_total_output"] = int(thread_total_output)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._data[key] = entry
        self._save()

    def thread_totals(
        self, user_id: int, working_dir: Path, thread_id: str
    ) -> Tuple[int, int]:
        """(input, output) running totals recorded at the end of the previous
        turn on `thread_id`, or (0, 0) if the store knows a different thread
        (fresh session) or none."""
        entry = self._data.get(self._key(user_id, working_dir))
        if not entry or entry.get("session_id") != thread_id:
            return 0, 0
        return (
            int(entry.get("thread_total_input", 0) or 0),
            int(entry.get("thread_total_output", 0) or 0),
        )

    def thread_for(self, user_id: int, working_dir: Path) -> Optional[str]:
        entry = self._data.get(self._key(user_id, working_dir))
        return entry.get("session_id") if entry else None

    def info_for(self, user_id: int, working_dir: Path) -> Optional[Dict[str, Any]]:
        return self._data.get(self._key(user_id, working_dir))

    def clear(self, user_id: int, working_dir: Path) -> None:
        self._data.pop(self._key(user_id, working_dir), None)
        self._save()

    def sessions_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        return [e for e in self._data.values() if e.get("user_id") == user_id]


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
        session_store: CodexSessionStore,
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
        self.prompt_composer = prompt_composer
        self.profile_registry = profile_registry
        self.memory_retriever = memory_retriever
        self.bundle_provider = bundle_provider
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

    def attach_agent_bridge(self, bridge: Any) -> None:
        """Wire the agent-tools bridge (post-startup: it needs the scheduler)."""
        self.agent_bridge = bridge
        logger.info("Codex backend: agent bridge attached", url=bridge.url)

    # --- AgentBackend surface (src/agent_backends/protocol.py) ---

    def attach_tools(self, services: Any, bridge: Any = None) -> None:
        """Out-of-process backend: only the HTTP bridge can carry the tools."""
        if bridge is not None:
            self.attach_agent_bridge(bridge)

    async def resumable_session_for(
        self, user_id: int, working_directory: Path
    ) -> Optional[str]:
        """The thread ``_exec_turn`` would resume when no session id is passed."""
        return self.store.thread_for(user_id, working_directory)

    async def sessions_for_user(self, user_id: int) -> List["BackendSession"]:
        """Threads in the JSON store for ``user_id`` as backend-neutral records."""
        from datetime import datetime, timezone

        from .protocol import BackendSession

        out: List[BackendSession] = []
        for e in self.store.sessions_for_user(user_id):
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
        return self.config.agent_model or "openai-codex/gpt-5.4-mini"

    def _build_argv(
        self,
        *,
        model: str,
        cwd: Path,
        out_file: Path,
        resume_id: Optional[str],
        images: Optional[List[Path]] = None,
    ) -> List[str]:
        # Exec-level options must precede the `resume` subcommand — `codex exec
        # resume` does not accept them as its own args (it rejects e.g. `-C`).
        base = [self.codex_bin, "exec"]
        base += [
            "--json",
            "-C",
            str(cwd),
            "-m",
            _strip_provider_prefix(model),
            "-s",
            "danger-full-access",
            "--skip-git-repo-check",
            "-o",
            str(out_file),
        ]
        for img in images or []:
            base += ["-i", str(img)]
        base += self._mcp_overrides(cwd)
        if resume_id:
            base += ["resume", resume_id]
        return base

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

        overrides: List[str] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            command = cfg.get("command")
            if not command:
                continue
            key = str(name).replace("-", "_")
            env = {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
            if name == "telegram":
                env[IMAGE_ROOTS_ENV] = image_roots_env(
                    self.config.approved_directory, working_directory
                )
            # json.dumps renders a TOML basic string / array; `-c` parses its
            # value as TOML, so quoting has to survive the shell-free argv.
            overrides += ["-c", f"mcp_servers.{key}.command={json.dumps(str(command))}"]
            args = [str(a) for a in (cfg.get("args") or [])]
            if args:
                overrides += ["-c", f"mcp_servers.{key}.args={json.dumps(args)}"]
            for var, value in env.items():
                overrides += ["-c", f"mcp_servers.{key}.env.{var}={json.dumps(value)}"]
        return overrides

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
        child_env: Optional[Dict[str, str]] = None
        if self.agent_bridge is not None:
            from src.agent_bridge import TOKEN_ENV_VAR

            token = self.agent_bridge.mint(
                self._turn_context(
                    working_directory, user_id, requester_chat_id, family_id, role
                )
            )
            # The ONLY place the token goes: this child's environment. Not the
            # command line (world-readable in /proc), not a config file, not a
            # log line.
            child_env = {**os.environ, TOKEN_ENV_VAR: token}
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
            self.store.clear(user_id, working_directory)
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
        import aiosqlite

        async with db_manager.get_connection() as conn:
            conn.row_factory = aiosqlite.Row
            rows = await (
                await conn.execute(
                    "SELECT prompt, response FROM messages "
                    "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (thread_id, SEED_TAIL_TURNS),
                )
            ).fetchall()
            count_row = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    (thread_id,),
                )
            ).fetchone()
        pairs = [(r["prompt"], r["response"]) for r in reversed(rows)]
        total = int(count_row[0]) if count_row else None
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
    ) -> ClaudeResponse:
        """One `codex exec` subprocess, start to finish. No retry logic."""
        start = time.time()
        prompt = await self._with_persona(working_directory, prompt, family_id, user_id)

        resume_id: Optional[str] = None
        if not force_new:
            resume_id = session_id or self.store.thread_for(user_id, working_directory)

        out_file = working_directory / ".codex_last_message.txt"
        argv = self._build_argv(
            model=model or self.default_model(),
            cwd=working_directory,
            out_file=out_file,
            resume_id=resume_id,
            images=images,
        )
        argv.append(prompt)

        thread_id: Optional[str] = resume_id
        usage: Dict[str, Any] = {}
        final_fallback: Optional[str] = None
        # Tool activity, in the ClaudeResponse shape. Codex only reports it as
        # `item.started` stream events, so without this the turn persists no
        # `tool_usage` rows and the save-honesty check (which reads
        # response.tools_used) sees every Codex turn as tool-less and flags
        # any "I saved it" as a mismatch.
        tools_used: List[Dict[str, Any]] = []
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
            # None -> inherit the bot's environment (the pre-bridge default).
            # Otherwise a copy of it plus this turn's bridge token.
            env=child_env,
            # StreamReader's default line limit is 64KiB, and readline() RAISES
            # on a longer line rather than returning it. codex emits single
            # lines far past that on both streams (a models-parse error carries
            # a ~250KB body; a JSON event can carry a large tool result), so
            # the default turns a big line into a dead reader and a hung turn.
            limit=STREAM_LIMIT,
        )
        assert proc.stdout is not None and proc.stderr is not None
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
                    timeout=self.config.agent_timeout_seconds,
                )
                if not line:
                    break
                try:
                    event = json.loads(line.decode().strip())
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
                if outcome.thread_id:
                    thread_id = outcome.thread_id
                if outcome.usage:
                    usage = outcome.usage
                if outcome.final_message:
                    final_fallback = outcome.final_message
                if outcome.stream_update:
                    tools_used.extend(outcome.stream_update.tool_calls or [])
                    if on_stream:
                        await on_stream(outcome.stream_update)
            await asyncio.wait_for(
                proc.wait(), timeout=self.config.agent_timeout_seconds
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

        answer = ""
        try:
            if out_file.exists():
                answer = out_file.read_text().strip()
                out_file.unlink()
        except OSError:
            pass
        answer = answer or (final_fallback or "")

        if proc.returncode != 0 and not answer:
            # NOT `proc.stderr.read()`: the drainer above already consumed the
            # whole stream, so re-reading it here returns b"" and the operator
            # gets "exit 1: " with no reason. Report what was actually
            # captured — the stdout error events first, since that is where
            # `codex exec --json` puts API failures.
            reason = " | ".join(dict.fromkeys(e for e in error_events if e))
            if not reason:
                reason = "\n".join(stderr_tail)
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
        # tokens after a handful of chat turns (codex_sessions.json,
        # 2026-09-05).
        total_in = int(usage.get("input_tokens", 0) or 0)
        total_out = int(usage.get("output_tokens", 0) or 0)
        prev_in, prev_out = (
            self.store.thread_totals(user_id, working_directory, thread_id)
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
            self.store.record(
                user_id,
                working_directory,
                thread_id,
                in_tok,
                out_tok,
                thread_total_input=total_in,
                thread_total_output=total_out,
            )

        # cost=None, not 0.0 (dv-0722): the CLI reports no USD figure, the
        # catalog (`codex debug models`) carries no prices, and the account is
        # a ChatGPT subscription. 0.0 told the rate limiter, the watchdog and
        # /usage that Codex was free; None makes them budget the token delta
        # above instead (RateLimiter.record_tokens).
        response = ClaudeResponse(
            content=answer or "(No response from Codex.)",
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
        """Prepend the active profile's composed persona to the user prompt.

        Codex reads a project's own ``AGENTS.md`` from cwd as developer
        instructions, so the bot persona must NOT be written there: the root
        ``AGENTS.md`` is human-authored, dev-only, and no agent may clobber it.
        Injecting through the prompt each turn keeps the persona + DB-backed
        memory facts fresh without touching any project file.

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
        )
