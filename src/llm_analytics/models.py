"""Typed rows produced by the transcript parser and scan bookkeeping."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ApiCall:
    """One real API request (synthetic entries are never parsed into this)."""

    request_id: str
    ts: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cache_5m_tokens: int
    cache_1h_tokens: int
    entry_uuid: str | None


@dataclass(frozen=True)
class AgentSpawnInput:
    """Input of an `Agent` tool_use block — feeds subagent_runs labels."""

    description: str | None
    subagent_type: str | None
    model: str | None


@dataclass(frozen=True)
class ToolUse:
    tool_use_id: str
    name: str
    ts: str
    request_id: str | None
    assistant_uuid: str | None
    input_summary: str | None
    agent_input: AgentSpawnInput | None


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    result_ts: str
    is_error: int


@dataclass(frozen=True)
class SpawnLink:
    """Best-effort subagent spawn linkage (agentId regex / task-notification)."""

    agent_id: str
    tool_use_id: str | None
    description: str | None = None


@dataclass(frozen=True)
class TranscriptTurn:
    ts: str
    excerpt: str | None


@dataclass
class ParsedEntry:
    """Everything one JSONL line contributes to the index."""

    api_call: ApiCall | None = None
    tool_uses: list[ToolUse] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    spawn_links: list[SpawnLink] = field(default_factory=list)
    turn: TranscriptTurn | None = None
    user_text: str | None = None


@dataclass
class ScanStats:
    files_seen: int = 0
    files_scanned: int = 0
    files_skipped: int = 0
    files_truncated: int = 0
    lines_consumed: int = 0
    api_calls_inserted: int = 0
    tool_calls_inserted: int = 0
    sessions_touched: int = 0
    elapsed_s: float = 0.0
