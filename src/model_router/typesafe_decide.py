"""Decision calls on TypeSafe Jev (dv-7e11).

Jev is a non-generative "System One" model: typed questions about a state in,
typed answers with probabilities out. Plain httpx, no SDK.

The key (dv-e690): the bot's config loader keeps secrets out of ``os.environ``,
so the primary source in the bot process is Settings (``TYPESAFE_API_KEY`` in
the bot's dotenv / secrets file), handed over once at startup with
:func:`configure_typesafe_key`. The environment variable stays as an override
and as the path for processes outside the bot (``scripts/withkeys typesafe --``
puts the /setkey store's value in the job's env). Order: env, then Settings.

Request and answer shapes: https://docs.typesafe.ai/api.md. Score levels are
numbered from 0 (verified live 2026-10-17).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional, Union

import httpx

from .types import (
    TYPESAFE_HARNESS,
    Answer,
    Choice,
    DecideRequest,
    DecideResult,
    ModelCallError,
    ModelCallTimeout,
    Noul,
    Question,
    Score,
)

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_KEY_ENV = "TYPESAFE_API_KEY"

# Vendor price (2026-10-17): $0.042 per 1M input tokens, output free. Not in
# config/llm_pricing.json: that table prices Claude transcripts for analytics.
JEV_INPUT_USD_PER_MTOK = 0.042


_configured_key: Optional[str] = None


def configure_typesafe_key(key: Optional[str]) -> None:
    """Register the key Settings holds (main.py, once). None clears it."""
    global _configured_key
    _configured_key = key or None


def typesafe_key() -> Optional[str]:
    """Env override first, else the key registered from Settings."""
    return os.environ.get(TYPESAFE_KEY_ENV) or _configured_key


def question_body(question: Question) -> Dict[str, Any]:
    if isinstance(question, Choice):
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": dict(question.criteria),
        }
    if isinstance(question, Score):
        return {
            "type": "score",
            "instructions": question.instructions,
            "criteria": list(question.levels),
        }
    body: Dict[str, Any] = {"type": "noul", "instructions": question.instructions}
    if question.criteria:
        body["criteria"] = dict(question.criteria)
    return body


def request_body(request: DecideRequest) -> Dict[str, Any]:
    return {
        "state": _state(request.state),
        "model": request.model,
        "questions": {qid: question_body(q) for qid, q in request.questions.items()},
    }


class TypesafeDecider:
    """Runs one :class:`DecideRequest` on Jev and returns its typed answers."""

    async def __call__(self, request: DecideRequest) -> DecideResult:
        key = typesafe_key()
        if not key:
            raise ModelCallError(f"{TYPESAFE_KEY_ENV} is not set")
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(
                    TYPESAFE_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json=request_body(request),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise ModelCallTimeout(
                f"timeout after {request.timeout_seconds:g}s (typesafe)"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ModelCallError(
                f"TypeSafe call failed: HTTP {exc.response.status_code} "
                f"{exc.response.text[:300]}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelCallError(f"TypeSafe call failed: {exc}") from exc
        return result_from(request, data)


def result_from(request: DecideRequest, data: Any) -> DecideResult:
    try:
        raw = data["answers"]
        answers = {
            qid: _answer(question, raw[qid])
            for qid, question in request.questions.items()
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelCallError(f"unexpected TypeSafe response shape: {data!r}") from exc
    usage = data.get("usage") or {}
    input_tokens = _int(usage.get("input_tokens"))
    return DecideResult(
        answers=answers,
        harness=TYPESAFE_HARNESS,
        model=str(data.get("model") or request.model),
        input_tokens=input_tokens,
        output_tokens=_int(usage.get("output_tokens")),
        cost_usd=input_tokens * JEV_INPUT_USD_PER_MTOK / 1_000_000,
    )


def _answer(question: Question, raw: Mapping[str, Any]) -> Answer:
    value: Union[str, float]
    if isinstance(question, Choice):
        value = str(raw["choice"])
        if value not in question.criteria:
            raise ValueError(f"choice {value!r} is not an option")
        kind = "choice"
    elif isinstance(question, Score):
        value, kind = float(raw["score"]), "score"
    elif isinstance(question, Noul):
        value, kind = float(raw["noul"]), "noul"
    probs = raw.get("probabilities")
    confidence = raw.get("confidence")
    return Answer(
        kind=kind,
        value=value,
        confidence=float(confidence) if confidence is not None else None,
        probabilities=(
            {str(k): float(v) for k, v in probs.items()}
            if isinstance(probs, Mapping)
            else None
        ),
    )


def _state(state: Union[str, Mapping[str, Any]]) -> Union[str, Dict[str, Any]]:
    return state if isinstance(state, str) else dict(state)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
