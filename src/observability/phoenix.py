"""devvy -> Phoenix: finished model calls as OpenInference spans (dv-87d8).

A projection of the store, not a second capture path: every model call
already lands as one ``model_calls`` row (the router's single pass, bot turns
and jobs alike) with its ``tool_calls`` linked. A native scheduler job sends
each row once it has settled (120 s: cost settles ~6 s after a turn, the
trace is flushed before the row is linked) to Phoenix REST
``POST {base}/v1/projects/{project}/spans``, then marks it ``exported_at``.

Ids are deterministic (sha256 of the row id), so a re-send is a duplicate,
never a second copy. Phoenix answers a batch holding any stored span with 400
and inserts *none* of it; the body lists them, so those are stripped and the
rest re-sent once. Phoenix down (5xx, connection error) = mark nothing, the
next tick catches up. Never raises.

Chat question/answer text is not sent (owner, 1.1): Phoenix has no login of
its own. Tool spans carry dv-2260's short, scrubbed previews only.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set

import httpx
import structlog

from ..storage.model_calls import ModelCallRepository
from ..storage.tool_calls import ToolCallRepository

logger = structlog.get_logger()

SERVICE = "devvy"
SETTLE_SECONDS = 120
BATCH = 50
TIMEOUT_S = 15.0

_PUMP_JOB = re.compile(
    r"^pump-(?P<task>.+)-"
    r"(?P<stage>analyze|architect|testdesign|implement|review|acceptance)"
    r"(?:-p\d+)?$"
)
_ROOT_KIND = {"turn": "AGENT", "plain": "LLM", "decide": "CHAIN"}
_SUBAGENT_TOOLS = {"Agent", "Task"}


def _hex(text: str, n: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def trace_id_of(model_call_id: int) -> str:
    return _hex(f"{SERVICE}:mc:{model_call_id}", 32)


def span_id_of(model_call_id: int, part: str) -> str:
    """``part`` is ``root`` or a tool's ``tool_use_id``."""
    return _hex(f"{SERVICE}:mc:{model_call_id}:{part}", 16)


def _utc(value: Any) -> Optional[datetime]:
    """A stored timestamp as aware UTC; SQLite's CURRENT_TIMESTAMP is naive."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _clean(attrs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None and v != ""}


def pump_job(job_name: Optional[str]) -> Optional[Dict[str, str]]:
    """``{"task", "stage"}`` of a pump stage job name, else None."""
    m = _PUMP_JOB.match(job_name or "")
    return {"task": m["task"], "stage": m["stage"]} if m else None


def _root_name(call: Dict[str, Any], pump: Optional[Dict[str, str]]) -> str:
    if pump:
        return f"pump {pump['stage']} {pump['task']}"
    if call.get("job_name"):
        return f"job {call['job_name']}"
    profile = call.get("profile")
    return f"{call['purpose']} · {profile}" if profile else str(call["purpose"])


def spans_for(
    call: Dict[str, Any], tool_rows: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """One ``model_calls`` row + its tool rows -> Phoenix REST spans."""
    mc = int(call["id"])
    trace_id = trace_id_of(mc)
    root_id = span_id_of(mc, "root")
    end = _utc(call.get("ts")) or datetime.now(UTC)
    start = end - timedelta(milliseconds=int(call.get("duration_ms") or 0))
    pump = pump_job(call.get("job_name"))
    ok = call.get("outcome") == "ok"
    kind = _ROOT_KIND.get(str(call.get("kind")), "CHAIN")

    prompt = (
        int(call.get("input_tokens") or 0)
        + int(call.get("cache_read_tokens") or 0)
        + int(call.get("cache_creation_tokens") or 0)
    )
    completion = int(call.get("output_tokens") or 0)
    metadata = _clean(
        {
            "service": SERVICE,
            "agent": call.get("profile"),
            "purpose": call.get("purpose"),
            "kind": call.get("kind"),
            "harness": call.get("harness"),
            "harness_version": call.get("harness_version"),
            "attended": bool(call.get("attended")),
            "model_call_id": mc,
            "job_name": call.get("job_name"),
            "task_id": pump["task"] if pump else None,
            "pump_stage": pump["stage"] if pump else None,
            "outcome": call.get("outcome"),
            "cost_source": call.get("cost_source"),
        }
    )
    tags = [t for t in (call.get("purpose"), call.get("profile")) if t]
    if pump:
        tags.append("pump")
    root = {
        "name": _root_name(call, pump),
        "context": {"trace_id": trace_id, "span_id": root_id},
        "span_kind": kind,
        "parent_id": None,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "status_code": "OK" if ok else "ERROR",
        "status_message": "" if ok else str(call.get("error") or call.get("outcome")),
        "attributes": _clean(
            {
                "openinference.span.kind": kind,
                "session.id": call.get("session_id"),
                "user.id": (
                    str(call["user_id"]) if call.get("user_id") is not None else None
                ),
                "llm.model_name": call.get("model"),
                "llm.provider": call.get("provider"),
                "llm.token_count.prompt": prompt,
                "llm.token_count.prompt_details.cache_read": int(
                    call.get("cache_read_tokens") or 0
                ),
                "llm.token_count.prompt_details.cache_write": int(
                    call.get("cache_creation_tokens") or 0
                ),
                "llm.token_count.completion": completion,
                "llm.token_count.total": prompt + completion,
                "llm.cost.total": call.get("cost_usd"),
                "metadata": json.dumps(metadata, ensure_ascii=False),
                "tag.tags": tags,
            }
        ),
    }

    known = {r.get("tool_use_id") for r in tool_rows if r.get("tool_use_id")}
    spans = [root]
    for row in tool_rows:
        tool_use_id = row.get("tool_use_id") or f"row{row.get('id')}"
        parent = row.get("parent_tool_use_id")
        parent_id = span_id_of(mc, str(parent)) if parent in known else root_id
        name = str(row.get("tool") or "tool")
        tkind = (
            "AGENT" if row.get("subagent_type") or name in _SUBAGENT_TOOLS else "TOOL"
        )
        t_start = _utc(row.get("started_at")) or start
        duration = row.get("duration_ms")
        tstatus = row.get("status")
        detached = tstatus == "detached"
        if duration is None or tstatus in ("detached", "unfinished"):
            t_end = end  # unfinished / detached: frozen at the turn's end
            t_start = min(t_start, t_end)
        else:
            t_end = t_start + timedelta(milliseconds=int(duration))
        if tstatus == "error":
            status, message = "ERROR", str(row.get("error") or "failed")
        elif tstatus == "ok":
            status, message = "OK", ""
        else:
            status, message = "UNSET", ""
        tmeta = _clean(
            {
                "seq": row.get("seq"),
                "subagent_type": row.get("subagent_type"),
                "result_bytes": row.get("result_bytes"),
                "detached": True if detached else None,
                "unfinished": True if tstatus == "unfinished" else None,
            }
        )
        spans.append(
            {
                "name": name,
                "context": {
                    "trace_id": trace_id,
                    "span_id": span_id_of(mc, tool_use_id),
                },
                "span_kind": tkind,
                "parent_id": parent_id,
                "start_time": t_start.isoformat(),
                "end_time": t_end.isoformat(),
                "status_code": status,
                "status_message": message,
                "attributes": _clean(
                    {
                        "openinference.span.kind": tkind,
                        "tool.name": name,
                        "input.value": row.get("input"),
                        "output.value": row.get("result_preview"),
                        "session.id": call.get("session_id"),
                        "metadata": json.dumps(tmeta) if tmeta else None,
                    }
                ),
            }
        )
    return spans


# --- sending ---------------------------------------------------------------

_OK, _RETRY, _REJECT = "ok", "retry", "reject"


def _listed(body: Any) -> Optional[Dict[str, Set[str]]]:
    """Span ids Phoenix's 400 names as duplicate / invalid; None if the body
    is not that shape."""
    if not isinstance(body, dict):
        return None
    dup, bad = body.get("duplicate_spans"), body.get("invalid_spans")
    if not isinstance(dup, list) or not isinstance(bad, list):
        return None
    if not dup and not bad:
        return None

    def ids(items: List[Any]) -> Set[str]:
        return {
            str(i["span_id"]) for i in items if isinstance(i, dict) and i.get("span_id")
        }

    return {"duplicate": ids(dup), "invalid": ids(bad)}


async def _post(
    client: httpx.AsyncClient, url: str, spans: List[Dict[str, Any]]
) -> httpx.Response:
    return await client.post(url, json={"data": spans}, timeout=TIMEOUT_S)


async def _send(
    client: httpx.AsyncClient,
    url: str,
    spans: List[Dict[str, Any]],
    invalid: Set[str],
) -> str:
    """POST ``spans``; on a 400 that lists duplicates / invalid spans, strip
    them and re-send the rest once. ``invalid`` collects rejected span ids."""
    for attempt in (1, 2):
        try:
            resp = await _post(client, url, spans)
        except httpx.HTTPError as e:
            logger.warning("phoenix export: unreachable", error=str(e))
            return _RETRY
        if resp.is_success:
            return _OK
        if resp.status_code not in (400, 422):
            logger.warning("phoenix export: not accepted", status=resp.status_code)
            return _RETRY
        try:
            listed = _listed(resp.json())
        except ValueError:
            listed = None
        if listed is None or attempt == 2:
            return _REJECT
        invalid |= listed["invalid"]
        drop = listed["duplicate"] | listed["invalid"]
        spans = [s for s in spans if s["context"]["span_id"] not in drop]
        if not spans:
            return _OK
    return _REJECT  # pragma: no cover - loop always returns


async def export_pending(
    *,
    model_calls: ModelCallRepository,
    tool_calls: ToolCallRepository,
    client: httpx.AsyncClient,
    base_url: str,
    project: str,
    older_than_s: int = SETTLE_SECONDS,
    limit: int = BATCH,
) -> int:
    """One tick: send settled unexported calls, mark them. Returns how many
    were marked. A fixed 3 queries whatever the batch size. Never raises."""
    try:
        calls = await model_calls.unexported(older_than_s=older_than_s, limit=limit)
        if not calls:
            return 0
        ids = [int(c["id"]) for c in calls]
        tools = await tool_calls.for_model_calls(ids)
        per_call = {
            int(c["id"]): spans_for(c, tools.get(int(c["id"]), [])) for c in calls
        }
        url = f"{base_url.rstrip('/')}/v1/projects/{project}/spans"

        invalid: Set[str] = set()
        result = await _send(
            client, url, [s for ss in per_call.values() for s in ss], invalid
        )
        if result == _RETRY:
            return 0
        if result == _OK:
            done = ids
        else:
            # The batch stays rejected: one POST per call, so one bad call
            # cannot hold the rest. A call still rejected is marked (logged),
            # or it would block every tick.
            done = []
            for mc, spans in per_call.items():
                outcome = await _send(client, url, spans, invalid)
                if outcome == _REJECT:
                    logger.warning("phoenix export: call rejected", model_call_id=mc)
                if outcome != _RETRY:
                    done.append(mc)
        for mc, spans in per_call.items():
            if any(s["context"]["span_id"] in invalid for s in spans):
                logger.warning("phoenix export: invalid span", model_call_id=mc)
        await model_calls.mark_exported(done)
        return len(done)
    except Exception as e:  # noqa: BLE001 - observability never breaks the bot
        logger.warning("phoenix export failed", error=str(e))
        return 0


def register_phoenix_export(
    scheduler: Any, db: Any, phoenix_url: Optional[str], project: str = SERVICE
) -> Optional[Callable[[], Awaitable[None]]]:
    """Add the ``phoenix-export`` native job (every minute; with the 120 s
    settle a call shows within ~3 min). Unset URL = nothing scheduled."""
    if not phoenix_url:
        return None
    model_calls = ModelCallRepository(db)
    tool_calls = ToolCallRepository(db)

    async def _tick() -> None:
        async with httpx.AsyncClient() as client:
            n = await export_pending(
                model_calls=model_calls,
                tool_calls=tool_calls,
                client=client,
                base_url=phoenix_url,
                project=project,
            )
        if n:
            logger.info("phoenix export", calls=n, project=project)

    scheduler.add_native_job("phoenix-export", "* * * * *", _tick)
    return _tick
