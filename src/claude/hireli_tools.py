"""Hireli (job-application site) tools, proxied through the bot (dv-9f02).

Hireli serves an MCP endpoint that accepts a *trusted client*: one service key
plus ``X-Telegram-User-Id`` naming the user the call is for. The key is a
trusted-client credential, so it must never reach an agent. Mounting Hireli as a plain HTTP MCP server would put it in the agent
CLI's env or argv, where the agent's Bash can read it (dv-2976).

So the bot is the MCP client. Each Hireli tool is a :class:`ToolSpec` whose
handler POSTs ``tools/call`` from the bot process with the key held in
Settings and the Telegram id taken from ``ctx.user_id`` — server context the
model never supplies (dv-6dbc). Both transports (in-process SDK server and the
Codex bridge) get the same specs.

Mounted for profiles whose YAML sets ``hireli: true``. Until the user links
their Telegram account on the site, ``hireli_whoami`` returns
``linked: false`` with instructions and every other tool errors with the
same text; linking is the model calling ``hireli_link_telegram(code)``.

Hireli validates every call server-side.
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Dict, List, Optional

import httpx
import structlog

from .tool_context import AgentServices, ToolSpec, TurnContext

logger = structlog.get_logger()

# Hireli's prep-coach tools make an LLM call server-side; 120 s covers them.
_TIMEOUT = httpx.Timeout(120.0, connect=5.0)

_PREFIX = "hireli_"


class HireliClient:
    """Minimal MCP client for Hireli's stateless streamable-HTTP endpoint.

    Stateless + JSON responses means no ``initialize`` handshake and no
    session id: every ``tools/call`` is one self-contained POST (verified
    against the live server 2026-09-27).
    """

    def __init__(
        self,
        url: str,
        service_key: str,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._url = url
        self._key = service_key
        self._ids = itertools.count(1)
        self._http = httpx.AsyncClient(timeout=_TIMEOUT, transport=transport)

    async def call(
        self, tool: str, arguments: Dict[str, Any], telegram_user_id: int
    ) -> Dict[str, Any]:
        """Call one Hireli tool as ``telegram_user_id``; SDK tool result shape."""
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "X-Telegram-User-Id": str(telegram_user_id),
            "Accept": "application/json, text/event-stream",
        }
        try:
            resp = await self._http.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("hireli unreachable", tool=tool, error=str(exc))
            return _err(f"Hireli is unreachable ({type(exc).__name__})")
        if resp.status_code != 200:
            # 401/403 = key or trust config wrong; 429 = rate limit. The body
            # is Hireli's own JSON error, safe to show (no secrets in it).
            logger.warning("hireli http error", tool=tool, status=resp.status_code)
            return _err(f"Hireli returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            return _err("Hireli returned a non-JSON response")
        if "error" in payload:
            message = (payload.get("error") or {}).get("message", "unknown error")
            return _err(f"Hireli: {message}")
        result = payload.get("result") or {}
        content = result.get("content") or [{"type": "text", "text": ""}]
        out: Dict[str, Any] = {"content": content}
        if result.get("isError"):
            out["isError"] = True
        return out

    async def aclose(self) -> None:
        await self._http.aclose()


def _err(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


def _schema(
    props: Optional[Dict[str, Dict[str, Any]]] = None, *required: str
) -> Dict[str, Any]:
    return {"type": "object", "properties": props or {}, "required": list(required)}


_STR = {"type": "string"}
_RUN_ID = {
    "type": "string",
    "description": "Application id from hireli_list_applications",
}


def hireli_available(services: AgentServices, ctx: TurnContext) -> bool:
    """Mount only with a client, a resolved sender, and a profile opted in.

    Fail closed: no user_id (webhook/scheduler turns) means no Telegram id to
    send, and sending none would be a request with no owner.
    """
    if services.hireli is None or ctx.user_id is None:
        return False
    if not services.profile_registry:
        return False
    base = services.profile_registry.get_by_slug(ctx.profile_slug.split("#", 1)[0])
    return bool(getattr(base, "hireli", False)) if base else False


def _proxy(remote: str) -> Any:
    async def handler(
        services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        # Re-check, per the ToolSpec contract: `available` is a filter, not
        # the authorization.
        if services.hireli is None or ctx.user_id is None:
            return _err("Hireli is not available in this session")
        client: HireliClient = services.hireli
        return await client.call(remote, dict(args or {}), ctx.user_id)

    return handler


# (remote tool name, description, schema) — descriptions condensed from
# Hireli's own, tool names referenced inside them carry the local prefix.
_TOOLS: List[tuple[str, str, Dict[str, Any]]] = [
    (
        "whoami",
        "Hireli (job-application site) account this Telegram user is linked to. "
        "If `linked` is false, relay the `note` to the user.",
        _schema(),
    ),
    (
        "link_telegram",
        "Link this Telegram user to their Hireli account with the one-time code "
        "from hireli.xyz/account (user sends e.g. `link AB12CD34`; 10-minute TTL).",
        _schema({"code": _STR}, "code"),
    ),
    (
        "list_roles",
        "The user's Hireli roles (target job titles) with the run_ids under each.",
        _schema(),
    ),
    (
        "list_applications",
        "The user's job applications, newest first: company, title, funnel status, "
        "match score, run_id. Filter by status (saved, applied, screening, interview, "
        "offer, rejected, declined) or role_id.",
        _schema({"status": _STR, "role_id": _STR}),
    ),
    (
        "get_application",
        "One application: fit score, top gaps, assessment summary, recommended "
        "actions, prep history size.",
        _schema({"run_id": _RUN_ID}, "run_id"),
    ),
    (
        "get_prep_notes",
        "Interview-prep notes saved on an application.",
        _schema({"run_id": _RUN_ID}, "run_id"),
    ),
    (
        "get_prep_history",
        "Recent interview-prep coach conversation turns for an application.",
        _schema({"run_id": _RUN_ID, "limit": {"type": "integer"}}, "run_id"),
    ),
    (
        "get_career_profile",
        "The user's career profile from their CV: skills, trajectory, level, domain.",
        _schema(),
    ),
    (
        "get_cv_download",
        "One-time 10-minute URL for an application's tailored CV (format: html). "
        "Fetch it with a plain GET, no auth, and send the file to the user.",
        _schema({"run_id": _RUN_ID, "format": _STR}, "run_id"),
    ),
    (
        "get_prep_seed",
        "Start or resume interview prep for an application. First call generates "
        "the coach's opening (costs credits). Use before hireli_ask_prep_coach when "
        "unsure whether prep has started.",
        _schema({"run_id": _RUN_ID}, "run_id"),
    ),
    (
        "ask_prep_coach",
        "Send one message to the application's interview-prep coach and get the "
        "reply (same thread as the site; costs credits). Pass the user's words as-is.",
        _schema({"run_id": _RUN_ID, "message": _STR}, "run_id", "message"),
    ),
    (
        "add_prep_note",
        "Save an interview-prep note (short topic + content) on an application.",
        _schema(
            {"run_id": _RUN_ID, "topic": _STR, "content": _STR},
            "run_id",
            "topic",
            "content",
        ),
    ),
    (
        "update_prep_note",
        "Edit a prep note (note_id from hireli_get_prep_notes); pass only fields to "
        "change. Notes cannot be deleted.",
        _schema(
            {"run_id": _RUN_ID, "note_id": _STR, "topic": _STR, "content": _STR},
            "run_id",
            "note_id",
        ),
    ),
    (
        "reset_prep",
        "Wipe an application's prep chat, notes and context. Irreversible: only "
        "with confirm=true after the user explicitly confirmed.",
        _schema({"run_id": _RUN_ID, "confirm": {"type": "boolean"}}, "run_id"),
    ),
    (
        "update_application_status",
        "Move an application through the funnel: saved, applied, screening, "
        "interview_round_N, offer, rejected, declined. position_id (from "
        "hireli_list_applications) may be omitted for single-position applications; "
        "note is an optional one-line reason.",
        _schema(
            {"run_id": _RUN_ID, "status": _STR, "position_id": _STR, "note": _STR},
            "run_id",
            "status",
        ),
    ),
]

HIRELI_SPECS: List[ToolSpec] = [
    ToolSpec(
        name=_PREFIX + remote,
        description=description,
        schema=schema,
        handler=_proxy(remote),
        available=hireli_available,
    )
    for remote, description, schema in _TOOLS
]
