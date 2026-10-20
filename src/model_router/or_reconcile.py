"""Daily backstop: our OpenRouter total vs OpenRouter's own (dv-d2f6).

Per-call costs come from ``/api/v1/generation`` (see turn_cost); this check
catches what that cannot: calls we never saw (a CLI side request, a plain
caller that records no cost, a script on the same key) and lookups that kept
their estimate. Once a day, shortly before the UTC day ends, the sum of
today's ``model_calls`` rows with provider=openrouter is compared with the
key's ``usage_daily`` (UTC day, includes every user of the key). Drift above
the threshold alerts the owner; otherwise one log line.

Report-only, never blocks. Hard spend limits belong on the key itself
(openrouter.ai -> Keys -> limit + daily reset), which OpenRouter enforces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

import httpx
import structlog

from .routes import PROVIDER_OPENROUTER, openrouter_key
from .turn_cost import OPENROUTER_KEY_URL

if TYPE_CHECKING:
    from src.storage.model_calls import ModelCallRepository

logger = structlog.get_logger()

# 23:50 UTC: OpenRouter's usage_daily is a UTC day, so is model_calls.ts.
RECONCILE_CRON = "50 23 * * *"
DRIFT_RATIO = 0.05
# Below this absolute gap a ratio means nothing (a few test calls).
DRIFT_FLOOR_USD = 0.01


@dataclass(frozen=True)
class Reconciliation:
    day: str
    ours: float
    calls: int
    theirs: float
    key_usage: float
    key_limit: Optional[float]
    key_limit_reset: Optional[str]

    @property
    def drift(self) -> float:
        return self.theirs - self.ours

    @property
    def alarming(self) -> bool:
        gap = abs(self.drift)
        return gap > DRIFT_FLOOR_USD and gap > DRIFT_RATIO * max(self.theirs, 1e-9)

    def text(self) -> str:
        limit = (
            f"key limit ${self.key_limit:g}, reset {self.key_limit_reset or 'never'}"
            if self.key_limit is not None
            else "key has no limit"
        )
        return (
            f"OpenRouter cost drift {self.day}: OpenRouter billed "
            f"${self.theirs:.4f}, the bot recorded ${self.ours:.4f} over "
            f"{self.calls} calls (gap ${self.drift:+.4f}). Unrecorded spend on "
            f"this key: test scripts, plain calls, or lookups that kept their "
            f"estimate. Lifetime usage ${self.key_usage:.2f}; {limit}."
        )


async def fetch_key_info(key: str, timeout: float = 20.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key}"}
        )
        resp.raise_for_status()
        return dict(resp.json().get("data") or {})


class OpenRouterReconciler:
    def __init__(
        self,
        model_calls: "ModelCallRepository",
        notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        key_fn: Callable[[], Optional[str]] = openrouter_key,
        key_info: Callable[[str], Awaitable[dict]] = fetch_key_info,
    ) -> None:
        self._calls = model_calls
        self._notify = notify_callback
        self._key_fn = key_fn
        self._key_info = key_info

    async def run(self, now: Optional[datetime] = None) -> Optional[Reconciliation]:
        """Compare today (UTC); None when no key is configured."""
        key = self._key_fn()
        if not key:
            return None
        day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
        info: Any = await self._key_info(key)
        ours, calls = await self._calls.provider_cost_on(PROVIDER_OPENROUTER, day)
        rec = Reconciliation(
            day=day,
            ours=ours,
            calls=calls,
            theirs=float(info.get("usage_daily") or 0.0),
            key_usage=float(info.get("usage") or 0.0),
            key_limit=(float(info["limit"]) if info.get("limit") is not None else None),
            key_limit_reset=info.get("limit_reset"),
        )
        log = logger.warning if rec.alarming else logger.info
        log(
            "OpenRouter daily reconciliation",
            day=day,
            ours=round(ours, 6),
            theirs=round(rec.theirs, 6),
            drift=round(rec.drift, 6),
            calls=calls,
            key_limit=rec.key_limit,
            key_limit_reset=rec.key_limit_reset,
        )
        if rec.alarming and self._notify is not None:
            await self._notify(rec.text())
        return rec


def register_daily_reconcile(
    scheduler: Any,
    model_calls: "ModelCallRepository",
    notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    cron_expression: str = RECONCILE_CRON,
) -> OpenRouterReconciler:
    """Register the check as a native scheduler job (same mechanism as the
    fact-quality check). Runs every day; does nothing while no key is set."""
    service = OpenRouterReconciler(model_calls, notify_callback=notify_callback)

    async def _daily_or_reconcile() -> None:
        try:
            await service.run()
        except Exception as e:
            logger.error("OpenRouter reconciliation failed", error=str(e))

    scheduler.add_native_job(
        "openrouter-reconcile", cron_expression, _daily_or_reconcile
    )
    return service
