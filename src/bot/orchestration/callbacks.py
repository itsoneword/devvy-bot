"""Callback-query handlers for the orchestrator.

cd:/model:/agent: callback handling, backend-switch window rolling and
switch-message pinning. Code is moved verbatim from src/bot/orchestrator.py.
"""

import asyncio
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ...profiles.registry import ensure_workspace_for
from ..utils.html_format import escape_html
from .agents_menu import (
    CREATOR_SLUG,
    NEW_AGENT_INTAKE,
    build_agents_manage,
    build_agents_picker,
)
from .base import CLAUDE_BACKEND, CODEX_BACKEND, OrchestratorBase, policy_for
from .routing import _PROFILE_BADGES
from .windowing import refresh_window, window_conversation_text

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
        """On a real backend change, run the SAME transition as a 150k
        compaction (``refresh_window``): final extraction over the outgoing
        window's un-extracted tail, roll, and arm the seed durably in the DB
        so the next turn continues the conversation on the new backend.

        This used to roll and write the seed to ``user_data`` only. That is
        in-memory: a restart between the switch and the next message dropped
        both the seed and ``force_new_session``, so the turn resumed the
        retired session with no continuity — the dv-9f31 failure, which had
        only been fixed on the compaction path (dv-a826).
        """
        if new_backend == prev_backend:
            return
        window_repo = context.bot_data.get("window_repo")
        if not window_repo:
            return
        # Same resolver as text turns and /new (dv-c07a): rolling on the
        # current_directory/home fallback rotated a phantom window while the
        # conversation kept running in the profile workspace.
        user_id = query.from_user.id if query.from_user else None
        if user_id is None or context.user_data is None:
            return
        working_dir = self._routed_working_dir(context, user_id)
        state = await window_repo.get_or_create(user_id, working_dir)
        # The extraction runs on the backend just selected: prefs are already
        # persisted at this point, so it is the active one — same rule as the
        # compaction path, which evaluates on ``selected_backend``.
        await refresh_window(
            window_repo,
            context.bot_data.get("evaluator_service"),
            context.user_data,
            user_id=user_id,
            working_dir=working_dir,
            backend=new_backend,
            conversation_text=await window_conversation_text(
                context.bot_data.get("storage"), user_id, working_dir, state
            ),
            agent_id=self._agent_id_for(context, working_dir, user_id),
            family_id=self._family_id_for(context, user_id, working_dir),
        )

    async def _handle_model_callback(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        # The picker's callback keys are UI routes ("model:claude:<tier>",
        # "model:codex:<name>"), one block per backend the keyboard offers;
        # a third backend adds a keyboard row and a block here. Validation
        # and the ids written to prefs come from the backend's policy
        # (src/agent_backends/protocol.py), not from inline literals.
        prev_backend = context.user_data.get("agent_backend", CLAUDE_BACKEND)
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

        if data.startswith("agents:"):
            await self._handle_agents_menu_callback(query, context, data)
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
