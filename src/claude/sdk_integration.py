"""Claude Code Python SDK integration."""

import asyncio
import json
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

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
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import McpHttpServerConfig, StreamEvent

from ..config.settings import Settings
from ..model_router import quota
from ..model_router.harness import CLAUDE_ACCOUNT_ENV
from ..model_router.routes import (
    PROVIDER_ANTHROPIC_SUB,
    PROVIDER_OPENROUTER,
    cost_for,
    cost_from_tokens,
    openrouter_key,
    provider_env,
    resolve_turn_route,
)
from ..model_router.trace import current_trace
from ..model_router.turn_cost import (
    GenerationLookup,
    SessionCostTracker,
    Settlement,
    TurnCostMeter,
)
from ..security import policy
from ..security.ask_explain import AskContext
from ..security.bash_box import BashBox, BoxUnavailable
from ..security.read_scope import ReadScope, build_read_scope
from ..security.validators import SecurityValidator
from ..utils.paths import bot_claude_md_excludes
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
    from ..profiles.registry import ProfileDefinition, ProfileRegistry
    from ..storage.memory_repository import MemoryRetriever
    from .agent_tools import AgentServices
    from .confirmation import ConfirmationManager
    from .window_bundle import WindowBundleProvider

from .process_tree import cli_pid, terminate_tree
from .prompt_composer import PromptComposer
from .tool_context import TurnContext
from .turn_activity import TurnActivity
from .turn_activity import set_current as set_current_activity
from .turn_setup import (
    compose_persona,
    composer_for,
    image_roots_env,
    resolve_turn_context,
)
from .verification_budget import VerificationBudget
from .verification_budget import current_phase as current_verification_phase
from .verification_budget import make_budget

logger = structlog.get_logger()

# CLI 2.x deserializer injects this synthetic assistant placeholder after
# auto-injecting "Continue from where you left off." on session resume when
# the prior turn ended mid-tool-use. The placeholder has model="<synthetic>"
# and 0 tokens — the model never produces it.
_SYNTHETIC_PLACEHOLDER_TEXTS = frozenset(
    {"No response requested.", "No response requested"}
)


async def _await_with_idle_timeout(
    task: "asyncio.Future[Any]",
    last_activity: Callable[[], float],
    idle_limit: float,
) -> None:
    """Await ``task``, timing out only after ``idle_limit`` seconds of silence.

    ``last_activity()`` returns a ``time.monotonic()`` stamp updated by the
    task every time the CLI sends something. The deadline is recomputed from
    that stamp, so a turn that streams for an hour is fine while one that
    stalls for ``idle_limit`` is killed — wall-clock length is not evidence of
    a hang (see the call site for the incident this replaced).

    Raises ``asyncio.TimeoutError`` on stall; the task is always cancelled and
    awaited first so its cleanup runs. Any exception from the task propagates.
    """
    try:
        while True:
            idle_for = time.monotonic() - last_activity()
            if idle_for >= idle_limit:
                raise asyncio.TimeoutError
            done, _pending = await asyncio.wait({task}, timeout=idle_limit - idle_for)
            if done:
                break
    except BaseException:
        task.cancel()
        with suppress(BaseException):
            await task
        raise
    task.result()


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
    """Map a cwd to its `~/.claude/projects/<sanitized>/` directory.

    The CLI replaces EVERY non-alphanumeric character with ``-``, not just
    ``/``: ``/home/x/Devvybot/devvy_bot`` lives under
    ``-home-x-Devvybot-devvy-bot``. Replacing only slashes made this helper
    return a directory that never exists for any cwd containing ``_`` or
    ``.``, so ``transcript.exists()`` was always False and every process
    error on such a profile was classified as ``transcript_missing`` and
    deleted the session mapping (2026-09-17 13:59, devops).
    """
    sanitized = re.sub(r"[^A-Za-z0-9]", "-", str(working_directory))
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


# Bot-side bounds on a turn's tail (dv-586f). The idle watchdog only sees
# silence; these catch a CLI that keeps talking (or acks an interrupt) without
# ever finishing the turn.
# After /stop interrupts a run, wait this long for the CLI's ResultMessage,
# then abandon the run on the bot side (disconnect kills the CLI).
_INTERRUPT_GRACE_SECONDS = 15.0
# After the model's final answer (a top-level text message with no tool use),
# the ResultMessage follows within milliseconds. If it has not arrived after
# this long, take the answer we have instead of waiting out the CLI.
_FINAL_RESULT_GRACE_SECONDS = 60.0
# Partial-stream events that mean the model is generating something new.
_NEW_GENERATION_EVENTS = frozenset(
    {"message_start", "content_block_start", "content_block_delta"}
)
# One stream callback (a Telegram edit/send) may not stall consumption of the
# CLI stream; past this it is abandoned for that message.
_STREAM_CALLBACK_TIMEOUT_SECONDS = 20.0
# How often the receive loop wakes to check the deadlines above.
_POLL_SECONDS = 1.0


def _is_final_answer(blocks: List[Any], stop_reason: Optional[str]) -> bool:
    """True when a top-level assistant message ends the turn (dv-586f).

    ``stop_reason`` is None on streamed assistant messages, so text with no
    tool call is the signal; ``end_turn`` is honoured when present.
    """
    if any(isinstance(b, ToolUseBlock) for b in blocks):
        return False
    if stop_reason == "end_turn":
        return True
    return any(isinstance(b, TextBlock) and b.text.strip() for b in blocks)


def _trailing_answer(messages: List[Any]) -> List[Any]:
    """The final answer's messages: the top-level text-only assistant
    messages after the last tool call or tool result (dv-586f)."""
    out: List[Any] = []
    for msg in reversed(messages):
        if isinstance(msg, AssistantMessage) and not msg.parent_tool_use_id:
            blocks = msg.content or []
            if any(isinstance(b, ToolUseBlock) for b in blocks):
                break
            out.append(msg)
        elif isinstance(msg, UserMessage) and not getattr(
            msg, "parent_tool_use_id", None
        ):
            break
    out.reverse()
    return out


def _sum_assistant_usage(messages: List[Any]) -> Tuple[int, int, int, int]:
    """Token totals (in, out, cache read, cache write) from assistant
    messages, one usage per API message id (the last wins)."""
    per_id: Dict[str, Dict[str, Any]] = {}
    for i, msg in enumerate(messages):
        if isinstance(msg, AssistantMessage) and msg.usage:
            per_id[msg.message_id or f"#{i}"] = msg.usage
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    a, b, c, d = (sum(u.get(k, 0) or 0 for u in per_id.values()) for k in keys)
    return a, b, c, d


@dataclass(eq=False)
class _ActiveRun:
    """One in-flight SDK client, addressable by (user, agent) for /stop."""

    client: Any
    user_id: int
    profile: str
    interrupted_at: Optional[float] = None
    # Set once the CLI process tree was ended by force (dv-b399): the broken
    # stream that follows is the stop, not a failure.
    hard_stopped: bool = False


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


_TOOL_ERROR_MAX_CHARS = 1000


def _tool_result_error_text(content: Any) -> str:
    """Render a failed ToolResultBlock's content as a short error string.

    ``content`` is a str, a list of content-block dicts (usually
    ``{"type": "text", "text": ...}``), or None. Truncated so one runaway
    tool error (e.g. a dumped stack trace) doesn't bloat tool_calls rows.
    """
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        text = "\n".join(parts) if parts else str(content)
    else:
        text = str(content)
    text = text.strip()
    if len(text) > _TOOL_ERROR_MAX_CHARS:
        text = text[:_TOOL_ERROR_MAX_CHARS] + "...[truncated]"
    return text or "tool call failed"


def _tool_result_errors(messages: List[Any]) -> Dict[str, str]:
    """Map ``tool_use_id`` -> error text for every failed ToolResultBlock.

    Tool results (including denials from the PreToolUse hook / can_use_tool,
    which the CLI surfaces as an ``is_error`` result on the denied call, not
    as a separate event) arrive as ``ToolResultBlock``s inside a
    ``UserMessage.content`` list. ``ToolUseBlock``s carry no outcome of their
    own (dv-73e2), so this is paired against them by ``tool_use_id``.
    """
    errors: Dict[str, str] = {}
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, ToolResultBlock) and block.is_error:
                errors[block.tool_use_id] = _tool_result_error_text(block.content)
    return errors


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

    content: str
    session_id: str
    # USD spent by this turn as the backend reports it. Three states, and the
    # difference matters to the budget (dv-0722):
    #   float > 0  -- priced (Claude's total_cost_usd; an estimate on OAuth)
    #   0.0        -- priced and free (nothing was spent)
    #   None       -- UNPRICED: the backend has no USD figure for this turn
    #                 (Codex on a ChatGPT subscription). The limiter and the
    #                 watchdog then budget input_tokens + output_tokens via
    #                 RateLimiter.record_tokens instead of pretending $0.
    cost: Optional[float]
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
    # Human-readable reason when THIS turn opened a fresh backend session
    # ("cold start, 10 turns seeded", "resume failed (transcript_missing),
    # 5 turns seeded"). None on a plain resumed turn. Rendered in the footer
    # so a context reset is distinguishable from a healthy window roll
    # (dv-2a5a): before this, both read as the same drop in the level gauge.
    session_fresh_reason: Optional[str] = None
    # Set only when the turn ran on a provider other than the subscription
    # (dv-d2f6): the router records them instead of the harness defaults.
    provider: Optional[str] = None
    model: Optional[str] = None
    # Set by the router (dv-2260): this turn's harness trace and its
    # ``model_calls`` row; the storage facade ties the trace's tool_calls
    # rows to the saved message by ``trace_id``.
    trace_id: Optional[str] = None
    model_call_id: Optional[int] = None
    # Third-party routes only (dv-d2f6): "estimate" while any call is priced
    # from tokens, "exact" once every call has OpenRouter's total_cost.
    # ``cost_settlement`` resolves to the final figure (~6 s after the last
    # call); the cost sinks swap their estimate via turn_cost.when_settled.
    cost_source: Optional[str] = None
    cost_settlement: Optional["asyncio.Task[Settlement]"] = field(
        default=None, repr=False, compare=False
    )
    # Model calls seen in the stream, and how many lacked the stream-json
    # ``request_id`` (the live smoke fails on any: the field is undocumented).
    api_calls: int = 0
    request_ids_missing: int = 0

    @property
    def cost_usd(self) -> float:
        """Dollars to write into the USD ledgers: ``cost`` with None as 0.0.

        For the SQLite cost rows and session/user totals, which are "money
        spent" and have no way to say "unknown". Budget enforcement must NOT
        use this -- it hides the unpriced case; use ``cost`` and branch on
        None (see turns.py, the record_cost/record_tokens split).
        """
        return self.cost or 0.0

    @property
    def context_tokens(self) -> int:
        """The conversation's real size as of this turn, or 0 if unknown.

        `peak_context_tokens` is the only level. 0 means "unknown"; callers
        key off the falsy value and leave the window's last known level
        alone. Returning any of the flow fields instead is what made rotation
        fire on every turn -- twice: the Claude ResultMessage sum (dv-9f31,
        a 20-round-trip turn at ~55k context sums past 1M) and then Codex's
        `turn.completed.usage.input_tokens`, which is the thread's running
        total and tripped the 150k roll on the first tool-using turn of every
        fresh window (2026-09-05, docs/FINDINGS.md). The old "Codex has no
        cache, so input_tokens is the context" fallback was wrong on both
        counts; Codex now reads its level from the rollout file.
        """
        return self.peak_context_tokens


@dataclass
class StreamUpdate:
    """Streaming update from Claude SDK."""

    # 'assistant', 'thinking', 'user', 'system', 'result', 'stream_delta'
    type: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    metadata: Optional[Dict] = None


# The rules moved to src/security/policy.py (dv-1601); these names stay for
# can_use_tool below and for existing imports.
_is_tenant_data = policy.is_tenant_data
_is_secret_path = policy.is_secret_path
_glob_literal_prefix = policy.glob_literal_prefix
_SEARCH_TOOLS = policy.SEARCH_TOOLS


def _make_pre_tool_use_hook(
    working_directory: Path,
    full_access: bool = False,
    read_scope: Optional[ReadScope] = None,
    unrestricted: bool = False,
    confirmation_manager: Optional["ConfirmationManager"] = None,
    verification_budget: Optional[VerificationBudget] = None,
    bash_box: Optional[BashBox] = None,
    ask_context: Optional[AskContext] = None,
) -> Any:
    """Deny secrets / multi-tenant stores for EVERY tool call.

    Also hosts the destructive-op confirmation gate (dv-6f50). It used to live
    in ``can_use_tool``, which the CLI skips for any command matching a
    settings allow rule: on 2026-09-29 an allow-listed ``node -e 0 perp.js``
    produced no gate event at all, so a bare ``node perp.js ...`` would have
    placed a real order unapproved. Hooks run before allow rules.

    ``can_use_tool`` is not enough on its own: the CLI consults it only for
    paths *outside* the session cwd. Verified 2026-08-04 -- with cwd set to this
    repo, `Read /home/.../devvy_bot/.env` was auto-approved and the callback was
    never invoked, while the same read from a workspace cwd was denied. That is
    exactly the devops case, where cwd IS the repo holding `.env` and `bot.db`.
    The SDK says as much in its CanUseToolShadowedWarning: "To gate every tool
    call, use a PreToolUse hook."

    So this hook carries the denylist concerns (which must hold everywhere) and
    ``can_use_tool`` keeps the boundary/confirmation logic. (dv-6dbc)

    The rules themselves live in ``src/security/policy.py`` (dv-1601): the
    Codex PreToolUse hook reaches the same ``decide()`` over the agent bridge.

    ``unrestricted`` (profile flag, dv-e38e): skip the data-root areas
    (secrets/, archive/, db/); the credential-filename rules still apply.

    ``verification_budget`` (dv-9f41) is this turn's search-class counter; it
    lives here for the same reason — WebSearch is in ``allowed_tools`` and so
    never reaches ``can_use_tool``. Built fresh per turn by the caller.

    ``bash_box`` (dv-98f2): a sandboxed profile's Bash commands are rewritten
    here, via ``updatedInput``, to run inside bwrap (``src/security/bash_box.py``).
    The CLI's own sandbox cannot start on this host, so this is the OS boundary.

    ``ask_context`` (dv-781f) goes with every ask to the explainer: a chat turn
    passes its profile.
    """
    turn = policy.TurnPolicy(
        working_directory=working_directory,
        full_access=full_access,
        unrestricted=unrestricted,
        read_scope=read_scope,
    )

    def deny(tool_name: str, reason: str) -> Dict[str, Any]:
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

    async def pre_tool_use(
        input_data: Dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> Dict[str, Any]:
        tool_name = input_data.get("tool_name") or ""
        tool_input = input_data.get("tool_input") or {}
        decision = policy.decide(tool_name, tool_input, turn)
        if decision.action == policy.DENY:
            return deny(tool_name, decision.reason)
        # Confirmation gate. No manager to ask -> deny, the same fail-closed
        # answer the Codex route gives (src/agent_bridge/policy_gate.py).
        if decision.action == policy.ASK and confirmation_manager is None:
            return deny(
                tool_name,
                f"{decision.reason}: needs the owner's confirmation, "
                "which is not available",
            )
        if decision.action == policy.ASK and confirmation_manager is not None:
            approved = await confirmation_manager.request(
                decision.reason, decision.preview, context=ask_context
            )
            if not approved:
                logger.warning(
                    "Destructive op denied by confirmation gate",
                    tool_name=tool_name,
                    reason=decision.reason,
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"Denied by owner (confirmation gate): {decision.reason}"
                        ),
                    }
                }
        if verification_budget is not None:
            over_reason = verification_budget.charge(tool_name)
            if over_reason:
                return deny(tool_name, over_reason)
            if verification_budget.over and not verification_budget.exceeded_logged:
                # Once per turn: the count-only telemetry line (dv-9f41).
                verification_budget.exceeded_logged = True
                logger.warning(
                    "verification_budget_exceeded",
                    phase=verification_budget.phase,
                    limit=verification_budget.limit,
                    used=verification_budget.used,
                    tool_name=tool_name,
                    cwd=str(working_directory),
                )
        if bash_box is not None and tool_name == "Bash":
            command = tool_input.get("command")
            if isinstance(command, str) and command:
                try:
                    boxed = bash_box.wrap(command, input_data.get("cwd"))
                except BoxUnavailable as exc:
                    # Fail closed: no box, no bash.
                    return deny(tool_name, f"Bash is unavailable: {exc}")
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "updatedInput": {**tool_input, "command": boxed},
                    }
                }
        return {}

    return pre_tool_use


def _make_can_use_tool_callback(
    security_validator: SecurityValidator,
    working_directory: Path,
    approved_directory: Optional[Path],
    full_access: bool = False,
    confirmation_manager: Optional["ConfirmationManager"] = None,
    read_scope: Optional[ReadScope] = None,
    extra_write_roots: Sequence[Path] = (),
    unrestricted: bool = False,
) -> Any:
    """Create a can_use_tool callback for SDK-level tool permission validation.

    The callback validates file path boundaries and bash directory boundaries
    *before* the SDK executes the tool, providing preventive security enforcement.

    On **full-access** profiles (sandbox off) it additionally routes destructive
    ops through ``confirmation_manager`` for an owner approve/deny prompt.

    ``extra_write_roots`` are the profile's ``extra_dirs`` (dv-3103): the same
    paths handed to the CLI as ``--add-dir``, so the in-process write boundary
    and the bwrap jail agree on where a sandboxed profile may write.
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

        roots = [
            working_directory.resolve(),
            *session_scratch_roots(),
            *extra_write_roots,
        ]
        for root in roots:
            try:
                resolved.relative_to(root)
                return True, None
            except ValueError:
                continue

        return False, (
            f"Access denied: writing to '{path}' is outside this agent's "
            f"workspace ({working_directory}). Reads outside are fine; writes "
            f"must stay in the workspace, the session scratch dir"
            + (
                f", or one of: {', '.join(str(r) for r in extra_write_roots)}"
                if extra_write_roots
                else ""
            )
            + "."
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
        # A sandboxed bash reaching a host outside sandbox.network.allowedDomains
        # is not refused by the CLI: it asks, as a "SandboxNetworkAccess" tool
        # call. The default-allow at the bottom of this callback let every such
        # call through, so the allowlist was decoration (dv-98f2).
        if tool_name == "SandboxNetworkAccess":
            logger.warning(
                "can_use_tool denied sandbox network access",
                host=tool_input.get("host"),
                port=tool_input.get("port"),
            )
            return PermissionResultDeny(
                message=(
                    "Network access to this host is not allowed for this "
                    "agent. The owner can add it to the profile's "
                    "sandbox_allowed_domains."
                )
            )
        # The destructive-op confirmation gate moved to the PreToolUse hook
        # (dv-6f50): this callback is skipped for allow-listed commands.

        # File path validation
        if tool_name in _FILE_TOOLS:
            file_path = tool_input.get("file_path") or tool_input.get("path")
            if file_path:
                # Allow Claude Code internal paths (~/.claude/plans/, etc.)
                if _is_claude_internal_path(file_path):
                    return PermissionResultAllow()

                valid, _resolved, error = security_validator.validate_path(
                    file_path, working_directory, unrestricted=unrestricted
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
                    unrestricted=unrestricted,
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
                        command,
                        working_directory,
                        [*session_scratch_roots(), *extra_write_roots],
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
        self._active_runs: Dict[Tuple[int, str], List[_ActiveRun]] = {}
        # Hard-stop watchdogs (one per interrupted run), kept alive here.
        self._stop_watchdogs: set["asyncio.Future[None]"] = set()
        # dv-4517: the CLI's total_cost_usd is session-cumulative on resume.
        self._session_costs = SessionCostTracker()
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

    def _idle_limit_seconds(self) -> int:
        """Turn idle limit; with a confirmation gate it outlives the owner wait."""
        if self._confirmation_manager is not None:
            return int(self.config.claude_idle_limit_seconds)
        return int(self.config.claude_timeout_seconds)

    def attach_confirmation_manager(self, manager: "ConfirmationManager") -> None:
        """Wire the destructive-op confirmation gate (post-startup, needs bot)."""
        self._confirmation_manager = manager
        logger.info("Confirmation manager attached")

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def _profile_slug_for(self, working_directory: Path, user_id: Optional[int]) -> str:
        """Bare profile slug (no ``#u<id>`` suffix) a run belongs to: the key
        half of ``_active_runs``, matched by ``stop_turns``."""
        return self._agent_id_for(working_directory, user_id).split("#", 1)[0]

    async def _hard_stop_run(self, run: _ActiveRun, why: str) -> None:
        """End the run's CLI AND everything it spawned (Task sub-agents live
        inside the CLI; child shells are its descendants). Detached bgjobs are
        systemd units, not descendants, and keep running. Idempotent."""
        if run.hard_stopped:
            return
        run.hard_stopped = True
        pid = cli_pid(run.client)
        stopped = await terminate_tree(pid) if pid else 0
        logger.warning(
            "Turn hard-stopped",
            why=why,
            user_id=run.user_id,
            profile=run.profile,
            cli_pid=pid,
            processes=stopped,
        )

    async def _hard_stop_if_running(self, run: _ActiveRun) -> None:
        """Watchdog: an interrupt the CLI acknowledged but did not act on
        within the grace is escalated to a hard stop."""
        await asyncio.sleep(_INTERRUPT_GRACE_SECONDS)
        if run in self._active_runs.get((run.user_id, run.profile), ()):
            await self._hard_stop_run(run, "still running after interrupt")

    async def _interrupt_one(self, run: _ActiveRun) -> bool:
        """Interrupt one run; escalate to a hard stop if the CLI does not
        answer within the grace (it is wedged in a tool: dv-b399 2026-10-20,
        'Control request timeout: interrupt' after 60 s)."""
        run.interrupted_at = time.monotonic()
        try:
            await asyncio.wait_for(run.client.interrupt(), _INTERRUPT_GRACE_SECONDS)
        except Exception as exc:  # incl. TimeoutError: the CLI is not answering
            logger.warning(
                "interrupt failed during stop",
                error=str(exc) or type(exc).__name__,
            )
            await self._hard_stop_run(run, "interrupt failed")
            return True  # the turn is ended either way
        task = asyncio.ensure_future(self._hard_stop_if_running(run))
        self._stop_watchdogs.add(task)
        task.add_done_callback(self._stop_watchdogs.discard)
        return True

    async def _interrupt_runs(self, runs: List[_ActiveRun]) -> int:
        results = await asyncio.gather(*(self._interrupt_one(r) for r in runs))
        return sum(1 for ok in results if ok)

    async def stop_turns(self, user_id: int, profile: str) -> int:
        """Scoped /stop: interrupt only ``user_id``'s in-flight run(s) on
        ``profile``. No killswitch, scheduler untouched. Returns the number of
        runs interrupted."""
        runs = list(self._active_runs.get((user_id, profile), ()))
        interrupted = await self._interrupt_runs(runs)
        logger.info(
            "Turn stopped", user_id=user_id, profile=profile, interrupted=interrupted
        )
        return interrupted

    async def stop_all(self) -> int:
        """Killswitch (/stop all): refuse new runs, interrupt every in-flight
        SDK client, and pause the scheduler. Returns the number of runs
        interrupted."""
        self._stopped = True
        runs = [r for group in self._active_runs.values() for r in group]
        interrupted = await self._interrupt_runs(runs)
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
        :meth:`_turn_context_for` resolution prompt composition uses, so the
        tools operate exactly where this turn reads. The resolved strings are
        baked into the tools' closures; the model never supplies a scope
        (dv-6dbc). ``user_id`` is also passed through raw for
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

        ctx = self._turn_context_for(
            working_directory,
            user_id=user_id,
            requester_chat_id=requester_chat_id,
            family_id=family_id,
            role=role,
        )
        server = build_agent_mcp_server(
            self._agent_services,
            ctx.profile_slug,
            working_directory,
            requester_chat_id=ctx.requester_chat_id,
            memory_agent_id=ctx.memory_agent_id,
            memory_family_agent_id=ctx.family_agent_id,
            user_id=ctx.user_id,
            role=ctx.role,
        )
        return {"agent": server}

    def _turn_context_for(
        self,
        working_directory: Path,
        *,
        user_id: Optional[int] = None,
        requester_chat_id: Optional[int] = None,
        family_id: Optional[int] = None,
        role: Optional[str] = None,
    ) -> TurnContext:
        """This turn's identity — the same resolution the Codex backend makes.

        Shared with ``CodexCliIntegration._turn_context`` through
        :func:`turn_setup.resolve_turn_context` (dv-a342), so a tool reached
        over either transport operates on precisely the scope this turn
        reads from.
        """
        return resolve_turn_context(
            self.profile_registry,
            working_directory,
            user_id=user_id,
            requester_chat_id=requester_chat_id,
            family_id=family_id,
            role=role,
        )

    async def _persona_text(
        self,
        working_directory: Path,
        prompt: str,
        *,
        user_id: Optional[int] = None,
        family_id: Optional[int] = None,
        full_access: bool = False,
    ) -> Optional[str]:
        """The composed persona for this turn, or None without a composer.

        Frozen bundle first, live compose second (see
        :func:`turn_setup.compose_persona`, shared with the Codex backend).
        The SDK path passes the result as ``system_prompt``.
        """
        return await compose_persona(
            self._turn_context_for(
                working_directory, user_id=user_id, family_id=family_id
            ),
            prompt=prompt,
            composer=self._composer_for(working_directory),
            memory_retriever=self.memory_retriever,
            bundle_provider=self.bundle_provider,
            family_id=family_id,
            full_access=full_access,
        )

    def _profile_for(self, working_directory: Path) -> Optional["ProfileDefinition"]:
        """The profile owning ``working_directory`` (guest workspaces included)."""
        if self.profile_registry is None:
            return None
        return self.profile_registry.get_by_working_dir(working_directory)

    def _extra_dirs_for(self, working_directory: Path) -> Tuple[Path, ...]:
        """The profile's extra write roots (dv-3103); empty outside profiles.

        Resolved via the owning profile so a per-user workspace
        (``<root>/users/<uid>``) inherits the profile's grant.
        """
        if self.profile_registry is None:
            return ()
        profile = self.profile_registry.get_by_working_dir(working_directory)
        return profile.extra_dirs if profile is not None else ()

    def _bash_box_for(self, working_directory: Path) -> BashBox:
        """The bash box of a sandboxed profile (dv-98f2): workspace +
        ``extra_dirs`` writable, ``sandbox_read_dirs`` read-only, ``sandbox_keys``
        injected (dv-0277)."""
        profile = (
            self.profile_registry.get_by_working_dir(working_directory)
            if self.profile_registry
            else None
        )
        return BashBox.for_profile(profile, Path(working_directory))

    def _unrestricted_for(self, working_directory: Path) -> bool:
        """The profile's ``unrestricted`` flag (dv-e38e); False outside profiles."""
        if self.profile_registry is None:
            return False
        profile = self.profile_registry.get_by_working_dir(working_directory)
        return bool(profile is not None and profile.unrestricted)

    def _verification_budget_for(
        self, working_directory: Path
    ) -> Optional[VerificationBudget]:
        """A fresh per-turn search budget, or None if the profile at this
        working dir is not in ``VERIFICATION_BUDGET_PROFILES`` (dv-9f41).

        The phase comes from the orchestrator's ContextVar: follow-up confirm /
        escalate turns set it around their ``run_command``.
        """
        cfg = self.config
        if self.profile_registry is None:
            return None
        profile = self.profile_registry.get_by_working_dir(working_directory)
        if profile is None:
            return None
        allowed = {
            slug.strip()
            for slug in cfg.verification_budget_profiles.split(",")
            if slug.strip()
        }
        if profile.slug not in allowed:
            return None
        return make_budget(
            current_verification_phase(),
            draft_max=cfg.verification_budget_draft_max,
            confirm_max=cfg.verification_budget_confirm_max,
            enforce=cfg.verification_budget_enforce,
        )

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

    def _generation_lookup(self, key: str) -> GenerationLookup:
        """OpenRouter generation-cost client for one turn (tests swap it)."""
        return GenerationLookup(key)

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
        auth_env = str(spec.get("auth_env") or "")
        if self._seam_token(auth_env):
            # By reference, not value (dv-2976): the SDK passes mcp_servers to
            # the CLI as `--mcp-config <json>` argv, which `ps` shows to every
            # user. Claude CLI expands ${VAR} in MCP headers from its own env
            # (verified on 2.1.283), and _seam_env puts the token there.
            headers["Authorization"] = f"Bearer ${{{auth_env}}}"
        cfg: McpHttpServerConfig = {"type": "http", "url": str(spec["url"])}
        if headers:
            cfg["headers"] = headers
        return {"channel-intel": cfg}

    def _seam_token(self, auth_env: str) -> Optional[str]:
        """The bearer an MCP seam names by env var: Settings first (secrets
        are scrubbed from os.environ at startup), then the env (dev runs)."""
        from src.utils.agent_env import is_env_name

        if not is_env_name(auth_env):
            return None
        return self.config.secret_value(auth_env) or os.environ.get(auth_env)

    def _seam_env(self, working_directory: Path) -> Dict[str, str]:
        """``{auth_env: token}`` for this profile's MCP seam, or {}.

        Goes into ``ClaudeAgentOptions.env``: only this turn's CLI gets it.
        """
        if not self.profile_registry:
            return {}
        profile = self.profile_registry.get_by_working_dir(working_directory)
        if not profile or not profile.mcp:
            return {}
        try:
            spec = json.loads(Path(profile.mcp).read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(spec, dict):
            return {}
        auth_env = str(spec.get("auth_env") or "")
        token = self._seam_token(auth_env)
        return {auth_env: token} if token else {}

    def _route_mcp_env_secrets(self, servers: Any) -> Dict[str, str]:
        """Move secret-looking stdio MCP ``env`` values out of argv (dv-2976).

        Each such entry is replaced by ``${NAME}`` (the CLI expands it from
        its own env, verified on 2.1.283) and returned for
        ``ClaudeAgentOptions.env``. Today only TELEGRAM_IMAGE_ROOTS (paths)
        is set, so this is a guard for the next server someone adds.
        """
        from src.utils.agent_env import split_mcp_env

        routed_all: Dict[str, str] = {}
        if not isinstance(servers, dict):
            return routed_all
        secrets = self.config.secret_values()
        for name, cfg in servers.items():
            env = cfg.get("env") if isinstance(cfg, dict) else None
            if not isinstance(env, dict) or not env:
                continue
            _, routed = split_mcp_env(env, secrets)
            for var, value in routed.items():
                env[var] = f"${{{var}}}"
                routed_all[var] = value
            if routed:
                logger.warning(
                    "MCP env secret routed via environment, not argv",
                    server=name,
                    names=sorted(routed),
                )
        return routed_all

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
            unrestricted = self._unrestricted_for(Path(working_directory))
            extra_dirs = self._extra_dirs_for(Path(working_directory))

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
            # Frozen per-window bundle (dv-5e2a) first, live compose as the
            # fallback for callers with no user_id (no window to key) or
            # deployments without the provider — the same shared path the
            # Codex backend takes (turn_setup.compose_persona, dv-a342).
            persona = await self._persona_text(
                Path(working_directory),
                prompt,
                user_id=user_id,
                family_id=family_id,
                full_access=full_access,
            )
            if persona is not None:
                base_prompt = persona
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
            # uncapped for owners. Enforced by OUR stream loop on every route
            # (dv-d2f6): the meter charges each model call as it arrives and
            # the loop interrupts the turn at the cap. The CLI's own
            # max_budget_usd stays off: it prices every model by its Anthropic
            # table, ~15x wrong on OpenRouter.
            max_budget_usd = self._max_budget_for(Path(working_directory), role)

            # Provider route (dv-d2f6): subscription unless the profile opts in.
            route = resolve_turn_route(self._profile_for(working_directory), model)
            if route.provider != PROVIDER_ANTHROPIC_SUB:
                model = route.model
            or_key = openrouter_key() if route.provider == PROVIDER_OPENROUTER else None
            partial_for_meter_only = stream_callback is None and route.is_third_party
            meter = TurnCostMeter(
                route,
                cap=max_budget_usd,
                lookup=self._generation_lookup(or_key) if or_key else None,
            )

            # Build Claude Agent options
            options = ClaudeAgentOptions(
                max_turns=self._max_turns_for(working_directory),
                model=model or self.config.claude_model or None,
                max_budget_usd=None,
                cwd=str(working_directory),
                # Profile extra_dirs (dv-3103): `--add-dir` is what makes a path
                # writable for bash *inside* the bwrap jail (verified live,
                # docs/agent-capabilities-sandbox-plan.md). Full-access
                # profiles already see everything, so it is a no-op there.
                add_dirs=[str(d) for d in extra_dirs],
                allowed_tools=sdk_allowed_tools,
                disallowed_tools=sdk_disallowed_tools,
                cli_path=self.config.claude_cli_path or None,
                permission_mode="default",
                # Third-party routes always: the per-call usage and the
                # provider's charge arrive only in the partial-stream
                # message_delta (dv-d2f6); the assistant events carry zeros.
                include_partial_messages=(
                    stream_callback is not None or partial_for_meter_only
                ),
                # The CLI's native sandbox cannot start on this host (AppArmor
                # strips its nested seccomp step), so it is off for everyone;
                # sandboxed profiles get the bwrap box via the PreToolUse hook
                # instead (src/security/bash_box.py, dv-98f2).
                # autoAllowBashIfSandboxed stays False (dv-6dbc): bash must
                # keep reaching can_use_tool.
                sandbox={  # type: ignore[arg-type]
                    "enabled": False,
                    "autoAllowBashIfSandboxed": False,
                },
                # Neither the owner's global CLAUDE.md nor the install dir's
                # (dv-fe02): the CLI's upward walk loads both from any cwd
                # under $HOME, project source or not. The SDK merges this
                # with `sandbox` above into one --settings value.
                settings=json.dumps(
                    {"claudeMdExcludes": list(bot_claude_md_excludes())}
                ),
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

            # Per-turn env for the CLI (dv-2976): secrets reach it only here,
            # never via os.environ (scrubbed at startup) or argv.
            # The account-separation switches are set here explicitly, the
            # same copy plain calls get (dv-11a5), not left to inheritance.
            turn_env = {
                **CLAUDE_ACCOUNT_ENV,
                **self._seam_env(Path(working_directory)),
                **self._route_mcp_env_secrets(options.mcp_servers),
                **provider_env(route, or_key),
            }
            if turn_env:
                options.env = {**options.env, **turn_env}

            # Denylist enforcement that must hold for EVERY tool call, including
            # reads of files inside cwd -- which can_use_tool never sees.
            # dv-586f: ledger of what this turn's CLI is waiting on. Set in
            # the context so in-process MCP tools (run by SDK tasks spawned
            # from this one) time themselves into it.
            activity = TurnActivity(
                f"{self._profile_slug_for(working_directory, user_id)}:"
                f"{(session_id or 'new')[:8]}"
            )
            set_current_activity(activity)
            options.hooks = {
                "PreToolUse": [
                    HookMatcher(
                        hooks=[
                            activity.wrap(
                                "hook:PreToolUse",
                                _make_pre_tool_use_hook(
                                    working_directory=Path(working_directory),
                                    full_access=full_access,
                                    read_scope=read_scope,
                                    unrestricted=unrestricted,
                                    confirmation_manager=self._confirmation_manager,
                                    verification_budget=self._verification_budget_for(
                                        working_directory
                                    ),
                                    bash_box=(
                                        self._bash_box_for(Path(working_directory))
                                        if sandbox_enabled
                                        else None
                                    ),
                                    ask_context=AskContext(
                                        profile=self._profile_slug_for(
                                            working_directory, user_id
                                        )
                                    ),
                                ),
                            )
                        ],
                        # The gate waits up to confirmation_timeout_seconds
                        # for the owner's tap; the SDK's 60s default would
                        # kill the hook first.
                        timeout=self.config.confirmation_hook_timeout_seconds,
                    )
                ]
            }

            # Wire can_use_tool callback for preventive tool validation
            if self.security_validator:
                options.can_use_tool = activity.wrap(
                    "can_use_tool",
                    _make_can_use_tool_callback(
                        security_validator=self.security_validator,
                        working_directory=working_directory,
                        approved_directory=self.config.approved_directory,
                        full_access=full_access,
                        confirmation_manager=self._confirmation_manager,
                        read_scope=read_scope,
                        extra_write_roots=extra_dirs,
                        unrestricted=unrestricted,
                    ),
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
            # The harness trace (dv-2260) the router opened for this turn:
            # every tool call is written as it streams, so an interrupted or
            # failed turn keeps its rows.
            trace = current_trace()

            # Watchdog clock: the timeout is measured from the last message the
            # CLI sent, not from the start of the turn. See the wait loop below.
            last_activity = time.monotonic()

            run_key = (user_id or 0, self._profile_slug_for(working_directory, user_id))
            # Set by the loop when it gives up waiting for the CLI's
            # ResultMessage ("interrupted" | "result_missing").
            abandoned: List[str] = []
            was_interrupted = False
            budget_stopped = False

            async def _run_client() -> None:
                nonlocal last_activity, was_interrupted, budget_stopped
                # Use connect(None) + query(prompt) pattern because
                # can_use_tool requires the prompt as AsyncIterable, not
                # a plain string. connect(None) uses an empty async
                # iterable internally, satisfying the requirement.
                client = ClaudeSDKClient(options)
                run = _ActiveRun(client=client, user_id=run_key[0], profile=run_key[1])
                self._active_runs.setdefault(run_key, []).append(run)
                final_at: Optional[float] = None
                post_final_seen: List[str] = []
                next_msg: Optional["asyncio.Future[Any]"] = None
                # dv-55ad: the turn's last rate_limit_event (raw info, seen at),
                # recorded once in the finally, never inside this loop.
                latest_usage: Optional[Tuple[Any, datetime]] = None
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
                    stream = client._query.receive_messages().__aiter__()
                    while True:
                        # Poll in 1s slices so the interrupt / final-answer
                        # deadlines below fire even when the CLI goes quiet
                        # or keeps emitting non-result traffic.
                        if next_msg is None:
                            next_msg = asyncio.ensure_future(stream.__anext__())
                        done, _ = await asyncio.wait({next_msg}, timeout=_POLL_SECONDS)
                        now = time.monotonic()
                        # dv-586f: a quiet CLI is described in the log (ledger
                        # of last events + CLI process/socket state).
                        if not done and activity.stall_due():
                            await activity.log_stall(cli_pid(client))
                        # Checked on every slice, not only when the CLI is
                        # quiet: a run that keeps streaming after an interrupt
                        # (sub-agent, long tool) otherwise outlives /stop by
                        # minutes (dv-b399: 2m43s on 2026-10-20).
                        if (
                            run.interrupted_at is not None
                            and now - run.interrupted_at > _INTERRUPT_GRACE_SECONDS
                        ):
                            was_interrupted = True
                            abandoned.append("interrupted")
                            # disconnect() ends only the CLI; its shells and
                            # whatever they started would outlive the stop.
                            await self._hard_stop_run(run, "interrupt grace over")
                            break
                        if not done:
                            if (
                                final_at is not None
                                and now - final_at > _FINAL_RESULT_GRACE_SECONDS
                            ):
                                abandoned.append("result_missing")
                                break
                            continue
                        fut, next_msg = next_msg, None
                        try:
                            raw_data = fut.result()
                        except StopAsyncIteration:
                            break
                        except Exception:
                            if run.hard_stopped:
                                # The CLI was ended by force; its dying stream
                                # is the stop, not a failure to report.
                                was_interrupted = True
                                abandoned.append("interrupted")
                                break
                            raise
                        if run.interrupted_at is not None:
                            was_interrupted = True
                        # Any traffic from the CLI — including a tool result
                        # after a long-running command — resets the watchdog.
                        last_activity = time.monotonic()
                        if isinstance(raw_data, dict):
                            activity.note_raw(raw_data)
                            if raw_data.get("type") == "rate_limit_event":
                                latest_usage = (
                                    raw_data.get("rate_limit_info"),
                                    datetime.now(UTC),
                                )
                        # Cost per model call, read from the RAW event: the
                        # SDK's AssistantMessage drops ``request_id`` (dv-d2f6).
                        # Over the cap and asking for a tool = the turn would
                        # spend more, so stop it; a final answer is let through.
                        if (
                            meter.observe(raw_data)
                            and not budget_stopped
                            and meter.over_cap
                            and meter.wants_more
                        ):
                            budget_stopped = True
                            logger.warning(
                                "Per-request cost cap reached; interrupting",
                                cap=max_budget_usd,
                                spent=round(meter.total, 6),
                                provider=route.provider,
                                calls=len(meter.calls),
                            )
                            await self._interrupt_runs([run])
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
                        # Partial-stream events forced on for the meter only
                        # (third-party route, no streaming caller): the rest
                        # of the loop sees what it saw before.
                        if partial_for_meter_only and isinstance(message, StreamEvent):
                            continue

                        messages.append(message)
                        if trace is not None:
                            trace.feed_sdk(message)

                        if isinstance(message, ResultMessage):
                            break

                        # Track the model's final answer: afterwards only the
                        # ResultMessage is expected (see _FINAL_RESULT_GRACE).
                        if isinstance(message, AssistantMessage) and not getattr(
                            message, "parent_tool_use_id", None
                        ):
                            blocks = message.content if message.content else []
                            # The CLI leaves AssistantMessage.stop_reason None
                            # (only the ResultMessage carries "end_turn"; probed
                            # CLI 2.1.302 / SDK 0.2.123), so a top-level message
                            # with text and no tool call is the final answer.
                            final_at = (
                                time.monotonic()
                                if _is_final_answer(blocks, message.stop_reason)
                                else None
                            )
                        elif isinstance(message, StreamEvent):
                            # New generation under way: not the end after all.
                            # The closing events of the final message itself
                            # (content_block_stop, message_delta, message_stop)
                            # arrive after it and must not reset the grace;
                            # they did, so it never started.
                            if (message.event or {}).get(
                                "type"
                            ) in _NEW_GENERATION_EVENTS:
                                final_at = None
                        elif final_at is not None:
                            # Evidence for dv-586f: what does the CLI send
                            # between the final answer and the result?
                            kind = (
                                f"{type(message).__name__}:"
                                f"{getattr(message, 'subtype', '')}"
                            )
                            if kind not in post_final_seen and len(post_final_seen) < 8:
                                post_final_seen.append(kind)
                                logger.info(
                                    "Message after final answer",
                                    kind=kind,
                                    secs_since_final=round(
                                        time.monotonic() - final_at, 1
                                    ),
                                )

                        # Handle streaming callback. Bounded: a hung Telegram
                        # call must not stop us reading the ResultMessage.
                        if stream_callback:
                            cb_token = activity.begin(
                                "stream_cb", type(message).__name__
                            )
                            try:
                                await asyncio.wait_for(
                                    self._handle_stream_message(
                                        message, stream_callback
                                    ),
                                    _STREAM_CALLBACK_TIMEOUT_SECONDS,
                                )
                            except Exception as callback_error:
                                logger.warning(
                                    "Stream callback failed",
                                    error=str(callback_error),
                                    error_type=type(callback_error).__name__,
                                )
                            finally:
                                activity.end(cb_token)
                finally:
                    # First and not awaited: a second cancel during the
                    # disconnect below cannot skip it (dv-55ad).
                    if latest_usage is not None and not route.is_third_party:
                        quota.record_usage_soon(
                            latest_usage[0], "claude-turn", latest_usage[1]
                        )
                    activity.log_summary(reason=abandoned[0] if abandoned else "end")
                    if next_msg is not None and not next_msg.done():
                        next_msg.cancel()
                    group = self._active_runs.get(run_key, [])
                    if run in group:
                        group.remove(run)
                    if not group:
                        self._active_runs.pop(run_key, None)
                    await client.disconnect()

            # Execute with an *idle* timeout, not a total-wall-clock one.
            #
            # This used to be asyncio.wait_for(..., claude_timeout_seconds),
            # which capped the entire turn. A turn that is healthily streaming
            # tool calls for 11 minutes was killed exactly like a hung one:
            # on 2026-09-17 two turns died at 600s while sub-agents were still
            # reporting, losing the whole reply (the work on disk survived, the
            # answer did not). Wall-clock length is not evidence of a hang;
            # silence is. So the clock now restarts on every message from the
            # CLI, and only an actually stalled turn trips it.
            idle_limit = self._idle_limit_seconds()
            await _await_with_idle_timeout(
                asyncio.ensure_future(_run_client()),
                lambda: last_activity,
                idle_limit,
            )

            if abandoned:
                logger.warning(
                    "Stopped waiting for the CLI result",
                    reason=abandoned[0],
                    user_id=user_id,
                    profile=run_key[1],
                )

            # Extract cost, tools, token usage, and session_id from result message
            cost = 0.0
            tools_used: List[Dict[str, Any]] = []
            claude_session_id = None
            result_content = None
            result_subtype = None
            result_is_error = False
            input_tokens = 0
            output_tokens = 0
            cache_read_tokens = 0
            cache_creation_tokens = 0
            for message in messages:
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    result_subtype = getattr(message, "subtype", None)
                    result_is_error = bool(getattr(message, "is_error", False))
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
                    # dv-73e2: a ToolUseBlock alone carries no outcome. Pair
                    # each one against its ToolResultBlock (by tool_use_id) so
                    # a failed/denied call is recorded with its error instead
                    # of silently counting as a success in tools_used.
                    tool_errors = _tool_result_errors(messages)
                    for msg in messages:
                        if isinstance(msg, AssistantMessage):
                            msg_content = getattr(msg, "content", [])
                            if msg_content and isinstance(msg_content, list):
                                for block in msg_content:
                                    if isinstance(block, ToolUseBlock):
                                        tool_entry: Dict[str, Any] = {
                                            "name": getattr(block, "name", "unknown"),
                                            "timestamp": current_time,
                                            "input": getattr(block, "input", {}),
                                        }
                                        error = tool_errors.get(
                                            getattr(block, "id", None)
                                        )
                                        if error:
                                            tool_entry["error"] = error
                                        tools_used.append(tool_entry)
                    break

            # No ResultMessage (abandoned after the final answer, dv-586f): the
            # cost total is unknown, so ``cost`` stays 0 and the next resumed
            # turn's delta absorbs it (the baseline is kept). Tokens come from
            # the assistant messages so the no-baseline fallback has input.
            if not any(isinstance(m, ResultMessage) for m in messages):
                (
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_creation_tokens,
                ) = _sum_assistant_usage(messages)

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

            # dv-4517: total_cost_usd is the session's running total; record
            # this turn's share (the one place; every reader sees the delta).
            cli_total = cost
            cost = self._session_costs.turn_cost(
                cli_total,
                resumed_from=session_id if (session_id and continue_session) else None,
                final_session_id=claude_session_id,
                fallback=cost_for(
                    route,
                    model=model,
                    usage={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": cache_read_tokens,
                        "cache_creation_input_tokens": cache_creation_tokens,
                    },
                ),
            )

            # The CLI prices every model by its Anthropic table; for another
            # provider's model the figure is wrong, so the meter's per-call sum
            # is billed instead (OpenRouter's exact cost where already in, the
            # conservative estimate otherwise; the settlement swaps the rest).
            # No rule for the model -> None (unpriced), never a made-up number.
            billed_cost: Optional[float] = cost
            cost_source: Optional[str] = None
            settlement = None
            if route.is_third_party:
                if meter.calls:
                    billed_cost = meter.reported_cost()
                    settlement = meter.settle()
                else:
                    billed_cost = cost_from_tokens(
                        route.model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        cache_creation_tokens=cache_creation_tokens,
                    )
                cost_source = meter.source
                logger.info(
                    "Third-party turn cost",
                    provider=route.provider,
                    model=route.model,
                    calls=len(meter.calls),
                    cost=billed_cost,
                    cost_source=cost_source,
                    pending_lookups=len(meter.pending),
                    cli_cost=cost,
                )
            if meter.missing_request_ids:
                logger.warning(
                    "Stream events without request_id; cost stays an estimate",
                    missing=meter.missing_request_ids,
                    calls=len(meter.calls),
                )

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
                for msg in (
                    _trailing_answer(messages)
                    if "result_missing" in abandoned
                    else messages
                ):
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
            # A /stop must not be undone by the empty-result retry below.
            if was_interrupted:
                aborted = True
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

            if budget_stopped:
                # Our cap, not a /stop: the same label the CLI's cap had.
                stop_reason = "max_budget"
                logger.warning(
                    "Claude run stopped at the per-request cost cap",
                    session_id=final_session_id,
                    max_budget_usd=max_budget_usd,
                    spent=round(meter.total, 6),
                    provider=route.provider,
                )
            if was_interrupted and not stop_reason:
                stop_reason = "interrupted"

            # A subscription limit (dv-7ad9) ends in an error result whose
            # text is the CLI's limit line and 0 tokens; it used to log as an
            # ok turn. Checked once on the final result, never per event.
            limit = (
                quota.parse_limit(result_content or "")
                if result_is_error or not output_tokens
                else None
            )
            if limit is not None:
                stop_reason = "quota"
                quota.record(limit, "claude-turn")
                logger.warning(
                    "Claude run hit a usage limit",
                    session_id=final_session_id,
                    kind=limit.kind,
                    until=limit.until.isoformat(),
                )

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=billed_cost,
                provider=route.provider,
                model=route.model if route.is_third_party else None,
                cost_source=cost_source,
                cost_settlement=settlement,
                api_calls=len(meter.calls),
                request_ids_missing=meter.missing_request_ids,
                duration_ms=duration_ms,
                is_error=limit is not None,
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
                idle_timeout_seconds=self._idle_limit_seconds(),
            )
            raise ClaudeTimeoutError(
                "Claude SDK produced no output for " f"{self._idle_limit_seconds()}s"
            )

        except CLINotFoundError as e:
            logger.error("Claude CLI not found", error=str(e))
            error_msg = (
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  curl -fsSL https://claude.ai/install.sh | bash\n\n"
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

        The value comes from :func:`turn_setup.image_roots_env`, shared with
        the Codex backend's ``-c mcp_servers.telegram.env`` override so both
        transports tell the tool the same deliverable roots.
        """
        from src.bot.utils.image_extractor import IMAGE_ROOTS_ENV

        if not isinstance(servers, dict):
            return
        cfg = servers.get("telegram")
        if not isinstance(cfg, dict):
            return
        env = cfg.setdefault("env", {})
        if isinstance(env, dict):
            env[IMAGE_ROOTS_ENV] = image_roots_env(
                self.config.approved_directory,
                working_directory,
                self._extra_dirs_for(working_directory),
            )

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
