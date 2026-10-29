"""One entry point for every model call (dv-f59b / dv-11a5)."""

from typing import TYPE_CHECKING, Any

from .types import (
    CLAUDE_HARNESS,
    CODEX_HARNESS,
    OPENROUTER_HARNESS,
    PURPOSES,
    TYPESAFE_HARNESS,
    Answer,
    CallRequest,
    CallResult,
    Choice,
    DecideRequest,
    DecideResult,
    ModelCallError,
    ModelCallTimeout,
    Noul,
    Score,
)

if TYPE_CHECKING:
    from .harness import CLAUDE_ACCOUNT_ENV, HarnessVersions
    from .router import ModelRouter, complete_text, get_router, set_router

# `router` pulls claude_agent_sdk -> mcp (1.4 s). scripts/taskpump only needs
# `quota`, and importing a submodule runs this file, so these load on first use
# (dv-f6af: `taskpump status --json` was 2.3 s, 1.4 s of it this import).
_LAZY = {
    "CLAUDE_ACCOUNT_ENV": ".harness",
    "HarnessVersions": ".harness",
    "ModelRouter": ".router",
    "complete_text": ".router",
    "get_router": ".router",
    "set_router": ".router",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "Answer",
    "Choice",
    "DecideRequest",
    "DecideResult",
    "Noul",
    "Score",
    "TYPESAFE_HARNESS",
    "CLAUDE_ACCOUNT_ENV",
    "CLAUDE_HARNESS",
    "CODEX_HARNESS",
    "CallRequest",
    "CallResult",
    "HarnessVersions",
    "ModelCallError",
    "ModelCallTimeout",
    "ModelRouter",
    "OPENROUTER_HARNESS",
    "PURPOSES",
    "complete_text",
    "get_router",
    "set_router",
]
