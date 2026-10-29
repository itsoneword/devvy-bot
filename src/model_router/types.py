"""Shapes shared by the model router and its adapters (dv-11a5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple, Union

CLAUDE_HARNESS = "claude"
CODEX_HARNESS = "openai_codex"
OPENROUTER_HARNESS = "openrouter"
TYPESAFE_HARNESS = "typesafe"

# What a call is for. Agent turns (``run_turn``) and plain calls
# (``complete`` / ``complete_text`` / ``decide``'s LLM leg) are disjoint: a
# plain purpose's harness and model come from the call route table
# (``routes.DEFAULT_CALL_ROUTES`` + ``config/models.yaml``, dv-781f), a turn's
# from its profile (``routes.resolve_turn_route``).
TURN_PURPOSES = frozenset({"chat", "scheduled", "webhook", "background_job", "monitor"})
PLAIN_PURPOSES = frozenset(
    {
        "extraction",
        "screen_extraction",
        "reconcile",
        "consolidation",
        "fact_quality",
        "scorer",
        "onboarding",
        "screen",
        "ask_explain",
        "design_critic",
    }
)
PURPOSES = TURN_PURPOSES | PLAIN_PURPOSES

# Background jobs (dv-d63d): each job names a row of the ``jobs:`` section of
# the route table (``routes.DEFAULT_JOB_ROUTES``). bgjob / job_start run
# ``background_job``, the server monitor ``monitor``, each Pump stage
# ``pump_<stage>``, the design loop's variant generator ``design_generate``
# (dv-9133).
PUMP_STAGES = (
    "analyze",
    "architect",
    "testdesign",
    "implement",
    "review",
    "acceptance",
)
JOB_ROUTE_NAMES = frozenset(
    {"background_job", "monitor", "design_generate"}
    | {f"pump_{stage}" for stage in PUMP_STAGES}
)

# ``DecideRequest.fallback_model``: run the LLM leg on the purpose's route.
ROUTE = "route"


class ModelCallError(RuntimeError):
    """A plain call failed; the message carries the harness's reason."""


class ModelCallTimeout(ModelCallError):
    """A plain call did not finish within its timeout; the child was stopped."""


def refuse_images(request: "CallRequest", harness: str) -> None:
    """Fail loudly when a harness that cannot see images gets some (dv-9133).

    Dropping them silently would let a critic judge renders it never saw.
    """
    if request.images:
        raise ModelCallError(
            f"images not supported on {harness} "
            f"({len(request.images)} attached, purpose {request.purpose})"
        )


@dataclass(frozen=True)
class CallRequest:
    """A plain call: prompt in, text or JSON out, no tools.

    ``harness`` and ``model`` are explicit overrides; None (the norm) = the
    purpose's route. ``backend`` is the chat's agent backend, a hint for a
    route whose harness is ``backend`` (extraction follows the chat). The
    adapter always sees the resolved harness and model.

    ``json_schema`` is the caller's contract, any root type; the router
    hands the adapter the route's form of it and returns canonical JSON
    text that matches it (``json_contract``, dv-781f). ``timeout_seconds``
    bounds one adapter call; ``deadline_seconds`` (None = unbounded) bounds
    the whole chain: JSON retry and fallback included.

    ``images`` are file paths attached to the prompt (dv-9133). Only Codex
    takes them (``codex exec -i``); every other adapter refuses a call with
    images rather than answer about pictures it never saw.
    """

    purpose: str
    prompt: str
    system_prompt: str
    model: Optional[str] = None
    harness: Optional[str] = None
    backend: Optional[str] = None
    json_schema: Optional[Dict[str, Any]] = None
    timeout_seconds: float = 120.0
    deadline_seconds: Optional[float] = None
    images: Tuple[str, ...] = ()


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
    through :meth:`ModelRouter.complete`; :data:`ROUTE` (the default) = the
    purpose's call route. ``timeout_seconds`` bounds the Jev
    call only; the fallback has its own, LLM-sized timeout. ``fallback_model``
    None = no LLM leg: decide raises :class:`ModelCallError` when Jev cannot
    answer (for callers that escalate on their own, e.g. the reconcile gate).
    """

    purpose: str
    state: Union[str, Mapping[str, Any]]
    questions: Mapping[str, Question]
    model: str = "jev-latest"
    timeout_seconds: float = 10.0
    fallback_model: Optional[str] = ROUTE
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
