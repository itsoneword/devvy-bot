"""In-process MCP tools exposing schedule / notify / task capabilities to agents.

These are **SDK MCP tools** — they execute inside the bot process, OUTSIDE the
Bash sandbox. That is the whole point: an agent's Bash tool is network- and
filesystem-isolated (bwrap), so the HTTP ``agentctl`` path to ``127.0.0.1:8080``
gets ``connection refused``. These tools call the scheduler / task repo / event
bus objects directly — no localhost round-trip, no sandbox to escape.

Every tool is a :class:`ToolSpec` whose handler takes the turn's
:class:`TurnContext` explicitly — who the turn is for is an argument, not a
closure. Two transports bind it (dv-ffcb): :func:`build_agent_mcp_server`
wraps the specs for the in-process SDK server, and ``src/agent_bridge``
serves the same specs over loopback HTTP to the Codex backend, resolving the
context from a per-turn bearer token. A tool added here appears on both.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from datetime import tzinfo as _tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import structlog
from claude_agent_sdk import create_sdk_mcp_server

from ..evaluator.validator import MAX_CONTENT_LENGTH, VALID_TIERS
from ..events.types import AgentResponseEvent
from ..storage import board_policy
from .exchange_tools import EXCHANGE_SPECS, ONCHAIN_SPECS
from .hireli_tools import HIRELI_SPECS
from .item_tools import ITEM_SPECS
from .tool_context import (
    AgentServices,
    ToolPredicate,
    ToolSpec,
    TurnContext,
    always,
    sdk_tool,
)
from .tool_schema import tool_schema
from .user_data_tools import USER_DATA_READ_SPECS, USER_DATA_WRITE_SPECS
from .weather_tool import get_weather_for

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

logger = structlog.get_logger()

# Same ceiling the evaluator's validator enforces on an extracted fact -- one
# limit for both write paths, so a fact the tool accepts is one the batch job
# would also have accepted.
MAX_FACT_CHARS = MAX_CONTENT_LENGTH

# `recall` (facts): explicit retrieval has no per-turn injection cap to lean
# on, so it needs its own ceiling on how much a single call can return.
RECALL_DEFAULT_LIMIT = 10
RECALL_MAX_LIMIT = 30

# `recall_conversation` (raw messages): snippets are bigger than a fact, so
# the cap on hit COUNT is tighter -- total response size must stay bounded.
RECALL_CONVERSATION_DEFAULT_LIMIT = 5
RECALL_CONVERSATION_MAX_LIMIT = 20


# Storage bookkeeping the model has never read, stripped from every rendered
# result. Mirrors _DROP_COLUMNS in user_data_tools (dv-dd7e).
_DROP_COLUMNS = frozenset({"user_id", "created_at", "updated_at", "imported_from"})


def _compact(value: Any) -> Any:
    """Strip render-only noise from a payload, recursively.

    Nulls are kept: absent and null are not the same claim about a fact, and
    these tools hand the model memory and task rows it reasons about.
    """
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if k not in _DROP_COLUMNS}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _text(payload: Any) -> Dict[str, Any]:
    """Wrap a value as an SDK MCP tool text result, rendered compactly.

    Results live in the transcript for the whole conversation window and are
    resent every turn, so render-only bytes are paid per turn until rotation
    (dv-dd7e). ``ensure_ascii=False`` matters most here: these payloads are
    largely Russian, and escaping turns each Cyrillic character into a
    six-character ``\\uXXXX`` sequence.
    """
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(
            _compact(payload),
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        )
    return {"content": [{"type": "text", "text": text}]}


def _err(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


def _str_arg(args: Dict[str, Any], key: str) -> str:
    """Stripped string value of a tool argument, whatever the model sent.

    A declared ``str`` in the tool schema is not a guarantee — a model can
    (and does) put a dict or a number there. Non-strings become "" rather
    than raising, so validation below produces a tool error the model can act
    on instead of an exception it never sees.
    """
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _int_arg(args: Dict[str, Any], key: str, default: int, hard_cap: int) -> int:
    """Clamp a model-supplied limit to [1, hard_cap].

    Falls back to ``default`` for anything not usable as a positive int --
    missing, wrong type, zero, negative -- rather than raising or serving an
    unbounded query.
    """
    try:
        n = int(args.get(key))
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, hard_cap)


def _id_arg(args: Dict[str, Any], key: str) -> Optional[int]:
    """A model-supplied row id, or None for anything not a positive int."""
    value = args.get(key)
    if isinstance(value, bool):  # bool is an int subclass; True is not an id
        return None
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _resolve_tz(name: str) -> "ZoneInfo":
    """The configured local zone, falling back to UTC for a bad name.

    A misconfigured ``USER_TIMEZONE`` must not break every ``schedule_add``
    call (dv-ca94).
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_dt(value: str, local_tz: _tzinfo) -> datetime:
    """Parse an ISO datetime.

    A naive value (no UTC offset) is interpreted in ``local_tz`` — the
    install's configured user timezone — not UTC (dv-ca94): an agent that
    types "today at 17:00" as a bare ISO string means the user's local
    17:00, and silently treating that as UTC fired reminders an hour+ late
    for a Lisbon user. A value WITH an explicit offset (``Z``, ``+01:00``,
    ...) is left exactly as given — the caller already disambiguated it.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz)
    return dt


def _owns_scheduled_job(ctx: TurnContext, row: Dict[str, Any]) -> bool:
    """May this turn see/remove this ``scheduled_jobs`` row?

    Thin wrapper over :func:`src.scheduler.scheduler.job_visible_to` — THE
    authorization rule (dv-8c19), shared with the ``/schedules`` command so
    the two paths can't drift. Exposing ``schedule_list``/``schedule_remove``
    over the bridge without it would hand any Codex turn every other user's
    reminder prompts and target chat ids.
    """
    from ..scheduler.scheduler import job_visible_to

    return job_visible_to(row, ctx.user_id, ctx.role, ctx.requester_chat_id)


def _job_tools_allowed(role: Optional[str]) -> bool:
    """Background-job tools are owner-only (dv-a900).

    ``scripts/bgjob`` spawns the raw ``claude`` binary with
    ``--permission-mode bypassPermissions`` — none of the per-turn SDK
    enforcement (PreToolUse denylist, can_use_tool confinement) applies to
    it. Fail closed: only an exactly-resolved ``"owner"`` role mounts the
    job tools; ``"guest"``, ``None`` (unresolved — webhook/scheduler
    callers), and any future role get nothing.
    """
    return role == "owner"


# --------------------------------------------------------------------------
# Spec registry. ``available`` is the mount filter both transports apply; it
# encodes exactly the conditions the old builder used to append under.
# --------------------------------------------------------------------------

AGENT_TOOL_SPECS: List[ToolSpec] = []


def _jobs_available(services: AgentServices, ctx: TurnContext) -> bool:
    """Background-job tools are owner-only (dv-a900)."""
    return _job_tools_allowed(ctx.role)


def _memory_write_available(services: AgentServices, ctx: TurnContext) -> bool:
    """``remember`` needs a writer AND a resolved memory scope."""
    return bool(services.memory_writer and ctx.memory_agent_id)


def _memory_read_available(services: AgentServices, ctx: TurnContext) -> bool:
    """``recall`` / ``memory_list`` need the repo AND a resolved scope."""
    return bool(services.memory_repo and ctx.memory_agent_id)


def _memory_audit_available(services: AgentServices, ctx: TurnContext) -> bool:
    """Audit writes additionally need the repo (for the server-side ownership
    check) and the user_request-stamped writer (dv-a9e1)."""
    return bool(
        services.audit_memory_writer and services.memory_repo and ctx.memory_agent_id
    )


def _conversation_available(services: AgentServices, ctx: TurnContext) -> bool:
    """No user id means no safe way to scope the search (dv-r3c1)."""
    return bool(services.message_repo and ctx.user_id is not None)


def _spec(
    name: str,
    description: str,
    schema: Dict[str, Any],
    available: ToolPredicate = always,
) -> Any:
    """Register one agent tool. Definition order is mount order."""

    def decorator(handler: Any) -> Any:
        AGENT_TOOL_SPECS.append(
            ToolSpec(
                name=name,
                description=description,
                schema=schema,
                handler=handler,
                available=available,
            )
        )
        return handler

    return decorator


def _resolve_working_dir(
    services: AgentServices, ctx: TurnContext, profile: Optional[str]
) -> Optional[Path]:
    if not profile:
        return ctx.working_dir
    if not services.profile_registry:
        return ctx.working_dir
    p = services.profile_registry.get_by_slug(profile)
    return p.working_dir if p else ctx.working_dir


@_spec(
    "notify",
    "Send the user a Telegram message right now. Owners may set source to "
    "server-monitor, taskpump:status, crypto, or news to route a report to "
    "the matching reports topic. Omit source for a direct message.",
    tool_schema({"text": str, "source": str}, "text"),
)
async def notify(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    text = (args.get("text") or "").strip()
    if not text:
        return _err("text is required")
    source = (args.get("source") or "").strip()
    if source and (
        ctx.role != "owner"
        or source not in {"server-monitor", "taskpump:status", "crypto", "news"}
    ):
        return _err("source is restricted to owner report routes")
    # Requester chat comes from server context only; a model-supplied
    # chat_id argument is ignored so agents cannot address other chats.
    chat_id = ctx.requester_chat_id or 0
    event = AgentResponseEvent(
        chat_id=int(chat_id),
        text=text,
        parse_mode="markdown",
        source=source or "agent",
    )
    await services.event_bus.publish(event)
    logger.info("agent tool notify", profile=ctx.profile_slug, chat_id=chat_id)
    return _text({"status": "sent", "event_id": event.id})


@_spec(
    "schedule_add",
    "Register a scheduled job that survives restarts. Provide exactly one of "
    "'cron' (crontab syntax, server time is UTC) or 'at' (ISO datetime). "
    "For 'at': give a bare local time/date with NO UTC offset ('2026-09-27T"
    "17:00:00') and it is interpreted in the user's local timezone (see the "
    "<current_time> line in this turn) — do not convert it to UTC yourself, "
    "that is how reminders end up an hour off. Give an explicit offset only "
    "when you mean UTC or another zone on purpose. The reply echoes back "
    "both the local and UTC time actually stored — check it. Two delivery "
    "modes: deliver='message' sends the prompt text to the user VERBATIM "
    "when it fires — no agent turn, so write the final user-facing message "
    "in the user's language, as if you were sending it yourself at that "
    "moment. deliver='agent' (default) runs the prompt as an agent turn in "
    "the target profile first. Prefer 'at' + deliver='message' for one-shot "
    "reminders (cheap, keeps the conversation cache warm); use "
    "deliver='agent' only when the firing turn must look at fresh data "
    "before writing to the user. 'cron' for routines. For a reminder to the "
    "user or a family member use item_add instead — it is visible to both "
    "sides and runs in local time.",
    # cron/at are deliberately BOTH optional: the handler rejects the call
    # when neither or both are supplied, so requiring either here would
    # make the schema demand exactly what the handler refuses.
    tool_schema(
        {
            "name": str,
            "prompt": str,
            "cron": str,
            "at": str,
            "profile": str,
            "deliver": str,
        },
        "name",
        "prompt",
    ),
)
async def schedule_add(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if not services.scheduler:
        return _err("scheduler not enabled")
    name = (args.get("name") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    if not name or not prompt:
        return _err("name and prompt are required")
    cron = (args.get("cron") or "").strip() or None
    at_raw = (args.get("at") or "").strip() or None
    if bool(cron) == bool(at_raw):
        return _err("provide exactly one of cron / at")
    deliver = (args.get("deliver") or "agent").strip()
    if deliver not in ("agent", "message"):
        return _err("deliver must be 'agent' or 'message'")
    local_tz = _resolve_tz(services.user_timezone)
    run_at: Optional[datetime] = None
    if at_raw:
        try:
            run_at = _parse_dt(at_raw, local_tz)
        except ValueError:
            return _err(f"could not parse 'at' datetime: {at_raw!r}")
        if run_at <= datetime.now(UTC):
            return _err("'at' is in the past")
    target = _resolve_working_dir(
        services, ctx, args.get("profile") or ctx.profile_slug
    )
    try:
        job_id = await services.scheduler.add_job(
            job_name=name,
            cron_expression=cron,
            prompt=prompt,
            target_chat_ids=(
                [ctx.requester_chat_id] if ctx.requester_chat_id else None
            ),
            working_directory=target,
            run_at=run_at,
            delivery=deliver,
            created_by=ctx.user_id or 0,
        )
    except ValueError as exc:
        return _err(str(exc))
    logger.info("agent tool schedule_add", profile=ctx.profile_slug, job_id=job_id)
    result: Dict[str, Any] = {"status": "scheduled", "job_id": job_id}
    if run_at is not None:
        # Unambiguous: what got stored (UTC) alongside what it means for the
        # user (local) — so a confirmation the model relays states the right
        # time even if it never reasoned about the offset itself (dv-ca94).
        result["at_local"] = run_at.astimezone(local_tz).isoformat()
        result["at_utc"] = run_at.astimezone(UTC).isoformat()
    return _text(result)


@_spec(
    "schedule_list",
    "List the scheduled jobs you can manage (cron and one-shot) with their "
    "next run times. Only jobs created by this user are listed.",
    tool_schema({}),
)
async def schedule_list(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if not services.scheduler:
        return _err("scheduler not enabled")
    rows = await services.scheduler.list_jobs()
    # Item-linked jobs are managed through item_* (dv-752a): the item is the
    # source of truth, removing its job alone would leave a zombie item.
    return _text(
        [
            row
            for row in rows
            if not row.get("item_id") and _owns_scheduled_job(ctx, row)
        ]
    )


@_spec(
    "schedule_remove",
    "Remove a scheduled job by its job_id (from schedule_list).",
    tool_schema({"job_id": str}, "job_id"),
)
async def schedule_remove(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if not services.scheduler:
        return _err("scheduler not enabled")
    job_id = (args.get("job_id") or "").strip()
    if not job_id:
        return _err("job_id is required")
    # Ownership check before the removal, not inside the scheduler: see
    # _owns_scheduled_job. remove_job() itself takes only an id and returns
    # True unconditionally, so without this any id from any user would go.
    rows = await services.scheduler.list_jobs()
    row = next((r for r in rows if str(r.get("job_id")) == job_id), None)
    if row is None:
        return _text({"status": "not_found", "job_id": job_id})
    if row.get("item_id"):
        return _err(
            f"job {job_id} fires item #{row['item_id']}; use item_cancel "
            "with that id instead"
        )
    if not _owns_scheduled_job(ctx, row):
        # A distinct message rather than a silent no-op (dv-6dbc: a denial the
        # agent can relay). Job ids are uuid4 hex, so saying "not yours"
        # discloses nothing an enumerator could use.
        logger.warning(
            "schedule_remove refused: not this user's job",
            job_id=job_id,
            user_id=ctx.user_id,
            role=ctx.role,
        )
        return _err(f"job {job_id} belongs to another user")
    removed = await services.scheduler.remove_job(job_id)
    return _text({"status": "removed" if removed else "not_found", "job_id": job_id})


@_spec(
    "task_add",
    "Add an item to the devvy task board (ids look like dv-3f21; an agent "
    "with its own repo board writes that board's files instead, this tool "
    "does not see it). Filing does not start work: a new task is a parked "
    "todo the pipeline ignores until the owner moves it to in_progress or "
    "you call job_start(task_id) — if you only filed it, tell the owner "
    "'filed, not started'. Optionally set a due datetime (ISO, "
    "UTC unless offset given) and an assignee profile slug. To ask devops "
    "for engineering work (a script, a tool, more access), set "
    "assignee='devops': the task goes straight to analysis, the owner gets "
    "the plan and decides; nothing is built without the owner's go. Limits "
    "for agents: 5 new tasks a day, 3 open devops tasks; an open task with "
    "the same title is returned instead of a duplicate.",
    # 'due' optional: a required due date is a deadline nobody chose —
    # three board tasks still share an invented 2026-08-22T12:00:00Z.
    tool_schema({"title": str, "body": str, "assignee": str, "due": str}, "title"),
)
async def task_add(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    title = (args.get("title") or "").strip()
    if not title:
        return _err("title is required")
    due_at: Optional[datetime] = None
    due_raw = (args.get("due") or "").strip() or None
    if due_raw:
        try:
            # Task due dates stay UTC-default (unlike schedule_add's 'at',
            # dv-ca94 is scoped to reminders) — the tool schema already
            # documents 'due' as "ISO, UTC unless offset given".
            due_at = _parse_dt(due_raw, UTC)
        except ValueError:
            return _err(f"could not parse 'due' datetime: {due_raw!r}")
    body = (args.get("body") or "").strip() or None
    assignee = (args.get("assignee") or "").strip() or None
    if not board_policy.is_privileged(ctx.profile_slug):
        # dv-535b: every other agent files through the agent rules
        # (dedupe, caps, devops tasks straight to pump analysis).
        out = await board_policy.file_agent_task(
            services.task_repo,
            slug=ctx.profile_slug,
            title=title,
            body=body,
            assignee=assignee,
            due_at=due_at,
        )
        logger.info(
            "agent tool task_add",
            profile=ctx.profile_slug,
            result=out["status"],
            task_id=out.get("task_id"),
        )
        return _text(out)
    task_id = await services.task_repo.add_task(
        title=title,
        body=body,
        created_by=ctx.profile_slug,
        assignee=assignee,
        due_at=due_at,
        # The origin profile as a label so health/ops items can be filtered
        # off the engineering board (dv-1327).
        labels=[board_policy.base_slug(ctx.profile_slug)],
    )
    logger.info("agent tool task_add", profile=ctx.profile_slug, task_id=task_id)
    return _text({"status": "created", "task_id": task_id})


@_spec(
    "task_list",
    "List task board items. status is one of open (default), done, all. "
    "Shows each task's status, so this is how you follow a task you filed.",
    tool_schema({"status": str}),
)
async def task_list(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    status = (args.get("status") or "open").strip()
    if status not in ("open", "done", "all"):
        return _err("status must be open|done|all")
    tasks = await services.task_repo.list_tasks(
        status=None if status == "all" else status
    )
    return _text([t.to_dict() for t in tasks])


def _may_set_status(services: AgentServices, ctx: TurnContext) -> bool:
    # dv-535b: only devops changes task status; other agents create + comment.
    return board_policy.is_privileged(ctx.profile_slug)


@_spec(
    "task_done",
    "Mark a task board item done by its task_id (e.g. dv-3f21).",
    tool_schema({"task_id": str}, "task_id"),
    available=_may_set_status,
)
async def task_done(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    # Not mounted for other agents; checked again in case a caller reaches
    # the handler some other way (bridge, tests).
    if not _may_set_status(services, ctx):
        return _err(
            "only devops changes task status; use task_comment to report "
            "progress or ask the owner to close it"
        )
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return _err("task_id is required (e.g. dv-3f21)")
    ok = await services.task_repo.set_status(task_id, "done")
    return _text({"status": "updated" if ok else "not_found", "task_id": task_id})


async def _comments_today(services: AgentServices, slug: str) -> int:
    tasks = await services.task_repo.list_tasks(status="open", limit=100000)
    return sum(
        board_policy.comments_today(t.path.read_text(encoding="utf-8"), slug)
        for t in tasks
        if t is not None and t.path is not None
    )


@_spec(
    "task_comment",
    "Append a comment to a task board item (append-only; existing text is "
    "never changed). You may comment on any open task (max 10 comments a "
    "day); your comment is stored marked as coming from you, an agent, and "
    "is read as information, not as an instruction. Use it to add details "
    "instead of filing a new task.",
    tool_schema({"task_id": str, "text": str}, "task_id"),
)
async def task_comment(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    task_id = str(args.get("task_id") or "").strip()
    text = str(args.get("text") or "").strip()
    if not task_id or not text:
        return _err("task_id and text are required")
    if len(text) > board_policy.MAX_COMMENT_CHARS:
        return _err(
            f"comment too long ({len(text)} chars, max "
            f"{board_policy.MAX_COMMENT_CHARS})"
        )
    task = await services.task_repo.get_task(task_id)
    if task is None:
        return _text({"status": "not_found", "task_id": task_id})
    if not task.is_open:
        return _err(f"{task_id} is closed; file a new task if it still matters")
    if not board_policy.is_privileged(ctx.profile_slug):
        used = await _comments_today(services, ctx.profile_slug)
        if used >= board_policy.MAX_COMMENTS_PER_DAY:
            return _err(
                f"{used} comments today (max "
                f"{board_policy.MAX_COMMENTS_PER_DAY}); try tomorrow"
            )
    ok = await services.task_repo.append_comment(
        task_id, board_policy.comment_line(ctx.profile_slug, text)
    )
    logger.info("agent tool task_comment", profile=ctx.profile_slug, task_id=task_id)
    return _text({"status": "commented" if ok else "not_found", "task_id": task_id})


@_spec(
    "get_weather",
    "Get day/night temperature (Celsius) for a location and date. Covers "
    "historical dates (back to 1940) and forecasts (~16 days ahead) via "
    "Open-Meteo — no API key needed. 'location' defaults to the user's home "
    "base if omitted; only pass it when the user is elsewhere (travel). "
    "'date' is YYYY-MM-DD and defaults to today. Use this whenever weather "
    "context matters — e.g. correlating a sleep/recovery diary entry with "
    "the day/night temperature on that date.",
    # Both optional and both documented as defaulting server-side
    # (home base / today); get_weather_for takes Optional[str] for each.
    tool_schema({"location": str, "date": str}),
)
async def get_weather(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    try:
        result = await get_weather_for(args.get("location"), args.get("date"))
    except Exception as exc:
        logger.warning("agent tool get_weather failed", error=str(exc))
        return _err(str(exc))
    return _text(result)


async def _bgjob(
    services: AgentServices, ctx: TurnContext, *argv: str
) -> Dict[str, Any]:
    """Run scripts/bgjob out-of-sandbox and return its output.

    The profile is pinned to the CALLER's slug server-side and is never a
    tool parameter, so an agent cannot start a job in another profile's
    workspace. bgjob then confines the job's writes (bubblewrap) to that
    profile's working dir.
    """
    return await _run_script(ctx, "bgjob", *argv)


async def _run_script(ctx: TurnContext, name: str, *argv: str) -> Dict[str, Any]:
    """Run scripts/<name> (bgjob, taskpump) out-of-sandbox, owner only."""
    # Invocation-time role gate (dv-a900), independent of the mount-time
    # filter below: even if a job tool ever ends up mounted in a
    # non-owner session (mis-wire, stale tool reference), the call still
    # refuses here. Deliberately a literal comparison, not
    # _job_tools_allowed(), so the two gates cannot be disabled together.
    if ctx.role != "owner":
        logger.warning(
            "bgjob invocation refused: non-owner role",
            profile=ctx.profile_slug,
            role=ctx.role,
        )
        return _err("background jobs are not available in this session")
    script = Path(__file__).resolve().parents[2] / "scripts" / name
    if not script.exists():
        return _err(f"{name} not found at {script}")
    env = dict(os.environ)
    # systemd --user needs this; the bot process does not have it set.
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    proc = await asyncio.create_subprocess_exec(
        str(script),
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    out, _ = await proc.communicate()
    text = (out or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return _err(text or f"{name} exited {proc.returncode}")
    return _text(text)


async def _caller_board(ctx: TurnContext) -> "tuple[str, Dict[str, Any] | None]":
    """(board, None) for the caller's profile, or ("", error). The caller's
    profile picks the board — two boards can both mint `dv-` ids — and taskpump
    is the one reader of the boards file. Never a tool parameter."""
    found = await _run_script(ctx, "taskpump", "board-of", ctx.profile_slug)
    if found.get("isError") and ctx.role != "owner":
        return "", found  # _run_script's role refusal, passed through as is
    if found.get("isError"):
        return "", _err(
            f"this agent ({ctx.profile_slug}) has no board in the task pipeline; "
            "use job_start without task_id for one-off work"
        )
    return found["content"][0]["text"].strip().splitlines()[-1].strip(), None


def _on_board(board: str, task_id: str) -> "tuple[str, Dict[str, Any] | None]":
    """(`board:bare`, None), or ("", error) when the id names another board."""
    named, _, bare = task_id.rpartition(":")
    if named and named != board:
        return "", _err(f"{task_id} is not on this agent's board ({board})")
    return f"{board}:{bare}", None


async def _start_board_task(ctx: TurnContext, task_id: str) -> Dict[str, Any]:
    """job_start(task_id) (dv-a438): put the ticket into the board pipeline
    (`taskpump start`) instead of running it as one job."""
    board, err = await _caller_board(ctx)
    if err:
        return err
    full, err = _on_board(board, task_id)
    if err:
        return err
    return await _run_script(ctx, "taskpump", "start", full, "--by", ctx.profile_slug)


async def _refresh_jobs_pin(services: AgentServices, ctx: TurnContext) -> None:
    """Re-render the pinned 'jobs running now' status post.

    Sends empty text when nothing is running, which retires the pin. Best
    effort: a failure here must never break starting a job.
    """
    if services.event_bus is None:
        return
    try:
        listing = await _bgjob(services, ctx, "list")
        if listing.get("isError"):
            return
        lines = [
            ln for ln in listing["content"][0]["text"].splitlines() if "RUNNING" in ln
        ]
        if lines:
            body = "\n".join(f"• {ln.strip()}" for ln in lines)
            text = f"⏳ Background jobs running ({len(lines)})\n{body}"
        else:
            text = ""
        await services.event_bus.publish(
            AgentResponseEvent(
                chat_id=int(ctx.requester_chat_id or 0),
                text=text,
                parse_mode=None,
                pin_as="jobs",
            )
        )
    except Exception as exc:
        logger.warning("jobs pin refresh failed", error=str(exc))


_TASK_ID_RE = re.compile(r"^([a-z]+:)?dv-[0-9a-f]{4,}$")


def valid_task_id(task_id: str) -> bool:
    """A board ticket id (`dv-xxxx`, optionally `board:dv-xxxx`)."""
    return bool(_TASK_ID_RE.match(task_id))


def _command_job_argv(
    services: AgentServices, ctx: TurnContext, command: str
) -> "tuple[str, str]":
    """(command to run, error). Owner-gated like Bash: the policy's deny/ask
    rules apply (an ask is refused -- no owner button in a detached job), and a
    sandboxed profile's command runs inside its bash box."""
    from ..security.bash_box import BashBox, BoxUnavailable
    from ..security.policy import ALLOW, decide, turn_policy_for

    if ctx.working_dir is None:
        return "", "this turn has no working directory"
    registry = services.profile_registry
    try:
        turn = turn_policy_for(
            registry, True, ctx.working_dir, user_id=ctx.user_id, role=ctx.role
        )
        decision = decide("Bash", {"command": command}, turn)
    except Exception as exc:
        logger.warning("job_start command policy failed", error=str(exc))
        return "", "the policy check failed"
    if decision.action != ALLOW:
        return "", (
            f"command not allowed in a detached job ({decision.action}: "
            f"{decision.reason}); run it in the turn instead"
        )
    if turn.full_access:
        return command, ""
    profile = registry.get_by_working_dir(ctx.working_dir) if registry else None
    box = BashBox.for_profile(profile, Path(ctx.working_dir))
    try:
        return box.wrap(command, str(ctx.working_dir)), ""
    except BoxUnavailable:
        return "", "bash box unavailable (bwrap missing)"


@_spec(
    "job_start",
    "Start a DETACHED background job for work too long for one turn "
    "(>5 min: deep research, big audits, bulk processing). Unlike a "
    "backgrounded sub-agent — which is always terminated the moment you "
    "reply — this survives the turn AND a bot restart, and messages the "
    "user when it finishes. The job runs in YOUR profile's workspace and "
    "can only write there. Give it a self-contained prompt: it starts "
    "fresh with no memory of this conversation. For a board ticket pass "
    "task_id (dv-xxxx) instead: that starts no job here but puts the "
    "ticket of YOUR board into the task pipeline (analysis -> owner "
    "questions -> build -> owner review, each step a separate job, hours "
    "not minutes); creating a task does not start it, this does. You do "
    "not execute board tasks yourself, and prompt is then optional. A profile with no board "
    "gets an error. Standing job rules and the <summary> contract are added "
    "automatically; do not restate them. Tell the user it is running and "
    "stop; do not wait for it. For a plain shell command with no model "
    "(the FULL test suite, a long build) pass `command` instead of prompt: "
    "it runs detached, judged by the same policy as your Bash tool, and "
    "its pass/fail line is pinged to Jobs. Reply right away that it started. "
    "A prompt job whose name matches a job running or finished "
    "OK in the last 30 min is refused (error names that job); pass "
    "force=true only for an intentional re-run. Jobs you start in the same "
    "turn are reported back together in ONE digest in this chat when all "
    "have finished (or 30 min after the first), so start related jobs in "
    "one turn.",
    tool_schema(
        {
            "prompt": str,
            "command": str,
            "name": str,
            "task_id": str,
            "force": bool,
        }
    ),
    available=_jobs_available,
)
async def job_start(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    prompt = (args.get("prompt") or "").strip()
    command = (args.get("command") or "").strip()
    task_id = (args.get("task_id") or "").strip()
    if command and task_id:
        return _err("task_id is for prompt jobs, not command jobs")
    if task_id:
        if not valid_task_id(task_id):
            return _err("task_id must look like dv-xxxx")
        logger.info("agent tool job_start", profile=ctx.profile_slug, task=task_id)
        return await _start_board_task(ctx, task_id)
    if bool(prompt) == bool(command):
        return _err("give exactly one of prompt or command")
    # --name takes a VALUE, and argparse rejects a value that looks like an
    # option ("expected one argument"), so a leading dash is stripped rather
    # than passed through. bgjob slugifies the rest itself.
    name = (args.get("name") or "job").strip().lstrip("-") or "job"
    logger.info("agent tool job_start", profile=ctx.profile_slug, name=name)
    if command:
        run_cmd, err = _command_job_argv(services, ctx, command)
        if err:
            return _err(err)
        return await _bgjob(
            services,
            ctx,
            "run",
            "--profile",
            ctx.profile_slug,
            "--name",
            name,
            "--cmd",
            run_cmd,
        )
    # The start ping and pin refresh are sent by `scripts/bgjob run`
    # itself (commit 3d483b7) -- NOT here. They used to be duplicated:
    # this tool published its own start notification AND _bgjob() below
    # shelled out to a script that, as of that commit, also sends one.
    # Every launch through this tool was silently double-pinging. Single
    # source of truth now lives in the runner so it fires identically
    # regardless of entry point (this tool, a shell, cron).
    # `--` first, model-supplied text after it. `bgjob run` takes the prompt as
    # an argparse POSITIONAL next to --profile/--name/--cwd/--allow-dir, and
    # --cwd/--allow-dir are exactly the flags that decide what the detached
    # job may write to. Today a prompt like "--cwd=/" only crashes the run
    # (argparse then has no positional left), but that is an accident of the
    # current signature, not a boundary. With the separator no model-supplied
    # byte can ever be read as an option.
    return await _bgjob(
        services,
        ctx,
        "run",
        "--profile",
        ctx.profile_slug,
        "--name",
        name,
        *(["--force"] if args.get("force") else []),
        # Jobs started in this turn are digested together in the agent's chat.
        "--batch",
        ctx.turn_id,
        "--",
        prompt,
    )


# Pump tools (dv-a9b0): a sandboxed board agent (e.g. `app`) cannot run
# scripts/taskpump in its bash box, so status / pending / answer are tools,
# pinned like job_start(task_id) to the caller's own board.
_PUMP_ID_RE = re.compile(r"^([a-z][a-z0-9-]{0,15}:)?[a-z]{2,8}-[0-9a-z]{4,}$")


@_spec(
    "pump_status",
    "What the task pipeline is doing on YOUR board right now: one line per "
    "task in it (state, phase, running step). For 'is my task running?'.",
    tool_schema({}),
    available=_jobs_available,
)
async def pump_status(
    services: AgentServices, ctx: TurnContext, _args: Dict[str, Any]
) -> Dict[str, Any]:
    board, err = await _caller_board(ctx)
    if err:
        return err
    return await _run_script(ctx, "taskpump", "status", "--board", board)


@_spec(
    "pump_pending",
    "The task-pipeline questions on YOUR board still waiting for the "
    "owner: each task id with its last question card. Call it before "
    "pump_answer to match a reply to the right task.",
    tool_schema({}),
    available=_jobs_available,
)
async def pump_pending(
    services: AgentServices, ctx: TurnContext, _args: Dict[str, Any]
) -> Dict[str, Any]:
    board, err = await _caller_board(ctx)
    if err:
        return err
    return await _run_script(ctx, "taskpump", "pending", "--board", board)


@_spec(
    "pump_answer",
    "Record the owner's answer to a task-pipeline question on YOUR board; "
    "the task then resumes (build starts). text = the owner's reply "
    "VERBATIM — never paraphrase, translate or summarise it, the build job "
    "reads exactly these words. If several tasks are pending and the reply "
    "names none, ask the owner which one instead of picking.",
    tool_schema({"task_id": str, "text": str}, "task_id"),
    available=_jobs_available,
)
async def pump_answer(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    task_id = (args.get("task_id") or "").strip()
    text = args.get("text") or ""
    board, err = await _caller_board(ctx)
    if err:
        return err
    if not _PUMP_ID_RE.match(task_id):
        return _err("task_id must look like dv-xxxx (as pump_pending prints it)")
    if not text.strip():
        return _err("text is required: the owner's reply, verbatim")
    full, err = _on_board(board, task_id)
    if err:
        return err
    logger.info("agent tool pump_answer", profile=ctx.profile_slug, task=full)
    # The owner's words after `--`: no reply text can be read as an option.
    return await _run_script(ctx, "taskpump", "answer", full, "--", text)


@_spec(
    "jobs_refresh_pin",
    "Re-render the pinned 'jobs running now' status post. Call this after "
    "you notice a job finished (e.g. after job_list) so the pin stops "
    "advertising work that is already done.",
    tool_schema({}),
    available=_jobs_available,
)
async def jobs_refresh_pin(
    services: AgentServices, ctx: TurnContext, _args: Dict[str, Any]
) -> Dict[str, Any]:
    await _refresh_jobs_pin(services, ctx)
    return _text({"status": "refreshed"})


@_spec(
    "job_list",
    "List detached background jobs, one compact line each: all RUNNING "
    "plus the last 8 finished (all=true for every job ever). Use this to "
    "answer 'are the agents still working?' instead of guessing.",
    tool_schema({"all": bool}),
    available=_jobs_available,
)
async def job_list(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if args.get("all"):
        return await _bgjob(services, ctx, "list", "--compact")
    return await _bgjob(services, ctx, "list", "--compact", "--limit", "8")


@_spec(
    "job_output",
    "Read a job's result by id. Default: its short <summary> (<=15 lines). "
    "full=true returns the whole output, tail=N its last N lines; ask for "
    "those only when the summary is not enough.",
    tool_schema({"id": str, "tail": int, "full": bool}, "id"),
    available=_jobs_available,
)
async def job_output(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    jid = (args.get("id") or "").strip()
    if not jid:
        return _err("id is required")
    argv = ["output"]
    if args.get("tail"):
        argv += ["--tail", str(int(args["tail"]))]
    elif args.get("full"):
        argv += ["--full"]
    # Same reasoning as job_start: the model-supplied id goes after `--`, so
    # it can only ever be the positional argument.
    argv += ["--", jid]
    return await _bgjob(services, ctx, *argv)


@_spec(
    "remember",
    "Save one durable fact about the user to long-term memory RIGHT NOW, "
    "in the moment it comes up. Use it for things that stay true across "
    "sessions (preferences, constraints, allergies, goals, names, "
    "recurring routines) — not for this turn's scratch state and not for "
    "anything you can re-read from a file. One fact per call, stated in "
    "full so it makes sense with no conversation around it "
    "(good: 'Anna is allergic to walnuts'; bad: 'she is allergic'). "
    "tier: 'pinned' = a standing constraint that must never be violated, "
    "in ANY domain — safety/health limits, privacy and disclosure rules "
    "(what must never be published or shown), and standing 'never do X' / "
    "'always do X' instructions. If violating the fact would harm the user "
    "or break a promise to them, it is pinned, however it was phrased. "
    "'core' = a hard identity fact the agent must always know: age/body "
    "metrics, goals, training availability, the current agreed plan, "
    "standing defaults. Core facts change only when the user's life "
    "changes — never day to day. "
    "'general' (the default) = soft preferences, history, status, project "
    "notes; only the newest are injected, the rest reachable via recall. "
    "Pinned and core facts enter EVERY future turn, so use them for what "
    "matters and nothing else. scope: 'private' (default, only this user) or 'family' "
    "(shared with the user's household; silently downgraded to private if "
    "they have no family). You may NOT choose whose memory this lands in: "
    "it is always the current session's own. Saying 'I'll remember that' "
    "without calling this tool is a lie — a separate batch process may "
    "catch the fact hours later, or may not.",
    # Only 'fact' is required. 'tier' especially must stay optional: it
    # decides whether the fact becomes a standing constraint injected into
    # every future turn, and a forced choice is a guessed one.
    tool_schema({"fact": str, "tier": str, "scope": str, "label": str}, "fact"),
    available=_memory_write_available,
)
async def remember(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    # Guarded at mount time too; kept so a mis-wire fails loudly instead of
    # reporting a write that never happened.
    if not services.memory_writer or not ctx.memory_agent_id:
        return _err("memory is not available in this session")
    # Every field goes through _str_arg: the declared schema says `str`,
    # but nothing stops a model emitting a dict/list/number there, and an
    # AttributeError escaping this handler is exactly the silent
    # "confirmed but never written" outcome this tool exists to avoid.
    fact = _str_arg(args, "fact")
    if not fact:
        return _err("fact is required")
    if len(fact) > MAX_FACT_CHARS:
        return _err(f"fact must be at most {MAX_FACT_CHARS} characters")
    tier = _str_arg(args, "tier").lower() or "general"
    if tier not in VALID_TIERS:
        return _err(f"tier must be one of {sorted(VALID_TIERS)}")
    label = _str_arg(args, "label") or None

    # `scope` is a SELECTOR between two server-derived scopes, never a
    # scope identifier. Anything that isn't the literal "family" -- an
    # agent_id, another user's slug, junk -- falls through to the
    # session's own private scope. And "family" only resolves when this
    # session actually has a family scope; otherwise it is coerced
    # private, exactly as the evaluator's validator does (allow_family).
    want_family = _str_arg(args, "scope").lower() == "family"
    target = (
        ctx.family_agent_id
        if want_family and ctx.family_agent_id
        else ctx.memory_agent_id
    )

    writer = services.memory_writer
    try:
        applied = await writer.apply_ops(
            ctx.memory_agent_id,
            [
                {
                    "op": "add",
                    # In-session captures are user facts by definition;
                    # conversation results reach memory via the evaluator.
                    "kind": "user_fact",
                    "tier": tier,
                    "content": fact,
                    "label": label,
                    "scope": "family" if target != ctx.memory_agent_id else "private",
                }
            ],
            family_agent_id=ctx.family_agent_id,
        )
    except Exception as exc:
        logger.warning(
            "agent tool remember failed", profile=ctx.profile_slug, error=str(exc)
        )
        return _err(str(exc))

    # Per-run outcome from ApplyResult (dv-c5d9 decision 4), not a diff of
    # process-lifetime counters — the old diff misattributed a concurrent
    # writer's rejection to this call.
    status = (
        "saved"
        if applied.added
        else ("duplicate" if applied.dropped_duplicate else "failed")
    )
    # One line per call, whatever the outcome. tool_calls already records
    # that the call happened; this records what it did, so "the model
    # called remember and nothing landed" is visible instead of silent.
    logger.info(
        "agent tool remember",
        profile=ctx.profile_slug,
        agent_id=target,
        tier=tier,
        status=status,
        chars=len(fact),
    )
    if status == "failed":
        return _err("the fact could not be saved; do not tell the user it was")
    return _text(
        {
            "status": status,
            "scope": "family" if target != ctx.memory_agent_id else "private",
            "tier": tier,
            "detail": (
                "already known, nothing written"
                if status == "duplicate"
                else "stored in long-term memory"
            ),
        }
    )


@_spec(
    "recall",
    "Search your long-term FACT memory explicitly -- for facts the "
    "automatic per-turn injection did not surface (it is capped and "
    "picks what seems relevant to THIS turn; this searches everything). "
    "Use whenever the user refers to something you can't find in the "
    "current context. 'query': free-text keywords (required). 'limit': "
    "max facts to return (default 10, max 30). Searches your private "
    "facts and, if you have one, your family's shared facts. "
    "'include_history': true additionally returns invalidated facts with "
    "their validity window — use for 'when did this change' / 'what did "
    "it used to be' questions.",
    tool_schema({"query": str, "limit": int, "include_history": bool}, "query"),
    available=_memory_read_available,
)
async def recall(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if not services.memory_repo or not ctx.memory_agent_id:
        return _err("memory is not available in this session")
    query = _str_arg(args, "query")
    if not query:
        return _err("query is required")
    limit = _int_arg(args, "limit", RECALL_DEFAULT_LIMIT, RECALL_MAX_LIMIT)
    # Same defensiveness as _str_arg: the declared bool may arrive as a
    # string ("true") or junk; anything unrecognised means False.
    raw_hist = args.get("include_history")
    include_history = raw_hist is True or (
        isinstance(raw_hist, str) and raw_hist.strip().lower() in {"1", "true", "yes"}
    )

    hits = [
        (fact, "private")
        for fact in await services.memory_repo.search(
            ctx.memory_agent_id, query, limit, include_invalid=include_history
        )
    ]
    if ctx.family_agent_id:
        hits += [
            (fact, "family")
            for fact in await services.memory_repo.search(
                ctx.family_agent_id,
                query,
                limit,
                include_invalid=include_history,
            )
        ]
    hits = hits[:limit]

    logger.info(
        "agent tool recall",
        profile=ctx.profile_slug,
        query_chars=len(query),
        hits=len(hits),
        include_history=include_history,
    )

    def _iso(dt: Any) -> Optional[str]:
        return dt.isoformat() if dt else None

    return _text(
        [
            {
                "content": fact.content,
                "tier": fact.tier,
                "label": fact.label,
                "scope": scope,
                "kind": fact.kind,
                "valid_from": _iso(fact.valid_from),
                "invalid_at": _iso(fact.invalid_at),
                "source_ref": fact.source_ref,
            }
            for fact, scope in hits
        ]
    )


@_spec(
    "recall_conversation",
    "Keyword search over your RAW past conversation with this user in "
    "this workspace -- messages that rotated out of context, not just "
    "extracted facts. Use for 'what did we say about X', 'you mentioned "
    "this before', anything `recall` doesn't surface. 'query': free-text "
    "keywords (required); matched as a forgiving prefix search, so "
    "partial words still hit. 'limit': max hits (default 5, max 20). "
    "Returns truncated snippets, newest first.",
    tool_schema({"query": str, "limit": int}, "query"),
    available=_conversation_available,
)
async def recall_conversation(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if not services.message_repo or ctx.user_id is None:
        return _err("conversation search is not available in this session")
    query = _str_arg(args, "query")
    if not query:
        return _err("query is required")
    limit = _int_arg(
        args,
        "limit",
        RECALL_CONVERSATION_DEFAULT_LIMIT,
        RECALL_CONVERSATION_MAX_LIMIT,
    )

    hits = await services.message_repo.search_fts(
        ctx.user_id, ctx.working_dir, query, limit
    )
    logger.info(
        "agent tool recall_conversation",
        profile=ctx.profile_slug,
        query_chars=len(query),
        hits=len(hits),
    )
    return _text(
        [
            {
                "timestamp": hit.timestamp.isoformat() if hit.timestamp else None,
                "prompt": hit.prompt_snippet,
                "response": hit.response_snippet,
                "window_id": hit.window_id,
            }
            for hit in hits
        ]
    )


# ------------------------------------------------------------------
# Memory audit tools (dv-a9e1): list / forget / correct. Same scope
# closure as remember/recall — fact ids the model passes are validated
# server-side to belong to this session's own scopes before any write.
# ------------------------------------------------------------------


def _fact_json(fact: Any, scope: str) -> Dict[str, Any]:
    def _iso(dt: Any) -> Optional[str]:
        return dt.isoformat() if dt else None

    return {
        "id": fact.id,
        "content": fact.content,
        "tier": fact.tier,
        "kind": fact.kind,
        "label": fact.label,
        "scope": scope,
        "status": fact.status,
        "valid_from": _iso(fact.valid_from),
        "invalid_at": _iso(fact.invalid_at),
        "invalidated_by": fact.invalidated_by,
        "superseded_by": fact.superseded_by,
    }


async def _own_fact(
    services: AgentServices, ctx: TurnContext, fact_id: int
) -> tuple[Any, str] | tuple[None, None]:
    """Resolve a fact id inside this session's scopes only.

    (fact, "private"|"family") or (None, None). An id belonging to any
    other scope is indistinguishable from a nonexistent one — the
    dv-6dbc boundary, same as remember's scope selector.
    """
    fact = await services.memory_repo.get_fact(fact_id, ctx.memory_agent_id)
    if fact is not None:
        return fact, "private"
    if ctx.family_agent_id:
        fact = await services.memory_repo.get_fact(fact_id, ctx.family_agent_id)
        if fact is not None:
            return fact, "family"
    return None, None


@_spec(
    "memory_list",
    "List everything in your long-term FACT memory for this user — the "
    "answer to 'what do you remember about me?'. With no arguments: every "
    "ACTIVE fact (private and, if any, family scope) with its id, tier, "
    "kind and date — show the user contents and dates, keep the ids for "
    "your own follow-up calls. With 'fact_id': that fact's full history — "
    "the supersede chain with validity windows ('you said X until 04.08, "
    "then Y'), including closed rows. Use the ids it returns with "
    "memory_forget / memory_correct.",
    # No required params — the no-argument call IS the primary use ("what
    # do you remember about me"). Requiring fact_id meant inventing one.
    tool_schema({"fact_id": int}),
    available=_memory_read_available,
)
async def memory_list(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if not services.memory_repo or not ctx.memory_agent_id:
        return _err("memory is not available in this session")
    fact_id = _id_arg(args, "fact_id")
    if fact_id is not None:
        chain = await services.memory_repo.get_fact_history(
            fact_id, ctx.memory_agent_id
        )
        scope = "private"
        if not chain and ctx.family_agent_id:
            chain = await services.memory_repo.get_fact_history(
                fact_id, ctx.family_agent_id
            )
            scope = "family"
        if not chain:
            return _err(
                "no fact with that id in your memory — "
                "call memory_list without arguments first"
            )
        logger.info(
            "agent tool memory_list history",
            profile=ctx.profile_slug,
            fact_id=fact_id,
            chain_length=len(chain),
        )
        return _text([_fact_json(f, scope) for f in chain])

    listed = [
        (fact, "private")
        for fact in await services.memory_repo.get_facts(ctx.memory_agent_id)
    ]
    if ctx.family_agent_id:
        listed += [
            (fact, "family")
            for fact in await services.memory_repo.get_facts(ctx.family_agent_id)
        ]
    logger.info(
        "agent tool memory_list",
        profile=ctx.profile_slug,
        facts=len(listed),
    )
    return _text([_fact_json(fact, scope) for fact, scope in listed])


@_spec(
    "memory_forget",
    "Close one fact in long-term memory because the user asked you to "
    "forget it ('forget that', 'забудь это'). Takes the fact's id from "
    "memory_list. The row is CLOSED, never deleted — it drops out of "
    "every future injection but stays in history, so this is reversible "
    "and auditable; say 'closed', not 'erased', if asked. If the user "
    "states a REPLACEMENT ('that's wrong, it's Y'), use memory_correct "
    "instead. Saying 'forgotten' without calling this tool is a lie.",
    tool_schema({"fact_id": int, "reason": str}, "fact_id"),
    available=_memory_audit_available,
)
async def memory_forget(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if (
        not services.audit_memory_writer
        or not services.memory_repo
        or not ctx.memory_agent_id
    ):
        return _err("memory is not available in this session")
    fact_id = _id_arg(args, "fact_id")
    if fact_id is None:
        return _err("fact_id is required — get it from memory_list")
    fact, scope = await _own_fact(services, ctx, fact_id)
    if fact is None:
        return _err(
            "no fact with that id in your memory — "
            "call memory_list and use an id it returned"
        )
    if fact.invalid_at is not None:
        return _text(
            {
                "status": "already_closed",
                "id": fact.id,
                "content": fact.content,
                "detail": "this fact was already closed; nothing changed",
            }
        )
    reason = _str_arg(args, "reason") or None
    applied = await services.audit_memory_writer.apply_ops(
        ctx.memory_agent_id,
        [{"op": "invalidate", "target_id": fact_id, "scope": scope}],
        family_agent_id=ctx.family_agent_id,
    )
    logger.info(
        "agent tool memory_forget",
        profile=ctx.profile_slug,
        fact_id=fact_id,
        scope=scope,
        reason=reason,
        applied=bool(applied),
    )
    if not applied:
        return _err(
            "the fact could not be closed; do not tell the user it was " "forgotten"
        )
    return _text(
        {
            "status": "forgotten",
            "id": fact.id,
            "content": fact.content,
            "scope": scope,
            "detail": "closed, not deleted — history is preserved",
        }
    )


@_spec(
    "memory_correct",
    "Replace one wrong fact in long-term memory with what the user says "
    "is right ('that's wrong, it's Y'). Takes the old fact's id from "
    "memory_list plus the corrected statement, written in full so it "
    "stands alone with no conversation around it. The old row is closed "
    "and linked to the new one — no text is ever merged, the history "
    "stays visible. For 'forget it' with no replacement use "
    "memory_forget; for a brand-new fact use remember. Saying 'corrected' "
    "without calling this tool is a lie.",
    tool_schema({"fact_id": int, "new_fact": str}, "fact_id", "new_fact"),
    available=_memory_audit_available,
)
async def memory_correct(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    if (
        not services.audit_memory_writer
        or not services.memory_repo
        or not ctx.memory_agent_id
    ):
        return _err("memory is not available in this session")
    fact_id = _id_arg(args, "fact_id")
    if fact_id is None:
        return _err("fact_id is required — get it from memory_list")
    new_fact = _str_arg(args, "new_fact")
    if not new_fact:
        return _err("new_fact is required")
    if len(new_fact) > MAX_FACT_CHARS:
        return _err(f"new_fact must be at most {MAX_FACT_CHARS} characters")
    fact, scope = await _own_fact(services, ctx, fact_id)
    if fact is None:
        return _err(
            "no fact with that id in your memory — "
            "call memory_list and use an id it returned"
        )
    if fact.invalid_at is not None:
        return _err(
            "that fact is already closed — save the new statement with "
            "remember instead"
        )
    writer = services.audit_memory_writer
    applied = await writer.apply_ops(
        ctx.memory_agent_id,
        [
            {
                "op": "supersede",
                "supersedes_id": fact_id,
                # Tier/kind/label carry over: a correction changes what
                # the fact SAYS, not what kind of fact it is.
                "tier": fact.tier,
                "kind": fact.kind,
                "label": fact.label,
                "content": new_fact,
                "scope": scope,
            }
        ],
        family_agent_id=ctx.family_agent_id,
    )
    deduped = applied.dropped_duplicate > 0
    logger.info(
        "agent tool memory_correct",
        profile=ctx.profile_slug,
        fact_id=fact_id,
        scope=scope,
        applied=bool(applied),
        noop=deduped,
    )
    if not applied:
        if deduped:
            return _text(
                {
                    "status": "unchanged",
                    "id": fact.id,
                    "detail": (
                        "the correction says the same thing as the stored "
                        "fact; nothing changed"
                    ),
                }
            )
        return _err(
            "the correction could not be applied; do not tell the user " "it was"
        )
    return _text(
        {
            "status": "corrected",
            "old_id": fact.id,
            "old_content": fact.content,
            "new_content": new_fact,
            "scope": scope,
            "detail": "old fact closed and linked; new fact saved verbatim",
        }
    )


# Every tool both transports can offer, in mount order: platform + jobs +
# memory first, then the profile's data.db tools, then Hireli, then the
# exchange tools. Built at import time, after every decorator above has run.
ALL_TOOL_SPECS: List[ToolSpec] = (
    AGENT_TOOL_SPECS
    + ITEM_SPECS
    + USER_DATA_READ_SPECS
    + USER_DATA_WRITE_SPECS
    + HIRELI_SPECS
    + EXCHANGE_SPECS
    + ONCHAIN_SPECS
)


def available_tool_specs(services: AgentServices, ctx: TurnContext) -> List[ToolSpec]:
    """The tools this context may see — the ONE filter, shared by both
    transports.

    Both the in-process SDK server and the HTTP bridge list exactly what this
    returns, so a tool can never be visible on one backend and hidden on the
    other for the same user.
    """
    return [spec for spec in ALL_TOOL_SPECS if spec.available(services, ctx)]


def build_agent_mcp_server(
    services: AgentServices,
    profile_slug: str,
    working_directory: Path,
    requester_chat_id: Optional[int] = None,
    memory_agent_id: Optional[str] = None,
    memory_family_agent_id: Optional[str] = None,
    user_id: Optional[int] = None,
    role: Optional[str] = None,
) -> Any:
    """Build the in-process ``agent`` MCP server for one turn.

    The arguments are this turn's :class:`TurnContext` spelled out; see that
    class for what each field means and why every one of them is server
    context rather than a tool parameter (dv-6dbc). They are resolved by the
    caller (``sdk_integration._agent_mcp_server_for``) and bound here, once,
    to every tool the context is allowed to see.
    """
    ctx = TurnContext(
        profile_slug=profile_slug,
        working_dir=working_directory,
        requester_chat_id=requester_chat_id,
        memory_agent_id=memory_agent_id,
        family_agent_id=memory_family_agent_id,
        user_id=user_id,
        role=role,
    )
    specs = available_tool_specs(services, ctx)
    data_specs = [spec for spec in specs if spec.group]
    if data_specs:
        logger.info(
            "data tools mounted",
            profile=profile_slug,
            groups=sorted({spec.group for spec in data_specs if spec.group}),
            tools=[spec.name for spec in data_specs],
        )
    tools = [sdk_tool(spec, services, ctx) for spec in specs]
    return create_sdk_mcp_server(name="agent", version="1.0.0", tools=tools)
