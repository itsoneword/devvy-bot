"""Shared base for MessageOrchestrator and its command mixins.

Holds construction (settings/deps/turn locks), dependency injection and
thread-routing enforcement, and the small helpers used across method
clusters. Command mixins subclass this; behavior is unchanged — code is
moved verbatim from src/bot/orchestrator.py.
"""

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ...config.settings import Settings
from ...profiles.registry import agent_id_for_working_dir
from ...security.auth import fallback_role
from .routing import seed_prefs_into_user_data

logger = structlog.get_logger()


_CODEX_DEFAULT_MODEL = "openai-codex/gpt-5.4-mini"
_CODEX_HARD_MODEL = "openai-codex/gpt-5.5"
_CODEX_SUPPORTED_MODELS = {
    _CODEX_HARD_MODEL,
    _CODEX_DEFAULT_MODEL,
}


class OrchestratorBase:
    """Shared state and helpers for the orchestrator class family."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        # One turn at a time per user. concurrent_updates(True) must stay on
        # (confirmation buttons need dispatch during a turn), so without this
        # a second message mid-turn starts a second parallel claude process:
        # OOM risk on this host + a last-writer-wins race on the session file.
        # Commands and callback queries never take this lock. The registry is
        # shared with AgentHandler (via deps) so scheduler/webhook/job-finish
        # turns queue behind Telegram turns too.
        from ...utils.turn_locks import TurnLocks

        self._turn_locks: TurnLocks = deps.get("turn_locks") or TurnLocks()

    def _turn_lock(self, user_id: int) -> asyncio.Lock:
        return self._turn_locks.get(user_id)

    def _workspace_base(self, context: ContextTypes.DEFAULT_TYPE) -> Path:
        """Root directory that /repo lists and switches within.

        Allowlist mode has one ``approved_directory`` and that is the boundary.
        Denylist mode (the native service) has none, and /repo used to read it
        straight into ``base.iterdir()`` — ``AttributeError`` on ``None``, i.e.
        /repo was broken in the shipped configuration (dv-a1c8). The meaning of
        "my workspace" once profiles exist is the active profile's
        ``working_dir``, so that is the fallback.
        """
        if self.settings.approved_directory is not None:
            return Path(self.settings.approved_directory)
        registry = context.bot_data.get("profile_registry") or self.deps.get(
            "profile_registry"
        )
        if registry is not None:
            slug = context.user_data.get("active_profile")
            profile = registry.get_by_slug(slug) if slug else None
            if profile is None:
                profile = registry.get_default()
            if profile is not None:
                return Path(profile.working_dir)
        current = context.user_data.get("current_directory")
        return Path(current) if current else Path.home()

    def _routed_working_dir(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int
    ) -> Path:
        """Working dir resolved through the SAME profile router as text turns.

        Commands that operate on "the current conversation" (/new, /status)
        must resolve the workspace exactly like the message pipeline does, or
        they act on a different window than the one the user is talking in.
        Live failure (dv-5f3a, 2026-08-15): /new read ``current_directory``
        (unset -> home-dir fallback) and rolled the window for the service
        user's home directory while the user's coach conversation kept running
        in the coach workspace — the reset silently went to the wrong scope.
        Same class as the media fix dv-b3d7.
        """
        from .routing import apply_route

        default = context.user_data.get(
            "current_directory", self.settings.approved_directory or Path.home()
        )
        registry = context.bot_data.get("profile_registry")
        if not registry:
            return Path(str(default))
        role = self._resolve_role(user_id, context)
        user_data = context.user_data if context.user_data is not None else {}
        decision = apply_route("", user_data, registry, role=role, user_id=user_id)
        return decision.working_dir

    def _resolve_role(
        self, user_id: Optional[int], context: ContextTypes.DEFAULT_TYPE
    ) -> str:
        """Resolve owner|guest for this update.

        Prefers the live auth session (authoritative), falls back to the role
        stamped into user_data by the auth middleware, then fails closed:
        env-configured owners (``ALLOWED_USERS``) stay owner — the explicit
        single-owner install case — and everyone else resolves to guest. An
        unresolvable role must never promote to owner (dv-09d8).
        """
        auth_manager = context.bot_data.get("auth_manager")
        if auth_manager is not None and user_id is not None:
            getter = getattr(auth_manager, "get_user_role", None)
            if callable(getter):
                role = getter(user_id)
                if isinstance(role, str):
                    return role
        user_data = context.user_data or {}
        stamped = user_data.get("role")
        if isinstance(stamped, str):
            return stamped
        return fallback_role(user_id, self.settings)

    def _owner_only(  # type: ignore[type-arg]
        self, handler: Callable, command: str
    ) -> Callable:
        """Gate a command handler to the owner role; guests get a refusal."""
        import functools

        @functools.wraps(handler)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            user_id = user.id if user else None
            role = self._resolve_role(user_id, context)
            if role != "owner":
                logger.warning(
                    "Owner-only command denied",
                    command=command,
                    user_id=user_id,
                    role=role,
                )
                storage = context.bot_data.get("storage")
                if storage and user_id is not None:
                    try:
                        await storage.log_bot_event(
                            user_id,
                            "owner_only_denied",
                            {"command": command, "role": role},
                            success=False,
                        )
                    except Exception:
                        logger.warning("owner_only audit log failed", exc_info=True)
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "This command is owner-only."
                    )
                return
            await handler(update, context)

        return wrapped

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            if not context.user_data.get("_prefs_loaded"):
                context.user_data["_prefs_loaded"] = True
                storage = context.bot_data.get("storage")
                user = update.effective_user
                if storage is not None and user is not None:
                    try:
                        saved = await storage.preferences.get(user.id)
                        seed_prefs_into_user_data(context.user_data, saved)
                    except Exception as exc:  # best-effort; fall back to defaults
                        logger.warning(
                            "prefs load failed", user_id=user.id, error=str(exc)
                        )

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    def _selected_backend(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        return str(
            context.user_data.get("agent_backend") or self.settings.agent_backend
        )

    def _selected_model(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        backend = self._selected_backend(context)
        configured = context.user_data.get("agent_model") or self.settings.agent_model
        if backend == "openai_codex":
            model = str(configured or _CODEX_DEFAULT_MODEL)
            if model not in _CODEX_SUPPORTED_MODELS:
                context.user_data["agent_model"] = _CODEX_DEFAULT_MODEL
                return _CODEX_DEFAULT_MODEL
            return model
        if configured in self._CLAUDE_TIERS:
            return str(configured)
        return str(self.settings.claude_model or "CLI default")

    _CLAUDE_TIERS = ("sonnet", "opus")

    def _claude_model_override(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> Optional[str]:
        """User-selected Claude tier ('sonnet'/'opus'), or None for the config default."""
        if self._selected_backend(context) == "openai_codex":
            return None
        m = context.user_data.get("agent_model") or self.settings.agent_model
        if m in self._CLAUDE_TIERS:
            return str(m)
        return None

    async def _save_user_prefs(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int, **fields: Any
    ) -> None:
        """Persist selected prefs; best-effort (no-op if storage absent)."""
        storage = context.bot_data.get("storage")
        if storage is None:
            return
        try:
            await storage.preferences.set(user_id, **fields)
        except Exception as exc:  # best-effort; never break message handling
            logger.warning("prefs save failed", user_id=user_id, error=str(exc))

    @staticmethod
    def _session_key(backend: str) -> str:
        return "codex_session_id" if backend == "openai_codex" else "claude_session_id"

    def _agent_id_for(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        working_dir: Any,
        user_id: Optional[int] = None,
    ) -> str:
        """Active profile slug = memory scope key for ``working_dir``.

        Defaults to 'devops' in single-profile mode or when no profile matches.
        ``user_id`` additionally applies per-user scoping on profiles with
        ``personal_memory: true`` (every user gets their own scope there,
        not just guests) — see ``agent_id_for_working_dir``.
        """
        return agent_id_for_working_dir(
            context.bot_data.get("profile_registry"), working_dir, user_id
        )

    def _family_id_for(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: Optional[int],
        working_dir: Any = None,
    ) -> Optional[int]:
        """The user's family id for this turn, or None (no family / no store).

        Reads the UserStore's in-memory snapshot (kept fresh by the auth
        middleware and every family mutation) — no DB hit per turn. None means
        prompts and evaluator behave exactly as before families existed.

        ``working_dir``, when given, gates on the active profile's
        ``family_memory`` flag: profiles that opt out (e.g. a personal
        health coach) always get None here, so neither the read path
        (prompt composition) nor the write path (evaluator) ever sees or
        writes family-shared facts for that profile, regardless of how the
        user frames the message. Other profiles are unaffected.
        """
        user_store = context.bot_data.get("user_store")
        if user_store is None or user_id is None:
            return None
        family_id = user_store.get_family_id_cached(user_id)
        # Defensive typing: only a real int enables the family scope.
        if not isinstance(family_id, int):
            return None
        if working_dir is not None:
            registry = context.bot_data.get("profile_registry")
            if registry is not None:
                profile = registry.get_by_working_dir(Path(str(working_dir)))
                if profile is not None and not profile.family_memory:
                    return None
        return family_id

    async def _start_codex_login(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "login",
            "--device-auth",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await query.edit_message_text(
            "Starting OpenAI Codex device login… I'll send the code shortly."
        )
        context.application.create_task(
            self._relay_codex_login(query.message.chat_id, proc, context.bot)
        )

    async def _relay_codex_login(self, chat_id: int, proc: Any, bot: Any) -> None:
        assert proc.stdout is not None
        sent_code = False
        buf = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            buf.append(text)
            # CLI prints a verification URL + user code; relay the first useful lines.
            if not sent_code and ("http" in text or "code" in text.lower()):
                await bot.send_message(chat_id=chat_id, text=text)
                sent_code = True
        await proc.wait()
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "OpenAI Codex login complete."
                if proc.returncode == 0
                else "OpenAI Codex login failed:\n" + "\n".join(buf[-5:])
            ),
        )
