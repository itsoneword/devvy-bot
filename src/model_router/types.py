"""Shapes shared by the model router and its adapters (dv-11a5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple, Union

CLAUDE_HARNESS = "claude"
CODEX_HARNESS = "openai_codex"
OPENROUTER_HARNESS = "openrouter"
TYPESAFE_HARNESS = "typesafe"

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
        "onboarding",
        "screen",
        "screen_extraction",
        "ask_explain",
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


# --- decide: typed questions about a state, typed answers back (dv-7e11) ---


@dataclass(frozen=True)
class Choice:
    """Pick one option. ``criteria`` maps option -> description (or None)."""

    instructions: str
    criteria: Mapping[str, Optional[str]]


@dataclass(frozen=True)
class Score:
    """Rate on an ordered scale; ``levels`` are the level descriptions, low
    to high (2-10). The answer is a level index, 0-based."""

    instructions: str
    levels: Tuple[str, ...]


@dataclass(frozen=True)
class Noul:
    """Yes/no. The answer is the probability of yes.

    ``instructions`` is text, or a mapping of named parts Jev reads as
    separate fields (the screen sends ``{"saved_fact": ..., "question": ...}``).
    ``criteria`` optionally maps ``"true"`` / ``"false"`` to what each means.
    """

    instructions: Union[str, Mapping[str, str]]
    criteria: Optional[Mapping[str, str]] = None


Question = Union[Choice, Score, Noul]


@dataclass(frozen=True)
class DecideRequest:
    """A decision call: typed questions about ``state``, keyed by your ids.

    Runs on Jev (``model``) when it is available, else on ``fallback_model``
    through :meth:`ModelRouter.complete`. ``timeout_seconds`` bounds the Jev
    call only; the fallback has its own, LLM-sized timeout. ``fallback_model``
    None = no LLM leg: decide raises :class:`ModelCallError` when Jev cannot
    answer (for callers that escalate on their own, e.g. the reconcile gate).
    """

    purpose: str
    state: Union[str, Mapping[str, Any]]
    questions: Mapping[str, Question]
    model: str = "jev-latest"
    timeout_seconds: float = 10.0
    fallback_model: Optional[str] = "haiku"
    fallback_timeout_seconds: float = 120.0


@dataclass(frozen=True)
class Answer:
    """One typed answer. ``value``: the option (Choice), the 0-based level,
    possibly fractional (Score), or P(yes) (Noul). ``confidence`` and
    ``probabilities`` are None when the backend gives none (the LLM path)."""

    kind: str  # "choice" | "score" | "noul"
    value: Union[str, float]
    confidence: Optional[float] = None
    probabilities: Optional[Dict[str, float]] = None


@dataclass(frozen=True)
class DecideResult:
    answers: Dict[str, Answer]
    harness: str
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None
    fallback_reason: Optional[str] = None  # set when the LLM path answered
