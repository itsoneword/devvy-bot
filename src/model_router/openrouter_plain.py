"""Plain calls to OpenRouter chat completions (dv-ef90).

Used by the answer scorer, which wants a judge from a different model family
than the one being judged. The key is read from the environment at call time;
credential files are never read.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from .types import CallRequest, CallResult, ModelCallError, ModelCallTimeout

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"


def openrouter_key() -> Optional[str]:
    return os.environ.get(OPENROUTER_KEY_ENV) or None


class OpenRouterPlainCaller:
    """Runs one :class:`CallRequest` as a chat completion and returns its text."""

    async def __call__(self, request: CallRequest) -> CallResult:
        key = openrouter_key()
        if not key:
            raise ModelCallError(f"{OPENROUTER_KEY_ENV} is not set")
        messages = [{"role": "user", "content": request.prompt}]
        if request.system_prompt:
            messages.insert(0, {"role": "system", "content": request.system_prompt})
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(
                    OPENROUTER_CHAT_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    # Plain calls judge and extract; they want the same answer
                    # for the same input.
                    json={
                        "model": request.model,
                        "messages": messages,
                        "temperature": 0,
                    },
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
        return CallResult(
            text=text,
            model=str(data.get("model") or request.model),
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
        )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
