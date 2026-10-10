"""Judge providers for Tier-2 answer scoring (dv-300d).

Two interchangeable judges behind one `complete(prompt) -> raw text`
interface, selected by `scripts/score_answers.py --provider`. Both make their
call through the model router (purpose `scorer`):

- ``openrouter`` (default): a different model family than the one being
  judged, which is the whole point — same-family judges are documented to
  favor their own family's outputs. Model is resolved at runtime from
  OpenRouter's live catalogue rather than a hardcoded id that goes stale.
- ``claude_cli``: the local Claude CLI. Same-family as the
  bot's own models — acceptable only as a build/debug fallback for when
  OPENROUTER_API_KEY isn't available yet; scores from it are for rubric
  iteration, not for trusting the final numbers.
"""

from __future__ import annotations

import os
import re
from typing import Optional, Protocol

import httpx
import structlog

from src.model_router import (
    CLAUDE_HARNESS,
    OPENROUTER_HARNESS,
    CallRequest,
    get_router,
)
from src.model_router.openrouter_plain import openrouter_key

logger = structlog.get_logger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict, honest quality judge. Follow the output format in "
    "the user message exactly and reply with JSON only — no prose, no "
    "markdown fences."
)

# Model-name substrings that disqualify an otherwise-matching Qwen listing
# from "instruct chat model" — vision, embedding, moderation and extended
# "thinking" variants are all worse fits for a fast structured-JSON judge.
_QWEN_EXCLUDE = ("base", "vl", "embed", "guard", "thinking", "coder")

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)b(?:[^a-z]|$)", re.IGNORECASE)


class JudgeProvider(Protocol):
    model_name: str
    last_cost_usd: Optional[float]

    async def resolve(self) -> str: ...

    async def complete(self, prompt: str) -> str: ...


class ClaudeCliJudge:
    """The local Claude CLI as judge, through the model router."""

    def __init__(self, model: str = "opus", timeout_seconds: float = 120) -> None:
        self.model_name = f"claude-cli:{model}"
        self._model = model
        self._timeout = timeout_seconds
        self.last_cost_usd: Optional[float] = None

    async def resolve(self) -> str:
        """The claude_cli model is known upfront; nothing to resolve."""
        return self.model_name

    async def complete(self, prompt: str) -> str:
        self.last_cost_usd = None
        result = await get_router().complete(
            CallRequest(
                purpose="scorer",
                harness=CLAUDE_HARNESS,
                prompt=prompt,
                model=self._model,
                system_prompt=_JUDGE_SYSTEM_PROMPT,
                timeout_seconds=self._timeout,
            )
        )
        self.last_cost_usd = result.cost_usd
        return result.text


class OpenRouterJudge:
    """OpenRouter chat completions, model resolved at runtime."""

    def __init__(self, model: Optional[str] = None) -> None:
        if not openrouter_key():
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set (checked the environment only — "
                "credential files are never read). Set it, or run with "
                "--provider claude_cli."
            )
        self._explicit_model = model or os.environ.get("SCORER_MODEL")
        self.model_name = self._explicit_model or ""
        self._resolved = bool(self._explicit_model)
        self.last_cost_usd: Optional[float] = None
        # Per-token prices for the resolved model, filled in by
        # pick_best_value_qwen; None for an explicit --model override (cost
        # tracking is best-effort, not required for that path).
        self._prompt_price: Optional[float] = None
        self._completion_price: Optional[float] = None

    async def resolve(self) -> str:
        """Public, idempotent: callers that need judge_model before the
        first `complete()` (e.g. for an idempotency check) can force
        resolution up front."""
        return await self._resolve_model()

    async def _resolve_model(self) -> str:
        if self._resolved:
            return self.model_name
        model = await self.pick_best_value_qwen()
        self.model_name = model
        self._resolved = True
        logger.info("openrouter_model_resolved", model=model)
        return model

    async def pick_best_value_qwen(self) -> str:
        """Current best-value Qwen instruct chat model on OpenRouter.

        "Best value" = largest parameter count (the strongest judge we can
        get) among paid (non-$0, so not the rate-limited free tier) Qwen
        chat/instruct listings, tie-broken by lowest combined token price.
        Queried live so a stale hardcoded id never silently keeps scoring
        against a deprecated model.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            payload = resp.json()

        candidates: list[tuple[float, float, str, float, float]] = []
        for entry in payload.get("data", []):
            model_id = str(entry.get("id") or "")
            lowered = model_id.lower()
            if "qwen" not in lowered:
                continue
            if any(bad in lowered for bad in _QWEN_EXCLUDE):
                continue
            pricing = entry.get("pricing") or {}
            try:
                prompt_price = float(pricing.get("prompt") or 0)
                completion_price = float(pricing.get("completion") or 0)
            except (TypeError, ValueError):
                continue
            price = prompt_price + completion_price
            size_match = _SIZE_RE.search(lowered)
            size = float(size_match.group(1)) if size_match else 0.0
            candidates.append((price, size, model_id, prompt_price, completion_price))

        if not candidates:
            raise RuntimeError(
                "no Qwen instruct chat models found on OpenRouter /models "
                "(catalogue shape may have changed)"
            )

        paid = [c for c in candidates if c[0] > 0]
        pool = paid or candidates  # fall back to free tier if truly nothing paid
        pool.sort(key=lambda c: (-c[1], c[0]))  # biggest first, cheapest tiebreak
        best = pool[0]
        self._prompt_price, self._completion_price = best[3], best[4]
        return best[2]

    async def complete(self, prompt: str) -> str:
        model = await self._resolve_model()
        self.last_cost_usd = None
        result = await get_router().complete(
            CallRequest(
                purpose="scorer",
                harness=OPENROUTER_HARNESS,
                prompt=prompt,
                model=model,
                system_prompt=_JUDGE_SYSTEM_PROMPT,
                timeout_seconds=120.0,
            )
        )
        if self._prompt_price is not None and self._completion_price is not None:
            self.last_cost_usd = (
                result.input_tokens * self._prompt_price
                + result.output_tokens * self._completion_price
            )
        return result.text


def build_judge(provider: str, model: Optional[str] = None) -> JudgeProvider:
    if provider == "openrouter":
        return OpenRouterJudge(model=model)
    if provider == "claude_cli":
        return ClaudeCliJudge(model=model or os.environ.get("SCORER_CLI_MODEL", "opus"))
    raise ValueError(f"unknown judge provider: {provider!r}")
