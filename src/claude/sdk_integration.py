"""Claude Code Python SDK integration."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    HookMatcher,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    ThinkingBlock,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import McpHttpServerConfig, StreamEvent

from ..config.settings import Settings
from ..security.read_scope import ReadScope, build_read_scope
from ..security.validators import SecurityValidator
from .exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeStoppedError,
    ClaudeTimeoutError,
)
from .monitor import (
    _is_claude_internal_path,
    check_bash_denied_paths,
    check_bash_directory_boundary,
    check_bash_workspace_writes,
)

if TYPE_CHECKING:
    from ..profiles.registry import ProfileRegistry
    from ..storage.memory_repository import MemoryRetriever
    from .agent_tools import AgentServices
    from .confirmation import ConfirmationManager
    from .window_bundle import WindowBundleProvider
from .prompt_composer import PromptComposer

logger = structlog.get_logger()

# CLI 2.x deserializer injects this synthetic assistant placeholder after
# auto-injecting "Continue from where you left off." on session resume when
# the prior turn ended mid-tool-use. The placeholder has model="<synthetic>"
# and 0 tokens — the model never produces it.
_SYNTHETIC_PLACEHOLDER_TEXTS = frozenset(
    {"No response requested.", "No response requested"}
)


def _is_synthetic_placeholder(text: Optional[str]) -> bool:
    if not text:
        return False
    return text.strip() in _SYNTHETIC_PLACEHOLDER_TEXTS


_JSON_DECODE_MARKERS = ("failed to decode json", "exceeded maximum buffer size")


def _is_json_decode_text(text: str) -> bool:
    """Does this error text describe an SDK stream-json decode failure?

    Needed because the SDK strips the exception type off reader-task errors
    (see the call site in `execute_command`), leaving only the message.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _JSON_DECODE_MARKERS)


def _project_jsonl_dir(working_directory: Path) -> Path:
    """Map a cwd to its `~/.claude/projects/<sanitized>/` directory."""
    sanitized = str(working_directory).replace("/", "-")
    return Path.home() / ".claude" / "projects" / sanitized


def _session_has_orphaned_tool_use(session_id: str, working_directory: Path) -> bool:
    """Return True if the saved transcript ends mid-tool-use.

    The CLI's deserializer detects this state on resume and injects a
    "Continue from where you left off." user-meta message followed by a
    synthetic "No response requested." assistant placeholder. We try to
    catch the same condition before resume so we can skip it.
    """
    if not session_id:
        return False
    try:
        jsonl_path = _project_jsonl_dir(working_directory) / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return False

        outstanding_tool_uses: Set[str] = set()
        last_meaningful_kind: Optional[str] = None
        last_assistant_had_tool_use = False

        with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = rec.get("type")
                if t in {"system", "progress", "file-history-snapshot", "attachment"}:
                    continue
                if rec.get("isMeta"):
                    continue

                msg = rec.get("message") or {}
                # Skip the synthetic placeholder itself (model="<synthetic>").
                if t == "assistant" and msg.get("model") == "<synthetic>":
                    continue

                content = msg.get("content", [])

                if t == "assistant":
                    last_meaningful_kind = "assistant"
                    last_assistant_had_tool_use = False
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use":
                                tid = block.get("id")
                                if tid:
                                    outstanding_tool_uses.add(tid)
                                    last_assistant_had_tool_use = True
                elif t == "user":
                    last_meaningful_kind = "user"
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "tool_result"
                            ):
                                tid = block.get("tool_use_id")
                                if tid:
                                    outstanding_tool_uses.discard(tid)

        # Risk pattern: trailing assistant message had a tool_use that never got
        # a matching tool_result. The CLI will treat this as an interrupted turn
        # on the next resume.
        return (
            last_meaningful_kind == "assistant"
            and last_assistant_had_tool_use
            and bool(outstanding_tool_uses)
        )
    except Exception as e:
        logger.warning(
            "Resume safety check failed; allowing resume",
            session_id=session_id,
            error=str(e),
        )
        return False


def transcript_lost_prose_turns(
    session_id: str,
    working_directory: Path,
    lookback_turns: int = 6,
) -> int:
    """Count recent turns whose assistant reply was a pure-prose answer that the
    transcript failed to persist.

    The bot runs one CLI invocation per turn (connect/query/disconnect). A long,
    text-only assistant turn can be cut off before the CLI commits it to the
    session JSONL; on the next resume the CLI back-fills a synthetic
    "No response requested." placeholder. Such turns are *unrecoverable* from the
    transcript — unlike tool turns, whose tool_use/results persist and carry the
    meaning. We detect exactly the unrecoverable case: a user turn whose
    assistant response left neither committed text nor any tool_use.

    Returns the count among the last `lookback_turns` user-initiated turns, so a
    caller can decide whether to heal context from durable storage (SQLite).
    """
    if not session_id:
        return 0
    try:
        jsonl_path = _project_jsonl_dir(working_directory) / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return 0

        turns: List[Dict[str, bool]] = []
        cur: Optional[Dict[str, bool]] = None

        with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = rec.get("type")
                if t in {
                    "system",
                    "progress",
                    "file-history-snapshot",
                    "attachment",
                    "queue-operation",
                    "summary",
                }:
                    continue

                msg = rec.get("message") or {}
                content = msg.get("content")

                if t == "user":
                    if rec.get("isMeta"):
                        continue
                    # A real user turn carries text; a tool_result-only user
                    # message is part of the current turn, not a new one.
                    is_real = False
                    if isinstance(content, str) and content.strip():
                        is_real = True
                    elif isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "text"
                                and block.get("text", "").strip()
                            ):
                                is_real = True
                                break
                    if is_real:
                        cur = {"has_text": False, "has_tool": False}
                        turns.append(cur)
                elif t == "assistant" and cur is not None:
                    if msg.get("model") == "<synthetic>":
                        continue
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            bt = block.get("type")
                            if bt == "text":
                                text = block.get("text", "")
                                if text.strip() and not _is_synthetic_placeholder(text):
                                    cur["has_text"] = True
                            elif bt == "tool_use":
                                cur["has_tool"] = True

        recent = turns[-lookback_turns:]
        return sum(1 for tn in recent if not tn["has_text"] and not tn["has_tool"])
    except Exception as e:
        logger.warning(
            "Transcript prose-loss scan failed",
            session_id=session_id,
            error=str(e),
        )
        return 0


# ResultMessage subtypes that mean "the run stopped before producing an
# answer". All of them carry result=None, so whatever text the model had
# already streamed becomes the "response" unless we label it. Verified against
# CLI 2.1.220 (see docs/FINDINGS.md).
_ABORT_SUBTYPES = {
    "error_max_turns": "max_turns",
    "error_max_budget_usd": "max_budget",
    "error_max_structured_output_retries": "agent_error",
    "error_during_execution": "agent_error",
}


def _peak_context_tokens(messages: List[Any]) -> int:
    """Largest single-API-call context across a turn's AssistantMessages.

    Each ``AssistantMessage.usage`` describes ONE model call, so
    ``input + cache_read + cache_creation`` there is the prompt that call
    actually sent — i.e. the conversation's size at that moment. The max over
    the turn is the high-water mark, which is what a rotation threshold wants.

    Sub-agent messages (``parent_tool_use_id`` set) are excluded: a Task
    sub-agent runs its own separate context that the parent conversation never
    carries, so counting it would inflate the parent's level.

    Returns 0 when no usage was reported (aborted turn, or a CLI that does not
    emit per-message usage); callers treat 0 as "unknown", never as "empty".
    """
    peak = 0
    for msg in messages:
        if not isinstance(msg, AssistantMessage):
            continue
        if getattr(msg, "parent_tool_use_id", None):
            continue
        usage = getattr(msg, "usage", None) or {}
        if not isinstance(usage, dict):
            continue
        ctx = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
        peak = max(peak, ctx)
    return peak


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    # Prompt-cache halves of the input side, as reported by ResultMessage.
    # CAUTION: every ResultMessage.usage field is a SUM over all API calls the
    # turn made, not the state of any one call. Verified 2026-08-05: bot
    # `messages.cost` equals the summed `api_calls.est_cost_usd` for the whole
    # session to 4 decimals (msg 753: 1.3706 vs 1.3650 over 22 calls). These
    # are therefore FLOW, useful for spend, useless as a size gauge — see
    # `peak_context_tokens`. dv-9f31.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # LEVEL: the largest per-API-call context seen this turn, taken from the
    # individual AssistantMessage.usage records. This is the only number in
    # the turn that answers "how big is the conversation right now".
    peak_context_tokens: int = 0
    # True when this turn's resume failed and the facade retried on a fresh
    # backend session (dv-b41f) — the orchestrator surfaces it in the footer.
    session_restarted: bool = False

    @property
    def context_tokens(self) -> int:
        """The conversation's real size as of this turn, or 0 if unknown.

        Prefers `peak_context_tokens` (a level). Falls back to `input_tokens`
        ONLY for a backend that reported no cache split at all (Codex), where
        there is no cache to hide the context in.

        Returns 0 — not the ResultMessage sum — when caching was active but no
        per-call usage came through. 0 means "unknown"; callers key off the
        falsy value and leave the window's last known level alone. Returning
        the sum instead is what made rotation fire on every turn: an ordinary
        agentic turn of 20 tool round-trips at ~55k context sums to over 1M
        (dv-9f31, evidence in the task file).
        """
        if self.peak_context_tokens:
            return self.peak_context_tokens
        if not self.cache_read_tokens and not self.cache_creation_tokens:
            return self.input_tokens
        return 0


@dataclass
class StreamUpdate:
    """Streaming update from Claude SDK."""

    # 'assistant', 'thinking', 'user', 'system', 'result', 'stream_delta'
    type: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    metadata: Optional[Dict] = None


def _is_tenant_data(file_path: str) -> bool:
    """True when *file_path* is one of the shared multi-tenant SQLite stores."""
    import fnmatch

    from ..security.denylist import TENANT_DATA_PATTERNS

    name = Path(file_path).name
    return any(fnmatch.fnmatch(name, pat) for pat in TENANT_DATA_PATTERNS)


# Search tools recurse from their `path` root and print matched lines, so the
# root itself must pass the search-ancestor rule on guest sessions (dv-7de7).
_SEARCH_TOOLS = {"Grep", "Glob", "LS"}


def _make_pre_tool_use_hook(
    working_directory: Path,
    full_access: bool = False,
    read_scope: Optional[ReadScope] = None,
) -> Any:
    """Deny secrets / multi-tenant stores for EVERY tool call.

    ``can_use_tool`` is not enough on its own: the CLI consults it only for
    paths *outside* the session cwd. Verified 2026-08-04 -- with cwd set to this
    repo, `Read /home/.../devvy_bot/.env` was auto-approved and the callback was
    never invoked, while the same read from a workspace cwd was denied. That is
    exactly the devops case, where cwd IS the repo holding `.env` and `bot.db`.
    The SDK says as much in its CanUseToolShadowedWarning: "To gate every tool
    call, use a PreToolUse hook."

    So this hook carries the denylist concerns (which must hold everywhere) and
    ``can_use_tool`` keeps the boundary/confirmation logic. (dv-6dbc)
    """
    _PATH_KEYS = ("file_path", "path", "notebook_path")

    async def pre_tool_use(
        input_data: Dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> Dict[str, Any]:
        tool_name = input_data.get("tool_name") or ""
        tool_input = input_data.get("tool_input") or {}

        def deny(reason: str) -> Dict[str, Any]:
            logger.warning(
                "PreToolUse hook denied tool call",
                tool_name=tool_name,
                reason=reason,
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        if tool_name in {"Bash", "bash", "shell"}:
            command = tool_input.get("command", "")
            if command:
                ok, error = check_bash_denied_paths(
                    command,
                    working_directory,
                    full_access=full_access,
                    read_scope=read_scope,
                )
                if not ok:
                    return deny(error or "Denied path")
            return {}

        for key in _PATH_KEYS:
            candidate = tool_input.get(key)
            if not candidate or not isinstance(candidate, str):
                continue
            if _is_secret_path(candidate):
                return deny(
                    f"Access denied: '{candidate}' is a credential file. "
                    f"Secrets are not readable from an agent session by any "
                    f"profile -- ask the owner to read or change it directly."
                )
            if not full_access and _is_tenant_data(candidate):
                return deny(
                    f"Access denied: '{candidate}' holds every user's private "
                    f"data. Use the memory and tools this session already "
                    f"provides for the current user."
                )
            # Guest read scope (dv-7de7): cross-user trees + allowlist. The
            # hook is the primary gate because can_use_tool never fires for
            # in-cwd paths; both checks no-op on owner sessions.
            if read_scope is not None:
                ok, error = read_scope.check_read(candidate)
                if not ok:
                    return deny(error or "Denied path")
                if tool_name in _SEARCH_TOOLS:
                    ok, error = read_scope.check_search_root(candidate)
                    if not ok:
                        return deny(error or "Denied search root")
        return {}

    return pre_tool_use


def _is_secret_path(file_path: str) -> bool:
    """True when *file_path*'s basename matches a credential pattern."""
    import fnmatch

    from ..security.denylist import DEFAULT_DENIED_PATTERNS

    name = Path(file_path).name
    return any(fnmatch.fnmatch(name, pat) for pat in DEFAULT_DENIED_PATTERNS)


def _make_can_use_tool_callback(
    security_validator: SecurityValidator,
    working_directory: Path,
    approved_directory: Optional[Path],
    full_access: bool = False,
    confirmation_manager: Optional["ConfirmationManager"] = None,
    read_scope: Optional[ReadScope] = None,
) -> Any:
    """Create a can_use_tool callback for SDK-level tool permission validation.

    The callback validates file path boundaries and bash directory boundaries
    *before* the SDK executes the tool, providing preventive security enforcement.

    On **full-access** profiles (sandbox off) it additionally routes destructive
    ops through ``confirmation_manager`` for an owner approve/deny prompt.
    """
    # Every tool that names a path. MultiEdit/NotebookEdit were missing and are
    # both in the default allowed_tools, so they bypassed path validation
    # entirely even after the allow-list fix below (dv-6dbc).
    _FILE_TOOLS = {
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Read",
        "NotebookRead",
        # Search tools take a `path` too and will happily print the contents of
        # a denied file (Grep echoes matching lines).
        "Grep",
        "Glob",
        "LS",
        "create_file",
        "edit_file",
        "read_file",
    }
    # The subset that MUTATES. Sandboxed profiles may read across workspaces
    # (cross-agent reads are allowed by design) but may not write outside their
    # own working dir.
    _WRITE_TOOLS = {
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "create_file",
        "edit_file",
    }
    _BASH_TOOLS = {"Bash", "bash", "shell"}
    _SUBAGENT_TOOLS = {"Agent", "Task"}

    def _write_target_allowed(path: str) -> Tuple[bool, Optional[str]]:
        """Confine writes to the profile workspace on non-full-access profiles.

        In denylist mode ``validate_path`` checks only the denylist and never
        compares against ``working_directory`` (src/security/validators.py),
        so without this a sandboxed profile's ``Write`` could land anywhere the
        Unix user can reach -- verified live 2026-08-02: `Write ~/PROBE.txt`
        succeeded from the `general` profile. The CLI sandbox blocks this for
        bash but does not cover in-process file tools.
        """
        # Lazy: src.bot imports src.claude, so a module-level import here would
        # close the cycle (same reason as the image_extractor import below).
        from ..bot.utils.image_extractor import session_scratch_roots

        try:
            target = Path(path)
            if not target.is_absolute():
                target = working_directory / target
            resolved = target.resolve()
        except (ValueError, OSError):
            return False, f"Could not resolve write target: {path}"

        roots = [working_directory.resolve(), *session_scratch_roots()]
        for root in roots:
            try:
                resolved.relative_to(root)
                return True, None
            except ValueError:
                continue

        return False, (
            f"Access denied: writing to '{path}' is outside this agent's "
            f"workspace ({working_directory}). Reads outside are fine; writes "
            f"must stay in the workspace or the session scratch dir."
        )

    async def can_use_tool(
        tool_name: str,
        tool_input: Dict[str, Any],
        context: ToolPermissionContext,
    ) -> Any:
        # Background subagents can NEVER work here: the bot runs one CLI
        # process per turn and disconnects when the reply is sent, killing any
        # backgrounded subagent silently mid-flight (Claude then hallucinates
        # "still running" next turn). Since CLI 2.1.198 background is the
        # DEFAULT, so an omitted run_in_background means background too — only
        # an explicit false (Claude waits for the result this turn) may pass.
        if tool_name in _SUBAGENT_TOOLS and tool_input.get("run_in_background", True):
            logger.warning(
                "can_use_tool denied background subagent",
                tool_name=tool_name,
                subagent_type=tool_input.get("subagent_type"),
            )
            return PermissionResultDeny(
                message=(
                    "Background subagents die when this turn ends and their "
                    "output is lost. Either retry with run_in_background: "
                    "false (you will wait for the result in this turn; "
                    "parallel foreground agents are fine), or use the "
                    "job_start tool for work that must outlive the turn."
                )
            )
        # Destructive-op confirmation gate (full-access profiles only). Sandboxed
        # profiles can't do cross-FS/network damage from bash, so they skip this.
        if full_access and confirmation_manager is not None:
            from ..security.destructive import classify

            reason = classify(tool_name, tool_input, working_directory)
            if reason:
                preview = (
                    tool_input.get("command")
                    or tool_input.get("file_path")
                    or tool_input.get("path")
                    or ""
                )
                approved = await confirmation_manager.request(reason, str(preview))
                if not approved:
                    logger.warning(
                        "Destructive op denied by confirmation gate",
                        tool_name=tool_name,
                        reason=reason,
                    )
                    return PermissionResultDeny(
                        message=f"Denied by owner (confirmation gate): {reason}"
                    )

        # File path validation
        if tool_name in _FILE_TOOLS:
            file_path = tool_input.get("file_path") or tool_input.get("path")
            if file_path:
                # Allow Claude Code internal paths (~/.claude/plans/, etc.)
                if _is_claude_internal_path(file_path):
                    return PermissionResultAllow()

                valid, _resolved, error = security_validator.validate_path(
                    file_path, working_directory
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied file operation",
                        tool_name=tool_name,
                        file_path=file_path,
                        error=error,
                    )
                    return PermissionResultDeny(message=error or "Invalid file path")

                # Guest read scope (dv-7de7): mirror of the PreToolUse hook
                # checks, kept here for depth because "can_use_tool fires only
                # outside cwd" is CLI-version-dependent (same reasoning as
                # dv-6dbc). No-ops on owner sessions.
                if read_scope is not None:
                    ok, scope_error = read_scope.check_read(file_path)
                    if ok and tool_name in _SEARCH_TOOLS:
                        ok, scope_error = read_scope.check_search_root(file_path)
                    if not ok:
                        logger.warning(
                            "can_use_tool denied read-scope violation",
                            tool_name=tool_name,
                            file_path=file_path,
                            error=scope_error,
                        )
                        return PermissionResultDeny(
                            message=scope_error or "Denied path"
                        )

                # Multi-tenant stores: denied to sandboxed profiles only.
                # (Secrets are handled by the denylist above, for everyone.)
                if not full_access and _is_tenant_data(file_path):
                    logger.warning(
                        "can_use_tool denied multi-tenant data read",
                        tool_name=tool_name,
                        file_path=file_path,
                    )
                    return PermissionResultDeny(
                        message=(
                            f"Access denied: '{file_path}' holds every user's "
                            f"private data. Use the memory and tools this "
                            f"session already provides for the current user."
                        )
                    )

                # Workspace confinement for writes on sandboxed profiles.
                if not full_access and tool_name in _WRITE_TOOLS:
                    ok, boundary_error = _write_target_allowed(file_path)
                    if not ok:
                        logger.warning(
                            "can_use_tool denied out-of-workspace write",
                            tool_name=tool_name,
                            file_path=file_path,
                            working_directory=str(working_directory),
                        )
                        return PermissionResultDeny(
                            message=boundary_error or "Write outside workspace"
                        )

        # Bash validation
        if tool_name in _BASH_TOOLS:
            # The CLI lets the model lift the OS sandbox for a single call via
            # dangerouslyDisableSandbox (full FS writes + network). On sandboxed
            # profiles that voids the containment story and the guest invariant
            # in profiles/registry.py, so only full-access profiles may use it.
            if not full_access and tool_input.get("dangerouslyDisableSandbox"):
                logger.warning(
                    "can_use_tool denied sandbox disable",
                    tool_name=tool_name,
                    command=tool_input.get("command", ""),
                )
                return PermissionResultDeny(
                    message=(
                        "The sandbox cannot be disabled on this profile. Rerun "
                        "the command inside the sandbox. For charts/images, "
                        "render with python3 + matplotlib (set MPLCONFIGDIR to "
                        "a writable dir, e.g. under /tmp) instead of headless "
                        "Chrome; write the output file to your workspace or "
                        "/tmp and send it with the image tool."
                    )
                )
            command = tool_input.get("command", "")
            if command:
                # Denied paths (secrets, multi-tenant data stores, guest read
                # scope) -- applies to reads too, which the boundary check
                # below never inspects.
                valid, error = check_bash_denied_paths(
                    command,
                    working_directory,
                    full_access=full_access,
                    read_scope=read_scope,
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied bash command (denied path)",
                        tool_name=tool_name,
                        command=command,
                        error=error,
                    )
                    return PermissionResultDeny(message=error or "Denied path")

                # Workspace confinement for bash writes on sandboxed profiles.
                # Denylist mode only: in allowlist mode the boundary is
                # approved_directory (which may sit above working_directory),
                # and check_bash_directory_boundary below already enforces it.
                if not full_access and approved_directory is None:
                    from ..bot.utils.image_extractor import session_scratch_roots

                    valid, error = check_bash_workspace_writes(
                        command, working_directory, session_scratch_roots()
                    )
                    if not valid:
                        logger.warning(
                            "can_use_tool denied out-of-workspace bash write",
                            tool_name=tool_name,
                            command=command,
                            working_directory=str(working_directory),
                        )
                        return PermissionResultDeny(
                            message=error or "Write outside workspace"
                        )

                valid, error = check_bash_directory_boundary(
                    command, working_directory, approved_directory
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied bash command",
                        tool_name=tool_name,
                        command=command,
                        error=error,
                    )
                    return PermissionResultDeny(
                        message=error or "Bash directory boundary violation"
                    )

        return PermissionResultAllow()

    return can_use_tool


class ClaudeSDKManager:
    """Manage Claude Code SDK integration."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
        prompt_composer: Optional[PromptComposer] = None,
        profile_registry: Optional["ProfileRegistry"] = None,
        memory_retriever: Optional["MemoryRetriever"] = None,
        bundle_provider: Optional["WindowBundleProvider"] = None,
    ):
        """Initialize SDK manager with configuration."""
        self.config = config
        self.security_validator = security_validator
        self.prompt_composer = prompt_composer
        self.profile_registry = profile_registry
        self.memory_retriever = memory_retriever
        self.bundle_provider = bundle_provider
        self._profile_composers: Dict[str, PromptComposer] = {}
        # In-process agent MCP services (schedule/notify/task). Attached after
        # startup because the scheduler is created later than this manager.
        self._agent_services: Optional["AgentServices"] = None
        # Killswitch (/stop): when stopped, new runs are refused and in-flight
        # SDK clients are interrupted. Active clients are tracked so /stop can
        # reach them.
        self._stopped: bool = False
        self._active_clients: Set["ClaudeSDKClient"] = set()
        # Owner approve/deny gate for destructive ops on full-access profiles.
        self._confirmation_manager: Optional["ConfirmationManager"] = None

        # Set up environment for Claude Code SDK if API key is provided
        # If no API key is provided, the SDK will use existing CLI authentication
        if config.anthropic_api_key_str:
            os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key_str
            logger.info("Using provided API key for Claude SDK authentication")
        else:
            logger.info("No API key provided, using existing Claude CLI authentication")

    def attach_agent_services(self, services: "AgentServices") -> None:
        """Wire in-process schedule/notify/task services (post-startup).

        Mounts the ``agent`` SDK MCP server on every profile so agents can
        schedule/notify/manage tasks without the sandboxed HTTP ``agentctl``
        path. Idempotent; safe to call once services exist.
        """
        self._agent_services = services
        logger.info("Agent MCP services attached")

    def attach_confirmation_manager(self, manager: "ConfirmationManager") -> None:
        """Wire the destructive-op confirmation gate (post-startup, needs bot)."""
        self._confirmation_manager = manager
        logger.info("Confirmation manager attached")

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    async def stop_all(self) -> int:
        """Killswitch: refuse new runs, interrupt every in-flight SDK client,
        and pause the scheduler. Returns the number of runs interrupted."""
        self._stopped = True
        clients = list(self._active_clients)
        interrupted = 0
        for client in clients:
            try:
                await client.interrupt()
                interrupted += 1
            except Exception as exc:  # best-effort; disconnect happens in finally
                logger.warning("interrupt failed during stop", error=str(exc))
        if self._agent_services and self._agent_services.scheduler:
            try:
                self._agent_services.scheduler.pause()
            except Exception as exc:
                logger.warning("scheduler pause failed", error=str(exc))
        logger.info("Killswitch engaged", interrupted=interrupted)
        return interrupted

    def resume_all(self) -> None:
        """Clear the killswitch and resume the scheduler."""
        self._stopped = False
        if self._agent_services and self._agent_services.scheduler:
            try:
                self._agent_services.scheduler.resume()
            except Exception as exc:
                logger.warning("scheduler resume failed", error=str(exc))
        logger.info("Killswitch cleared")

    def _agent_mcp_server_for(
        self,
        working_directory: Path,
        requester_chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        family_id: Optional[int] = None,
        role: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the in-process ``agent`` MCP server for this working dir.

        ``requester_chat_id`` (the Telegram chat driving this turn) is captured
        by the ``notify``/``schedule_add`` tools so replies target the
        requesting chat instead of the broadcast list.

        ``user_id`` / ``family_id`` are used HERE, server-side, to resolve the
        memory scopes the ``remember``/``recall`` tools read/write — the same
        ``_agent_id_for`` / ``_family_agent_id_for`` calls prompt composition
        already uses, so the tools operate exactly where this turn reads. The
        resolved strings are baked into the tools' closures; the model never
        supplies a scope (dv-6dbc). ``user_id`` is also passed through raw for
        ``recall_conversation``, which scopes by (user_id, working_directory)
        rather than by memory agent_id (dv-r3c1).

        ``role`` gates the background-job tools: only ``"owner"`` gets
        ``job_start``/``job_list``/``job_output``/``jobs_refresh_pin``
        mounted; guests and callers with no resolved role (webhook,
        scheduler) fail closed to none (dv-a900).
        """
        if not self._agent_services:
            return {}
        from .agent_tools import build_agent_mcp_server

        profile_slug = self._agent_id_for(working_directory)
        memory_agent_id = self._agent_id_for(working_directory, user_id)
        memory_family_agent_id = (
            self._family_agent_id_for(working_directory, family_id, user_id)
            if family_id is not None
            else None
        )
        server = build_agent_mcp_server(
            self._agent_services,
            profile_slug,
            working_directory,
            requester_chat_id=requester_chat_id,
            memory_agent_id=memory_agent_id,
            memory_family_agent_id=memory_family_agent_id,
            user_id=user_id,
            role=role,
        )
        return {"agent": server}

    def _sandbox_enabled_for(self, working_directory: Path) -> bool:
        """Resolve the bash-sandbox flag for the profile at this working dir.

        Per-profile ``sandbox`` (in the registry) overrides the global
        ``SANDBOX_ENABLED`` default. ``devops`` ships with ``sandbox: false``
        (full access); everything else inherits the global default (on).
        """
        if self.profile_registry:
            profile = self.profile_registry.get_by_working_dir(working_directory)
            if profile is not None and profile.sandbox is not None:
                return profile.sandbox
        return self.config.sandbox_enabled

    def _max_turns_for(self, working_directory: Path) -> int:
        """Resolve the agent turn limit for the profile at this working dir.

        Per-profile ``max_turns`` (in the registry) overrides the global
        ``CLAUDE_MAX_TURNS`` default — ``devops`` runs long multi-file tasks
        that routinely blow past the conservative global cap.
        """
        if self.profile_registry:
            profile = self.profile_registry.get_by_working_dir(working_directory)
            if profile is not None and profile.max_turns is not None:
                return profile.max_turns
        return self.config.claude_max_turns

    def _voice_runtime_note(self) -> Optional[str]:
        """System-prompt note describing the live voice transcription setup.

        Without it the agent cannot answer "what do you hear / which languages"
        from context: the only file holding the provider is .env, which the
        denylist blocks, so it spelunks the repo and service state instead
        (dv-38ef). Appended after every prompt source (bundle, composer,
        fallback) so it is present regardless of how the base prompt was built.
        """
        cfg = self.config
        if not getattr(cfg, "enable_voice_messages", False):
            return None
        provider = getattr(cfg, "voice_provider", None)
        if not isinstance(provider, str):
            return None
        name = cfg.voice_provider_display_name
        model = cfg.resolved_voice_model
        language = getattr(cfg, "voice_language", None)
        lang = (
            f"language pinned to '{language}'"
            if language
            else "language auto-detected per message (multilingual, mixed-language ok)"
        )
        return (
            "<runtime>\n"
            "Voice messages reach you already transcribed — the text after "
            '"Voice message transcription:" is what the user said. Transcription '
            f"runs on {name} (model {model}), {lang}; you never hear audio. "
            "Answer questions about your own voice/transcription setup from this "
            "note. Do not inspect the codebase, config files, or service state to "
            "answer questions about your own capabilities unless the user asks "
            "you to investigate.\n"
            "</runtime>"
        )

    def _max_budget_for(
        self, working_directory: Path, role: Optional[str]
    ) -> Optional[float]:
        """Resolve the per-request SDK budget cap (USD) for this turn.

        Precedence: per-profile ``max_cost_per_request`` (profiles.yaml) >
        global ``CLAUDE_MAX_COST_PER_REQUEST`` > uncapped for owners.

        Owners are uncapped by default (owner decision 2026-08-03: the cap was
        aborting their own turns mid-answer). A profile that sets an explicit
        value opts back in for everyone on it — a cheap agent left in a loop
        should not be able to spend without bound just because the person who
        started it is the owner. 0 or negative disables the cap explicitly.
        """
        if self.profile_registry:
            profile = self.profile_registry.get_by_working_dir(working_directory)
            if profile is not None and profile.max_cost_per_request is not None:
                cap = profile.max_cost_per_request
                return cap if cap > 0 else None
        if role == "owner":
            return None
        return self.config.claude_max_cost_per_request

    def _composer_for(self, working_directory: Path) -> Optional[PromptComposer]:
        """Pick the per-profile composer for a working dir, else the default."""
        if not self.profile_registry:
            return self.prompt_composer
        profile = self.profile_registry.get_by_working_dir(working_directory)
        if not profile:
            return self.prompt_composer
        cached = self._profile_composers.get(profile.slug)
        if cached is None:
            cached = PromptComposer(
                memory_path=profile.memory_dir,
                agent_md_path=profile.persona,
            )
            self._profile_composers[profile.slug] = cached
        return cached

    def _agent_id_for(
        self, working_directory: Path, user_id: Optional[int] = None
    ) -> str:
        """Profile slug is the memory scope key; default profile is 'devops'.

        ``user_id`` applies per-user scoping on ``personal_memory`` profiles.
        """
        from ..profiles.registry import agent_id_for_working_dir

        return agent_id_for_working_dir(
            self.profile_registry, working_directory, user_id
        )

    def _family_agent_id_for(
        self, working_directory: Path, family_id: int, user_id: Optional[int] = None
    ) -> str:
        """Family memory scope (``f{family_id}:{slug}``) for this working dir."""
        from ..profiles.registry import family_agent_id_for_agent

        return family_agent_id_for_agent(
            self._agent_id_for(working_directory, user_id), family_id
        )

    def _mcp_servers_for(self, working_directory: Path) -> Dict[str, Any]:
        """Return MCP server config for the profile bound to working_directory.

        Reads the profile's ``mcp`` seam file (JSON with ``type``, ``url``, and
        ``auth_env`` fields) and produces a single ``channel-intel`` entry.
        Profiles without an ``mcp`` seam — or when there is no registry — return
        an empty dict.
        """
        if not self.profile_registry:
            return {}
        profile = self.profile_registry.get_by_working_dir(working_directory)
        if not profile or not profile.mcp:
            return {}
        try:
            spec = json.loads(Path(profile.mcp).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "profile mcp seam unreadable", path=str(profile.mcp), error=str(exc)
            )
            return {}
        if (
            not isinstance(spec, dict)
            or spec.get("type") != "http"
            or not spec.get("url")
        ):
            logger.warning("profile mcp seam invalid", path=str(profile.mcp))
            return {}
        headers: Dict[str, str] = {}
        tok = os.environ.get(spec.get("auth_env", ""))
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        cfg: McpHttpServerConfig = {"type": "http", "url": str(spec["url"])}
        if headers:
            cfg["headers"] = headers
        return {"channel-intel": cfg}

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        model: Optional[str] = None,
        requester_chat_id: Optional[int] = None,
        family_id: Optional[int] = None,
        user_id: Optional[int] = None,
        role: Optional[str] = None,
        _is_retry: bool = False,
    ) -> ClaudeResponse:
        """Execute Claude Code command via SDK.

        ``family_id`` (the requesting user's family, if any) adds the shared
        family fact scope to prompt composition. None = no family, prompts are
        unchanged.

        ``user_id`` additionally resolves per-user memory scope on profiles
        with ``personal_memory: true`` (see ``agent_id_for_working_dir``) —
        without it, every owner-role user on such a profile would share one
        private fact store.

        ``role`` gates the per-request SDK budget cap (``max_budget_usd``):
        "owner" removes it, anything else (including the default ``None``,
        used by webhook/scheduler callers with no human role to check) keeps
        it capped at ``claude_max_cost_per_request``. Defaulting to capped
        keeps every existing caller's behavior unchanged; only Telegram call
        sites that resolve a real role opt into the uncapped path. ``role``
        also gates the background-job agent tools: only "owner" gets them
        mounted, everything else fails closed to none (dv-a900).
        """
        if self._stopped:
            raise ClaudeStoppedError(
                "Bot is stopped via /stop. Send /resume to continue."
            )
        start_time = asyncio.get_event_loop().time()

        # Pre-resume safety check: if the saved session ended with an orphaned
        # tool_use, the CLI will inject a "Continue from where you left off." /
        # synthetic "No response requested." pair on resume. Skip resume in
        # that case and prepend a brief recap so the model has continuity.
        recap_prefix = ""
        if (
            session_id
            and continue_session
            and _session_has_orphaned_tool_use(session_id, working_directory)
        ):
            logger.warning(
                "Saved session ends with orphaned tool_use; starting fresh",
                session_id=session_id,
            )
            recap_prefix = (
                "[Note: the previous session ended mid-tool-call and has been "
                "discarded. Treat this as a fresh start in the same workspace.]\n\n"
            )
            session_id = None
            continue_session = False

        logger.info(
            "Starting Claude SDK command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
            is_retry=_is_retry,
        )

        try:
            # Capture stderr from Claude CLI for better error diagnostics
            stderr_lines: List[str] = []

            def _stderr_callback(line: str) -> None:
                stderr_lines.append(line)
                logger.debug("Claude CLI stderr", line=line)

            # Per-profile sandbox: devops runs unsandboxed (full access); the
            # prompt's cwd confinement is relaxed to match.
            sandbox_enabled = self._sandbox_enabled_for(Path(working_directory))
            full_access = not sandbox_enabled

            # Per-session read scope (dv-7de7): guest sessions get a read
            # allowlist + cross-user tree denial; owner sessions build a
            # no-op scope (guest_allowlist=None) and keep unrestricted
            # cross-agent reads. Lazy import: src.bot imports src.claude.
            from ..bot.utils.image_extractor import session_scratch_roots

            read_scope = build_read_scope(
                registry=self.profile_registry,
                working_directory=Path(working_directory),
                user_id=user_id,
                role=role,
                scratch_roots=session_scratch_roots(),
            )

            # Build the system prompt: identity + memory facts only.
            #
            # CLAUDE.md is deliberately NOT read here. `setting_sources` below
            # includes "project", and the CLI loads {cwd}/CLAUDE.md itself for
            # that source (claude_agent_sdk.types:ClaudeAgentOptions.
            # setting_sources -- "Must include 'project' to load CLAUDE.md
            # files"). Injecting it here too put the whole file in context
            # twice; for the devops profile that was ~3.9k duplicated tokens
            # on every turn. See docs/FINDINGS.md 2026-08-04.
            # Frozen per-window bundle (dv-5e2a): one composition per window,
            # byte-identical on every turn until the next roll or /new. The
            # live compose below survives only as a fallback for callers with
            # no user_id (no window to key) or deployments without the
            # provider.
            bundle = None
            if self.bundle_provider is not None and user_id is not None:
                bundle = await self.bundle_provider.get_or_materialize(
                    working_directory=Path(working_directory),
                    user_id=user_id,
                    family_id=family_id,
                    full_access=full_access,
                )
            composer = self._composer_for(Path(working_directory))
            if bundle is not None:
                base_prompt = bundle.text
            elif composer:
                pinned_facts: List[str] = []
                general_facts: List[str] = []
                family_facts: List[str] = []
                result_pointers: List[str] = []
                if self.memory_retriever:
                    (
                        pinned_facts,
                        general_facts,
                        result_pointers,
                    ) = await self.memory_retriever.select(
                        self._agent_id_for(Path(working_directory), user_id),
                        query=prompt,
                    )
                    if family_id is not None:
                        family_facts = await self.memory_retriever.select_family(
                            self._family_agent_id_for(
                                Path(working_directory), family_id, user_id
                            ),
                            query=prompt,
                        )
                base_prompt = composer.compose(
                    working_directory=Path(working_directory),
                    pinned_facts=pinned_facts,
                    general_facts=general_facts,
                    family_facts=family_facts or None,
                    result_pointers=result_pointers or None,
                    full_access=full_access,
                )
            else:
                # Fallback: original inline construction. CLAUDE.md is left to
                # the CLI here too -- see the note above.
                base_prompt = (
                    f"All file operations must stay within {working_directory}. "
                    "Use relative paths."
                )

            runtime_note = self._voice_runtime_note()
            if runtime_note:
                base_prompt = f"{base_prompt}\n\n{runtime_note}"

            # When DISABLE_TOOL_VALIDATION=true, pass None for allowed/disallowed
            # tools so the SDK does not restrict tool usage (e.g. MCP tools).
            if self.config.disable_tool_validation:
                sdk_allowed_tools = None
                sdk_disallowed_tools = None
            else:
                sdk_allowed_tools = self.config.claude_allowed_tools
                sdk_disallowed_tools = self.config.claude_disallowed_tools

            # Tools in allowed_tools are auto-approved by the CLI and never
            # reach can_use_tool, where validate_path, the denylist and the
            # bash boundary checks live. This strip is what routes them there.
            #
            # It used to be gated on `full_access`, which inverted the security
            # model: devops -- the ONE trusted profile -- was the only one whose
            # file operations were validated at all, while every guest-reachable
            # profile ran unchecked. That is dv-6dbc; the strip is now
            # unconditional. can_use_tool answers allow/deny programmatically,
            # so this adds enforcement, not approval prompts (the separate
            # confirmation gate inside the callback is still full-access only).
            # Read/Grep/Glob/LS are here for the same reason as the write tools:
            # the SDK warns "can_use_tool will not be invoked for: Read, Glob,
            # Grep, LS..." when they sit in allowed_tools, so `Read .env`
            # bypassed enforcement entirely. Verified by probe run 2026-08-04:
            # every other step passed while step 1 stayed ALLOWED until these
            # were stripped too.
            if isinstance(sdk_allowed_tools, list):
                _GATED = {
                    "Bash",
                    "Write",
                    "Edit",
                    "MultiEdit",
                    "NotebookEdit",
                    "Read",
                    "NotebookRead",
                    "Grep",
                    "Glob",
                    "LS",
                }
                sdk_allowed_tools = [t for t in sdk_allowed_tools if t not in _GATED]

            # Per-request budget cap: per-profile override > global config >
            # uncapped for owners. When this cap is hit the CLI aborts the turn
            # with result=None, which is why _ABORT_SUBTYPES below has to label
            # the outcome rather than let the partial narration stand.
            max_budget_usd = self._max_budget_for(Path(working_directory), role)

            # Build Claude Agent options
            options = ClaudeAgentOptions(
                max_turns=self._max_turns_for(working_directory),
                model=model or self.config.claude_model or None,
                max_budget_usd=max_budget_usd,
                cwd=str(working_directory),
                allowed_tools=sdk_allowed_tools,
                disallowed_tools=sdk_disallowed_tools,
                cli_path=self.config.claude_cli_path or None,
                include_partial_messages=stream_callback is not None,
                sandbox={
                    "enabled": sandbox_enabled,
                    # Must stay False: when true the CLI auto-approves bash for
                    # sandboxed profiles, so `sqlite3 data/bot.db` never reaches
                    # can_use_tool. The sandbox blocks bash *writes* and egress,
                    # not reads -- and reads are the cross-user leak (dv-6dbc).
                    "autoAllowBashIfSandboxed": False,
                    "excludedCommands": self.config.sandbox_excluded_commands or [],
                },
                system_prompt=base_prompt,
                setting_sources=["project"],
                stderr=_stderr_callback,
                max_buffer_size=self.config.claude_max_buffer_size,
            )

            # Pass MCP server configuration if enabled
            if self.config.enable_mcp and self.config.mcp_config_path:
                options.mcp_servers = self._load_mcp_config(self.config.mcp_config_path)
                # Tell the telegram server which roots are deliverable for THIS
                # turn, so send_image_to_user can refuse an out-of-bounds path
                # itself. Without this the bot's intercept drops the image
                # silently after the tool already reported success, and the
                # agent tells the user it sent a chart that never arrived.
                self._inject_image_roots(options.mcp_servers, working_directory)
                logger.info(
                    "MCP servers configured",
                    mcp_config_path=str(self.config.mcp_config_path),
                )

            # Merge per-profile MCP servers (e.g. channel-intel for the channel
            # profile) and the in-process agent server (schedule/notify/task).
            extra_mcp: Dict[str, Any] = {
                **self._mcp_servers_for(working_directory),
                **self._agent_mcp_server_for(
                    working_directory,
                    requester_chat_id=requester_chat_id,
                    user_id=user_id,
                    family_id=family_id,
                    role=role,
                ),
            }
            if extra_mcp:
                existing: Dict[str, Any] = (
                    options.mcp_servers  # type: ignore[assignment]
                    if isinstance(options.mcp_servers, dict)
                    else {}
                )
                options.mcp_servers = {**existing, **extra_mcp}
                logger.info(
                    "Extra MCP servers mounted",
                    servers=list(extra_mcp),
                    cwd=str(working_directory),
                )

            # Denylist enforcement that must hold for EVERY tool call, including
            # reads of files inside cwd -- which can_use_tool never sees.
            options.hooks = {
                "PreToolUse": [
                    HookMatcher(
                        hooks=[
                            _make_pre_tool_use_hook(
                                working_directory=Path(working_directory),
                                full_access=full_access,
                                read_scope=read_scope,
                            )
                        ]
                    )
                ]
            }

            # Wire can_use_tool callback for preventive tool validation
            if self.security_validator:
                options.can_use_tool = _make_can_use_tool_callback(
                    security_validator=self.security_validator,
                    working_directory=working_directory,
                    approved_directory=self.config.approved_directory,
                    full_access=full_access,
                    confirmation_manager=self._confirmation_manager,
                    read_scope=read_scope,
                )

            # Resume previous session if we have a session_id
            if session_id and continue_session:
                options.resume = session_id
                logger.info(
                    "Resuming previous session",
                    session_id=session_id,
                )

            # Collect messages via ClaudeSDKClient
            messages: List[Message] = []

            async def _run_client() -> None:
                # Use connect(None) + query(prompt) pattern because
                # can_use_tool requires the prompt as AsyncIterable, not
                # a plain string. connect(None) uses an empty async
                # iterable internally, satisfying the requirement.
                client = ClaudeSDKClient(options)
                self._active_clients.add(client)
                try:
                    await client.connect()
                    await client.query(
                        recap_prefix + prompt if recap_prefix else prompt
                    )

                    # Iterate over raw messages and parse them ourselves
                    # so that MessageParseError (e.g. from rate_limit_event)
                    # doesn't kill the underlying async generator. When
                    # parse_message raises inside the SDK's receive_messages()
                    # generator, Python terminates that generator permanently,
                    # causing us to lose all subsequent messages including
                    # the ResultMessage.
                    async for raw_data in client._query.receive_messages():
                        try:
                            message = parse_message(raw_data)
                        except MessageParseError as e:
                            logger.debug(
                                "Skipping unparseable message",
                                error=str(e),
                            )
                            continue

                        # SDK 0.2.x: parse_message returns None for
                        # unrecognized message types (forward compat)
                        if message is None:
                            continue

                        messages.append(message)

                        if isinstance(message, ResultMessage):
                            break

                        # Handle streaming callback
                        if stream_callback:
                            try:
                                await self._handle_stream_message(
                                    message, stream_callback
                                )
                            except Exception as callback_error:
                                logger.warning(
                                    "Stream callback failed",
                                    error=str(callback_error),
                                    error_type=type(callback_error).__name__,
                                )
                finally:
                    self._active_clients.discard(client)
                    await client.disconnect()

            # Execute with timeout
            await asyncio.wait_for(
                _run_client(),
                timeout=self.config.claude_timeout_seconds,
            )

            # Extract cost, tools, token usage, and session_id from result message
            cost = 0.0
            tools_used: List[Dict[str, Any]] = []
            claude_session_id = None
            result_content = None
            result_subtype = None
            input_tokens = 0
            output_tokens = 0
            cache_read_tokens = 0
            cache_creation_tokens = 0
            for message in messages:
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    result_subtype = getattr(message, "subtype", None)
                    # Extract token usage. Everything here is a per-turn SUM
                    # across API calls -- correct for spend, wrong for size.
                    # The size level comes from _peak_context_tokens (dv-9f31).
                    usage = getattr(message, "usage", None) or {}
                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation_tokens = (
                        usage.get("cache_creation_input_tokens", 0) or 0
                    )
                    claude_session_id = getattr(message, "session_id", None)
                    result_content = getattr(message, "result", None)
                    current_time = asyncio.get_event_loop().time()
                    for msg in messages:
                        if isinstance(msg, AssistantMessage):
                            msg_content = getattr(msg, "content", [])
                            if msg_content and isinstance(msg_content, list):
                                for block in msg_content:
                                    if isinstance(block, ToolUseBlock):
                                        tools_used.append(
                                            {
                                                "name": getattr(
                                                    block, "name", "unknown"
                                                ),
                                                "timestamp": current_time,
                                                "input": getattr(block, "input", {}),
                                            }
                                        )
                    break

            # Fallback: extract session_id from StreamEvent messages if
            # ResultMessage didn't provide one (can happen with some CLI versions)
            if not claude_session_id:
                for message in messages:
                    msg_session_id = getattr(message, "session_id", None)
                    if msg_session_id and not isinstance(message, ResultMessage):
                        claude_session_id = msg_session_id
                        logger.info(
                            "Got session ID from stream event (fallback)",
                            session_id=claude_session_id,
                        )
                        break

            # Calculate duration
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Use Claude's session_id if available, otherwise fall back
            final_session_id = claude_session_id or session_id or ""

            if claude_session_id and claude_session_id != session_id:
                logger.info(
                    "Got session ID from Claude",
                    claude_session_id=claude_session_id,
                    previous_session_id=session_id,
                )

            # Use ResultMessage.result if available, fall back to message extraction
            if result_content is not None:
                content = result_content
            else:
                content_parts = []
                for msg in messages:
                    if isinstance(msg, AssistantMessage):
                        msg_content = getattr(msg, "content", [])
                        if msg_content and isinstance(msg_content, list):
                            for block in msg_content:
                                if hasattr(block, "text"):
                                    text = block.text
                                    # Drop the CLI's synthetic resume placeholder.
                                    if _is_synthetic_placeholder(text):
                                        continue
                                    content_parts.append(text)
                        elif msg_content:
                            content_parts.append(str(msg_content))
                content = "\n".join(content_parts)

            # Reactive backstop: if the final content is the synthetic placeholder
            # or empty, the CLI's resume deserializer fired despite our pre-check
            # (or some other path produced an empty turn). Retry once with a
            # fresh session so the user gets a real response.
            #
            # Not for a deliberate abort (budget/turn cap): the retry would run
            # the same expensive turn straight into the same wall and bill for
            # it twice. Those fall through to the stop-reason branch below.
            aborted = (result_subtype or "") in _ABORT_SUBTYPES
            if (
                not _is_retry
                and not aborted
                and (not content.strip() or _is_synthetic_placeholder(content))
            ):
                logger.warning(
                    "Got synthetic/empty result; retrying once with fresh session",
                    original_session_id=session_id,
                    claude_session_id=claude_session_id,
                )
                return await self.execute_command(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=None,
                    continue_session=False,
                    stream_callback=stream_callback,
                    model=model,
                    requester_chat_id=requester_chat_id,
                    family_id=family_id,
                    user_id=user_id,
                    role=role,
                    _is_retry=True,
                )

            # An aborted run has no ResultMessage.result — the content above is
            # then just concatenated mid-task narration, which reads exactly
            # like a finished answer cut off mid-sentence. Surface the stop
            # reason instead of presenting the narration as a result.
            #
            # error_max_budget_usd is the one that bit us: verified against CLI
            # 2.1.220, a --max-budget-usd stop returns subtype
            # error_max_budget_usd, is_error=true and result=None. Nothing
            # matched on it, so the turn looked truncated with no log line.
            stop_reason = _ABORT_SUBTYPES.get(result_subtype or "")
            if stop_reason:
                logger.warning(
                    "Claude run aborted before finishing",
                    session_id=final_session_id,
                    subtype=result_subtype,
                    stop_reason=stop_reason,
                    max_turns=self._max_turns_for(working_directory),
                    max_budget_usd=max_budget_usd,
                    cost=cost,
                )
            elif result_subtype and result_subtype.startswith("error"):
                # Unknown error subtype: still better surfaced than silent.
                stop_reason = "agent_error"
                logger.warning(
                    "Claude run returned an unrecognized error subtype",
                    session_id=final_session_id,
                    subtype=result_subtype,
                )

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                error_type=stop_reason,
                num_turns=len(
                    [
                        m
                        for m in messages
                        if isinstance(m, (UserMessage, AssistantMessage))
                    ]
                ),
                tools_used=tools_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                peak_context_tokens=_peak_context_tokens(messages),
            )

        except asyncio.TimeoutError:
            logger.error(
                "Claude SDK command timed out",
                timeout_seconds=self.config.claude_timeout_seconds,
            )
            raise ClaudeTimeoutError(
                f"Claude SDK timed out after {self.config.claude_timeout_seconds}s"
            )

        except CLINotFoundError as e:
            logger.error("Claude CLI not found", error=str(e))
            error_msg = (
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  npm install -g @anthropic-ai/claude-code\n\n"
                "If already installed, try one of these:\n"
                "  1. Add Claude to your PATH\n"
                "  2. Create a symlink: ln -s $(which claude) /usr/local/bin/claude\n"
                "  3. Set CLAUDE_CLI_PATH environment variable"
            )
            raise ClaudeProcessError(error_msg)

        except ProcessError as e:
            error_str = str(e)
            # Include captured stderr for better diagnostics
            captured_stderr = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
            if captured_stderr:
                error_str = f"{error_str}\nStderr: {captured_stderr}"
            logger.error(
                "Claude process failed",
                error=error_str,
                exit_code=getattr(e, "exit_code", None),
                stderr=captured_stderr or None,
            )
            # Check if the process error is MCP-related
            if "mcp" in error_str.lower():
                raise ClaudeMCPError(f"MCP server error: {error_str}")
            raise ClaudeProcessError(f"Claude process error: {error_str}")

        except CLIConnectionError as e:
            error_str = str(e)
            logger.error("Claude connection error", error=error_str)
            # Check if the connection error is MCP-related
            if "mcp" in error_str.lower() or "server" in error_str.lower():
                raise ClaudeMCPError(f"MCP server connection failed: {error_str}")
            raise ClaudeProcessError(f"Failed to connect to Claude: {error_str}")

        except CLIJSONDecodeError as e:
            logger.error("Claude SDK JSON decode error", error=str(e))
            raise ClaudeParsingError(f"Failed to decode Claude response: {str(e)}")

        except ClaudeSDKError as e:
            logger.error("Claude SDK error", error=str(e))
            raise ClaudeProcessError(f"Claude SDK error: {str(e)}")

        except Exception as e:
            # The SDK never re-raises the reader task's typed exception: its
            # message reader catches everything, logs "Fatal error in message
            # reader" and pushes {"type": "error", ...} onto the stream, which
            # surfaces here as a bare Exception carrying only the text. So a
            # CLIJSONDecodeError (e.g. an oversized stream-json message) can
            # never reach the handler above -- match on the text instead.
            if _is_json_decode_text(str(e)):
                logger.error("Claude SDK JSON decode error", error=str(e))
                raise ClaudeParsingError(f"Failed to decode Claude response: {str(e)}")

            exceptions = getattr(e, "exceptions", None)
            if exceptions is not None:
                # ExceptionGroup from TaskGroup operations (Python 3.11+)
                logger.error(
                    "Task group error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                    exception_count=len(exceptions),
                    exceptions=[str(ex) for ex in exceptions[:3]],
                )
                raise ClaudeProcessError(
                    f"Claude SDK task error: {exceptions[0] if exceptions else e}"
                )

            logger.error(
                "Unexpected error in Claude SDK",
                error=str(e),
                error_type=type(e).__name__,
            )
            raise ClaudeProcessError(f"Unexpected error: {str(e)}")

    async def _handle_stream_message(
        self, message: Message, stream_callback: Callable[[StreamUpdate], Any]
    ) -> None:
        """Handle streaming message from claude-agent-sdk."""
        try:
            if isinstance(message, AssistantMessage):
                # Extract content from assistant message
                content = getattr(message, "content", [])
                text_parts: List[str] = []
                thinking_parts: List[str] = []
                tool_calls = []

                if content and isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolUseBlock):
                            tool_calls.append(
                                {
                                    "name": getattr(block, "name", "unknown"),
                                    "input": getattr(block, "input", {}),
                                    "id": getattr(block, "id", None),
                                }
                            )
                        elif isinstance(block, ThinkingBlock) or (
                            not hasattr(block, "text") and hasattr(block, "thinking")
                        ):
                            # Redacted/encrypted thinking arrives as
                            # ThinkingBlock(thinking='', signature='...') --
                            # it carries no readable text, so drop it.
                            thought = str(getattr(block, "thinking", "") or "").strip()
                            if thought:
                                thinking_parts.append(thought)
                        elif hasattr(block, "text"):
                            text_parts.append(block.text)

                if thinking_parts:
                    await stream_callback(
                        StreamUpdate(
                            type="thinking",
                            content="\n".join(thinking_parts),
                        )
                    )

                if text_parts or tool_calls:
                    update = StreamUpdate(
                        type="assistant",
                        content=("\n".join(text_parts) if text_parts else None),
                        tool_calls=tool_calls if tool_calls else None,
                    )
                    await stream_callback(update)
                elif content and not isinstance(content, list):
                    # Fallback for non-list content (a bare string payload).
                    # Never str() a block list here: unknown block types would
                    # reach the user as a dataclass repr (dv-4b71).
                    update = StreamUpdate(
                        type="assistant",
                        content=str(content),
                    )
                    await stream_callback(update)

            elif isinstance(message, StreamEvent):
                event = message.event or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            update = StreamUpdate(
                                type="stream_delta",
                                content=text,
                            )
                            await stream_callback(update)

            elif isinstance(message, UserMessage):
                # UserMessage.content is ``str | list[ContentBlock]``; only the
                # plain-string form is renderable. Block lists here are tool
                # results, and str()-ing them would leak dataclass reprs.
                user_content = getattr(message, "content", "")
                if user_content and isinstance(user_content, str):
                    update = StreamUpdate(
                        type="user",
                        content=user_content,
                    )
                    await stream_callback(update)

        except Exception as e:
            logger.warning("Stream callback failed", error=str(e))

    def _inject_image_roots(self, servers: Any, working_directory: Path) -> None:
        """Set TELEGRAM_IMAGE_ROOTS on the telegram MCP server config.

        The roots are the profile's working dir plus the session scratch dir
        (sandboxed profiles write throwaway charts to /tmp, which is legitimate
        and must stay deliverable).
        """
        import os as _os

        from src.bot.utils.image_extractor import (
            IMAGE_ROOTS_ENV,
            allowed_image_roots,
        )

        if not isinstance(servers, dict):
            return
        cfg = servers.get("telegram")
        if not isinstance(cfg, dict):
            return
        roots = allowed_image_roots(self.config.approved_directory or working_directory)
        env = cfg.setdefault("env", {})
        if isinstance(env, dict):
            env[IMAGE_ROOTS_ENV] = _os.pathsep.join(str(r) for r in roots)

    def _load_mcp_config(self, config_path: Path) -> Dict[str, Any]:
        """Load MCP server configuration from a JSON file.

        The new claude-agent-sdk expects mcp_servers as a dict, not a file path.
        """
        import json

        try:
            with open(config_path) as f:
                config_data = json.load(f)
            return config_data.get("mcpServers", {})
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "Failed to load MCP config", path=str(config_path), error=str(e)
            )
            return {}
