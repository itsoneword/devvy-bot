"""The model router: the one entry point for every model call (dv-11a5).

Three call kinds. :meth:`ModelRouter.run_turn` is an agent turn (tools,
session, streaming) and runs on a registered :class:`AgentBackend`.
:meth:`ModelRouter.complete` is a plain call (prompt in, text out, no tools)
and runs on a plain-call adapter. :meth:`ModelRouter.decide` is a typed
decision (questions about a state in, typed answers out) and runs on Jev,
falling back to a plain LLM call (dv-7e11). The router finds the harness, times the call and writes
one ``model_calls`` row for it, whatever the outcome.

It is a library, not a service: the bot builds one in ``main.py`` and
registers it with :func:`set_router`; code without a bot (scripts, tests) gets
a default router that makes the same calls and records nothing.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Union

import structlog

from . import llm_decide
from .claude_plain import ClaudePlainCaller
from .codex_plain import CodexPlainCaller
from .harness import HarnessVersions
from .openrouter_plain import OpenRouterPlainCaller
from .turn_cost import Settlement, when_settled
from .types import (
    CLAUDE_HARNESS,
    CODEX_HARNESS,
    OPENROUTER_HARNESS,
    PURPOSES,
    TYPESAFE_HARNESS,
    CallRequest,
    CallResult,
    DecideRequest,
    DecideResult,
    ModelCallError,
    ModelCallTimeout,
)
from .typesafe_decide import TYPESAFE_KEY_ENV, TypesafeDecider, typesafe_key

if TYPE_CHECKING:
    from src.agent_backends.protocol import AgentBackend
    from src.claude.sdk_integration import ClaudeResponse
    from src.storage.model_calls import ModelCallRepository

logger = structlog.get_logger()

PlainCaller = Callable[[CallRequest], Awaitable[CallResult]]
ProfileResolver = Callable[[Path], Optional[str]]
Decider = Callable[[DecideRequest], Awaitable[DecideResult]]

_PROVIDERS = {
    CLAUDE_HARNESS: "anthropic",
    CODEX_HARNESS: "openai",
    OPENROUTER_HARNESS: "openrouter",
    TYPESAFE_HARNESS: "typesafe",
}


class ModelRouter:
    def __init__(
        self,
        backends: Optional[Dict[str, "AgentBackend"]] = None,
        plain_callers: Optional[Dict[str, PlainCaller]] = None,
        recorder: Optional["ModelCallRepository"] = None,
        versions: Optional[HarnessVersions] = None,
        profile_resolver: Optional[ProfileResolver] = None,
        decider: Optional[Decider] = None,
    ) -> None:
        self.backends: Dict[str, "AgentBackend"] = backends if backends else {}
        self._plain: Dict[str, PlainCaller] = (
            plain_callers
            if plain_callers is not None
            else {
                CLAUDE_HARNESS: ClaudePlainCaller(),
                CODEX_HARNESS: CodexPlainCaller(),
                OPENROUTER_HARNESS: OpenRouterPlainCaller(),
            }
        )
        self._recorder = recorder
        self._versions = versions or HarnessVersions()
        self._profile_resolver = profile_resolver
        self._decider: Decider = decider or TypesafeDecider()

    async def run_turn(
        self,
        backend: Union[str, "AgentBackend"],
        *,
        purpose: str,
        attended: bool = True,
        **turn: Any,
    ) -> "ClaudeResponse":
        """Run one agent turn; ``turn`` is ``AgentBackend.run_command``'s kwargs."""
        _check_purpose(purpose)
        adapter = self.backends[backend] if isinstance(backend, str) else backend
        harness = str(adapter.backend_id)
        # Asked only when there is a row to write it into.
        model = turn.get("model") or (
            _default_model(adapter) if self._recorder is not None else None
        )
        started = time.monotonic()
        try:
            response = await adapter.run_command(**turn)
        except BaseException as exc:
            await self._record(
                purpose=purpose,
                kind="turn",
                harness=harness,
                model=model,
                attended=attended,
                working_dir=turn.get("working_directory"),
                user_id=turn.get("user_id"),
                session_id=turn.get("session_id"),
                started=started,
                outcome="error",
                error=_error_text(exc),
            )
            raise
        row_id = await self._record(
            purpose=purpose,
            kind="turn",
            harness=harness,
            model=getattr(response, "model", None) or model,
            provider=getattr(response, "provider", None),
            attended=attended,
            working_dir=turn.get("working_directory"),
            user_id=turn.get("user_id"),
            session_id=response.session_id,
            started=started,
            outcome="error" if response.is_error else "ok",
            error=response.error_type if response.is_error else None,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_creation_tokens=response.cache_creation_tokens,
            cost_usd=response.cost,
            cost_source=getattr(response, "cost_source", None),
        )
        recorder = self._recorder
        if row_id is not None and recorder is not None:

            async def _settle_row(settled: Settlement) -> None:
                assert row_id is not None and settled.cost is not None
                await recorder.settle_cost(row_id, settled.cost, settled.source)

            when_settled(
                getattr(response, "cost_settlement", None),
                _settle_row,
                what="model_calls",
            )
        return response

    async def complete(self, request: CallRequest) -> CallResult:
        """Run one plain call and return its result."""
        _check_purpose(request.purpose)
        caller = self._plain.get(request.harness)
        if caller is None:
            raise ModelCallError(f"no plain-call adapter for {request.harness}")
        started = time.monotonic()
        try:
            result = await caller(request)
        except BaseException as exc:
            await self._record(
                purpose=request.purpose,
                kind="plain",
                harness=request.harness,
                model=request.model,
                attended=False,
                started=started,
                outcome="timeout" if isinstance(exc, ModelCallTimeout) else "error",
                error=_error_text(exc),
            )
            raise
        await self._record(
            purpose=request.purpose,
            kind="plain",
            harness=request.harness,
            harness_version=result.harness_version,
            model=result.model or request.model,
            attended=False,
            session_id=result.session_id,
            started=started,
            outcome="ok",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            cost_usd=result.cost_usd,
        )
        return result

    async def decide(self, request: DecideRequest) -> DecideResult:
        """Answer typed questions about a state: Jev when its key is set,
        else (or on any Jev failure) the LLM fallback through
        :meth:`complete`. Jev is optional; the bot never requires it."""
        _check_purpose(request.purpose)
        if not request.questions:
            raise ValueError("decide needs at least one question")
        if not typesafe_key():
            reason = f"{TYPESAFE_KEY_ENV} not set"
        else:
            started = time.monotonic()
            try:
                result = await self._decider(request)
            except Exception as exc:
                reason = _error_text(exc)
                await self._record(
                    purpose=request.purpose,
                    kind="decide",
                    harness=TYPESAFE_HARNESS,
                    model=request.model,
                    attended=False,
                    started=started,
                    outcome="timeout" if isinstance(exc, ModelCallTimeout) else "error",
                    error=reason,
                )
            else:
                await self._record(
                    purpose=request.purpose,
                    kind="decide",
                    harness=TYPESAFE_HARNESS,
                    model=result.model,
                    attended=False,
                    started=started,
                    outcome="ok",
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                )
                logger.debug(
                    "decide answered",
                    purpose=request.purpose,
                    path=TYPESAFE_HARNESS,
                    model=result.model,
                )
                return result
        if request.fallback_model is None:
            raise ModelCallError(f"decide: jev unavailable, no fallback ({reason})")
        logger.info(
            "decide falling back to llm",
            purpose=request.purpose,
            model=request.fallback_model,
            reason=reason,
        )
        # complete() records this leg as an ordinary plain call.
        call = await self.complete(llm_decide.call_request(request))
        return llm_decide.result_from(request, call, reason)

    async def _record(
        self,
        *,
        purpose: str,
        kind: str,
        harness: str,
        model: Optional[str],
        attended: bool,
        started: float,
        outcome: str,
        harness_version: Optional[str] = None,
        provider: Optional[str] = None,
        working_dir: Optional[Path] = None,
        user_id: Optional[int] = None,
        session_id: Optional[str] = None,
        error: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cost_usd: Optional[float] = None,
        cost_source: Optional[str] = None,
    ) -> Optional[int]:
        """Write the call's row; its id, or None. A failed write never fails
        the call."""
        if self._recorder is None:
            return None
        try:
            from src.storage.model_calls import ModelCall

            profile: Optional[str] = None
            if self._profile_resolver is not None and working_dir is not None:
                profile = self._profile_resolver(Path(working_dir))
            return await self._recorder.record(
                ModelCall(
                    purpose=purpose,
                    kind=kind,
                    harness=harness,
                    harness_version=harness_version
                    or await self._versions.get(harness),
                    provider=provider or _PROVIDERS.get(harness, harness),
                    model=model,
                    attended=attended,
                    profile=profile,
                    user_id=user_id,
                    session_id=session_id,
                    input_tokens=int(input_tokens or 0),
                    output_tokens=int(output_tokens or 0),
                    cache_read_tokens=int(cache_read_tokens or 0),
                    cache_creation_tokens=int(cache_creation_tokens or 0),
                    cost_usd=cost_usd,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    outcome=outcome,
                    error=error,
                    cost_source=cost_source,
                )
            )
        except Exception as exc:
            logger.warning(
                "model call not recorded",
                purpose=purpose,
                harness=harness,
                error=str(exc),
            )
            return None


def _check_purpose(purpose: str) -> None:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown model call purpose: {purpose!r}")


def _default_model(adapter: "AgentBackend") -> Optional[str]:
    try:
        return str(adapter.default_model())
    except Exception:
        return None


def _error_text(exc: BaseException) -> str:
    return (f"{type(exc).__name__}: {exc}")[:500]


_router: Optional[ModelRouter] = None


def set_router(router: Optional[ModelRouter]) -> None:
    """Register the process's router (``main.py``); None resets to default."""
    global _router
    _router = router


def get_router() -> ModelRouter:
    """The process's router; a recorder-less default outside the bot."""
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


async def complete_text(
    prompt: str,
    *,
    purpose: str,
    model: str,
    system_prompt: str,
    timeout: float = 120.0,
) -> str:
    """Plain Claude call through the process's router; returns the text."""
    result = await get_router().complete(
        CallRequest(
            purpose=purpose,
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            timeout_seconds=timeout,
        )
    )
    return result.text
