"""Shapes shared by the model router and its adapters (dv-11a5)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

CLAUDE_HARNESS = "claude"
CODEX_HARNESS = "openai_codex"
OPENROUTER_HARNESS = "openrouter"
TYPESAFE_HARNESS = "typesafe"
# dv-75f1: the free FLUX image server (text in, PNG out), keyed from service `flux`.
FLUX_HARNESS = "flux"

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
        "consolidation_cards",  # the sweep's done-card judgement (dv-65dd)
        "fact_quality",
        "scorer",
        "onboarding",
        "screen",
        "ask_explain",
        "design_critic",
        "pump_summary",  # pump2's pinned board summary (dv-5c22)
        "feedback_question",  # the feature-feedback question (dv-a42c)
        "feedback_extract",  # fields of the owner's feedback answer (dv-a42c)
        "post_image",  # the devlog channel's picture (dv-fc9d), image out
        "devlog_style",  # owner's speech patterns for the devlog (dv-fc9d)
        "design_mockup",  # design step 0: a free FLUX mockup (dv-75f1), image out
        "design_mockup_paid",  # the same on OpenRouter, card label paidmockup
        "design_mockup_prompt",  # the mockups' subject lines from the brief
        "design_spec",  # a board's style spec drafted from its images
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
    # dv-ca8b: the owner-started merge job of the retired taskpump (rows stay).
    "merge",
    # dv-b8bb: pump2's resolve stage (every stop goes to it first).
    "resolve",
)
# Rows only the removed taskpump ran (dv-81a7). Still parsed, so an old
# models.yaml loads; the 🧠 Модели menu does not show them.
RETIRED_JOB_ROUTES = frozenset({"pump_architect", "pump_acceptance", "pump_merge"})
JOB_ROUTE_NAMES = frozenset(
    {"background_job", "monitor", "design_generate"}
    | {f"pump_{stage}" for stage in PUMP_STAGES}
)

_PUMP_ROUTE = re.compile(r"pump_[a-z0-9]+")


def is_job_route(name: str) -> bool:
    """A ``jobs:`` row name: a static one, or ``pump_<stage>`` for a board's
    custom pump2 stage (dv-3b99); an unconfigured one runs on ``jobs.default``."""
    return name in JOB_ROUTE_NAMES or bool(_PUMP_ROUTE.fullmatch(name))


# ``DecideRequest.fallback_model``: run the LLM leg on the purpose's route.
ROUTE = "route"


class ModelCallError(RuntimeError):
    """A plain call failed; the message carries the harness's reason.

    ``job_id`` (dv-c237): the FLUX server's id of a job it had accepted
    before the call failed, so a later call can fetch it
    (``CallRequest.image_job_id``) instead of drawing it again.
    """

    def __init__(self, *args: object, job_id: Optional[str] = None) -> None:
        super().__init__(*args)
        self.job_id = job_id


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


# dv-a5aa: image input, media type from the suffix (Claude blocks, and
# OpenRouter data URLs for an image_output call, dv-4032).
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def read_images(request: "CallRequest") -> List[Tuple[str, bytes]]:
    """``request.images`` as (media type, bytes), in the given order. A
    missing or non-image file raises :class:`ModelCallError` before any call:
    an image is never dropped."""
    out: List[Tuple[str, bytes]] = []
    for name in request.images:
        path = Path(name)
        media = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
        if media is None:
            raise ModelCallError(
                f"not an image ({path.suffix or 'no suffix'}): {path} "
                f"(purpose {request.purpose})"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ModelCallError(
                f"image not readable: {path} ({exc}; purpose {request.purpose})"
            ) from exc
        out.append((media, data))
    return out


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

    ``images`` are file paths attached to the prompt (dv-9133). Claude takes
    them as image blocks before the prompt (dv-a5aa), Codex as ``codex exec
    -i``; OpenRouter sends them as data URLs on an ``image_output`` call
    (dv-4032, reference images for a mockup) and refuses them on a text call
    rather than answer about pictures it never saw.
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
    # dv-fc9d: ask for a picture back (``CallResult.image``). Only the
    # OpenRouter and FLUX adapters produce one; the others refuse (``refuse_image_output``).
    image_output: bool = False
    # dv-75f1: size and preset for an image-output call; only the FLUX adapter
    # reads them (OpenRouter picks its own size).
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    image_preset: Optional[str] = None
    # Owner 13.11.26: an OpenRouter call billed to another key of the store,
    # "<service>:<NAME>" (design images: gymbro:OPENROUTER_API_KEY_DESIGN).
    # None = the default openrouter key.
    key_ref: Optional[str] = None
    # dv-c237: a FLUX job id from an earlier failed call; the FLUX adapter
    # fetches that job (no new submit) unless the server no longer has it.
    image_job_id: Optional[str] = None


def refuse_image_output(request: "CallRequest", harness: str) -> None:
    """Fail loudly when a harness that cannot draw is asked for a picture."""
    if request.image_output:
        raise ModelCallError(
            f"image output not supported on {harness} (purpose {request.purpose})"
        )


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
    # dv-fc9d: the picture's bytes for an ``image_output`` call, else None.
    image: Optional[bytes] = None


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
