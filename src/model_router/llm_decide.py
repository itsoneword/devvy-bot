"""The LLM side of ``decide`` (dv-7e11): the same typed questions rendered as a
prompt + JSON schema for :meth:`ModelRouter.complete`, and the reply read
back into typed answers. Used when Jev is unavailable; an LLM gives no
probabilities, so ``confidence`` and ``probabilities`` stay None."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .types import (
    ROUTE,
    Answer,
    CallRequest,
    CallResult,
    Choice,
    DecideRequest,
    DecideResult,
    ModelCallError,
    Score,
)

if TYPE_CHECKING:
    from .routes import CallRoute

SYSTEM_PROMPT = (
    "You answer typed decision questions about a given state. Read each "
    "question literally, judge only from the state, and reply with JSON that "
    "matches the schema: one field per question id."
)


def call_request(request: DecideRequest) -> CallRequest:
    """The LLM leg as a plain call on the purpose's route (:data:`ROUTE`), or
    on an explicit ``fallback_model`` there."""
    if request.fallback_model is None:
        raise ModelCallError("decide: no fallback model")
    model: Optional[str] = (
        None if request.fallback_model == ROUTE else request.fallback_model
    )
    return CallRequest(
        purpose=request.purpose,
        prompt=prompt(request),
        model=model,
        system_prompt=SYSTEM_PROMPT,
        json_schema=json_schema(request),
        timeout_seconds=request.fallback_timeout_seconds,
    )


def prompt(request: DecideRequest) -> str:
    state = (
        request.state
        if isinstance(request.state, str)
        else json.dumps(dict(request.state), ensure_ascii=False, indent=1)
    )
    lines: List[str] = ["<state>", state, "</state>", "", "Questions:"]
    for qid, question in request.questions.items():
        if isinstance(question, Choice):
            lines.append(f"- {qid} (pick one option): {question.instructions}")
            for option, description in question.criteria.items():
                lines.append(
                    f"    {option}" + (f": {description}" if description else "")
                )
        elif isinstance(question, Score):
            lines.append(f"- {qid} (level number): {question.instructions}")
            for level, description in enumerate(question.levels):
                lines.append(f"    {level}: {description}")
        else:
            text = (
                question.instructions
                if isinstance(question.instructions, str)
                else json.dumps(dict(question.instructions), ensure_ascii=False)
            )
            lines.append(f"- {qid} (true = yes, false = no): {text}")
            for answer, meaning in (question.criteria or {}).items():
                lines.append(f"    {answer}: {meaning}")
    return "\n".join(lines)


def json_schema(request: DecideRequest) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    for qid, question in request.questions.items():
        if isinstance(question, Choice):
            properties[qid] = {"type": "string", "enum": list(question.criteria)}
        elif isinstance(question, Score):
            properties[qid] = {
                "type": "integer",
                "minimum": 0,
                "maximum": len(question.levels) - 1,
            }
        else:
            properties[qid] = {"type": "boolean"}
    return {
        "type": "object",
        "properties": properties,
        "required": list(request.questions),
        "additionalProperties": False,
    }


def result_from(
    request: DecideRequest,
    result: CallResult,
    fallback_reason: str,
    route: "CallRoute",
) -> DecideResult:
    try:
        data = json.loads(result.text)
        answers = {}
        for qid, question in request.questions.items():
            raw = data[qid]
            if isinstance(question, Choice):
                if raw not in question.criteria:
                    raise ValueError(f"{qid}: {raw!r} is not an option")
                answers[qid] = Answer(kind="choice", value=str(raw))
            elif isinstance(question, Score):
                answers[qid] = Answer(kind="score", value=float(raw))
            else:
                if not isinstance(raw, bool):
                    raise ValueError(f"{qid}: {raw!r} is not a boolean")
                answers[qid] = Answer(kind="noul", value=1.0 if raw else 0.0)
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelCallError(
            f"decide fallback: bad reply {result.text[:300]!r}"
        ) from exc
    return DecideResult(
        answers=answers,
        harness=route.harness,
        model=result.model or route.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        fallback_reason=fallback_reason,
    )
