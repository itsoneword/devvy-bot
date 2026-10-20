"""Pure parsing of Claude Code transcript JSONL entries.

One entry dict in, one ParsedEntry out (or None when the line contributes
nothing). No I/O, no SQL — the indexer owns offsets, dedupe and writes.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

from .models import (
    AgentSpawnInput,
    ApiCall,
    ContextItem,
    ParsedEntry,
    SpawnLink,
    ToolResult,
    ToolUse,
    TranscriptTurn,
)

# Spawn linkage — the ONLY two mechanisms allowed (no model heuristics).
# {8,}: real agent ids are 17 hex chars; the bare + of the plan's regex
# false-matched prose like "with agentId: 0" in tool output (measured).
AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-f-]{8,})")
TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>.*?<task-id>\s*([0-9a-f-]+)\s*</task-id>"
    r".*?<tool-use-id>\s*(toolu_[A-Za-z0-9]+)\s*</tool-use-id>",
    re.DOTALL,
)

# Window-rotation seed markers injected by the bot (window_seed.py). The
# labeled template (dv-e574) opens with <conversation_context>; the older two
# markers stay so pre-existing transcripts still classify as seeded.
SEED_MARKERS = ("<conversation_context>", "<session_notes>", "<recent_conversation>")

INPUT_SUMMARY_MAX = 300
TURN_EXCERPT_MAX = 200

_HANDLED_TYPES = frozenset(
    {"assistant", "user", "queue-operation", "attachment", "api-request-shape"}
)

# Context-size accounting (dv-aeca). Measured on 12 real transcripts: a tool
# result costs ~2.3 chars/token (code, JSON and Cyrillic are token-dense); an
# image block carries no text, ~1.2-1.6k tokens each. Estimates only — the
# store uses the real next-call input delta wherever it can attribute one.
CHARS_PER_TOKEN = 2.3
IMAGE_TOKENS_EST = 1500

# Tags the bot wraps around a user turn (bot-side prompt composition).
TURN_PREFIX_TAGS = ("family_reminders", "current_time")
SEED_TAGS = ("conversation_context", "session_notes", "recent_conversation")
SYSTEM_SECTION_TAGS = (
    "agent_identity",
    "shared_capabilities",
    "constraints",
    "user_facts",
    "family_facts",
    "memory_pointers",
    "runtime",
)


def normalize_ts(raw: str) -> str:
    """Normalize any ISO-8601 timestamp to UTC `YYYY-MM-DDTHH:MM:SS.mmmZ`.

    Transcripts use millisecond 'Z' form; bot.db uses microsecond '+00:00'.
    One fixed-width form keeps TEXT timestamps lexicographically sortable.
    """
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def is_seeded_text(text: str) -> bool:
    """First user entry starting with a seed marker = window-rotation seed."""
    return text.startswith(SEED_MARKERS)


def summarize_input(tool_input: dict[str, Any]) -> str | None:
    """<=300-char summary of a tool_use input; never the full content."""
    for key in ("description", "command", "file_path", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value[:INPUT_SUMMARY_MAX]
    return None


def estimate_tokens(chars: int, images: int = 0) -> int:
    """Rough token count of `chars` characters plus `images` image blocks."""
    return round(chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS_EST


def result_size(content: Any) -> tuple[int, int]:
    """(text chars, image blocks) of a tool_result `content` payload."""
    if isinstance(content, str):
        return len(content), 0
    chars = images = 0
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                chars += len(block.get("text") or "")
            elif kind == "image":
                images += 1
            else:
                chars += len(json.dumps(block, ensure_ascii=False))
    return chars, images


def split_user_text(text: str, ts: str) -> list[ContextItem]:
    """Size the bot's tagged sections of one user message: per-turn prefix
    (family reminders, clock), window seed, and the user's own words."""
    items: list[ContextItem] = []
    rest = text
    for tag in TURN_PREFIX_TAGS + SEED_TAGS:
        match = re.search(rf"<{tag}>.*?</{tag}>", rest, re.DOTALL)
        if match:
            kind = "turn_prefix" if tag in TURN_PREFIX_TAGS else "seed"
            items.append(ContextItem(kind, tag, match.end() - match.start(), ts))
            rest = rest[: match.start()] + rest[match.end() :]
    rest = rest.strip()
    if rest:
        items.append(ContextItem("user_text", "user", len(rest), ts))
    return items


def _text_of(content: Any) -> str:
    """Plain text of a message/tool_result content (str or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def parse_entry(entry: dict[str, Any], *, kind: str) -> ParsedEntry | None:
    """Dispatch one JSONL entry. `kind` is 'session' or 'subagent'."""
    entry_type = entry.get("type")
    if entry_type not in _HANDLED_TYPES:
        return None
    if entry_type == "assistant":
        return _parse_assistant(entry)
    if entry_type == "user":
        return _parse_user(entry, kind=kind)
    if entry_type == "attachment":
        return _parse_attachment(entry)
    if entry_type == "api-request-shape":
        return _parse_shape(entry)
    return _parse_queue_operation(entry)


def _parse_assistant(entry: dict[str, Any]) -> ParsedEntry | None:
    message = entry.get("message")
    ts_raw = entry.get("timestamp")
    if not isinstance(message, dict) or not isinstance(ts_raw, str):
        return None
    request_id = entry.get("requestId")
    model = message.get("model")
    # Synthetic entries: harness-injected, not real API traffic. Skip fully.
    if (
        not isinstance(request_id, str)
        or not request_id
        or model == "<synthetic>"
        or bool(entry.get("isApiErrorMessage"))
    ):
        return None
    if not isinstance(model, str):
        return None
    ts = normalize_ts(ts_raw)
    uuid = entry.get("uuid") if isinstance(entry.get("uuid"), str) else None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    cache_creation = usage.get("cache_creation")
    if not isinstance(cache_creation, dict):
        cache_creation = {}
    result = ParsedEntry(
        api_call=ApiCall(
            request_id=request_id,
            ts=ts,
            model=model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            cache_5m_tokens=int(cache_creation.get("ephemeral_5m_input_tokens") or 0),
            cache_1h_tokens=int(cache_creation.get("ephemeral_1h_input_tokens") or 0),
            entry_uuid=uuid,
        )
    )
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "thinking"):
                if block["type"] == "text":
                    size = len(block.get("text") or "")
                else:
                    # Thinking text is not stored in transcripts; the signature
                    # is the only trace. Its tokens are inferred, not measured.
                    size = len(block.get("signature") or "")
                result.context_items.append(
                    ContextItem(
                        "assistant_text" if block["type"] == "text" else "thinking",
                        block["type"],
                        size,
                        ts,
                        request_id,
                    )
                )
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_use_id = block.get("id")
            name = block.get("name")
            if not isinstance(tool_use_id, str) or not isinstance(name, str):
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            agent_input = None
            if name == "Agent":
                agent_input = AgentSpawnInput(
                    description=_opt_str(tool_input.get("description")),
                    subagent_type=_opt_str(tool_input.get("subagent_type")),
                    model=_opt_str(tool_input.get("model")),
                )
            result.tool_uses.append(
                ToolUse(
                    tool_use_id=tool_use_id,
                    name=name,
                    ts=ts,
                    request_id=request_id,
                    assistant_uuid=uuid,
                    input_summary=summarize_input(tool_input),
                    agent_input=agent_input,
                    input_chars=len(json.dumps(tool_input, ensure_ascii=False)),
                )
            )
    return result


def _parse_user(entry: dict[str, Any], *, kind: str) -> ParsedEntry | None:
    if entry.get("isMeta"):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    ts_raw = entry.get("timestamp")
    ts = normalize_ts(ts_raw) if isinstance(ts_raw, str) else None
    tool_use_result = entry.get("toolUseResult")
    tur_description = None
    if isinstance(tool_use_result, dict):
        tur_description = _opt_str(tool_use_result.get("description"))
    result = ParsedEntry()
    has_tool_result = False
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            has_tool_result = True
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            if ts is not None:
                chars, images = result_size(block.get("content"))
                result.tool_results.append(
                    ToolResult(
                        tool_use_id=tool_use_id,
                        result_ts=ts,
                        is_error=1 if block.get("is_error") else 0,
                        result_chars=chars,
                        result_images=images,
                    )
                )
            match = AGENT_ID_RE.search(_text_of(block.get("content")))
            if match:
                result.spawn_links.append(
                    SpawnLink(
                        agent_id=match.group(1),
                        tool_use_id=tool_use_id,
                        description=tur_description,
                    )
                )
    if not has_tool_result:
        text = _text_of(content)
        if text:
            result.user_text = text
            if ts is not None:
                result.context_items.extend(split_user_text(text, ts))
            # Bot (sdk-py) turns come from bot.db; everything else — cli,
            # sdk-cli, unknown — gets a transcript turn.
            if kind == "session" and entry.get("entrypoint") != "sdk-py" and ts:
                result.turn = TranscriptTurn(ts=ts, excerpt=text[:TURN_EXCERPT_MAX])
    if (
        result.tool_results
        or result.spawn_links
        or result.user_text is not None
        or result.turn is not None
        or result.context_items
    ):
        return result
    return None


def _parse_attachment(entry: dict[str, Any]) -> ParsedEntry | None:
    """Harness-injected <system-reminder> content (CLAUDE.md, skill list,
    token counter, ...), sized from its rendered text and named by type."""
    ts_raw = entry.get("timestamp")
    attachment = entry.get("attachment")
    rendered = entry.get("rendered")
    if not isinstance(ts_raw, str) or not isinstance(attachment, dict):
        return None
    if not isinstance(rendered, list):
        return None
    chars = sum(
        len(block["content"])
        for block in rendered
        if isinstance(block, dict) and isinstance(block.get("content"), str)
    )
    if chars <= 0:
        return None
    name = attachment.get("type")
    item = ContextItem(
        "reminder", name if isinstance(name, str) else "?", chars, normalize_ts(ts_raw)
    )
    return ParsedEntry(context_items=[item])


def _parse_shape(entry: dict[str, Any]) -> ParsedEntry | None:
    """The request's fixed part: system prompt sections and tool schemas."""
    ts_raw = entry.get("timestamp")
    shape = entry.get("shape")
    if not isinstance(ts_raw, str) or not isinstance(shape, dict):
        return None
    ts = normalize_ts(ts_raw)
    items: list[ContextItem] = []
    for block in shape.get("system") or []:
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str):
            continue
        covered = 0
        for tag in SYSTEM_SECTION_TAGS:
            # Line-anchored: prose inside other sections mentions "<tag>" too.
            match = re.search(rf"^<{tag}>.*?</{tag}>$", text, re.DOTALL | re.MULTILINE)
            if match:
                size = match.end() - match.start()
                items.append(ContextItem("shape_system", tag, size, ts))
                covered += size
        if len(text) > covered:
            items.append(ContextItem("shape_system", "base", len(text) - covered, ts))
    for tool in shape.get("tools") or []:
        if isinstance(tool, dict):
            name = tool.get("name")
            size = len(json.dumps(tool, ensure_ascii=False))
            items.append(
                ContextItem(
                    "shape_tools", name if isinstance(name, str) else "?", size, ts
                )
            )
    return ParsedEntry(context_items=items) if items else None


def _parse_queue_operation(entry: dict[str, Any]) -> ParsedEntry | None:
    content = entry.get("content")
    if not isinstance(content, str) or "<task-notification>" not in content:
        return None
    links = [
        SpawnLink(agent_id=task_id, tool_use_id=tool_use_id)
        for task_id, tool_use_id in TASK_NOTIFICATION_RE.findall(content)
    ]
    if not links:
        return None
    return ParsedEntry(spawn_links=links)


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
