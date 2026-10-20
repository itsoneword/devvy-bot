"""Per-call cost of one agent turn, and our own per-request cap (dv-d2f6).

Design: docs/analysis/dv-d2f6-cost-caps.md (rev 2). The CLI's stream-json
``assistant`` event carries a top-level ``request_id``; on OpenRouter that is
the generation id (``gen-...``) and ``GET /api/v1/generation?id=`` returns the
exact ``total_cost`` ~6 s after the call. Every call is charged as soon as it
ends: OpenRouter's own ``usage.cost`` from the call's final SSE event when the
stream has it (measured 2026-10-17, CLI 2.1.302), else the conservative route
price (:func:`routes.cost_for`); the background lookup then replaces either
with the record of truth. The running sum is what the turn's cap is checked
against. The CLI's own ``max_budget_usd`` is off on every
route: this meter is the one enforcement point.

The subscription route uses the meter for the cap only (Anthropic list price,
no lookups); its reported cost stays the CLI's ``total_cost_usd``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import httpx
import structlog

from .routes import PROVIDER_OPENROUTER, Route, cost_for

logger = structlog.get_logger()

OPENROUTER_GENERATION_URL = "https://openrouter.ai/api/v1/generation"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
GENERATION_PREFIX = "gen-"

COST_EXACT = "exact"
COST_ESTIMATE = "estimate"

# Seconds to sleep before each /generation attempt. The record shows up ~6 s
# after the call; the tail covers a slow minute. ~42 s in total.
LOOKUP_DELAYS = (3.0, 3.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
# Upper bound on waiting for every pending lookup once the turn is over.
SETTLE_TIMEOUT_SECONDS = 60.0

Lookup = Callable[[str], Awaitable[Optional[float]]]

# Background tasks must be referenced or the loop may drop them mid-flight.
_BACKGROUND: Set["asyncio.Task[Any]"] = set()


def _keep(task: "asyncio.Task[Any]") -> "asyncio.Task[Any]":
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


class GenerationLookup:
    """``total_cost`` of one OpenRouter generation, retried until it exists.

    None on any failure (no key, 401, still 404 after the last retry,
    network): the caller keeps the estimate and flags the row.
    """

    def __init__(
        self,
        key: Optional[str],
        *,
        delays: tuple = LOOKUP_DELAYS,
        url: str = OPENROUTER_GENERATION_URL,
        timeout: float = 10.0,
    ) -> None:
        self._key = key
        self._delays = delays
        self._url = url
        self._timeout = timeout

    async def __call__(self, generation_id: str) -> Optional[float]:
        if not self._key:
            return None
        headers = {"Authorization": f"Bearer {self._key}"}
        last = "no attempt"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for delay in self._delays:
                await asyncio.sleep(delay)
                try:
                    resp = await client.get(
                        self._url, params={"id": generation_id}, headers=headers
                    )
                except httpx.HTTPError as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    continue
                if resp.status_code in (401, 403):
                    last = f"http {resp.status_code}"
                    break
                if resp.status_code != 200:
                    last = f"http {resp.status_code}"
                    continue
                try:
                    cost = (resp.json().get("data") or {}).get("total_cost")
                except ValueError:
                    last = "bad json"
                    continue
                if cost is not None:
                    return float(cost)
                last = "no total_cost yet"
        logger.warning(
            "OpenRouter generation lookup failed; keeping the estimate",
            generation_id=generation_id,
            reason=last,
        )
        return None


@dataclass
class _Call:
    key: str
    request_id: Optional[str] = None
    model: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    estimate: Optional[float] = None
    # OpenRouter's own charge, in the call's final SSE ``message_delta``
    # usage (``usage.cost``), visible with include_partial_messages. 0 s lag.
    billed: Optional[float] = None
    # ``/generation`` total_cost, ~6 s later: the record of truth.
    exact: Optional[float] = None
    assistant_seen: bool = False
    lookup: Optional["asyncio.Task[None]"] = None

    @property
    def cost(self) -> Optional[float]:
        for value in (self.exact, self.billed, self.estimate):
            if value is not None:
                return value
        return None

    @property
    def known(self) -> bool:
        """Priced by the provider, not by our table."""
        return self.exact is not None or self.billed is not None


@dataclass(frozen=True)
class Settlement:
    """A turn's final cost once every lookup has returned (or given up)."""

    cost: Optional[float]
    source: str  # COST_EXACT | COST_ESTIMATE
    reported: Optional[float]  # what the turn reported before settling

    @property
    def delta(self) -> float:
        return (self.cost or 0.0) - (self.reported or 0.0)


@dataclass
class TurnCostMeter:
    """Running USD sum of one turn's model calls, one entry per API call.

    Feed it every raw stream-json message (:meth:`observe`). Two event kinds
    carry a call's cost data, keyed by the API message id:

    * ``assistant`` (one per content block): ``request_id`` (the OpenRouter
      generation id) and, on the subscription, the call's usage. On
      OpenRouter its usage is all zeros (measured, CLI 2.1.302).
    * ``stream_event`` ``message_start`` / ``message_delta`` (only with
      include_partial_messages): the delta has the call's real usage and, on
      OpenRouter, ``usage.cost`` = the provider's charge.

    Per call: cost = /generation truth > stream ``usage.cost`` > tokens x the
    conservative price table. Calls whose assistant events lack
    ``request_id`` are counted in :attr:`missing_request_ids` (the live
    smoke fails on it). Sub-agent calls count too.
    """

    route: Route
    cap: Optional[float] = None
    lookup: Optional[Lookup] = None
    calls: Dict[str, _Call] = field(default_factory=dict)
    # The last observed event asks for a tool: the turn will spend more.
    wants_more: bool = False
    _open: Dict[Optional[str], str] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(c.cost or 0.0 for c in self.calls.values())

    @property
    def priced(self) -> bool:
        """Every call has a USD figure (estimate or provider-billed)."""
        return all(c.cost is not None for c in self.calls.values())

    @property
    def over_cap(self) -> bool:
        return self.cap is not None and self.cap > 0 and self.total >= self.cap

    @property
    def source(self) -> str:
        if self.calls and all(c.known for c in self.calls.values()):
            return COST_EXACT
        return COST_ESTIMATE

    @property
    def missing_request_ids(self) -> int:
        return sum(
            1 for c in self.calls.values() if c.assistant_seen and not c.request_id
        )

    def _call(self, key: Optional[str]) -> _Call:
        key = key or f"anon:{len(self.calls)}"
        call = self.calls.get(key)
        if call is None:
            call = _Call(key=key)
            self.calls[key] = call
        return call

    def _merge_usage(self, call: _Call, usage: Dict[str, Any]) -> None:
        call.usage = {k: _max(call.usage.get(k), v) for k, v in usage.items()} | {
            k: v for k, v in call.usage.items() if k not in usage
        }
        call.estimate = cost_for(self.route, model=call.model, usage=call.usage)

    def observe(self, raw: Any) -> bool:
        """Account one raw message; True when it belonged to a model call."""
        self.wants_more = False
        if not isinstance(raw, dict):
            return False
        kind = raw.get("type")
        if kind == "assistant":
            return self._observe_assistant(raw)
        if kind == "stream_event":
            return self._observe_stream(raw)
        return False

    def _observe_assistant(self, raw: Dict[str, Any]) -> bool:
        message = raw.get("message")
        if not isinstance(message, dict):
            return False
        call = self._call(message.get("id"))
        call.assistant_seen = True
        call.model = call.model or message.get("model")
        request_id = raw.get("request_id")
        if isinstance(request_id, str) and request_id and not call.request_id:
            call.request_id = request_id
            self._start_lookup(call)
        usage = message.get("usage")
        if isinstance(usage, dict):
            self._merge_usage(call, usage)
        elif call.estimate is None:
            call.estimate = cost_for(self.route, model=call.model, usage={})
        self.wants_more = has_tool_use(raw)
        return True

    def _observe_stream(self, raw: Dict[str, Any]) -> bool:
        event = raw.get("event")
        if not isinstance(event, dict):
            return False
        parent = raw.get("parent_tool_use_id")
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            call = self._call(message.get("id"))
            call.model = call.model or message.get("model")
            self._open[parent] = call.key
            usage = message.get("usage")
            if isinstance(usage, dict):
                self._merge_usage(call, usage)
            return True
        if etype == "message_delta":
            call = self._call(self._open.get(parent))
            usage = event.get("usage")
            if isinstance(usage, dict):
                self._merge_usage(call, usage)
                billed = usage.get("cost")
                if isinstance(billed, (int, float)) and self.route.is_third_party:
                    call.billed = float(billed)
            stop = (event.get("delta") or {}).get("stop_reason")
            self.wants_more = stop == "tool_use"
            return True
        return False

    def _start_lookup(self, call: _Call) -> None:
        if (
            self.lookup is None
            or self.route.provider != PROVIDER_OPENROUTER
            or not call.request_id
            or not call.request_id.startswith(GENERATION_PREFIX)
        ):
            return
        lookup = self.lookup
        gen_id = call.request_id

        async def _resolve() -> None:
            try:
                exact = await lookup(gen_id)
            except Exception as exc:  # never let a lookup break the turn
                logger.warning("generation lookup crashed", error=str(exc))
                return
            if exact is not None:
                call.exact = exact

        call.lookup = asyncio.ensure_future(_resolve())

    @property
    def pending(self) -> List["asyncio.Task[None]"]:
        return [
            c.lookup
            for c in self.calls.values()
            if c.lookup is not None and not c.lookup.done()
        ]

    def reported_cost(self) -> Optional[float]:
        """The figure the turn reports now: estimates plus truths so far."""
        return self.total if self.priced else None

    def settle(
        self, timeout: float = SETTLE_TIMEOUT_SECONDS
    ) -> Optional["asyncio.Task[Settlement]"]:
        """A task resolving to the turn's final cost; None if nothing pends."""
        pending = self.pending
        if not pending:
            return None
        reported = self.reported_cost()

        async def _wait() -> Settlement:
            done, still = await asyncio.wait(pending, timeout=timeout)
            for task in still:
                task.cancel()
            settled = Settlement(
                cost=self.reported_cost(), source=self.source, reported=reported
            )
            logger.info(
                "Turn cost settled",
                calls=len(self.calls),
                estimate=reported,
                cost=settled.cost,
                source=settled.source,
            )
            return settled

        return _keep(asyncio.ensure_future(_wait()))  # type: ignore[return-value]


def when_settled(
    settlement: Optional["asyncio.Task[Settlement]"],
    apply: Callable[[Settlement], Awaitable[None]],
    *,
    what: str,
) -> None:
    """Run ``apply`` with the settled cost in the background; never raises.

    The single way every cost sink (model_calls, the USD ledgers, the
    limiter) swaps its estimate for the truth.
    """
    if settlement is None:
        return

    async def _run() -> None:
        try:
            settled = await settlement
            if settled.cost is None:
                return
            if settled.source != COST_EXACT and abs(settled.delta) < 1e-9:
                return
            await apply(settled)
        except Exception as exc:
            logger.warning("cost true-up failed", sink=what, error=str(exc))

    _keep(asyncio.ensure_future(_run()))


def has_tool_use(raw: Any) -> bool:
    """The assistant event asks for a tool, so the turn will spend more."""
    if not isinstance(raw, dict):
        return False
    content = (raw.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )


def _max(a: Any, b: Any) -> Any:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return max(a, b)
    return b if b is not None else a


__all__ = [
    "COST_ESTIMATE",
    "COST_EXACT",
    "GenerationLookup",
    "Settlement",
    "TurnCostMeter",
    "has_tool_use",
    "when_settled",
]


class SessionCostTracker:
    """Per-turn cost from the CLI's session-cumulative ``total_cost_usd``.

    On a resumed session ``ResultMessage.total_cost_usd`` is the session's
    running total, not the turn's (dv-4517). The turn's cost is the difference
    to the previous total of the same session. The last total per session is
    kept in memory; after a restart a resumed session has no baseline, so the
    caller's token-priced ``fallback`` is used for that one turn.
    """

    def __init__(self, max_sessions: int = 2048) -> None:
        self._totals: dict[str, float] = {}
        self._max = max_sessions

    def turn_cost(
        self,
        total: float,
        *,
        resumed_from: Optional[str],
        final_session_id: Optional[str],
        fallback: Optional[float] = None,
    ) -> float:
        """Cost of this turn; remembers ``total`` under ``final_session_id``."""
        prev = self._totals.get(resumed_from) if resumed_from else None
        if not resumed_from:
            cost = total  # fresh session: the total is the turn
        elif prev is None:
            # Resumed with no baseline (restart): never record the whole
            # session total as one turn.
            cost = fallback if fallback is not None else 0.0
            cost = min(cost, total) if total > 0 else cost
        elif total >= prev:
            cost = total - prev
        else:
            cost = total  # counter reset (new CLI session / fork)
        if final_session_id and total > 0:
            if len(self._totals) >= self._max:
                self._totals.pop(next(iter(self._totals)))
            self._totals[final_session_id] = total
        return max(cost, 0.0)
