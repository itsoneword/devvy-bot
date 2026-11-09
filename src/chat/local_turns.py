"""Local channel adapter for the model router and durable window lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from src.bot.orchestration.windowing import window_conversation_text
from src.chat.turn_service import ChatTurnService


class LocalChatTurns:
    def __init__(self, settings: Any, deps: Dict[str, Any]) -> None:
        self.settings = settings
        self.deps = deps
        self._sessions: Dict[tuple[int, str], str] = {}
        self._locks: Dict[tuple[int, str], asyncio.Lock] = {}

    async def stream(
        self, *, user_id: int, profile: str, text: str
    ) -> AsyncIterator[Dict[str, Any]]:
        registry = self.deps.get("profile_registry")
        selected = registry.get_enabled_by_slug(profile) if registry else None
        if registry and selected is None:
            yield {"type": "error", "text": f"Unknown profile: {profile}"}
            return
        working_dir = (
            selected.working_dir
            if selected is not None
            else Path(self.settings.approved_directory or Path.cwd())
        )
        key = (user_id, str(working_dir))
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        shared_locks = self.deps.get("turn_locks")
        lock = (
            shared_locks.hold(user_id, "local chat", profile)
            if shared_locks is not None
            else self._locks.setdefault(key, asyncio.Lock())
        )

        async def run() -> None:
            try:
                async with lock:
                    await self._turn(user_id, profile, text, working_dir, key, queue)
            except Exception as exc:
                await queue.put({"type": "error", "text": str(exc)})
            finally:
                await queue.put({"type": "_end"})

        task = asyncio.create_task(run())
        try:
            while True:
                event = await queue.get()
                if event["type"] == "_end":
                    break
                yield event
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _turn(
        self,
        user_id: int,
        profile: str,
        text: str,
        working_dir: Path,
        key: tuple[int, str],
        queue: asyncio.Queue[Dict[str, Any]],
    ) -> None:
        backend_id = self.settings.agent_backend
        backend = self.deps["backends"][backend_id]
        repo = self.deps["window_repo"]
        storage = self.deps["storage"]
        evaluator = self.deps.get("evaluator_service")
        user_data: Dict[str, Any] = {}
        consolidation = self.deps.get("consolidation_service")
        force_new, prompt, before, threshold = await ChatTurnService.prepare(
            window_repo=repo,
            storage=storage,
            evaluator=evaluator,
            user_data=user_data,
            user_id=user_id,
            working_dir=working_dir,
            backend=backend_id,
            model_ref=ChatTurnService.default_model_ref(backend_id, self.settings),
            prompt=text,
            force_new=False,
            agent_id=profile,
            consolidation=consolidation,
            conversation_text=lambda window: window_conversation_text(
                storage, user_id, working_dir, window
            ),
        )
        session_id = await ChatTurnService.session(
            backend=backend,
            user_id=user_id,
            working_dir=working_dir,
            remembered=self._sessions.get(key),
            force_new=force_new,
            auto_resume=True,
        )
        deltas = False

        async def on_stream(update: Any) -> None:
            nonlocal deltas
            if update.type == "stream_delta" and update.content:
                deltas = True
                await queue.put({"type": "delta", "text": update.content})

        response = await ChatTurnService.run(
            self.deps["model_router"],
            backend,
            window_repo=repo,
            prompt=prompt,
            working_directory=working_dir,
            user_id=user_id,
            session_id=session_id,
            on_stream=on_stream,
            force_new=force_new,
            model=None,
            requester_chat_id=user_id,
            role="owner",
        )
        self._sessions[key] = response.session_id
        state = await repo.get_or_create(user_id, working_dir)
        await ChatTurnService.store(
            storage,
            user_id=user_id,
            session_id=response.session_id,
            prompt=text,
            response=response,
            window_id=state.window_id,
        )
        conversation = await window_conversation_text(
            storage, user_id, working_dir, state
        )
        await ChatTurnService.account(
            window_repo=repo,
            evaluator=evaluator,
            user_id=user_id,
            working_dir=working_dir,
            backend=backend_id,
            response=response,
            conversation_text=conversation,
            max_message_id=await storage.messages.max_message_id_for_project(
                user_id, str(working_dir)
            ),
            agent_id=profile,
            trigger_text=text,
            refresh_threshold_tokens=threshold,
        )
        after, rolled = await ChatTurnService.roll_if_needed(
            window_repo=repo,
            evaluator=evaluator,
            user_data=user_data,
            user_id=user_id,
            working_dir=working_dir,
            backend=backend_id,
            refresh_threshold_tokens=threshold,
            conversation_text=lambda window: window_conversation_text(
                storage, user_id, working_dir, window
            ),
            agent_id=profile,
            consolidation=consolidation,
        )
        if rolled:
            await queue.put({"type": "window_rolled", "window_id": after.window_id + 1})
        if prompt != text:
            await queue.put(
                {"type": "seeded", "from_window": before.pending_seed_window}
            )
        if not deltas:
            await queue.put({"type": "delta", "text": response.content})
        await queue.put({"type": "done", "session_id": response.session_id})
