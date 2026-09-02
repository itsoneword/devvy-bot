"""Callback-query handlers for the orchestrator.

cd:/model:/agent: callback handling, backend-switch window rolling and
switch-message pinning. Code is moved verbatim from src/bot/orchestrator.py.
"""

import asyncio
from pathlib import Path
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ...profiles.registry import ensure_workspace_for
from ..utils.html_format import escape_html
from .base import _CODEX_DEFAULT_MODEL, _CODEX_SUPPORTED_MODELS, OrchestratorBase
from .routing import _PROFILE_BADGES

logger = structlog.get_logger()


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
        """On a real backend change, roll the window and arm the seed so the
        next turn continues the conversation on the new backend."""
        if new_backend == prev_backend:
            return
        window_repo = context.bot_data.get("window_repo")
        if not window_repo:
            return
        working_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory or Path.home()
        )
        old, _new = await window_repo.roll_window(
            query.from_user.id, working_dir, new_backend=new_backend
        )
        context.user_data["pending_seed_window"] = old
        context.user_data["force_new_session"] = True

    async def _handle_model_callback(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        prev_backend = context.user_data.get("agent_backend", "claude")
        if data == "model:claude" or data.startswith("model:claude:"):
            tier = data.split(":", 2)[2] if data.count(":") >= 2 else ""
            model = tier if tier in self._CLAUDE_TIERS else "claude"
            context.user_data["agent_backend"] = "claude"
            context.user_data["agent_model"] = model
            if query.from_user:
                await self._save_user_prefs(
                    context,
                    query.from_user.id,
                    agent_backend="claude",
                    agent_model=model,
                )
            await self._maybe_roll_on_switch(
                context, query, new_backend="claude", prev_backend=prev_backend
            )
            label = model if model in self._CLAUDE_TIERS else "default model"
            await query.edit_message_text(
                f"Model backend set to Claude Code ({label})."
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
            if model_ref not in _CODEX_SUPPORTED_MODELS:
                await query.edit_message_text(
                    "That OpenAI Codex model is not available for ChatGPT auth. "
                    f"Using <code>{escape_html(_CODEX_DEFAULT_MODEL)}</code> instead.",
                    parse_mode="HTML",
                )
                model_ref = _CODEX_DEFAULT_MODEL
            context.user_data["agent_backend"] = "openai_codex"
            context.user_data["agent_model"] = model_ref
            codex_integration = context.bot_data.get("codex_integration")
            if codex_integration and query.from_user:
                codex_integration.set_user_model(query.from_user.id, model_ref)
            if query.from_user:
                await self._save_user_prefs(
                    context,
                    query.from_user.id,
                    agent_backend="openai_codex",
                    agent_model=model_ref,
                )
            await self._maybe_roll_on_switch(
                context, query, new_backend="openai_codex", prev_backend=prev_backend
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
        profile = registry.get_by_slug(slug) if registry else None
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

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data or ""
        if data.startswith("model:"):
            await self._handle_model_callback(query, context, data)
            return

        if data.startswith("agent:"):
            await self._handle_agent_callback(query, context, data)
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

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

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
