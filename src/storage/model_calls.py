"""One row per model call (schema v25, dv-11a5).

Written by the model router for every call it makes, so any answer can be
traced to a harness, its version and a model. Tokens and cost are filled where
the harness reports them; dv-830d completes that on every path.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from .database import DatabaseManager


@dataclass(frozen=True)
class ModelCall:
    purpose: str
    kind: str  # 'turn' | 'plain' | 'decide'
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
    # 'exact' (provider-billed) | 'estimate' (tokens x price table); None where
    # the harness's own figure is used (subscription, Codex). Migration 0004.
    cost_source: Optional[str] = None


class ModelCallRepository:
    """Append-only writer for ``model_calls``."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def record(self, call: ModelCall) -> Optional[int]:
        """Insert one row; returns its id (for :meth:`settle_cost`)."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO model_calls (
                    purpose, kind, harness, harness_version, provider, model,
                    attended, profile, user_id, session_id, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    cost_usd, duration_ms, outcome, error, cost_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                astuple(call),
            )
            await conn.commit()
            return cursor.lastrowid

    async def settle_cost(self, row_id: int, cost_usd: float, source: str) -> None:
        """Replace a row's estimated cost with the settled one (dv-d2f6)."""
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE model_calls SET cost_usd = ?, cost_source = ? WHERE id = ?",
                (cost_usd, source, row_id),
            )
            await conn.commit()

    async def provider_cost_on(self, provider: str, day: str) -> Tuple[float, int]:
        """(USD sum, row count) of ``provider``'s calls on UTC ``day``
        (YYYY-MM-DD), for the daily reconciliation against the provider."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM model_calls "
                "WHERE provider = ? AND date(ts) = ?",
                (provider, day),
            )
            row = await cursor.fetchone()
        if row is None:
            return 0.0, 0
        return float(row[0] or 0.0), int(row[1] or 0)
