"""Channel-neutral model turn and durable conversation bookkeeping."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import structlog

from src.agent_backends.protocol import policy_for
from src.bot.orchestration.windowing import (
    _context_tokens_of,
    account_turn_and_maybe_eval,
    consume_pending_seed,
    effective_refresh_threshold,
    mentions_remember_trigger,
    refresh_window,
)

logger = structlog.get_logger()


class ChatTurnService:
    """The turn lifecycle shared by the Telegram and local chat channels.

    ``prepare`` (window backstop + armed seed) -> ``session`` -> ``run``
    (model router) -> ``store`` -> ``account`` -> ``roll_if_needed``. Each
    channel adapter only resolves its own inputs (profile, model pick,
    presentation); the lifecycle steps are these, for both (dv-fe4d).
    """

    @staticmethod
    def default_model_ref(backend: str, settings: Any) -> Optional[str]:
        """The model a turn runs on when the channel has no per-user pick.

        The same resolution Telegram's ``_selected_model`` falls back to,
        so the model-aware roll threshold agrees across channels.
        """
        configured = getattr(settings, "agent_model", None)
        try:
            return str(
                policy_for(backend).display_model(
                    configured if isinstance(configured, str) else None, settings
                )
            )
        except Exception:
            return None

    @staticmethod
    async def session(
        *,
        backend: Any,
        user_id: int,
        working_dir: Any,
        remembered: Optional[str],
        force_new: bool,
        auto_resume: bool,
    ) -> Optional[str]:
        """The session id to resume, or None for a fresh one.

        Telegram passes ``auto_resume=False`` (the facade resumes by
        working_dir); the local channel asks the backend itself.
        """
        if force_new:
            return None
        if remembered is not None:
            return remembered
        if auto_resume:
            resumed: Optional[str] = await backend.resumable_session_for(
                user_id, working_dir
            )
            return resumed
        return None

    @staticmethod
    async def prepare(
        *,
        window_repo: Any,
        storage: Any,
        evaluator: Any,
        user_data: dict[str, Any],
        user_id: int,
        working_dir: Any,
        backend: str,
        model_ref: Optional[str],
        prompt: str,
        force_new: bool,
        agent_id: str,
        conversation_text: Callable[[Any], Awaitable[str]],
        family_id: Optional[int] = None,
        consolidation: Any = None,
    ) -> tuple[bool, str, Any, int]:
        """BPC seed, row-level force_new and the over-threshold backstop.

        Returns ``(force_new, prompt with the armed seed, window state,
        model-aware refresh threshold)``. The state is re-read after a
        backstop roll so its ``pending_seed_window`` names the seed source.
        """
        # The transition also set force_new_session=True. Read it from the
        # window row, not just user_data, so a restart between the roll
        # and this turn cannot cancel the rotation (dv-9f31).
        state = await window_repo.get_or_create(user_id, working_dir)
        threshold = effective_refresh_threshold(
            window_repo.refresh_threshold_tokens,
            backend=backend,
            model_ref=model_ref,
        )
        force_new = force_new or state.force_new_session
        if not force_new and window_repo.should_refresh(state, threshold):
            # Backstop: the window is already over the roll threshold and
            # nothing armed a transition (older build, lost flag, threshold
            # lowered under a running window, manual DB edit). Run the SAME
            # transition a normal turn-end crossing would, then let THIS
            # turn consume the seed below, so the message keeps continuity
            # (dv-d9e2, live incident 2026-08-08 10:21:08Z).
            logger.warning(
                "Window over refresh threshold with no armed transition; "
                "forcing a fresh session",
                user_id=user_id,
                working_dir=str(working_dir),
                context_tokens=state.last_context_tokens,
                refresh_threshold_tokens=threshold,
            )
            await refresh_window(
                window_repo,
                evaluator,
                user_data,
                user_id=user_id,
                working_dir=working_dir,
                backend=backend,
                conversation_text=await conversation_text(state),
                agent_id=agent_id,
                family_id=family_id,
                consolidation=consolidation,
            )
            force_new = True
            state = await window_repo.get_or_create(user_id, working_dir)
        seeded_prompt = await consume_pending_seed(
            window_repo,
            user_data,
            user_id=user_id,
            working_dir=working_dir,
            user_message=prompt,
            memory_repo=getattr(storage, "memory_repository", None),
            agent_id=agent_id,
        )
        return force_new, seeded_prompt, state, threshold

    @staticmethod
    async def run(
        router: Any,
        backend: Any,
        *,
        window_repo: Any = None,
        **kwargs: Any,
    ) -> Any:
        response = await router.run_turn(backend, purpose="chat", **kwargs)
        if kwargs.get("force_new") and window_repo is not None:
            await window_repo.clear_pending_transition(
                kwargs["user_id"], kwargs["working_directory"]
            )
        return response

    @staticmethod
    async def store(
        storage: Any,
        *,
        user_id: int,
        session_id: str,
        prompt: str,
        response: Any,
        window_id: Optional[int],
    ) -> Any:
        return await storage.save_claude_interaction(
            user_id=user_id,
            session_id=session_id,
            prompt=prompt,
            response=response,
            ip_address=None,
            window_id=window_id,
        )

    @staticmethod
    async def account(
        *,
        window_repo: Any,
        evaluator: Any,
        user_id: int,
        working_dir: Any,
        backend: str,
        response: Any,
        conversation_text: str,
        max_message_id: int,
        agent_id: str,
        trigger_text: str,
        refresh_threshold_tokens: int,
        family_id: Optional[int] = None,
    ) -> Any:
        return await account_turn_and_maybe_eval(
            window_repo=window_repo,
            evaluator=evaluator,
            user_id=user_id,
            working_dir=working_dir,
            backend=backend,
            added_tokens=response.input_tokens + response.output_tokens,
            context_tokens=_context_tokens_of(response),
            conversation_text=conversation_text,
            max_message_id=max_message_id,
            agent_id=agent_id,
            family_id=family_id,
            force_eval=mentions_remember_trigger(trigger_text),
            refresh_threshold_tokens=refresh_threshold_tokens,
        )

    @staticmethod
    async def roll_if_needed(
        *,
        window_repo: Any,
        evaluator: Any,
        user_data: dict[str, Any],
        user_id: int,
        working_dir: Any,
        backend: str,
        refresh_threshold_tokens: int,
        conversation_text: Callable[[Any], Awaitable[str]],
        agent_id: str,
        family_id: Optional[int] = None,
        consolidation: Any = None,
    ) -> tuple[Any, bool]:
        state = await window_repo.get_or_create(user_id, working_dir)
        if not window_repo.should_refresh(state, refresh_threshold_tokens):
            return state, False
        await refresh_window(
            window_repo,
            evaluator,
            user_data,
            user_id=user_id,
            working_dir=working_dir,
            backend=backend,
            conversation_text=await conversation_text(state),
            agent_id=agent_id,
            family_id=family_id,
            consolidation=consolidation,
        )
        return state, True
