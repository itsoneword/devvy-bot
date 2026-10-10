"""Agent tools for family todos & reminders (dv-752a phase A).

Same :class:`ToolSpec` pattern as ``agent_tools``: mounted on both backends
through ``available_tool_specs``. Who is acting always comes from the turn
context (``ctx.user_id``), never from a tool argument; the only person a model
names is the assignee, and :class:`ItemService` refuses anyone outside the
caller's family.
"""

from __future__ import annotations

from typing import Any, Dict, List

import structlog

from ..items.service import ItemError
from .tool_context import AgentServices, ToolSpec, TurnContext
from .tool_schema import tool_schema

logger = structlog.get_logger()

ITEM_SPECS: List[ToolSpec] = []


def _available(services: AgentServices, ctx: TurnContext) -> bool:
    return services.items is not None and ctx.user_id is not None


def _spec(name: str, description: str, schema: Dict[str, Any]) -> Any:
    def decorator(handler: Any) -> Any:
        ITEM_SPECS.append(
            ToolSpec(
                name=name,
                description=description,
                schema=schema,
                handler=handler,
                available=_available,
            )
        )
        return handler

    return decorator


def _ok(payload: Any) -> Dict[str, Any]:
    from .agent_tools import _text

    return _text(payload)


def _fail(message: str) -> Dict[str, Any]:
    from .agent_tools import _err

    return _err(message)


def _s(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))
    return value.strip() if isinstance(value, str) else ""


def _item_id(args: Dict[str, Any]) -> int:
    from .agent_tools import _id_arg

    item_id = _id_arg(args, "id")
    if item_id is None:
        raise ItemError("id is required — get it from item_list")
    return item_id


async def _run(ctx: TurnContext, name: str, coro: Any) -> Dict[str, Any]:
    try:
        result = await coro
    except ItemError as exc:
        logger.info("item tool refused", tool=name, user_id=ctx.user_id, why=str(exc))
        return _fail(str(exc))
    return _ok(result)


@_spec(
    "item_add",
    "Create a todo or reminder for this user or a family member. 'text': what "
    "to remind/do, written as the final message the recipient will read, in "
    "their language. 'for': 'me' (default) or a family member's id or name "
    "from the family list in <family_reminders>. 'at': a bare local ISO time "
    "('2026-09-29T17:00:00', the RECIPIENT's timezone) for a one-shot "
    "reminder, or 'cron': 5-field crontab in the recipient's local time "
    "('0 9 * * 1-5') for a recurring one; neither = a plain todo. "
    "'deadline': when it is DUE (local date '2026-10-09' = end of that day, "
    "or a local datetime; recipient's tz), separate from 'at'/'cron' which "
    "are when to remind: 'due Friday, remind Wednesday' = deadline + at. "
    "'agent': "
    "which of the recipient's agents shows the reminder (e.g. chef); default "
    "'general' for another member, this agent for yourself. 'deliver': "
    "'message' (default, sent verbatim) or 'agent' (runs as an agent turn; "
    "your own reminders only). The reply echoes the stored local + UTC time "
    "— confirm it to the user. Use this, not schedule_add, for reminders.",
    tool_schema(
        {
            "text": str,
            "for": str,
            "at": str,
            "cron": str,
            "agent": str,
            "deliver": str,
            "deadline": str,
        },
        "text",
    ),
)
async def item_add(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    return await _run(
        ctx,
        "item_add",
        services.items.add(
            int(ctx.user_id or 0),
            text=_s(args, "text"),
            who=_s(args, "for"),
            at=_s(args, "at") or None,
            cron=_s(args, "cron") or None,
            agent=_s(args, "agent") or None,
            deliver=_s(args, "deliver") or None,
            deadline=_s(args, "deadline") or None,
            source_profile=ctx.profile_slug,
            source_working_dir=ctx.working_dir,
        ),
    )


@_spec(
    "item_list",
    "List todos/reminders. 'scope': all (default) | mine (I set for myself) "
    "| by_me (I set for family members) | for_me (family set for me). "
    "'status': open (default) | fired (one-shot, already delivered) | done "
    "| cancelled | all. Each row names who it is for/from, the agent, and "
    "the deadline and next fire time in local time, and a notes count. Pass "
    "'id' to get one item with its notes (comments, research, status "
    "changes) instead of the list.",
    tool_schema({"scope": str, "status": str, "id": int}),
)
async def item_list(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if args.get("id") not in (None, ""):
        user_id = int(ctx.user_id or 0)

        async def one() -> Any:
            return await services.items.get(user_id, _item_id(args))

        return await _run(ctx, "item_list", one())
    return await _run(
        ctx,
        "item_list",
        services.items.list(
            int(ctx.user_id or 0),
            scope=_s(args, "scope") or "all",
            status=_s(args, "status") or "open",
        ),
    )


@_spec(
    "item_update",
    "Change a todo/reminder you created or that is for you, by 'id' (from "
    "item_list): new 'text', 'deadline' (when it is DUE: local date or "
    "datetime, recipient's tz; 'none' clears it), a new reminder time ('at' "
    "or 'cron', local time, replaces the old schedule; this is when to "
    "remind, not when it is due), 'agent', or 'deliver'.",
    tool_schema(
        {
            "id": int,
            "text": str,
            "deadline": str,
            "at": str,
            "cron": str,
            "agent": str,
            "deliver": str,
        },
        "id",
    ),
)
async def item_update(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    async def go() -> Any:
        return await services.items.update(
            int(ctx.user_id or 0),
            _item_id(args),
            text=_s(args, "text") or None,
            deadline=_s(args, "deadline") or None,
            at=_s(args, "at") or None,
            cron=_s(args, "cron") or None,
            agent=_s(args, "agent") or None,
            deliver=_s(args, "deliver") or None,
        )

    return await _run(ctx, "item_update", go())


@_spec(
    "item_note",
    "Attach a comment or research result to an existing todo by 'id' (from "
    "item_list). 'text': the note (max 4000 chars). 'kind': comment "
    "(default) | research. For long research, write the full text to a file "
    "and pass a one-line summary in 'text' plus the file location in "
    "'path'. Creator or recipient only.",
    tool_schema({"id": int, "text": str, "kind": str, "path": str}, "id", "text"),
)
async def item_note(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    async def go() -> Any:
        return await services.items.add_note(
            int(ctx.user_id or 0),
            _item_id(args),
            kind=_s(args, "kind") or "comment",
            body=_s(args, "text"),
            path=_s(args, "path") or None,
        )

    return await _run(ctx, "item_note", go())


@_spec(
    "item_done",
    "Mark a todo/reminder done by 'id' (from item_list). Stops a recurring "
    "reminder. Creator or recipient may do it.",
    tool_schema({"id": int}, "id"),
)
async def item_done(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    async def go() -> Any:
        return await services.items.close(int(ctx.user_id or 0), _item_id(args), "done")

    return await _run(ctx, "item_done", go())


@_spec(
    "item_cancel",
    "Cancel a todo/reminder by 'id' (from item_list); its reminder will not "
    "fire again. Creator or recipient may do it.",
    tool_schema({"id": int}, "id"),
)
async def item_cancel(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    async def go() -> Any:
        return await services.items.close(
            int(ctx.user_id or 0), _item_id(args), "cancelled"
        )

    return await _run(ctx, "item_cancel", go())


@_spec(
    "item_cancel_all_mine",
    "Cancel EVERY open timed reminder set for this user (by themselves or "
    "by family). Only when the user explicitly asks to stop all their "
    "reminders; confirm the count back to them.",
    tool_schema({}),
)
async def item_cancel_all_mine(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    return await _run(
        ctx,
        "item_cancel_all_mine",
        services.items.cancel_all_mine(int(ctx.user_id or 0)),
    )


@_spec(
    "family_member_set",
    "Record a family member's display name and/or relation (e.g. name "
    "'Аня', relation 'жена'), so reminders can name them. 'member': their "
    "id from the family list, or 'me'. Only members of this user's family.",
    tool_schema({"member": str, "name": str, "relation": str}, "member"),
)
async def family_member_set(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    async def go() -> Any:
        member = await services.items.set_member(
            int(ctx.user_id or 0),
            _s(args, "member"),
            _s(args, "name") or None,
            _s(args, "relation") or None,
        )
        return {
            "status": "saved",
            "id": member.telegram_id,
            "name": member.display_name,
            "relation": member.relation,
        }

    return await _run(ctx, "family_member_set", go())
