"""data.db read-lane MCP tools (dv-5f3a: lane 3 shaped reads + lane 2 export).

Built per turn by ``build_agent_mcp_server`` when the profile's YAML declares
``user_data_tools`` groups. ``user_id`` comes exclusively from server
context, baked into each tool's closure — no tool takes a user or scope
parameter, so a model cannot address another user's rows (the dv-6dbc rule).
Mounting is capability-config driven (dv-5f3a decision 1): no profile slug
is ever hardcoded here.

``export_my_data`` is the universal read path: the DB file itself is
denylisted for sandboxed profiles, so when no shaped tool fits a question
the agent exports the raw rows into its own workspace and analyzes them
with code — it is never left with zero ways to read data that exists.

Write tools (lane 1, ``build_user_data_write_tools``) return computed
aggregates so the model quotes them instead of doing arithmetic.
"""

import csv
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

import structlog
from claude_agent_sdk import tool

from ..events.types import AgentResponseEvent

logger = structlog.get_logger()

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# dv-c388: logs_activity provenance enum, mirrors products.provenance.
_ACTIVITY_SOURCES = ("user_reported", "measured", "estimated")

_JSON_TYPES = {str: "string", float: "number", int: "integer", bool: "boolean"}


def _schema(props: Dict[str, type], *required: str) -> Dict[str, Any]:
    """Explicit JSON Schema, so only the named params are actually required.

    dv-9679: the SDK's dict shorthand emits ``"required": list(properties)``
    — *every* key (``claude_agent_sdk/__init__.py``). A 14-field upsert like
    ``log_sleep`` then could not be called with the two fields the user
    mentioned: the model's only options were to fail the call or invent the
    other twelve. That is the dv-c388 fabrication class wired into the tool
    surface rather than into a persona rule, so every schema in this module
    names its required fields explicitly and the rest stay optional.

    Required here means "the handler rejects the call without it", not
    "usually present" — keep the two in sync or the model learns the schema
    lies.
    """
    return {
        "type": "object",
        "properties": {k: {"type": _JSON_TYPES[v]} for k, v in props.items()},
        "required": list(required),
    }


def _batch_schema(
    array_key: str,
    item_props: Dict[str, type],
    item_required: Sequence[str] = (),
    batch_props: Optional[Dict[str, type]] = None,
    batch_required: Sequence[str] = (),
) -> Dict[str, Any]:
    """Schema for a batch write: optional batch-level scalars + one row array.

    dv-b042 made every write tool take an array, so the dv-9679 rule now has
    to hold one level down as well: the ITEM object names its own required
    fields explicitly, and everything else stays optional.

    That is the whole safety argument for this helper. A per-row schema that
    marks `rpe` required does not merely fail — it hands the model a choice
    between failing the call and inventing eleven RPE values, and the batch
    shape multiplies that by the batch size. Worse, the invented value is
    usually 0, and a 0 is not "unknown": it lands in AVG() and drags every
    average that touches it. An absent key stays absent, ``_num``/``_str``
    turn it into None, and None is stored as NULL — empty stays empty
    (dv-3668). Required here means "the handler rejects the row without it",
    never "usually present".
    """
    properties: Dict[str, Any] = {
        k: {"type": _JSON_TYPES[v]} for k, v in (batch_props or {}).items()
    }
    properties[array_key] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {k: {"type": _JSON_TYPES[v]} for k, v in item_props.items()},
            "required": list(item_required),
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": [*batch_required, array_key],
    }


def _is_retroactive(date_str: str) -> bool:
    """True once ``date_str`` is more than one day in the past.

    Yesterday is exempt (almost always real, logged a bit late); the day
    before and earlier is the entire fabrication surface (dv-c388) and gets
    gated behind an explicit ``backfill_reason``.
    """
    cutoff = datetime.now(UTC).date() - timedelta(days=1)
    return date.fromisoformat(date_str) < cutoff


# Columns dropped from exports: user_id is always the caller's own (noise at
# best, confusing at worst in a file the model reads). Deliberately narrower
# than _DROP_COLUMNS below: an export is raw rows the user analyzes with code
# later, so the audit timestamps stay in the file even though the model never
# reads them in context.
_EXPORT_DROP = {"user_id"}

# Columns stripped from every rendered tool result: the caller's own id, and
# storage bookkeeping the model has never had a use for. ~55 chars per row of
# ISO timestamps alone (dv-dd7e).
_DROP_COLUMNS = frozenset({"user_id", "created_at", "updated_at", "imported_from"})


def _compact(value: Any, drop_nulls: bool) -> Any:
    """Strip render-only noise from a payload, recursively.

    ``drop_nulls`` is false for write tools on purpose. "Empty stays empty,
    never 0" (dv-3668, dv-adea) is only *observable* while the key is still
    there: a cleared field, a field the write never touched, and an aggregate
    with nothing to average all have to read as explicitly null rather than
    as absent, or the model cannot tell "no value" from "not reported". On a
    bulk read absent already means absent, so there the key is pure overhead.
    """
    if isinstance(value, dict):
        return {
            k: _compact(v, drop_nulls)
            for k, v in value.items()
            if k not in _DROP_COLUMNS and not (drop_nulls and v is None)
        }
    if isinstance(value, list):
        return [_compact(v, drop_nulls) for v in value]
    return value


def _text(payload: Any, *, drop_nulls: bool = False) -> Dict[str, Any]:
    """Wrap a value as an SDK MCP tool text result, rendered compactly.

    Every result stays in the transcript for the life of the conversation
    window and is resent on each following turn, so render-only bytes are
    paid again every turn until rotation. Minified separators plus the
    dropped columns roughly halve this module's share of the window and
    change no value the model reads (dv-dd7e).
    """
    import json

    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(
            _compact(payload, drop_nulls),
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        )
    )
    return {"content": [{"type": "text", "text": text}]}


def _err(message: str) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"error: {message}"}],
        "is_error": True,
    }


def _date_arg(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not _DATE.match(value.strip()):
        return ""
    return value.strip()


def _write_export_csv(path: Path, rows: List[Dict[str, Any]]) -> int:
    """Write rows as CSV (stable column order from the first row), returning
    the row count. None stays an empty cell — never 0."""
    columns = [k for k in rows[0] if k not in _EXPORT_DROP] if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if row[c] is None else row[c] for c in columns])
    return len(rows)


def build_user_data_read_tools(
    repo: Any,
    user_id: int,
    groups: FrozenSet[str],
    working_directory: Optional[Path] = None,
) -> List[Any]:
    """Read tools for the mounted groups, scoped to ``user_id`` by closure.

    ``working_directory`` is where ``export_my_data`` materializes files;
    the export tool is only mounted when it is provided.
    """
    tools: List[Any] = []

    if "logs" in groups:

        @tool(
            "day_summary",
            "Everything logged for one day: meals with computed kcal/macro "
            "totals, training sets, activities, and the sleep row. This is "
            "the data store of record — use it instead of reading CSV files "
            "or recomputing totals yourself.",
            _schema({"date": str}, "date"),
        )
        async def day_summary(args: Dict[str, Any]) -> Dict[str, Any]:
            date = _date_arg(args, "date")
            if not date:
                return _err("date must be YYYY-MM-DD")
            return _text(await repo.day_summary(user_id, date), drop_nulls=True)

        @tool(
            "sleep_last",
            "The last N sleep/recovery rows (sleep, HRV, RHR, weight, "
            "wellbeing), newest first.",
            _schema({"n": int}),
        )
        async def sleep_last(args: Dict[str, Any]) -> Dict[str, Any]:
            try:
                n = int(args.get("n") or 7)
            except (TypeError, ValueError):
                n = 7
            return _text(await repo.sleep_last(user_id, n), drop_nulls=True)

        @tool(
            "lift_history",
            "Set-by-set history for one exercise over its most recent "
            "training dates. Ground every progression suggestion in this — "
            "never quote weights or reps from memory.",
            _schema({"exercise": str, "limit": int}, "exercise"),
        )
        async def lift_history(args: Dict[str, Any]) -> Dict[str, Any]:
            exercise = (args.get("exercise") or "").strip()
            if not exercise:
                return _err("exercise is required")
            try:
                limit = int(args.get("limit") or 5)
            except (TypeError, ValueError):
                limit = 5
            return _text(
                await repo.lift_history(user_id, exercise, limit), drop_nulls=True
            )

        @tool(
            "week_summary",
            "Last 7 days at a glance: per-day meal counts and kcal/protein "
            "totals, training volume, activity minutes. Activity minutes "
            "exclude 'estimated' rows — see activity_estimated_excluded for "
            "what was left out and say so if you quote a total.",
            _schema({}),
        )
        async def week_summary(args: Dict[str, Any]) -> Dict[str, Any]:
            return _text(await repo.week_summary(user_id), drop_nulls=True)

        tools += [day_summary, sleep_last, lift_history, week_summary]

    if "products" in groups:

        @tool(
            "product_find",
            "Look up a food product or dish by name/brand substring. Returns "
            "kcal/macros per 100g WITH provenance: 'label_photo'/'official' "
            "values are authoritative; 'estimate' values are guesses — say so "
            "when you use one, and never present an estimate as label data.",
            _schema({"query": str}, "query"),
        )
        async def product_find(args: Dict[str, Any]) -> Dict[str, Any]:
            query = (args.get("query") or "").strip()
            if not query:
                return _err("query is required")
            return _text(await repo.product_find(query), drop_nulls=True)

        tools.append(product_find)

    if "portions" in groups:

        @tool(
            "portion_find",
            "This user's learned portion sizes for a phrase (e.g. what "
            "'нормальная пицца' weighs for THEM). Personal, not universal.",
            _schema({"phrase": str}, "phrase"),
        )
        async def portion_find(args: Dict[str, Any]) -> Dict[str, Any]:
            phrase = (args.get("phrase") or "").strip()
            if not phrase:
                return _err("phrase is required")
            return _text(await repo.portion_find(user_id, phrase), drop_nulls=True)

        tools.append(portion_find)

    _append_export_tool(tools, repo, user_id, groups, working_directory)
    return tools


def _entry_id(args: Dict[str, Any]) -> int:
    """Row id to act on. Anything unusable becomes 0, which every caller
    rejects — a bad id must never fall through to a statement."""
    try:
        return int(args.get("entry_id") or 0)
    except (TypeError, ValueError):
        return 0


def _clear_list(args: Dict[str, Any]) -> List[str]:
    """Comma-separated field names to blank out. Unknown names are dropped
    downstream, so a typo clears nothing rather than something else."""
    raw = args.get("clear")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _rows(args: Dict[str, Any], key: str) -> Tuple[List[Dict[str, Any]], str]:
    """Pull the row array out of a batch call, or explain what is wrong.

    Returns ``(rows, error)`` — error is "" on success. An empty array is an
    error rather than a silent no-op: a write tool that reports success
    having written nothing is how "записал" gets said about an empty log
    (the persona's earned-word rule), and the model has no way to notice.
    """
    raw = args.get(key)
    if not isinstance(raw, list) or not raw:
        return [], f"{key} must be a non-empty array of objects"
    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"{key}[{index}] must be an object"
        rows.append(item)
    return rows, ""


def _reject(key: str, index: int, why: str) -> Dict[str, Any]:
    """Reject a whole batch, naming the offending row.

    Says NOTHING WAS WRITTEN explicitly: told only "row 3 is bad", a model
    tends to assume rows 0-2 landed and retry from 3, which silently drops
    them. The batch is all-or-nothing, so the message has to say so.
    """
    return _err(f"{key}[{index}]: {why} — NOTHING WAS WRITTEN, resend the whole batch")


def _cap(rows: List[Dict[str, Any]], key: str) -> str:
    """Batch ceiling. 200 is far above any real workout or meal and exists
    only so a malformed loop cannot write thousands of rows in one call."""
    return "" if len(rows) <= 200 else f"{key} has {len(rows)} rows; 200 is the maximum"


def _num(args: Dict[str, Any], key: str) -> Optional[float]:
    """Optional numeric arg: None/'' stay None (empty stays empty, dv-3668);
    anything unusable as a number also degrades to None rather than 0."""
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str(args: Dict[str, Any], key: str) -> Optional[str]:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def build_user_data_write_tools(
    repo: Any,
    user_id: int,
    groups: FrozenSet[str],
    event_bus: Any = None,
    profile_slug: Optional[str] = None,
) -> List[Any]:
    """Write tools (lane 1) for the mounted groups, user-scoped by closure.

    Every write returns computed aggregates — the model quotes them instead
    of doing its own arithmetic (dv-7a3c failure class).

    ``event_bus`` / ``profile_slug`` back the dv-c388 owner-inform: a
    retroactive `estimated` activity write publishes a broadcast the same
    way the save-honesty check does, so a fabrication surfaces within a
    minute instead of after a user complaint.
    """
    tools: List[Any] = []

    if "logs" in groups:

        @tool(
            "log_food",
            "Record meals for ONE date. 'items' is always an array, one "
            "object per meal or component: a whole day, a three-part dinner "
            "and a single snack are each ONE call — never one call per "
            "component. Returns the day's computed totals ONCE, after every "
            "row is in — quote THOSE numbers back to the user, never your "
            "own arithmetic. Omit unknown numbers entirely: an absent field "
            "is stored as empty, but a 0 you supplied is a real zero and "
            "will drag every average that touches it. confidence: 'high' "
            "only with label/known-product data, else 'est'. source: "
            "'photo' or 'text'.",
            _batch_schema(
                "items",
                {
                    "meal": str,
                    "time": str,
                    "kcal": float,
                    "protein": float,
                    "fat": float,
                    "carb": float,
                    "confidence": str,
                    "source": str,
                },
                ("meal",),
                {"date": str},
                ("date",),
            ),
        )
        async def log_food(args: Dict[str, Any]) -> Dict[str, Any]:
            date = _date_arg(args, "date")
            if not date:
                return _err("date (YYYY-MM-DD) is required")
            items, error = _rows(args, "items")
            if error:
                return _err(error)
            if _cap(items, "items"):
                return _err(_cap(items, "items"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(items):
                meal = _str(item, "meal")
                if not meal:
                    return _reject("items", index, "meal is required")
                payload.append(
                    {
                        "meal": meal,
                        "time": _str(item, "time"),
                        "kcal": _num(item, "kcal"),
                        "protein": _num(item, "protein"),
                        "fat": _num(item, "fat"),
                        "carb": _num(item, "carb"),
                        "confidence": _str(item, "confidence"),
                        "source": _str(item, "source"),
                    }
                )
            return _text(await repo.log_food_items(user_id, date, payload))

        @tool(
            "food_edit",
            "Correct meals ALREADY logged, in place. 'edits' is always an "
            "array, one object per row being corrected. Use this whenever "
            "the user revises something you logged (wrong weight, wrong "
            "estimate, forgot an ingredient) — never log a second "
            "'correction' row with a negative number. Take entry_id from "
            "day_summary. Send only the fields that change; the rest are "
            "kept. To take back a number you invented, name it in 'clear' "
            "(e.g. 'kcal,protein') — that blanks it to empty, which is "
            "honest, whereas setting it to 0 is a false measurement. "
            "Returns the corrected rows plus each day's recomputed totals — "
            "quote THOSE.",
            _batch_schema(
                "edits",
                {
                    "entry_id": int,
                    "meal": str,
                    "time": str,
                    "kcal": float,
                    "protein": float,
                    "fat": float,
                    "carb": float,
                    "confidence": str,
                    "source": str,
                    "clear": str,
                },
                ("entry_id",),
            ),
        )
        async def food_edit(args: Dict[str, Any]) -> Dict[str, Any]:
            edits, error = _rows(args, "edits")
            if error:
                return _err(error)
            if _cap(edits, "edits"):
                return _err(_cap(edits, "edits"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(edits):
                entry_id = _entry_id(item)
                if entry_id <= 0:
                    return _reject(
                        "edits", index, "entry_id is required (from day_summary)"
                    )
                payload.append(
                    {
                        "entry_id": entry_id,
                        "meal": _str(item, "meal"),
                        "time": _str(item, "time"),
                        "kcal": _num(item, "kcal"),
                        "protein": _num(item, "protein"),
                        "fat": _num(item, "fat"),
                        "carb": _num(item, "carb"),
                        "confidence": _str(item, "confidence"),
                        "source": _str(item, "source"),
                        "clear": _clear_list(item),
                    }
                )
            result = await repo.edit_food_many(user_id, payload)
            if result.get("bad_index") is not None:
                index = int(result["bad_index"])
                return _reject(
                    "edits",
                    index,
                    f"no meal with id {payload[index]['entry_id']} in this "
                    "user's log — re-read day_summary for the right ids",
                )
            return _text(result)

        @tool(
            "train_edit",
            "Correct training sets ALREADY logged, in place (wrong weight, "
            "reps, or exercise name). 'edits' is always an array, one object "
            "per set — re-weighting all four sets of an exercise is ONE "
            "call. entry_id comes from day_summary. Send only what changes; "
            "'clear' blanks fields by name (e.g. 'rpe,weight_kg'), which is "
            "how you take back a number you should not have written — never "
            "overwrite it with 0.",
            _batch_schema(
                "edits",
                {
                    "entry_id": int,
                    "exercise": str,
                    "session": str,
                    "set_n": str,
                    "weight_kg": str,
                    "reps": str,
                    "rpe": float,
                    "clear": str,
                },
                ("entry_id",),
            ),
        )
        async def train_edit(args: Dict[str, Any]) -> Dict[str, Any]:
            edits, error = _rows(args, "edits")
            if error:
                return _err(error)
            if _cap(edits, "edits"):
                return _err(_cap(edits, "edits"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(edits):
                entry_id = _entry_id(item)
                if entry_id <= 0:
                    return _reject(
                        "edits", index, "entry_id is required (from day_summary)"
                    )
                payload.append(
                    {
                        "entry_id": entry_id,
                        "exercise": _str(item, "exercise"),
                        "session": _str(item, "session"),
                        "set_n": _str(item, "set_n"),
                        "weight_kg": _str(item, "weight_kg"),
                        "reps": _str(item, "reps"),
                        "rpe": _num(item, "rpe"),
                        "clear": _clear_list(item),
                    }
                )
            result = await repo.edit_train_many(user_id, payload)
            if result.get("bad_index") is not None:
                index = int(result["bad_index"])
                return _reject(
                    "edits",
                    index,
                    f"no training set with id {payload[index]['entry_id']} in "
                    "this user's log — re-read day_summary for the right ids",
                )
            return _text(result)

        @tool(
            "activity_edit",
            "Correct an activity ALREADY logged, in place. entry_id comes "
            "from day_summary. Send only what changes; 'clear' blanks fields "
            "by name (e.g. 'duration_min'). kcal_active/kcal_total/"
            "exercise_min/stand_h/steps/distance_km are Apple-Watch-style "
            "ring/step numbers, distinct from duration_min/intensity. "
            "source lets you upgrade a 'user_reported' placeholder to "
            "'measured' once real data arrives — never use this to change a "
            "value just because the user pushed back; that needs real data, "
            "not a revision.",
            _batch_schema(
                "edits",
                {
                    "entry_id": int,
                    "type": str,
                    "duration_min": float,
                    "intensity": str,
                    "kcal_active": float,
                    "kcal_total": float,
                    "exercise_min": float,
                    "stand_h": float,
                    "steps": float,
                    "distance_km": float,
                    "source": str,
                    "clear": str,
                },
                ("entry_id",),
            ),
        )
        async def activity_edit(args: Dict[str, Any]) -> Dict[str, Any]:
            edits, error = _rows(args, "edits")
            if error:
                return _err(error)
            if _cap(edits, "edits"):
                return _err(_cap(edits, "edits"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(edits):
                entry_id = _entry_id(item)
                if entry_id <= 0:
                    return _reject(
                        "edits", index, "entry_id is required (from day_summary)"
                    )
                source = _str(item, "source")
                if source is not None and source not in _ACTIVITY_SOURCES:
                    return _reject(
                        "edits",
                        index,
                        "source must be one of " + ", ".join(_ACTIVITY_SOURCES),
                    )
                payload.append(
                    {
                        "entry_id": entry_id,
                        "type_": _str(item, "type"),
                        "duration_min": _num(item, "duration_min"),
                        "intensity": _str(item, "intensity"),
                        "kcal_active": _num(item, "kcal_active"),
                        "kcal_total": _num(item, "kcal_total"),
                        "exercise_min": _num(item, "exercise_min"),
                        "stand_h": _num(item, "stand_h"),
                        "steps": _num(item, "steps"),
                        "distance_km": _num(item, "distance_km"),
                        "source": source,
                        "clear": _clear_list(item),
                    }
                )
            result = await repo.edit_activity_many(user_id, payload)
            if result.get("bad_index") is not None:
                index = int(result["bad_index"])
                return _reject(
                    "edits",
                    index,
                    f"no activity with id {payload[index]['entry_id']} in this "
                    "user's log — re-read day_summary for the right ids",
                )
            return _text(result)

        @tool(
            "log_delete",
            "Remove log rows that should not exist at all — logged twice, "
            "logged on the wrong day, or something the user says never "
            "happened. 'entries' is always an array, one object per row, so "
            "undoing a whole mis-logged session is ONE call. Prefer editing "
            "when the entry is merely WRONG: deletion loses the record. "
            "domain is food/sleep/train/activity; entry_id comes from "
            "day_summary. Every id is checked before anything is removed — "
            "one bad id and nothing is deleted. Tell the user exactly what "
            "you removed.",
            _batch_schema(
                "entries", {"domain": str, "entry_id": int}, ("domain", "entry_id")
            ),
        )
        async def log_delete(args: Dict[str, Any]) -> Dict[str, Any]:
            entries, error = _rows(args, "entries")
            if error:
                return _err(error)
            if _cap(entries, "entries"):
                return _err(_cap(entries, "entries"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(entries):
                domain = str(item.get("domain") or "").strip().lower()
                entry_id = _entry_id(item)
                if domain not in ("food", "sleep", "train", "activity"):
                    return _reject(
                        "entries",
                        index,
                        "domain must be one of food, sleep, train, activity",
                    )
                if entry_id <= 0:
                    return _reject(
                        "entries", index, "entry_id is required (from day_summary)"
                    )
                payload.append({"domain": domain, "entry_id": entry_id})
            result = await repo.delete_log_entries(user_id, payload)
            if result.get("bad_index") is not None:
                index = int(result["bad_index"])
                bad = payload[index]
                return _reject(
                    "entries",
                    index,
                    f"no {bad['domain']} entry with id {bad['entry_id']} in "
                    "this user's log — re-read day_summary for the right ids",
                )
            return _text(result)

        @tool(
            "log_sleep",
            "Record sleep/recovery fields. 'nights' is always an array, one "
            "object per DATE — each night carries its own 'date', so a week "
            "read off a watch export is ONE call. Upserts: provide only what "
            "you learned this turn, existing fields for that date are kept, "
            "not erased. Never fill a field you were not told: a night "
            "without an hrv reading must LEAVE HRV OUT, because a 0 written "
            "for two nights corrupts every average afterwards while an empty "
            "cell is simply skipped. Returns the merged row plus 7-day "
            "averages per date.",
            _batch_schema(
                "nights",
                {
                    "date": str,
                    "bed": str,
                    "wake": str,
                    "sleep_min": float,
                    "onset_min": float,
                    "score": float,
                    "hrv": float,
                    "rhr": float,
                    "weight_kg": float,
                    "wellbeing": str,
                    "alcohol_u": float,
                    "thc": str,
                    "room_temp": float,
                    "out_night": float,
                    "out_day": float,
                },
                ("date",),
            ),
        )
        async def log_sleep(args: Dict[str, Any]) -> Dict[str, Any]:
            nights, error = _rows(args, "nights")
            if error:
                return _err(error)
            if _cap(nights, "nights"):
                return _err(_cap(nights, "nights"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(nights):
                date = _date_arg(item, "date")
                if not date:
                    return _reject("nights", index, "date must be YYYY-MM-DD")
                payload.append(
                    {
                        "date": date,
                        "bed": _str(item, "bed"),
                        "wake": _str(item, "wake"),
                        "sleep_min": _num(item, "sleep_min"),
                        "onset_min": _num(item, "onset_min"),
                        "score": _num(item, "score"),
                        "hrv": _num(item, "hrv"),
                        "rhr": _num(item, "rhr"),
                        "weight_kg": _num(item, "weight_kg"),
                        "wellbeing": _str(item, "wellbeing"),
                        "alcohol_u": _num(item, "alcohol_u"),
                        "thc": _str(item, "thc"),
                        "room_temp": _num(item, "room_temp"),
                        "out_night": _num(item, "out_night"),
                        "out_day": _num(item, "out_day"),
                    }
                )
            return _text(await repo.log_sleep_nights(user_id, payload))

        @tool(
            "log_train",
            "Record training sets for ONE date. 'sets' is always an array, "
            "one object per SET: a whole workout is ONE call with every set "
            "in the array, and adding a single set later is the same call "
            "with a one-element array. Never one call per set. 'session' is "
            "the workout label (e.g. 'pull_chest'), given once for the "
            "batch. weight_kg and reps are TEXT — record what was said "
            "('10-12', 'bodyweight'), never invent per-set numbers, and "
            "leave set_n out rather than numbering sets yourself. Returns "
            "today's sets and the previous session's sets ONCE PER "
            "EXERCISE — ground progression advice in that.",
            _batch_schema(
                "sets",
                {
                    "exercise": str,
                    "set_n": str,
                    "weight_kg": str,
                    "reps": str,
                    "rpe": float,
                },
                ("exercise",),
                {"date": str, "session": str},
                ("date",),
            ),
        )
        async def log_train(args: Dict[str, Any]) -> Dict[str, Any]:
            date = _date_arg(args, "date")
            if not date:
                return _err("date (YYYY-MM-DD) is required")
            sets, error = _rows(args, "sets")
            if error:
                return _err(error)
            if _cap(sets, "sets"):
                return _err(_cap(sets, "sets"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(sets):
                exercise = _str(item, "exercise")
                if not exercise:
                    return _reject("sets", index, "exercise is required")
                payload.append(
                    {
                        "exercise": exercise,
                        "set_n": _str(item, "set_n"),
                        "weight_kg": _str(item, "weight_kg"),
                        "reps": _str(item, "reps"),
                        "rpe": _num(item, "rpe"),
                    }
                )
            return _text(
                await repo.log_train_sets(
                    user_id, date, payload, session=_str(args, "session")
                )
            )

        @tool(
            "log_activity",
            "Record activities (padel, climbing, walk...) for ONE date. "
            "'activities' is always an array, one object per activity — a "
            "day with a walk and a padel session is ONE call. "
            "kcal_active/kcal_total/exercise_min/stand_h/steps/distance_km "
            "are optional Apple-Watch-style ring/step numbers (e.g. "
            "'Move 393/600 ккал, Exercise 24/40 мин, шаги 8266') — omit "
            "whatever wasn't reported, empty stays empty, never 0. source is "
            "REQUIRED PER ACTIVITY, and stays that way in a batch: "
            "'user_reported' (the user told you), 'measured' (device/Watch "
            "data), or 'estimated' (you inferred or guessed it) — never "
            "write an estimate as 'user_reported' or 'measured', and never "
            "let one row inherit another row's source. Any date more than 1 "
            "day in the past needs a non-empty backfill_reason. Never write "
            "a row for a date the user has already said had nothing on it, "
            "and never change the number because the user pushed back — that "
            "calls for asking for real data, not a bigger guess. Returns the "
            "day's full activity list ONCE.",
            _batch_schema(
                "activities",
                {
                    "type": str,
                    "source": str,
                    "duration_min": float,
                    "intensity": str,
                    "kcal_active": float,
                    "kcal_total": float,
                    "exercise_min": float,
                    "stand_h": float,
                    "steps": float,
                    "distance_km": float,
                },
                ("type", "source"),
                {"date": str, "backfill_reason": str},
                ("date",),
            ),
        )
        async def log_activity(args: Dict[str, Any]) -> Dict[str, Any]:
            log_date = _date_arg(args, "date")
            if not log_date:
                return _err("date (YYYY-MM-DD) is required")
            activities, error = _rows(args, "activities")
            if error:
                return _err(error)
            if _cap(activities, "activities"):
                return _err(_cap(activities, "activities"))
            backfill_reason = _str(args, "backfill_reason")
            retroactive = _is_retroactive(log_date)
            if retroactive and not backfill_reason:
                return _err(
                    "date is more than 1 day in the past — backfill_reason "
                    "is required for a retroactive write"
                )
            # Validate every row BEFORE the first insert: in a batch, an
            # unlabelled row must not ride in behind a labelled one (dv-c388).
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(activities):
                activity_type = _str(item, "type")
                source = _str(item, "source")
                if not activity_type:
                    return _reject("activities", index, "type is required")
                if source not in _ACTIVITY_SOURCES:
                    return _reject(
                        "activities",
                        index,
                        "source is required: one of " + ", ".join(_ACTIVITY_SOURCES),
                    )
                payload.append(
                    {
                        "type": activity_type,
                        "source": source,
                        "duration_min": _num(item, "duration_min"),
                        "intensity": _str(item, "intensity"),
                        "kcal_active": _num(item, "kcal_active"),
                        "kcal_total": _num(item, "kcal_total"),
                        "exercise_min": _num(item, "exercise_min"),
                        "stand_h": _num(item, "stand_h"),
                        "steps": _num(item, "steps"),
                        "distance_km": _num(item, "distance_km"),
                    }
                )
            result = await repo.log_activities(user_id, log_date, payload)
            estimated = [r["type"] for r in payload if r["source"] == "estimated"]
            if retroactive and estimated:
                logger.warning(
                    "Estimated activity backfilled",
                    user_id=user_id,
                    date=log_date,
                    types=estimated,
                    backfill_reason=backfill_reason,
                )
                if event_bus is not None:
                    await event_bus.publish(
                        AgentResponseEvent(
                            chat_id=0,  # broadcast to owner chats
                            text=(
                                "⚠️ estimated backfill "
                                f"[{profile_slug or 'coach'}] user={user_id} "
                                f"date={log_date} types={estimated!r}: "
                                f"{backfill_reason}"
                            ),
                            parse_mode=None,
                            source="log_activity_backfill",
                        )
                    )
            return _text(result)

        tools += [
            log_food,
            food_edit,
            log_sleep,
            log_train,
            log_activity,
            train_edit,
            activity_edit,
            log_delete,
        ]

    if "products" in groups:

        @tool(
            "product_save",
            "Save or update food products/dishes (per 100g). 'products' is "
            "always an array, one object per product — a label photo or a "
            "menu that yields five products is ONE call. provenance is "
            "REQUIRED PER PRODUCT and never guessed: 'label_photo' only when "
            "reading an actual label, 'official' for verified official data, "
            "otherwise 'estimate' — one row's provenance never carries over "
            "to the next. An estimate can NEVER overwrite label/official "
            "values: that product is refused individually and the "
            "authoritative row is returned, while the rest of the batch is "
            "still saved. Omit a macro you do not know rather than sending "
            "0 — a 0 here is 'this food has no protein', not 'unknown'.",
            _batch_schema(
                "products",
                {
                    "name": str,
                    "brand": str,
                    "kind": str,
                    "kcal_100g": float,
                    "protein": float,
                    "fat": float,
                    "carb": float,
                    "salt": float,
                    "provenance": str,
                    "confirmed_on": str,
                },
                ("name", "kind", "provenance"),
            ),
        )
        async def product_save(args: Dict[str, Any]) -> Dict[str, Any]:
            products, error = _rows(args, "products")
            if error:
                return _err(error)
            if _cap(products, "products"):
                return _err(_cap(products, "products"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(products):
                name = _str(item, "name")
                kind = _str(item, "kind")
                provenance = _str(item, "provenance")
                if not name or kind not in ("product", "dish"):
                    return _reject(
                        "products",
                        index,
                        "name and kind ('product'|'dish') are required",
                    )
                if provenance not in ("label_photo", "official", "estimate"):
                    return _reject(
                        "products",
                        index,
                        "provenance must be label_photo, official, or estimate",
                    )
                payload.append(
                    {
                        "name": name,
                        "kind": kind,
                        "provenance": provenance,
                        "brand": _str(item, "brand") or "",
                        "kcal_100g": _num(item, "kcal_100g"),
                        "protein": _num(item, "protein"),
                        "fat": _num(item, "fat"),
                        "carb": _num(item, "carb"),
                        "salt": _num(item, "salt"),
                        "confirmed_on": _date_arg(item, "confirmed_on") or None,
                        "created_by": user_id,
                    }
                )
            return _text(await repo.save_products(payload))

        @tool(
            "product_delete",
            "Remove a product from the shared store — use it when a product "
            "is a duplicate or was never real. To fix WRONG numbers, call "
            "product_save instead: correcting keeps the history, deleting "
            "loses it. Products are SHARED, so this affects every user; "
            "confirm with the user first and say what you removed. "
            "product_id comes from product_find.",
            _schema({"product_id": int}, "product_id"),
        )
        async def product_delete(args: Dict[str, Any]) -> Dict[str, Any]:
            try:
                product_id = int(args.get("product_id") or 0)
            except (TypeError, ValueError):
                product_id = 0
            if product_id <= 0:
                return _err("product_id is required (get it from product_find)")
            result = await repo.delete_product(product_id)
            if not result.get("deleted"):
                return _err(f"no product with id {product_id}")
            return _text(result)

        tools += [product_save, product_delete]

    if "portions" in groups:

        @tool(
            "portion_save",
            "Save THIS user's portion sizes for phrases (e.g. 'нормальная "
            "пицца' = 350g for them). 'portions' is always an array, one "
            "object per phrase. Upserts on the phrase. grams must be a real "
            "number the user gave or agreed to — if you do not know it, ask, "
            "do not send a placeholder.",
            _batch_schema(
                "portions",
                {"phrase": str, "grams": float, "learned_from": str},
                ("phrase", "grams"),
            ),
        )
        async def portion_save(args: Dict[str, Any]) -> Dict[str, Any]:
            portions, error = _rows(args, "portions")
            if error:
                return _err(error)
            if _cap(portions, "portions"):
                return _err(_cap(portions, "portions"))
            payload: List[Dict[str, Any]] = []
            for index, item in enumerate(portions):
                phrase = _str(item, "phrase")
                grams = _num(item, "grams")
                if not phrase or grams is None or grams <= 0:
                    return _reject(
                        "portions", index, "phrase and positive grams are required"
                    )
                payload.append(
                    {
                        "phrase": phrase,
                        "grams": grams,
                        "learned_from": _str(item, "learned_from"),
                    }
                )
            return _text(await repo.save_portions(user_id, payload))

        tools.append(portion_save)

    return tools


def _append_export_tool(
    tools: List[Any],
    repo: Any,
    user_id: int,
    groups: FrozenSet[str],
    working_directory: Optional[Path],
) -> List[Any]:
    """Mount export_my_data (lane 2) when the group is declared and a
    workspace exists to write into."""
    if "export" in groups and working_directory is not None:

        @tool(
            "export_my_data",
            "Export raw data rows as CSV files (docs as .md) into "
            "exports/ in your working directory, then analyze them with "
            "code. THIS IS YOUR UNIVERSAL READ PATH: whenever no other data "
            "tool answers the question, export the relevant domain and "
            "compute over the files — never tell the user data is "
            "unreachable, and never guess. domain: sleep | food | train | "
            "activity | portions | issues | docs | products | all. Optional "
            "date_from/date_to (YYYY-MM-DD) bound the dated log domains. "
            "Files are overwritten on each call.",
            _schema({"domain": str, "date_from": str, "date_to": str}),
        )
        async def export_my_data(args: Dict[str, Any]) -> Dict[str, Any]:
            domain = (args.get("domain") or "").strip().lower()
            valid = set(repo.EXPORT_DOMAINS)
            domains = sorted(valid) if domain == "all" else [domain]
            if not set(domains) <= valid:
                return _err(f"domain must be one of: {', '.join(sorted(valid))}, all")
            date_from = _date_arg(args, "date_from") or None
            date_to = _date_arg(args, "date_to") or None

            export_dir = working_directory / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            report: Dict[str, Any] = {}
            for name in domains:
                rows = await repo.export_rows(
                    user_id, name, date_from=date_from, date_to=date_to
                )
                if name == "docs":
                    docs_dir = export_dir / "docs"
                    docs_dir.mkdir(exist_ok=True)
                    paths = []
                    for row in rows:
                        stem = row.get("period") or str(row.get("id"))
                        doc_path = docs_dir / f"{row.get('kind')}-{stem}.md"
                        doc_path.write_text(
                            str(row.get("content") or ""), encoding="utf-8"
                        )
                        paths.append(str(doc_path))
                    report[name] = {"rows": len(rows), "paths": paths}
                else:
                    csv_path = export_dir / f"{name}.csv"
                    count = _write_export_csv(csv_path, rows)
                    report[name] = {"rows": count, "path": str(csv_path)}
            logger.info(
                "agent tool export_my_data",
                user_id=user_id,
                domains=domains,
                date_from=date_from,
                date_to=date_to,
            )
            return _text(report)

        tools.append(export_my_data)

    return tools
