"""Run a backend call under a harness trace, as the router does (dv-2260)."""

from typing import Any, Awaitable, Callable

from src.model_router.trace import TraceCollector, activate


async def traced(storage: Any, call: Callable[[], Awaitable[Any]]) -> Any:
    """Await ``call()`` with a collector writing to ``storage.tool_calls``;
    the response comes back carrying ``trace_id`` like a routed turn."""
    trace = TraceCollector(storage.tool_calls)
    with activate(trace):
        response = await call()
    trace.close()
    await trace.flush()
    response.trace_id = trace.trace_id
    return response
