"""One row per model call (schema v25, dv-11a5).

Written by the model router for every call it makes, so any answer can be
traced to a harness, its version and a model. Tokens and cost are filled where
the harness reports them; dv-830d completes that on every path.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .database import DatabaseManager


@dataclass(frozen=True)
class ModelCall:
    purpose: str
    kind: str  # 'turn' | 'plain'
    harness: str
    harness_version: Optional[str]
    provider: str
    model: Optional[str]
    attended: bool
    profile: Optional[str]
    user_id: Optional[int]
    session_id: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: Optional[float]
    duration_ms: int
    outcome: str  # 'ok' | 'error' | 'timeout'
    error: Optional[str]


class ModelCallRepository:
    """Append-only writer for ``model_calls``."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def record(self, call: ModelCall) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO model_calls (
                    purpose, kind, harness, harness_version, provider, model,
                    attended, profile, user_id, session_id, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    cost_usd, duration_ms, outcome, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                astuple(call),
            )
            await conn.commit()
