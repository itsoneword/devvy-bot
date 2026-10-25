"""Data models for storage.

Using dataclasses for simplicity and type safety.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Dict, Optional

import aiosqlite


def _parse_datetime(value: Any) -> Any:
    """Parse datetime values from SQLite rows.

    With sqlite3 converters enabled, values may already be datetime instances.
    Without converters, values may be ISO strings.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


@dataclass
class UserModel:
    """User data model."""

    user_id: int
    telegram_username: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_active: Optional[datetime] = None
    is_allowed: bool = False
    total_cost: float = 0.0
    message_count: int = 0
    session_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert datetime to ISO format
        for key in ["first_seen", "last_active"]:
            if data[key]:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "UserModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        for field in ["first_seen", "last_active"]:
            data[field] = _parse_datetime(data.get(field))

        return cls(**data)


@dataclass
class SessionModel:
    """Session data model."""

    session_id: str
    user_id: int
    project_path: str
    created_at: datetime
    last_used: datetime
    total_cost: float = 0.0
    total_turns: int = 0
    message_count: int = 0
    is_active: bool = True
    # Audit trail for the soft delete (schema v21): when the resume mapping
    # was retired and by which code path. None while the session is live.
    deactivated_at: Optional[datetime] = None
    deactivated_reason: Optional[str] = None
    # Codex thread bookkeeping (schema v22, dv-ebde). backend is None for
    # Claude rows; see src/storage/codex_sessions.py.
    backend: Optional[str] = None
    thread_head: int = 0
    thread_total_input: int = 0
    thread_total_output: int = 0
    cumulative_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert datetime to ISO format
        for key in ["created_at", "last_used", "deactivated_at"]:
            if data[key]:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "SessionModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        for field in ["created_at", "last_used", "deactivated_at"]:
            data[field] = _parse_datetime(data.get(field))

        return cls(**data)

    def is_expired(self, timeout_hours: int) -> bool:
        """Check if session has expired."""
        if not self.last_used:
            return True

        age = datetime.now(UTC) - self.last_used
        return age.total_seconds() > (timeout_hours * 3600)


@dataclass
class ProjectThreadModel:
    """Project-thread mapping data model."""

    project_slug: str
    chat_id: int
    message_thread_id: int
    topic_name: str
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        for key in ["created_at", "updated_at"]:
            if data[key]:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "ProjectThreadModel":
        """Create from database row."""
        data = dict(row)

        for field in ["created_at", "updated_at"]:
            val = data.get(field)
            if val and isinstance(val, str):
                data[field] = datetime.fromisoformat(val)
        data["is_active"] = bool(data.get("is_active", True))

        return cls(**data)


@dataclass
class MessageModel:
    """Message data model."""

    session_id: str
    user_id: int
    timestamp: datetime
    prompt: str
    message_id: Optional[int] = None
    response: Optional[str] = None
    cost: float = 0.0
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    window_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert datetime to ISO format
        if data["timestamp"]:
            data["timestamp"] = data["timestamp"].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "MessageModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        data["timestamp"] = _parse_datetime(data.get("timestamp"))

        return cls(**data)


@dataclass
class AuditLogModel:
    """Audit log data model."""

    user_id: int
    event_type: str
    timestamp: datetime
    id: Optional[int] = None
    event_data: Optional[Dict[str, Any]] = None
    success: bool = True
    ip_address: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert datetime to ISO format
        if data["timestamp"]:
            data["timestamp"] = data["timestamp"].isoformat()
        # Convert event_data to JSON string if present
        if data["event_data"]:
            data["event_data"] = json.dumps(data["event_data"])
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "AuditLogModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        data["timestamp"] = _parse_datetime(data.get("timestamp"))

        # Parse JSON fields
        if data.get("event_data"):
            try:
                data["event_data"] = json.loads(data["event_data"])
            except (json.JSONDecodeError, TypeError):
                data["event_data"] = {}

        return cls(**data)


@dataclass
class CostTrackingModel:
    """Cost tracking data model."""

    user_id: int
    date: str  # ISO date format (YYYY-MM-DD)
    daily_cost: float = 0.0
    request_count: int = 0
    id: Optional[int] = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "CostTrackingModel":
        """Create from database row."""
        return cls(**dict(row))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class UserTokenModel:
    """User token data model."""

    user_id: int
    token_hash: str
    created_at: datetime
    token_id: Optional[int] = None
    expires_at: Optional[datetime] = None
    last_used: Optional[datetime] = None
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert datetime to ISO format
        for key in ["created_at", "expires_at", "last_used"]:
            if data[key]:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "UserTokenModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        for field in ["created_at", "expires_at", "last_used"]:
            data[field] = _parse_datetime(data.get(field))

        return cls(**data)

    def is_expired(self) -> bool:
        """Check if token has expired."""
        if not self.expires_at:
            return False
        return datetime.now(UTC) > self.expires_at


@dataclass
class SessionTrackingModel:
    """Per-turn token tracking data model."""

    session_id: str
    user_id: int
    input_tokens: int
    output_tokens: int
    cumulative_tokens: int
    cost: float = 0.0
    timestamp: Optional[datetime] = None
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        if data["timestamp"]:
            data["timestamp"] = data["timestamp"].isoformat()
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "SessionTrackingModel":
        """Create from database row."""
        data = dict(row)
        data["timestamp"] = _parse_datetime(data.get("timestamp"))
        return cls(**data)


@dataclass
class MemoryExtractionModel:
    """Memory extraction record data model."""

    session_id: str
    user_id: int
    prompt_hash: str
    validation_status: str = "pending"
    raw_response: Optional[str] = None
    parsed_insights: Optional[Dict[str, Any]] = None
    target_files: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    cumulative_tokens_at_run: Optional[int] = None
    timestamp: Optional[datetime] = None
    id: Optional[int] = None
    # Per-run pipeline breakdown (dv-c5d9): found/already_saved/added/
    # superseded/... plus reconcile verdicts. NULL for pre-pipeline rows.
    op_stats: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        if data["timestamp"]:
            data["timestamp"] = data["timestamp"].isoformat()
        if data["parsed_insights"]:
            data["parsed_insights"] = json.dumps(data["parsed_insights"])
        if data["target_files"]:
            data["target_files"] = json.dumps(data["target_files"])
        if data["op_stats"]:
            data["op_stats"] = json.dumps(data["op_stats"])
        return data

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "MemoryExtractionModel":
        """Create from database row."""
        data = dict(row)
        data["timestamp"] = _parse_datetime(data.get("timestamp"))

        # Parse JSON fields
        for json_field in ["parsed_insights", "target_files", "op_stats"]:
            val = data.get(json_field)
            if val and isinstance(val, str):
                try:
                    data[json_field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    data[json_field] = None

        return cls(**data)
