"""Shared plumbing for the agent tools: services, per-turn context, tool specs.

The tools themselves live in :mod:`agent_tools` (platform, jobs, memory) and
:mod:`user_data_tools` (data.db). This module holds the three things both of
them — and both *transports* — need:

* :class:`AgentServices` — the live repositories/handles, process-global.
* :class:`TurnContext` — WHO this turn is for, resolved server-side.
* :class:`ToolSpec` — one tool: name, description, schema, handler, and the
  predicate that decides whether this context may see it at all.

Two transports consume the same specs (dv-ffcb):

* the in-process SDK MCP server (``build_agent_mcp_server``), which wraps each
  spec in a ``claude_agent_sdk`` tool bound to one fixed context for the turn;
* the loopback HTTP bridge (``src/agent_bridge``), which resolves a per-turn
  bearer token to a :class:`TurnContext` and passes it per request.

The context is an *argument*, never something the model supplies. Every field
in :class:`TurnContext` is server-derived; a tool that took a scope as a
parameter would let any profile read another user's store (dv-6dbc).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


@dataclass
class AgentServices:
    """Live service handles the in-process agent tools call directly.

    Attached to :class:`ClaudeSDKManager` after startup (the scheduler is
    created later than the SDK manager), via ``attach_agent_services``. When
    unset, no agent MCP server is mounted and callers fall back to ``agentctl``.
    """

    event_bus: Any
    task_repo: Any
    scheduler: Any = None  # Optional["JobScheduler"]; None when scheduler disabled
    profile_registry: Any = None
    default_chat_ids: Optional[List[int]] = None
    # Optional["MemoryWriter"], constructed with source="agent_tool". When
    # None the ``remember`` tool is not mounted at all -- an always-erroring
    # tool still costs schema tokens and still invites the model to call it.
    memory_writer: Any = None
    # Optional["MemoryRepository"], read-only counterpart to memory_writer.
    # Powers the `recall` tool (explicit fact search); None -> not mounted.
    memory_repo: Any = None
    # Optional["MessageRepository"] (src/storage/repositories.py). Powers the
    # `recall_conversation` tool (FTS over raw past messages); None -> not
    # mounted.
    message_repo: Any = None
    # Optional["MemoryWriter"] constructed with source="user_request" — the
    # audit tools' write path (dv-a9e1). Separate from memory_writer so
    # `invalidated_by` records WHO closed a fact: the user asking, not the
    # model volunteering. None -> memory_forget/memory_correct not mounted.
    audit_memory_writer: Any = None
    # Optional["UserDataRepository"] (dv-5f3a): the data.db access seam.
    # Powers the per-profile data tools; None -> none mounted.
    user_data: Any = None
    # Optional["HireliClient"] (dv-9f02): holds the Hireli service key in the
    # bot process. None -> the hireli_* tools are not mounted.
    hireli: Any = None
    # Optional["ExchangeClient"] (dv-8664): runs the fetchcrypto/fetchmexc
    # scripts for the exchange_* tools. None -> those tools are not mounted.
    exchange: Any = None
    # IANA zone naive schedule times are interpreted in (dv-ca94). Install-
    # wide today (Settings.user_timezone, default Europe/Lisbon) — there is
    # no per-user timezone yet; that lands with the dv-752a settings menu.
    user_timezone: str = "UTC"
    # Optional["ItemService"] (dv-752a): family todos & reminders. None ->
    # the item_* tools are not mounted.
    items: Any = None
    # Optional["Storage"] facade + "WindowRepository" (dv-da50): a direct
    # ``notify`` is saved to the user's chat history through
    # ``storage.delivered.persist_delivered_text``. None -> notify only sends.
    storage: Any = None
    window_repo: Any = None
    # async (reason, ctx) -> "scheduled" | "already scheduled" (dv-da50):
    # main.py's closure over the process's TurnLocks and
    # ``commands.system.request_restart``. None -> restart_bot not mounted.
    request_restart: Optional[Callable[[str, "TurnContext"], Awaitable[str]]] = None


@dataclass(frozen=True)
class TurnContext:
    """Who one agent turn is acting as — resolved by the bot, never by a model.

    ``profile_slug`` / ``working_dir`` identify the calling agent so the tools
    can default job targets and task attribution to it.

    ``requester_chat_id`` is the Telegram chat that initiated the turn:
    ``notify`` stamps it on the published event and ``schedule_add`` persists
    it as the job's audience. When absent (webhook-triggered runs), delivery
    falls back to the configured ``NOTIFICATION_CHAT_IDS`` broadcast list.

    ``memory_agent_id`` / ``family_agent_id`` are the *already resolved* memory
    scopes for this session (``{slug}`` / ``{slug}#u{user_id}`` and
    ``f{family_id}:{slug}``). ``remember``/``recall`` must never accept a scope
    identifier as a tool argument, or any profile could read/write another
    user's store — the boundary dv-6dbc closed.

    ``user_id`` is the raw Telegram/DB user id driving the turn. It scopes
    ``recall_conversation`` and every data.db tool, and ``schedule_add`` stamps
    it as the new job's ``created_by`` (dv-3e91).

    ``role`` gates the background-job tools. Defaulting to None means every
    caller that does not explicitly resolve a role fails closed (dv-a900).
    """

    profile_slug: str = ""
    working_dir: Optional[Path] = None
    requester_chat_id: Optional[int] = None
    memory_agent_id: Optional[str] = None
    family_agent_id: Optional[str] = None
    user_id: Optional[int] = None
    role: Optional[str] = None
    # Env var NAMES (never values) of the profile's sandbox_keys that this
    # turn's Codex child holds (dv-28f2). Non-empty -> the policy gate wraps
    # every allowed Bash call in the bash box's ``--redact-env`` mode.
    redact_env: Tuple[str, ...] = ()
    # One id per turn (the context is built once per turn on both transports).
    # job_start stamps it on a job as its batch: jobs started in one turn are
    # digested together (dv-c41e amendment, 2026-10-22).
    # A per-turn random id, not part of the resolved context (excluded from ==).
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12], compare=False)


ToolHandler = Callable[
    [AgentServices, TurnContext, Dict[str, Any]], Awaitable[Dict[str, Any]]
]
ToolPredicate = Callable[[AgentServices, TurnContext], bool]


def always(services: AgentServices, ctx: TurnContext) -> bool:
    """Availability predicate for tools every session gets."""
    return True


@dataclass(frozen=True)
class ToolSpec:
    """One tool, transport-independent.

    ``available`` is the mount-time filter: both transports call it with the
    turn's context and offer the tool only when it returns True. It is a
    filter, not the authorization — every scoped handler re-checks its own
    preconditions, so a mis-wire fails loudly instead of running unscoped.

    ``group`` is the data.db capability group (``logs`` / ``products`` /
    ``portions`` / ``export``) for tools mounted from a profile's
    ``user_data_tools`` config; None for everything else.
    """

    name: str
    description: str
    schema: Dict[str, Any]
    handler: ToolHandler
    available: ToolPredicate = always
    group: Optional[str] = None


def sdk_tool(spec: ToolSpec, services: AgentServices, ctx: TurnContext) -> Any:
    """Wrap one spec as a ``claude_agent_sdk`` tool bound to this turn's context.

    The in-process transport: the returned object is what
    ``create_sdk_mcp_server`` takes, and its handler still receives only the
    model's arguments — the context is supplied here, by the bot.
    """
    from claude_agent_sdk import tool

    @tool(spec.name, spec.description, spec.schema)
    async def _run(args: Dict[str, Any]) -> Dict[str, Any]:
        # dv-586f: the CLI blocks on this call; the turn's ledger times it.
        from .turn_activity import current_activity

        activity = current_activity()
        token = activity.begin("mcp", spec.name) if activity else 0
        outcome = "ok"
        try:
            return await spec.handler(services, ctx, args)
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            if activity:
                activity.end(token, outcome)

    return _run
