"""Callback-query handlers for the orchestrator.

cd:/model:/agent: callback handling, backend-switch rebuild arming and
switch-message pinning. Code is moved verbatim from src/bot/orchestrator.py.
"""

import asyncio
from typing import Any, Optional, Tuple

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...profiles.registry import ensure_workspace_for
from ..utils.html_format import escape_html
from ..window_seed import model_switch_note
from .agents_menu import (
    CREATOR_SLUG,
    NEW_AGENT_INTAKE,
    build_agents_manage,
    build_agents_picker,
)
from .base import CLAUDE_BACKEND, CODEX_BACKEND, OrchestratorBase, policy_for
from .routing import _PROFILE_BADGES
from .windowing import arm_backend_switch

logger = structlog.get_logger()

INTERRUPT_PREFIX = "qi:"
INTERRUPT_NOTE = "[previous turn was interrupted by the user]"


def interrupt_markup(waiter_id: str) -> InlineKeyboardMarkup:
    """Button on a queued-message ack (dv-8b49)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏹ Прервать текущий и выполнить это",
                    callback_data=f"{INTERRUPT_PREFIX}{waiter_id}",
                )
            ]
        ]
    )


class CallbacksMixin(OrchestratorBase):
    """Inline callback queries (cd:/model:/agent:) and switch pinning."""

    async def _pin_switch_message(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int, message: Any
    ) -> None:
        """Pin the profile-switch confirmation, unpinning the previous one.

        Only ever touches the single message this method itself pinned last
        time (tracked in user_preferences) -- any other pins the user made by
        hand are left alone. Best-effort throughout: pin failures (e.g. no
        admin rights in a future group chat) are logged, not raised.
        """
        storage = context.bot_data.get("storage")
        if storage is None or message is None:
            return
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "message_id", None)
        if chat_id is None or message_id is None:
            return
        try:
            prefs = await storage.preferences.get(user_id)
            old_id = prefs.get("pinned_switch_message_id") if prefs else None
            if old_id and old_id != message_id:
                try:
                    await context.bot.unpin_chat_message(
                        chat_id=chat_id, message_id=old_id
                    )
                except Exception as exc:
                    logger.debug(
                        "unpin previous switch message failed (ignored)",
                        user_id=user_id,
                        message_id=old_id,
                        error=str(exc),
                    )
            await context.bot.pin_chat_message(
                chat_id=chat_id, message_id=message_id, disable_notification=True
            )
            await self._save_user_prefs(
                context, user_id, pinned_switch_message_id=message_id
            )
        except Exception as exc:  # best-effort; never break the switch flow
            logger.warning("pin switch message failed", user_id=user_id, error=str(exc))

    async def _maybe_roll_on_switch(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        query: Any,
        *,
        new_backend: str,
        prev_backend: str,
    ) -> None:
        """On a real backend change, arm a full-window rebuild (dv-168e).

        The window is NOT rolled: same window id, the token counter carries
        on, and no extraction runs here (the 150k rotation path still does
        its own). The next turn starts a fresh native session on the new
        backend seeded with every stored turn of the current window -- the
        new backend's old session, if any, lacks the other model's turns and
        must never be resumed. The intent is armed in ``conversation_windows``
        (not only user_data) so a restart before the next message cannot
        drop it (dv-9f31/dv-a826). Same-backend tier changes return early:
        their session resumes as is.
        """
        if new_backend == prev_backend:
            return
        window_repo = context.bot_data.get("window_repo")
        if not window_repo:
            return
        # Same resolver as text turns and /new (dv-c07a): arming on the
        # current_directory/home fallback would target a phantom window while
        # the conversation keeps running in the profile workspace.
        user_id = query.from_user.id if query.from_user else None
        if user_id is None or context.user_data is None:
            return
        await arm_backend_switch(
            window_repo,
            context.user_data,
            user_id=user_id,
            working_dir=self._routed_working_dir(context, user_id),
        )

    async def _note_model_switch(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        query: Any,
        *,
        prev: Tuple[str, Optional[str]],
        new: Tuple[str, Optional[str]],
    ) -> None:
        """Mark the switch point in the conversation itself (dv-168e).

        A backend switch rebuilds the current window in a fresh session on
        the new backend; a same-backend tier change resumes the session as is. In
        both cases the new model would otherwise read earlier replies as its
        own. The note rides the one-shot ``pending_notes`` queue (durable,
        same working-dir scope the next turn consumes), so it lands in the
        next prompt -- and the transcript -- exactly where the switch happened.
        """
        if prev == new:
            return
        storage = context.bot_data.get("storage")
        notes = getattr(storage, "pending_notes", None)
        user_id = query.from_user.id if query.from_user else None
        if notes is None or user_id is None or context.user_data is None:
            return
        working_dir = self._routed_working_dir(context, user_id)
        await notes.add(user_id, str(working_dir), model_switch_note(prev, new))

    async def _handle_model_callback(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        # The picker's callback keys are UI routes ("model:claude:<tier>",
        # "model:codex:<name>"), one block per backend the keyboard offers;
        # a third backend adds a keyboard row and a block here. Validation
        # and the ids written to prefs come from the backend's policy
        # (src/agent_backends/protocol.py), not from inline literals.
        prev_backend = context.user_data.get("agent_backend", CLAUDE_BACKEND)
        prev_model = context.user_data.get("agent_model")
        if data == "model:claude" or data.startswith("model:claude:"):
            tier = data.split(":", 2)[2] if data.count(":") >= 2 else ""
            # "claude" is the stored sentinel for "no tier: config default".
            model = policy_for(CLAUDE_BACKEND).resolve_model(tier) or "claude"
            context.user_data["agent_backend"] = CLAUDE_BACKEND
            context.user_data["agent_model"] = model
            if query.from_user:
                await self._save_user_prefs(
                    context,
                    query.from_user.id,
                    agent_backend=CLAUDE_BACKEND,
                    agent_model=model,
                )
            await self._maybe_roll_on_switch(
                context, query, new_backend=CLAUDE_BACKEND, prev_backend=prev_backend
            )
            await self._note_model_switch(
                context,
                query,
                prev=(prev_backend, prev_model),
                new=(CLAUDE_BACKEND, model),
            )
            label = model if model in self._CLAUDE_TIERS else "default model"
            await query.edit_message_text(
                f"Model backend set to Claude Code ({label})."
            )
            return
        if data.startswith("model:or:"):
            # A per-profile model (dv-d2f6): accepted only inside the active
            # profile's allowed_models, whatever the callback data says.
            pick = data.split(":", 2)[2]
            user_id = query.from_user.id if query.from_user else None
            profile = self._active_profile(context, user_id)
            if profile is None or pick not in profile.allowed_models:
                await query.edit_message_text(
                    "That model is not allowed for the active agent."
                )
                return
            context.user_data["agent_backend"] = CLAUDE_BACKEND
            context.user_data["agent_model"] = pick
            if user_id is not None:
                await self._save_user_prefs(
                    context, user_id, agent_backend=CLAUDE_BACKEND, agent_model=pick
                )
            await query.edit_message_text(
                f"Model set to <code>{escape_html(pick)}</code>.", parse_mode="HTML"
            )
            return
        if data == "model:codex_login":
            await self._start_codex_login(query, context)
            return
        if data == "model:codex_logout":
            proc = await asyncio.create_subprocess_exec(
                "codex",
                "logout",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            await query.edit_message_text(
                "OpenAI Codex credentials cleared."
                if proc.returncode == 0
                else "OpenAI Codex logout failed."
            )
            return
        if data.startswith("model:codex:"):
            model = data.split(":", 2)[2]
            model_ref = f"openai-codex/{model}"
            resolved = policy_for(CODEX_BACKEND).resolve_model(model_ref)
            if resolved != model_ref:
                await query.edit_message_text(
                    "That OpenAI Codex model is not available for ChatGPT auth. "
                    f"Using <code>{escape_html(str(resolved))}</code> instead.",
                    parse_mode="HTML",
                )
                model_ref = str(resolved)
            context.user_data["agent_backend"] = CODEX_BACKEND
            context.user_data["agent_model"] = model_ref
            if query.from_user:
                await self._save_user_prefs(
                    context,
                    query.from_user.id,
                    agent_backend=CODEX_BACKEND,
                    agent_model=model_ref,
                )
            await self._maybe_roll_on_switch(
                context, query, new_backend=CODEX_BACKEND, prev_backend=prev_backend
            )
            await self._note_model_switch(
                context,
                query,
                prev=(prev_backend, prev_model),
                new=(CODEX_BACKEND, model_ref),
            )
            await query.edit_message_text(
                "Model backend set to OpenAI Codex: "
                f"<code>{escape_html(model_ref)}</code>",
                parse_mode="HTML",
            )
            return
        await query.edit_message_text("Unknown model action.")

    async def _handle_agent_callback(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        """Handle agent:<slug> callbacks — switch the active profile + persist."""
        slug = data.split(":", 1)[1]
        registry = context.bot_data.get("profile_registry")
        profile = registry.get_enabled_by_slug(slug) if registry else None
        if not profile:
            await query.edit_message_text(f"Unknown profile: {slug}")
            return
        user_id = query.from_user.id if query.from_user else None
        role = self._resolve_role(user_id, context)
        if not profile.allows_role(role):
            await query.edit_message_text(f"🚫 The '{slug}' profile is owner-only.")
            storage = context.bot_data.get("storage")
            if storage and user_id is not None:
                await storage.log_bot_event(
                    user_id,
                    "profile_access_denied",
                    {"profile": slug, "role": role, "via": "agents_picker"},
                    success=False,
                )
            return
        working_dir = ensure_workspace_for(profile, role, user_id)
        context.user_data["active_profile"] = slug
        context.user_data["current_directory"] = working_dir
        if query.from_user:
            await self._save_user_prefs(
                context, query.from_user.id, active_profile=slug
            )
        badge = _PROFILE_BADGES.get(slug, slug)
        edited = await query.edit_message_text(
            f"{badge} — switched. You're now talking to {profile.name}."
        )
        if user_id is not None and hasattr(edited, "message_id"):
            await self._pin_switch_message(context, user_id, edited)
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger and query.from_user:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="agents",
                args=[slug],
                success=True,
            )

    async def _handle_agents_menu_callback(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        """agents:* callbacks — the /agents Manage view (dv-ffb4), owner-only.

        Toggles edit ``profiles.yaml`` (comment-preserving splice, validated
        before write); the running registry is boot-only, so the view is
        rendered from a fresh parse of the file and offers a restart once the
        two disagree.
        """
        import os
        import signal

        from ...profiles.config_edit import set_profile_enabled
        from ...profiles.registry import load_profile_registry

        user_id = query.from_user.id if query.from_user else None
        role = self._resolve_role(user_id, context)
        registry = context.bot_data.get("profile_registry")
        if not registry:
            await query.edit_message_text("Profiles are not configured.")
            return
        action = data.split(":", 2)[1]
        active = context.user_data.get("active_profile") or registry.get_default().slug

        if action == "back":
            text, markup = build_agents_picker(registry, active=active, role=role)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return

        if role != "owner":
            await query.edit_message_text("🚫 Managing agents is owner-only.")
            return

        config_path = self.settings.profiles_config_path
        audit_logger = context.bot_data.get("audit_logger")

        if action == "new":
            creator = registry.get_enabled_by_slug(CREATOR_SLUG)
            if creator is None:
                await query.edit_message_text(
                    f"The '{CREATOR_SLUG}' agent is not available; " "enable it first."
                )
                return
            working_dir = ensure_workspace_for(creator, role, user_id)
            context.user_data["active_profile"] = creator.slug
            context.user_data["current_directory"] = working_dir
            if user_id is not None:
                await self._save_user_prefs(
                    context, user_id, active_profile=creator.slug
                )
            edited = await query.edit_message_text(
                NEW_AGENT_INTAKE.format(creator=escape_html(creator.name)),
                parse_mode="HTML",
            )
            if user_id is not None and hasattr(edited, "message_id"):
                await self._pin_switch_message(context, user_id, edited)
            if audit_logger and user_id is not None:
                await audit_logger.log_command(user_id, "agents", ["new"], True)
            return

        if action == "restart":
            await query.edit_message_text(
                "🔄 <b>Restarting bot…</b>", parse_mode="HTML"
            )
            if audit_logger and user_id is not None:
                await audit_logger.log_command(user_id, "restart", ["agents"], True)
            logger.info("Restart requested via /agents manage", user_id=user_id)
            os.kill(os.getpid(), signal.SIGTERM)
            return

        if action == "toggle":
            slug = data.split(":", 2)[2]
            if not config_path:
                await query.edit_message_text("Profiles config path is not set.")
                return
            current = load_profile_registry(
                config_path, sandbox_default=self.settings.sandbox_enabled
            ).get_by_slug(slug)
            if current is None:
                await query.edit_message_text(f"Unknown profile: {slug}")
                return
            try:
                set_profile_enabled(
                    config_path,
                    slug,
                    not current.enabled,
                    sandbox_default=self.settings.sandbox_enabled,
                )
            except ValueError as exc:
                await query.edit_message_text(f"⚠️ Not changed: {escape_html(str(exc))}")
                return
            if audit_logger and user_id is not None:
                await audit_logger.log_command(
                    user_id,
                    "agents",
                    ["enable" if not current.enabled else "disable", slug],
                    True,
                )
            # fall through to re-render the manage view

        if action in ("manage", "toggle"):
            if not config_path:
                await query.edit_message_text("Profiles config path is not set.")
                return
            file_registry = load_profile_registry(
                config_path, sandbox_default=self.settings.sandbox_enabled
            )
            text, markup = build_agents_manage(file_registry, registry)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return

        await query.edit_message_text("Unknown agents action.")

    async def _handle_interrupt_callback(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        """qi:<waiter id>: stop the author's current turn on the same agent
        and run this queued message next (it jumps the queue)."""
        from ...profiles.registry import DEFAULT_AGENT_ID

        await query.answer()
        waiter = self._turn_locks.find_waiter(data[len(INTERRUPT_PREFIX) :])
        user_id = query.from_user.id
        if waiter is None:
            await self._drop_buttons(query, "Уже запущено или выполнено.")
            return
        if user_id != waiter.user_id and self._resolve_role(user_id, context) != (
            "owner"
        ):
            await query.message.reply_text("Только автор сообщения.")
            return
        if waiter.jumped:  # double press
            return
        lock = self._turn_locks.get(waiter.user_id, waiter.profile_id)
        if not lock.jump(waiter):
            await self._drop_buttons(query, "Уже запущено или выполнено.")
            return
        slug = waiter.profile_id or DEFAULT_AGENT_ID
        stopped = await self._stop_scoped_turns(context, waiter.user_id, slug)
        waiter.note_due = stopped > 0
        storage = context.bot_data.get("storage")
        if storage:
            await storage.log_bot_event(
                user_id,
                "queue_interrupt",
                {
                    "profile": slug,
                    "author": waiter.user_id,
                    "kind": waiter.kind,
                    "stopped": stopped,
                },
            )
        await self._drop_buttons(
            query, "⏹ Прерываю текущий, это сообщение — следующее."
        )

    @staticmethod
    async def _drop_buttons(query: Any, text: str) -> None:
        try:
            await query.edit_message_text(text)
        except Exception:
            pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        data = query.data or ""
        if data.startswith(INTERRUPT_PREFIX):
            # Answers the query itself (a refusal is an alert popup).
            await self._handle_interrupt_callback(query, context, data)
            return
        await query.answer()

        if data.startswith("model:"):
            await self._handle_model_callback(query, context, data)
            return

        if data.startswith("agent:"):
            await self._handle_agent_callback(query, context, data)
            return

        if data.startswith("agents:"):
            await self._handle_agents_menu_callback(query, context, data)
            return

        if data.startswith("rem:"):
            from .commands.reminders import handle_reminders_callback

            await handle_reminders_callback(query, context, data)
            return

        if data.startswith("vset:"):
            # /voice is owner-only; its buttons must be too — a callback
            # bypasses the command gate (dv-6b6c).
            if self._resolve_role(query.from_user.id, context) != "owner":
                await query.edit_message_text("🚫 Owner only.")
                return
            from .commands.voice import handle_voice_callback

            await handle_voice_callback(query, context, data)
            return

        if data.startswith("onb:"):
            # Same reason as vset: a button bypasses the owner-only command gate.
            if self._resolve_role(query.from_user.id, context) != "owner":
                await query.edit_message_text("🚫 Owner only.")
                return
            from .commands.setup import handle_callback

            await handle_callback(update, context, data)
            return

        if data.startswith("auth:"):
            from .commands.auth import handle_auth_callback

            await handle_auth_callback(query, context, data)
            return

        _, project_name = data.split(":", 1)

        base = self._workspace_base(context)
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing: every
        # registered backend re-points its session key (dv-5368).
        session_id = await self._refresh_resumable_sessions(
            context, query.from_user.id, new_path
        )

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
