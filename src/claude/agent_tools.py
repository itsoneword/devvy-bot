"""In-process MCP tools exposing schedule / notify / task capabilities to agents.

These are **SDK MCP tools** — they execute inside the bot process, OUTSIDE the
Bash sandbox. That is the whole point: an agent's Bash tool is network- and
filesystem-isolated (bwrap), so the HTTP ``agentctl`` path to ``127.0.0.1:8080``
gets ``connection refused``. These tools call the scheduler / task repo / event
bus objects directly — no localhost round-trip, no sandbox to escape.

The tools are built per turn by :func:`build_agent_mcp_server`, capturing the
calling profile's slug and working directory so ``schedule``/``task`` default
their target/attribution to the current agent.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from claude_agent_sdk import create_sdk_mcp_server, tool

from ..evaluator.validator import MAX_CONTENT_LENGTH, VALID_TIERS
from ..events.types import AgentResponseEvent
from .tool_schema import tool_schema
from .weather_tool import get_weather_for

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


def _parse_dt(value: str) -> datetime:
    """Parse an ISO datetime; assume UTC when no offset is given."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


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
    """Build the ``agent`` in-process MCP server for one turn.

    ``profile_slug`` / ``working_directory`` identify the calling agent so the
    tools can default job targets and task attribution to it.

    ``requester_chat_id`` is the Telegram chat that initiated this turn. It is
    server context, deliberately NOT a tool parameter — the model must not be
    able to address arbitrary chats. ``notify`` stamps it on the published
    event and ``schedule_add`` persists it as the job's audience; when absent
    (webhook-triggered runs), delivery falls back to the configured
    ``NOTIFICATION_CHAT_IDS`` broadcast list.

    ``memory_agent_id`` / ``memory_family_agent_id`` are the *already resolved*
    memory scopes for this session (``{slug}`` / ``{slug}#u{user_id}`` and
    ``f{family_id}:{slug}``), computed by the caller from the working
    directory, user id and family id. Same rule as ``requester_chat_id``, and
    it is the one that matters most here: ``remember``/``recall`` must never
    accept a scope identifier as a tool argument, or any profile could
    read/write another user's store — the boundary dv-6dbc closed. When
    ``memory_agent_id`` is None, ``remember``/``recall`` are not mounted.

    ``user_id`` is the raw Telegram/DB user id driving this turn -- server
    context, same rule again. It scopes ``recall_conversation`` (joined
    through ``sessions.project_path``, since ``messages`` has no
    ``working_dir`` column); the model never supplies it. When ``user_id`` is
    None (e.g. some webhook/scheduler callers with no resolved human), the
    tool is not mounted rather than searching unscoped. ``schedule_add`` also
    stamps it as the new job's ``created_by``, so the scheduler can resolve
    the job's real owner at fire time instead of always running as the bot's
    default user (dv-3e91).

    ``role`` is the resolved user role for this turn — server context, same
    rule again. It gates the background-job tools (``job_start`` /
    ``job_list`` / ``job_output`` / ``jobs_refresh_pin``): they are mounted
    only for ``role == "owner"`` and additionally refuse at invocation time
    for any other role (dv-a900). Defaulting to None means every caller
    that does not explicitly resolve a role fails closed.
    """

    def _resolve_working_dir(profile: Optional[str]) -> Optional[Path]:
        if not profile:
            return working_directory
        if not services.profile_registry:
            return working_directory
        p = services.profile_registry.get_by_slug(profile)
        return p.working_dir if p else working_directory

    @tool(
        "notify",
        "Send the user a Telegram message right now. Use for proactive pings, "
        "reminders firing, or surfacing something the user should see immediately.",
        tool_schema({"text": str}, "text"),
    )
    async def notify(args: Dict[str, Any]) -> Dict[str, Any]:
        text = (args.get("text") or "").strip()
        if not text:
            return _err("text is required")
        # Requester chat comes from server context only; a model-supplied
        # chat_id argument is ignored so agents cannot address other chats.
        chat_id = requester_chat_id or 0
        event = AgentResponseEvent(chat_id=int(chat_id), text=text, parse_mode=None)
        await services.event_bus.publish(event)
        logger.info("agent tool notify", profile=profile_slug, chat_id=chat_id)
        return _text({"status": "sent", "event_id": event.id})

    @tool(
        "schedule_add",
        "Register a scheduled job that survives restarts. Provide exactly one of "
        "'cron' (crontab syntax, server time is UTC) or 'at' (ISO datetime, UTC "
        "unless offset given). Two delivery modes: deliver='message' sends the "
        "prompt text to the user VERBATIM when it fires — no agent turn, so "
        "write the final user-facing message in the user's language, as if you "
        "were sending it yourself at that moment. deliver='agent' (default) "
        "runs the prompt as an agent turn in the target profile first. Prefer "
        "'at' + deliver='message' for one-shot reminders (cheap, keeps the "
        "conversation cache warm); use deliver='agent' only when the firing "
        "turn must look at fresh data before writing to the user. 'cron' for "
        "routines.",
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
    async def schedule_add(args: Dict[str, Any]) -> Dict[str, Any]:
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
        run_at: Optional[datetime] = None
        if at_raw:
            try:
                run_at = _parse_dt(at_raw)
            except ValueError:
                return _err(f"could not parse 'at' datetime: {at_raw!r}")
            if run_at <= datetime.now(UTC):
                return _err("'at' is in the past")
        target = _resolve_working_dir(args.get("profile") or profile_slug)
        try:
            job_id = await services.scheduler.add_job(
                job_name=name,
                cron_expression=cron,
                prompt=prompt,
                target_chat_ids=([requester_chat_id] if requester_chat_id else None),
                working_directory=target,
                run_at=run_at,
                delivery=deliver,
                created_by=user_id or 0,
            )
        except ValueError as exc:
            return _err(str(exc))
        logger.info("agent tool schedule_add", profile=profile_slug, job_id=job_id)
        return _text({"status": "scheduled", "job_id": job_id})

    @tool(
        "schedule_list",
        "List all scheduled jobs (cron and one-shot) with their next run times.",
        tool_schema({}),
    )
    async def schedule_list(args: Dict[str, Any]) -> Dict[str, Any]:
        if not services.scheduler:
            return _err("scheduler not enabled")
        return _text(await services.scheduler.list_jobs())

    @tool(
        "schedule_remove",
        "Remove a scheduled job by its job_id (from schedule_list).",
        tool_schema({"job_id": str}, "job_id"),
    )
    async def schedule_remove(args: Dict[str, Any]) -> Dict[str, Any]:
        if not services.scheduler:
            return _err("scheduler not enabled")
        job_id = (args.get("job_id") or "").strip()
        if not job_id:
            return _err("job_id is required")
        removed = await services.scheduler.remove_job(job_id)
        return _text(
            {"status": "removed" if removed else "not_found", "job_id": job_id}
        )

    @tool(
        "task_add",
        "Add an item to the task board (one board, shared by every agent and "
        "the user; ids look like dv-3f21). Optionally set a due datetime (ISO, "
        "UTC unless offset given) and an assignee profile slug.",
        # 'due' optional: a required due date is a deadline nobody chose —
        # three board tasks still share an invented 2026-08-22T12:00:00Z.
        tool_schema({"title": str, "body": str, "assignee": str, "due": str}, "title"),
    )
    async def task_add(args: Dict[str, Any]) -> Dict[str, Any]:
        title = (args.get("title") or "").strip()
        if not title:
            return _err("title is required")
        due_at: Optional[datetime] = None
        due_raw = (args.get("due") or "").strip() or None
        if due_raw:
            try:
                due_at = _parse_dt(due_raw)
            except ValueError:
                return _err(f"could not parse 'due' datetime: {due_raw!r}")
        task_id = await services.task_repo.add_task(
            title=title,
            body=(args.get("body") or "").strip() or None,
            created_by=profile_slug,
            assignee=(args.get("assignee") or "").strip() or None,
            due_at=due_at,
            # The origin profile as a label so health/ops items can be filtered
            # off the engineering board (dv-1327).
            labels=[profile_slug.split("#", 1)[0]],
        )
        logger.info("agent tool task_add", profile=profile_slug, task_id=task_id)
        return _text({"status": "created", "task_id": task_id})

    @tool(
        "task_list",
        "List task board items. status is one of open (default), done, all.",
        tool_schema({"status": str}),
    )
    async def task_list(args: Dict[str, Any]) -> Dict[str, Any]:
        status = (args.get("status") or "open").strip()
        if status not in ("open", "done", "all"):
            return _err("status must be open|done|all")
        tasks = await services.task_repo.list_tasks(
            status=None if status == "all" else status
        )
        return _text([t.to_dict() for t in tasks])

    @tool(
        "task_done",
        "Mark a task board item done by its task_id (e.g. dv-3f21).",
        tool_schema({"task_id": str}, "task_id"),
    )
    async def task_done(args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return _err("task_id is required (e.g. dv-3f21)")
        ok = await services.task_repo.set_status(task_id, "done")
        return _text({"status": "updated" if ok else "not_found", "task_id": task_id})

    @tool(
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
    async def get_weather(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = await get_weather_for(args.get("location"), args.get("date"))
        except Exception as exc:
            logger.warning("agent tool get_weather failed", error=str(exc))
            return _err(str(exc))
        return _text(result)

    async def _bgjob(*argv: str) -> Dict[str, Any]:
        """Run scripts/bgjob out-of-sandbox and return its output.

        The profile is pinned to the CALLER's slug server-side and is never a
        tool parameter, so an agent cannot start a job in another profile's
        workspace. bgjob then confines the job's writes (bubblewrap) to that
        profile's working dir.
        """
        # Invocation-time role gate (dv-a900), independent of the mount-time
        # filter below: even if a job tool ever ends up mounted in a
        # non-owner session (mis-wire, stale tool reference), the call still
        # refuses here. Deliberately a literal comparison, not
        # _job_tools_allowed(), so the two gates cannot be disabled together.
        if role != "owner":
            logger.warning(
                "bgjob invocation refused: non-owner role",
                profile=profile_slug,
                role=role,
            )
            return _err("background jobs are not available in this session")
        script = Path(__file__).resolve().parents[2] / "scripts" / "bgjob"
        if not script.exists():
            return _err(f"bgjob not found at {script}")
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
            return _err(text or f"bgjob exited {proc.returncode}")
        return _text(text)

    async def _refresh_jobs_pin() -> None:
        """Re-render the pinned 'jobs running now' status post.

        Sends empty text when nothing is running, which retires the pin. Best
        effort: a failure here must never break starting a job.
        """
        if services.event_bus is None:
            return
        try:
            listing = await _bgjob("list")
            if listing.get("isError"):
                return
            lines = [
                ln
                for ln in listing["content"][0]["text"].splitlines()
                if "RUNNING" in ln
            ]
            if lines:
                body = "\n".join(f"• {ln.strip()}" for ln in lines)
                text = f"⏳ Background jobs running ({len(lines)})\n{body}"
            else:
                text = ""
            await services.event_bus.publish(
                AgentResponseEvent(
                    chat_id=int(requester_chat_id or 0),
                    text=text,
                    parse_mode=None,
                    pin_as="jobs",
                )
            )
        except Exception as exc:
            logger.warning("jobs pin refresh failed", error=str(exc))

    @tool(
        "job_start",
        "Start a DETACHED background job for work too long for one turn "
        "(>5 min: deep research, big audits, bulk processing). Unlike a "
        "backgrounded sub-agent — which is always terminated the moment you "
        "reply — this survives the turn AND a bot restart, and messages the "
        "user when it finishes. The job runs in YOUR profile's workspace and "
        "can only write there. Give it a self-contained prompt: it starts "
        "fresh with no memory of this conversation. Tell the user it is "
        "running and stop; do not wait for it.",
        tool_schema({"prompt": str, "name": str}, "prompt"),
    )
    async def job_start(args: Dict[str, Any]) -> Dict[str, Any]:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return _err("prompt is required")
        name = (args.get("name") or "job").strip() or "job"
        logger.info("agent tool job_start", profile=profile_slug, name=name)
        # The start ping and pin refresh are sent by `scripts/bgjob run`
        # itself (commit 3d483b7) -- NOT here. They used to be duplicated:
        # this tool published its own start notification AND _bgjob() below
        # shelled out to a script that, as of that commit, also sends one.
        # Every launch through this tool was silently double-pinging. Single
        # source of truth now lives in the runner so it fires identically
        # regardless of entry point (this tool, a shell, cron).
        return await _bgjob("run", prompt, "--profile", profile_slug, "--name", name)

    @tool(
        "jobs_refresh_pin",
        "Re-render the pinned 'jobs running now' status post. Call this after "
        "you notice a job finished (e.g. after job_list) so the pin stops "
        "advertising work that is already done.",
        tool_schema({}),
    )
    async def jobs_refresh_pin(_args: Dict[str, Any]) -> Dict[str, Any]:
        await _refresh_jobs_pin()
        return _text({"status": "refreshed"})

    @tool(
        "job_list",
        "List detached background jobs and their state (RUNNING / done / "
        "failed) with output size. Use this to answer 'are the agents still "
        "working?' instead of guessing.",
        tool_schema({}),
    )
    async def job_list(_args: Dict[str, Any]) -> Dict[str, Any]:
        return await _bgjob("list")

    @tool(
        "job_output",
        "Read a finished (or partial) job's output by id. Output is streamed "
        "to disk as the job runs, so this returns useful text even if the job "
        "died midway.",
        tool_schema({"id": str, "tail": int}, "id"),
    )
    async def job_output(args: Dict[str, Any]) -> Dict[str, Any]:
        jid = (args.get("id") or "").strip()
        if not jid:
            return _err("id is required")
        argv = ["output", jid]
        if args.get("tail"):
            argv += ["--tail", str(int(args["tail"]))]
        return await _bgjob(*argv)

    @tool(
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
    )
    async def remember(args: Dict[str, Any]) -> Dict[str, Any]:
        # Guarded at mount time too; kept so a mis-wire fails loudly instead of
        # reporting a write that never happened.
        if not services.memory_writer or not memory_agent_id:
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
            memory_family_agent_id
            if want_family and memory_family_agent_id
            else memory_agent_id
        )

        writer = services.memory_writer
        try:
            applied = await writer.apply_ops(
                memory_agent_id,
                [
                    {
                        "op": "add",
                        # In-session captures are user facts by definition;
                        # conversation results reach memory via the evaluator.
                        "kind": "user_fact",
                        "tier": tier,
                        "content": fact,
                        "label": label,
                        "scope": "family" if target != memory_agent_id else "private",
                    }
                ],
                family_agent_id=memory_family_agent_id,
            )
        except Exception as exc:
            logger.warning(
                "agent tool remember failed", profile=profile_slug, error=str(exc)
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
        # One line per call, whatever the outcome. tool_usage already records
        # that the call happened; this records what it did, so "the model
        # called remember and nothing landed" is visible instead of silent.
        logger.info(
            "agent tool remember",
            profile=profile_slug,
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
                "scope": "family" if target != memory_agent_id else "private",
                "tier": tier,
                "detail": (
                    "already known, nothing written"
                    if status == "duplicate"
                    else "stored in long-term memory"
                ),
            }
        )

    @tool(
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
    )
    async def recall(args: Dict[str, Any]) -> Dict[str, Any]:
        if not services.memory_repo or not memory_agent_id:
            return _err("memory is not available in this session")
        query = _str_arg(args, "query")
        if not query:
            return _err("query is required")
        limit = _int_arg(args, "limit", RECALL_DEFAULT_LIMIT, RECALL_MAX_LIMIT)
        # Same defensiveness as _str_arg: the declared bool may arrive as a
        # string ("true") or junk; anything unrecognised means False.
        raw_hist = args.get("include_history")
        include_history = raw_hist is True or (
            isinstance(raw_hist, str)
            and raw_hist.strip().lower() in {"1", "true", "yes"}
        )

        hits = [
            (fact, "private")
            for fact in await services.memory_repo.search(
                memory_agent_id, query, limit, include_invalid=include_history
            )
        ]
        if memory_family_agent_id:
            hits += [
                (fact, "family")
                for fact in await services.memory_repo.search(
                    memory_family_agent_id,
                    query,
                    limit,
                    include_invalid=include_history,
                )
            ]
        hits = hits[:limit]

        logger.info(
            "agent tool recall",
            profile=profile_slug,
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

    @tool(
        "recall_conversation",
        "Keyword search over your RAW past conversation with this user in "
        "this workspace -- messages that rotated out of context, not just "
        "extracted facts. Use for 'what did we say about X', 'you mentioned "
        "this before', anything `recall` doesn't surface. 'query': free-text "
        "keywords (required); matched as a forgiving prefix search, so "
        "partial words still hit. 'limit': max hits (default 5, max 20). "
        "Returns truncated snippets, newest first.",
        tool_schema({"query": str, "limit": int}, "query"),
    )
    async def recall_conversation(args: Dict[str, Any]) -> Dict[str, Any]:
        if not services.message_repo or user_id is None:
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
            user_id, working_directory, query, limit
        )
        logger.info(
            "agent tool recall_conversation",
            profile=profile_slug,
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

    async def _own_fact(fact_id: int) -> tuple[Any, str] | tuple[None, None]:
        """Resolve a fact id inside this session's scopes only.

        (fact, "private"|"family") or (None, None). An id belonging to any
        other scope is indistinguishable from a nonexistent one — the
        dv-6dbc boundary, same as remember's scope selector.
        """
        fact = await services.memory_repo.get_fact(fact_id, memory_agent_id)
        if fact is not None:
            return fact, "private"
        if memory_family_agent_id:
            fact = await services.memory_repo.get_fact(fact_id, memory_family_agent_id)
            if fact is not None:
                return fact, "family"
        return None, None

    @tool(
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
    )
    async def memory_list(args: Dict[str, Any]) -> Dict[str, Any]:
        if not services.memory_repo or not memory_agent_id:
            return _err("memory is not available in this session")
        fact_id = _id_arg(args, "fact_id")
        if fact_id is not None:
            chain = await services.memory_repo.get_fact_history(
                fact_id, memory_agent_id
            )
            scope = "private"
            if not chain and memory_family_agent_id:
                chain = await services.memory_repo.get_fact_history(
                    fact_id, memory_family_agent_id
                )
                scope = "family"
            if not chain:
                return _err(
                    "no fact with that id in your memory — "
                    "call memory_list without arguments first"
                )
            logger.info(
                "agent tool memory_list history",
                profile=profile_slug,
                fact_id=fact_id,
                chain_length=len(chain),
            )
            return _text([_fact_json(f, scope) for f in chain])

        listed = [
            (fact, "private")
            for fact in await services.memory_repo.get_facts(memory_agent_id)
        ]
        if memory_family_agent_id:
            listed += [
                (fact, "family")
                for fact in await services.memory_repo.get_facts(memory_family_agent_id)
            ]
        logger.info(
            "agent tool memory_list",
            profile=profile_slug,
            facts=len(listed),
        )
        return _text([_fact_json(fact, scope) for fact, scope in listed])

    @tool(
        "memory_forget",
        "Close one fact in long-term memory because the user asked you to "
        "forget it ('forget that', 'забудь это'). Takes the fact's id from "
        "memory_list. The row is CLOSED, never deleted — it drops out of "
        "every future injection but stays in history, so this is reversible "
        "and auditable; say 'closed', not 'erased', if asked. If the user "
        "states a REPLACEMENT ('that's wrong, it's Y'), use memory_correct "
        "instead. Saying 'forgotten' without calling this tool is a lie.",
        tool_schema({"fact_id": int, "reason": str}, "fact_id"),
    )
    async def memory_forget(args: Dict[str, Any]) -> Dict[str, Any]:
        if (
            not services.audit_memory_writer
            or not services.memory_repo
            or not memory_agent_id
        ):
            return _err("memory is not available in this session")
        fact_id = _id_arg(args, "fact_id")
        if fact_id is None:
            return _err("fact_id is required — get it from memory_list")
        fact, scope = await _own_fact(fact_id)
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
            memory_agent_id,
            [{"op": "invalidate", "target_id": fact_id, "scope": scope}],
            family_agent_id=memory_family_agent_id,
        )
        logger.info(
            "agent tool memory_forget",
            profile=profile_slug,
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

    @tool(
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
    )
    async def memory_correct(args: Dict[str, Any]) -> Dict[str, Any]:
        if (
            not services.audit_memory_writer
            or not services.memory_repo
            or not memory_agent_id
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
        fact, scope = await _own_fact(fact_id)
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
            memory_agent_id,
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
            family_agent_id=memory_family_agent_id,
        )
        deduped = applied.dropped_duplicate > 0
        logger.info(
            "agent tool memory_correct",
            profile=profile_slug,
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

    tools = [
        notify,
        schedule_add,
        schedule_list,
        schedule_remove,
        task_add,
        task_list,
        task_done,
        get_weather,
    ]
    # Background-job tools are owner-only (dv-a900): bgjob runs the raw CLI
    # with bypassPermissions, outside every per-turn SDK protection. Guests
    # and unresolved roles get no job tools at all — not mounting them (vs an
    # always-erroring stub) also saves their schema tokens every turn.
    if _job_tools_allowed(role):
        tools += [job_start, job_list, job_output, jobs_refresh_pin]
    # Only offered when a memory scope was actually resolved for this session.
    if services.memory_writer and memory_agent_id:
        tools.append(remember)
    if services.memory_repo and memory_agent_id:
        tools.append(recall)
        tools.append(memory_list)
    # Audit writes additionally need the repo (for the server-side ownership
    # check) and the user_request-stamped writer (dv-a9e1).
    if services.audit_memory_writer and services.memory_repo and memory_agent_id:
        tools.append(memory_forget)
        tools.append(memory_correct)
    # Only offered when a real user id was resolved for this session -- no
    # user id means no safe way to scope the search (dv-r3c1).
    if services.message_repo and user_id is not None:
        tools.append(recall_conversation)
    # data.db tools (dv-5f3a): mounted by the profile's YAML capability list,
    # never by slug. Fail-closed: needs the repo, a resolved user_id, and a
    # profile whose config declares groups. profile_slug may carry a per-user
    # suffix ("coach#u123") — capabilities live on the base profile.
    if services.user_data and user_id is not None and services.profile_registry:
        base_profile = services.profile_registry.get_by_slug(
            profile_slug.split("#", 1)[0]
        )
        data_groups = base_profile.user_data_tools if base_profile else frozenset()
        if data_groups:
            from .user_data_tools import (
                build_user_data_read_tools,
                build_user_data_write_tools,
            )

            data_tools = build_user_data_read_tools(
                services.user_data,
                user_id,
                data_groups,
                working_directory=working_directory,
            ) + build_user_data_write_tools(
                services.user_data,
                user_id,
                data_groups,
                event_bus=services.event_bus,
                profile_slug=profile_slug,
            )
            tools += data_tools
            logger.info(
                "data tools mounted",
                profile=profile_slug,
                groups=sorted(data_groups),
                tools=[t.name for t in data_tools],
            )

    return create_sdk_mcp_server(name="agent", version="1.0.0", tools=tools)
