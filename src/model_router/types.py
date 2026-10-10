"""Shapes shared by the model router and its adapters (dv-11a5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

CLAUDE_HARNESS = "claude"
CODEX_HARNESS = "openai_codex"
OPENROUTER_HARNESS = "openrouter"

# What a call is for. Model choice per purpose moves here in dv-1d5a; until
# then each caller passes its own model and the purpose is only recorded.
PURPOSES = frozenset(
    {
        "chat",
        "scheduled",
        "webhook",
        "extraction",
        "reconcile",
        "consolidation",
        "fact_quality",
        "scorer",
        "background_job",
        "monitor",
    }
)


class ModelCallError(RuntimeError):
    """A plain call failed; the message carries the harness's reason."""


class ModelCallTimeout(ModelCallError):
    """A plain call did not finish within its timeout; the child was stopped."""


@dataclass(frozen=True)
class CallRequest:
    """A plain call: prompt in, text or JSON out, no tools."""

    purpose: str
    prompt: str
    model: str
    system_prompt: str
    harness: str = CLAUDE_HARNESS
    json_schema: Optional[Dict[str, Any]] = None
    timeout_seconds: float = 120.0


@dataclass
class CallResult:
    """What a plain-call adapter reports back to the router."""

    text: str
    model: Optional[str] = None
    harness_version: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: Optional[float] = None
    session_id: Optional[str] = None
