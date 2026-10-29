"""Plain calls to OpenRouter chat completions (dv-ef90).

Any purpose routed to ``openrouter`` in ``models.yaml`` runs here; the answer
scorer was the first. The key comes from the owner's key store
(``/setkey openrouter``) at call time, the environment being a dev fallback:
the bot scrubs secrets from its env at startup, so an env-only read never
worked inside the bot (dv-781f).

A schema call sends strict ``response_format``; a model that rejects it is
asked once more without it, inside the same call (dv-781f phase 2).
"""

from __future__ import annotations

import json
from typing import Any, Dict

import httpx

from .routes import OPENROUTER_KEY_NAME, cost_from_tokens, openrouter_key
from .types import (
    OPENROUTER_HARNESS,
    CallRequest,
    CallResult,
    ModelCallError,
    ModelCallTimeout,
    refuse_images,
)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_ENV = OPENROUTER_KEY_NAME


class OpenRouterPlainCaller:
    """Runs one :class:`CallRequest` as a chat completion and returns its text."""

    async def __call__(self, request: CallRequest) -> CallResult:
        refuse_images(request, OPENROUTER_HARNESS)
        key = openrouter_key()
        if not key:
            raise ModelCallError(
                f"{OPENROUTER_KEY_ENV} is not in the key store: "
                f"/setkey openrouter {OPENROUTER_KEY_ENV} <value>"
            )
        if not request.model:
            raise ModelCallError("OpenRouter call needs a model")
        messages = [{"role": "user", "content": request.prompt}]
        if request.system_prompt:
            messages.insert(0, {"role": "system", "content": request.system_prompt})
        # Plain calls judge and extract; they want the same answer for the
        # same input.
        body: Dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "temperature": 0,
        }
        if request.json_schema is not None:
            # The router sent the strict form (json_contract.native_schema).
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.purpose,
                    "strict": True,
                    "schema": request.json_schema,
                },
            }
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                headers = {"Authorization": f"Bearer {key}"}
                resp = await client.post(
                    OPENROUTER_CHAT_URL, headers=headers, json=body
                )
                if "response_format" in body and _rejects_response_format(resp):
                    # Not every model/provider takes structured outputs: say
                    # the schema in the prompt instead; the router validates.
                    body = _without_response_format(body, request)
                    resp = await client.post(
                        OPENROUTER_CHAT_URL, headers=headers, json=body
                    )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise ModelCallTimeout(
                f"timeout after {request.timeout_seconds:g}s (openrouter)"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelCallError(f"OpenRouter call failed: {exc}") from exc

        try:
            text = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelCallError(
                f"unexpected OpenRouter response shape: {data!r}"
            ) from exc
        usage = data.get("usage") or {}
        model = str(data.get("model") or request.model)
        input_tokens = _int(usage.get("prompt_tokens"))
        output_tokens = _int(usage.get("completion_tokens"))
        return CallResult(
            text=text,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # Tokens x llm_pricing.json; None when no rule (unknown is not 0).
            cost_usd=cost_from_tokens(
                model, input_tokens=input_tokens, output_tokens=output_tokens
            ),
        )


# How OpenRouter says a model or provider cannot do structured outputs.
_RESPONSE_FORMAT_REJECTIONS = (
    "response_format",
    "json_schema",
    "structured output",
    "no endpoints found that can handle the requested parameters",
)


def _rejects_response_format(resp: httpx.Response) -> bool:
    if resp.status_code not in (400, 404):
        return False
    text = resp.text.lower()
    return any(marker in text for marker in _RESPONSE_FORMAT_REJECTIONS)


def _without_response_format(
    body: Dict[str, Any], request: CallRequest
) -> Dict[str, Any]:
    """``body`` without ``response_format``, the schema told in a system line."""
    resent = {k: v for k, v in body.items() if k != "response_format"}
    line = "Reply with JSON only, matching this JSON Schema: " + json.dumps(
        request.json_schema
    )
    resent["messages"] = [{"role": "system", "content": line}] + list(body["messages"])
    return resent


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
