"""What filled the context window, call by call (dv-aeca).

Pure functions, no I/O. The transcript gives one hard number per API call —
the context it was sent (input + cache read + cache creation). Between two
consecutive calls that number grows by exactly

    delta = output tokens of the previous call + tokens of everything the
            harness appended since (tool results, reminders, user text)

so the growth of each step is *measured*, and only its split between the
pieces that arrived together is estimated (by size). Row 0 is the fixed part
(system prompt, tool schemas, CLAUDE.md, skill list, window seed), split the
same way.

Categories: base_system, base_tools, base_reminder, base_unknown, seed,
turn_prefix, user_text, reminder, tool_result, subagent_result, tool_use,
assistant_text, thinking, unattributed.
"""

import math
from typing import Any

from .parser import CHARS_PER_TOKEN, IMAGE_TOKENS_EST

# Visible model output (text, tool_use JSON) measured ~2.0 chars/token on the
# bot's transcripts; thinking text is not stored, so its tokens are the
# remainder of the call's output tokens.
OUTPUT_CHARS_PER_TOKEN = 2.0
# Tool whose result is another model's report, not a tool's own output.
SUBAGENT_TOOL = "Agent"
INPUT_KINDS = frozenset({"reminder", "user_text", "turn_prefix", "seed"})


def _entry(
    category: str,
    name: str,
    tokens: float,
    chars: int = 0,
    tool_use_id: str | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "name": name,
        "tokens": round(tokens),
        "chars": chars,
        "tool_use_id": tool_use_id,
    }


def _split(
    total: float, weighted: list[tuple[dict[str, Any], float]]
) -> list[dict[str, Any]]:
    """Share `total` tokens over entries in proportion to their weights."""
    weight_sum = sum(weight for _, weight in weighted)
    out = []
    for proto, weight in weighted:
        share = total * weight / weight_sum if weight_sum else 0.0
        out.append({**proto, "tokens": round(share)})
    return out


def _base_entries(
    context: int, shape_items: list[dict[str, Any]], inputs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    weighted: list[tuple[dict[str, Any], float]] = []
    for item in shape_items:
        category = "base_system" if item["kind"] == "shape_system" else "base_tools"
        weighted.append(
            (_entry(category, item["name"], 0, item["chars"]), item["chars"])
        )
    for item in inputs:
        category = "base_reminder" if item["kind"] == "reminder" else item["kind"]
        weighted.append(
            (_entry(category, item["name"], 0, item["chars"]), item["chars"])
        )
    if not shape_items:
        # Older CLI versions logged no request shape: whatever the inputs we
        # can see do not explain is the unmeasured fixed part.
        seen = sum(item["chars"] for item in inputs) / CHARS_PER_TOKEN
        unknown = max(0.0, context - seen)
        entries = [
            _entry(
                proto["category"], proto["name"], weight / CHARS_PER_TOKEN, int(weight)
            )
            for proto, weight in weighted
        ]
        entries.append(_entry("base_unknown", "system+tools", unknown))
        return entries
    return _split(context, weighted)


def _output_entries(
    out_tokens: int,
    tool_uses: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Split one call's output tokens into text / tool_use / hidden thinking."""
    visible: list[tuple[dict[str, Any], float]] = []
    for tool in tool_uses:
        chars = int(tool.get("input_chars") or 0) + len(tool["name"]) + 30
        visible.append(
            (_entry("tool_use", tool["name"], 0, chars, tool["tool_use_id"]), chars)
        )
    thinking = False
    for item in items:
        if item["kind"] == "assistant_text":
            visible.append(
                (_entry("assistant_text", "text", 0, item["chars"]), item["chars"])
            )
        elif item["kind"] == "thinking":
            thinking = True
    vis_chars = sum(weight for _, weight in visible)
    entries: list[dict[str, Any]] = []
    vis_tokens = float(out_tokens)
    if thinking:
        vis_tokens = min(float(out_tokens), vis_chars / OUTPUT_CHARS_PER_TOKEN)
        entries.append(_entry("thinking", "hidden", out_tokens - vis_tokens))
    if visible:
        entries.extend(_split(vis_tokens, visible))
    elif not thinking and out_tokens:
        entries.append(_entry("assistant_text", "text", out_tokens))
    return entries


def build_rows(
    calls: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per call (ordered by ts): {request_id, ts, context,
    output_tokens, delta, entries}. `entries` sum to `context` for row 0 and
    to `delta` for the rest. calls: request_id, ts, input_tokens,
    output_tokens, cache_read_tokens, cache_creation_tokens. tools:
    tool_use_id, name, request_id, result_ts, result_chars, result_images,
    input_chars. items: kind, name, chars, ts, request_id."""
    if not calls:
        return []
    contexts = [
        int(c["input_tokens"])
        + int(c["cache_read_tokens"])
        + int(c["cache_creation_tokens"])
        for c in calls
    ]
    tools_by_request: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        tools_by_request.setdefault(str(tool["request_id"]), []).append(tool)
    items_by_request: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["request_id"]:
            items_by_request.setdefault(str(item["request_id"]), []).append(item)
    inputs = sorted(
        (i for i in items if i["kind"] in INPUT_KINDS), key=lambda i: str(i["ts"])
    )
    results = sorted(
        (t for t in tools if t.get("result_ts")), key=lambda t: str(t["result_ts"])
    )
    shapes = [i for i in items if i["kind"] in ("shape_system", "shape_tools")]

    rows: list[dict[str, Any]] = []
    in_pos = res_pos = 0
    prev_ts = ""
    for idx, call in enumerate(calls):
        ts = str(call["ts"])
        seg_inputs: list[dict[str, Any]] = []
        while in_pos < len(inputs) and str(inputs[in_pos]["ts"]) <= ts:
            if str(inputs[in_pos]["ts"]) > prev_ts:
                seg_inputs.append(inputs[in_pos])
            in_pos += 1
        seg_results: list[dict[str, Any]] = []
        while res_pos < len(results) and str(results[res_pos]["result_ts"]) <= ts:
            if str(results[res_pos]["result_ts"]) > prev_ts:
                seg_results.append(results[res_pos])
            res_pos += 1
        context = contexts[idx]
        row: dict[str, Any] = {
            "request_id": call["request_id"],
            "ts": ts,
            "context": context,
            "output_tokens": int(call["output_tokens"]),
            "delta": None,
            "entries": [],
        }
        if idx == 0:
            first_shape_ts = min((str(s["ts"]) for s in shapes), default=None)
            shape_items = [s for s in shapes if str(s["ts"]) == first_shape_ts]
            row["entries"] = _base_entries(context, shape_items, seg_inputs)
        else:
            prev = calls[idx - 1]
            delta = context - contexts[idx - 1]
            row["delta"] = delta
            prev_rid = str(prev["request_id"])
            entries = _output_entries(
                int(prev["output_tokens"]),
                tools_by_request.get(prev_rid, []),
                items_by_request.get(prev_rid, []),
            )
            remainder = delta - int(prev["output_tokens"])
            weighted: list[tuple[dict[str, Any], float]] = []
            for item in seg_inputs:
                weighted.append(
                    (
                        _entry(item["kind"], item["name"], 0, item["chars"]),
                        item["chars"],
                    )
                )
            for tool in seg_results:
                chars = int(tool.get("result_chars") or 0)
                images = int(tool.get("result_images") or 0)
                category = (
                    "subagent_result"
                    if tool["name"] == SUBAGENT_TOOL
                    else "tool_result"
                )
                weight = chars + images * IMAGE_TOKENS_EST * CHARS_PER_TOKEN
                weighted.append(
                    (
                        _entry(category, tool["name"], 0, chars, tool["tool_use_id"]),
                        weight,
                    )
                )
            if remainder >= 0 and any(w for _, w in weighted):
                entries.extend(_split(remainder, weighted))
            elif remainder:
                entries.append(
                    _entry(
                        "unattributed" if remainder > 0 else "context_shrink",
                        "?",
                        remainder,
                    )
                )
            row["entries"] = entries
        rows.append(row)
        prev_ts = ts
    return rows


def _percentile(values: list[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def summarize(rows: list[dict[str, Any]], *, top: int = 10) -> dict[str, Any]:
    """Roll a window's rows up: tokens by category, by tool, largest items."""
    categories: dict[str, int] = {}
    per_tool: dict[str, dict[str, Any]] = {}
    biggest: list[dict[str, Any]] = []
    for number, row in enumerate(rows, 1):
        for entry in row["entries"]:
            tokens = entry["tokens"]
            categories[entry["category"]] = (
                categories.get(entry["category"], 0) + tokens
            )
            if entry["category"] in ("tool_result", "subagent_result"):
                stat = per_tool.setdefault(
                    entry["name"], {"name": entry["name"], "samples": [], "chars": 0}
                )
                stat["samples"].append(tokens)
                stat["chars"] += entry["chars"]
            if not entry["category"].startswith("base_"):
                biggest.append({**entry, "call": number, "ts": row["ts"]})
    tools = []
    for stat in per_tool.values():
        samples = stat.pop("samples")
        tools.append(
            {
                **stat,
                "calls": len(samples),
                "tokens": sum(samples),
                "p95": _percentile(samples, 0.95),
                "max": max(samples),
            }
        )
    tools.sort(key=lambda t: -t["tokens"])
    biggest.sort(key=lambda e: -e["tokens"])
    return {
        "calls": len(rows),
        "context_end": rows[-1]["context"] if rows else 0,
        "categories": [
            {"category": name, "tokens": tokens}
            for name, tokens in sorted(categories.items(), key=lambda kv: -kv[1])
        ],
        "tools": tools,
        "top_items": biggest[:top],
    }
