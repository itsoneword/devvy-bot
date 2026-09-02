"""Admin command handlers moved verbatim from src/bot/orchestrator.py."""

from datetime import timedelta
from typing import Any, List, Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ....storage.user_repository import SOURCE_ADMIN, SOURCE_ENV, SOURCE_FAMILY
from ..base import OrchestratorBase
from ..formatting import format_family, parse_telegram_id

logger = structlog.get_logger()


class AdminCommandsMixin(OrchestratorBase):
    """User & family management commands (users & families v1)."""

    async def agentic_adduser(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Owner-only: grant a Telegram user guest access (runtime, no restart)."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        user_store = context.bot_data.get("user_store")
        storage = context.bot_data.get("storage")
        if user_store is None:
            await message.reply_text("User management is not available.")
            return
        target = parse_telegram_id(context.args or [])
        if target is None:
            await message.reply_text(
                "Usage: /adduser <telegram_id> — a positive numeric Telegram id."
            )
            return
        caller_id = user.id
        try:
            if target in (self.settings.allowed_users or []):
                await message.reply_text(f"{target} is already an owner.")
                return
            existing = await user_store.get_user(target)
            if existing is not None:
                await message.reply_text(
                    f"{target} already has access "
                    f"(role {existing.role}, source {existing.source})."
                )
                return
            await user_store.add_user(
                target, role="guest", source=SOURCE_ADMIN, added_by=caller_id
            )
        except Exception:
            logger.warning("adduser failed", target=target, exc_info=True)
            await message.reply_text("Could not add the user. Please try again.")
            return
        if storage:
            await storage.log_bot_event(
                caller_id, "command_adduser", {"target": target}
            )
        await message.reply_text(f"Added guest {target}.")

    async def agentic_rmuser(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Owner-only: revoke a DB-managed user's access (clears family too)."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        user_store = context.bot_data.get("user_store")
        storage = context.bot_data.get("storage")
        auth_manager = context.bot_data.get("auth_manager")
        if user_store is None:
            await message.reply_text("User management is not available.")
            return
        target = parse_telegram_id(context.args or [])
        if target is None:
            await message.reply_text(
                "Usage: /rmuser <telegram_id> — a positive numeric Telegram id."
            )
            return
        caller_id = user.id
        try:
            if target in (self.settings.allowed_users or []):
                await message.reply_text(
                    f"{target} is an env-configured owner; "
                    "remove them from ALLOWED_USERS instead."
                )
                return
            removed = await user_store.remove_user(target)
        except Exception:
            logger.warning("rmuser failed", target=target, exc_info=True)
            await message.reply_text("Could not remove the user. Please try again.")
            return
        if not removed:
            await message.reply_text(f"{target} is not a managed user.")
            return
        # Drop any live session so revocation takes effect immediately.
        if auth_manager is not None:
            try:
                auth_manager.end_session(target)
            except Exception:
                logger.warning("rmuser end_session failed", target=target)
        if storage:
            await storage.log_bot_event(caller_id, "command_rmuser", {"target": target})
        reply = f"Removed {target}."
        if target in (self.settings.guest_users or []):
            reply += (
                " Note: they are still listed in GUEST_USERS; "
                "remove them there to fully revoke access."
            )
        await message.reply_text(reply)

    async def agentic_family(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/family [add <id> | delete <id> | manage].

        View (manage) and delete are open to all users; ``add`` is owner-only
        (dv-09d8) — otherwise the invite graph is transitive: a guest could
        grant a stranger bot access and shared family memory.
        """
        message = update.effective_message
        if message is None:
            return
        if context.bot_data.get("user_store") is None:
            await message.reply_text("Family management is not available.")
            return
        args = list(context.args or [])
        sub = args[0].lower() if args else "manage"
        if sub == "add":
            await self._family_add(update, context, args[1:])
        elif sub == "delete":
            await self._family_delete(update, context, args[1:])
        elif sub == "manage":
            await self._family_manage(update, context)
        else:
            await message.reply_text(
                "Usage: /family — show your family\n"
                "/family add <telegram_id> — add a member\n"
                "/family delete <telegram_id> — remove a member"
            )

    async def _family_add(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: List[str],
    ) -> None:
        """Add a user to the caller's family (auto-creates it). Owner-only."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        user_store = context.bot_data["user_store"]
        storage = context.bot_data.get("storage")
        caller_id = user.id
        caller_role = self._resolve_role(caller_id, context)
        if caller_role != "owner":
            logger.warning(
                "family add denied: caller is not an owner",
                user_id=caller_id,
                role=caller_role,
            )
            if storage:
                await storage.log_bot_event(
                    caller_id,
                    "family_add_denied",
                    {"reason": "not_owner", "role": caller_role},
                    success=False,
                )
            await message.reply_text(
                "Only owners can add family members. "
                "Ask an owner to run /family add for you."
            )
            return
        target = parse_telegram_id(args)
        if target is None:
            await message.reply_text(
                "Usage: /family add <telegram_id> — a positive numeric Telegram id."
            )
            return
        if target == caller_id:
            await message.reply_text("That's you — no need to add yourself.")
            return
        cap = self.settings.family_max_members
        try:
            # Owners may join a family for fact-sharing; membership never
            # changes their role or access.
            target_is_owner = target in (self.settings.allowed_users or [])
            target_row = await user_store.get_user(target)
            if target_row is not None and target_row.role == "owner":
                target_is_owner = True

            caller_row = await user_store.get_user(caller_id)
            family_id = caller_row.family_id if caller_row else None

            if target_row is not None and target_row.family_id is not None:
                if family_id is not None and target_row.family_id == family_id:
                    await message.reply_text(f"{target} is already in your family.")
                else:
                    await message.reply_text(
                        f"{target} already belongs to another family."
                    )
                if storage:
                    await storage.log_bot_event(
                        caller_id,
                        "family_add_denied",
                        {"target": target, "reason": "already_in_family"},
                        success=False,
                    )
                return

            if family_id is None:
                family = await user_store.create_family(caller_id)
                family_id = family.id
                if caller_row is None:
                    # Env-configured caller (owner) without a DB row yet:
                    # create their membership row. Role mirrors their resolved
                    # role (the gate above guarantees owner).
                    await user_store.add_user(
                        caller_id,
                        role=caller_role,
                        source=SOURCE_ENV,
                        family_id=family_id,
                    )
                else:
                    await user_store.set_family(caller_id, family_id)

            member_count = await user_store.count_family_members(family_id)
            if member_count >= cap:
                await message.reply_text(
                    f"Your family is full ({member_count}/{cap} members)."
                )
                if storage:
                    await storage.log_bot_event(
                        caller_id,
                        "family_add_denied",
                        {"target": target, "reason": "family_full"},
                        success=False,
                    )
                return

            if target_row is not None:
                await user_store.set_family(target, family_id)
            elif target_is_owner:
                # Env-configured owner without a DB row: membership row only,
                # role stays owner and source stays env (never revocable via
                # family delete).
                await user_store.add_user(
                    target,
                    role="owner",
                    source=SOURCE_ENV,
                    added_by=caller_id,
                    family_id=family_id,
                )
            else:
                await user_store.add_user(
                    target,
                    role="guest",
                    source=SOURCE_FAMILY,
                    added_by=caller_id,
                    family_id=family_id,
                )
            member_count += 1
        except Exception:
            logger.warning("family add failed", target=target, exc_info=True)
            await message.reply_text("Could not add the member. Please try again.")
            return

        if storage:
            await storage.log_bot_event(
                caller_id,
                "family_add",
                {"target": target, "family_id": family_id, "count": member_count},
            )
        await self._notify_owner_family_add(
            context, caller_id, target, member_count, cap
        )
        if target_is_owner:
            await message.reply_text(
                f"Added {target} to your family ({member_count}/{cap} members). "
                "They're an owner — this only shares family facts."
            )
        else:
            await message.reply_text(
                f"Added {target} to your family ({member_count}/{cap} members). "
                "They can now talk to Devvy."
            )

    async def _notify_owner_family_add(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        caller_id: int,
        target: int,
        member_count: int,
        cap: int,
    ) -> None:
        """Broadcast a who-added-whom notice to the owner chat(s)."""
        event_bus = context.bot_data.get("event_bus")
        if event_bus is None:
            return
        try:
            from ....events.types import AgentResponseEvent

            await event_bus.publish(
                AgentResponseEvent(
                    chat_id=0,  # broadcast to NOTIFICATION_CHAT_IDS
                    text=(
                        f"👪 Family update: {caller_id} added {target} "
                        f"to their family ({member_count}/{cap} members)."
                    ),
                    parse_mode=None,
                    source="family",
                )
            )
        except Exception:
            logger.warning("family add owner-notify failed", exc_info=True)

    async def _family_delete(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: List[str],
    ) -> None:
        """Remove a member from a family; revoke access if family-sourced."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        user_store = context.bot_data["user_store"]
        storage = context.bot_data.get("storage")
        auth_manager = context.bot_data.get("auth_manager")
        caller_id = user.id
        target = parse_telegram_id(args)
        if target is None:
            await message.reply_text(
                "Usage: /family delete <telegram_id> — "
                "a positive numeric Telegram id."
            )
            return
        caller_role = self._resolve_role(caller_id, context)
        try:
            target_row = await user_store.get_user(target)
            if target_row is None or target_row.family_id is None:
                await message.reply_text(f"{target} is not in a family.")
                return
            if caller_role != "owner":
                caller_row = await user_store.get_user(caller_id)
                if caller_row is None or caller_row.family_id != target_row.family_id:
                    await message.reply_text(
                        "You can only remove members of your own family."
                    )
                    if storage:
                        await storage.log_bot_event(
                            caller_id,
                            "family_delete_denied",
                            {"target": target, "reason": "not_own_family"},
                            success=False,
                        )
                    return
            revoked = target_row.source == SOURCE_FAMILY and target_row.role != "owner"
            if revoked:
                await user_store.remove_user(target)
                if auth_manager is not None:
                    try:
                        auth_manager.end_session(target)
                    except Exception:
                        logger.warning("family delete end_session failed")
            else:
                await user_store.set_family(target, None)
        except Exception:
            logger.warning("family delete failed", target=target, exc_info=True)
            await message.reply_text("Could not remove the member. Please try again.")
            return
        if storage:
            await storage.log_bot_event(
                caller_id,
                "family_delete",
                {
                    "target": target,
                    "family_id": target_row.family_id,
                    "revoked": revoked,
                },
            )
        if revoked:
            await message.reply_text(f"Removed {target}. Their bot access is revoked.")
        else:
            await message.reply_text(
                f"Removed {target} from the family. They keep bot access."
            )

    async def _family_manage(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show the caller's family: members, roles, dates, count vs cap."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        user_store = context.bot_data["user_store"]
        storage = context.bot_data.get("storage")
        caller_id = user.id
        try:
            caller_row = await user_store.get_user(caller_id)
            if caller_row is None or caller_row.family_id is None:
                await message.reply_text(
                    "You don't have a family yet. "
                    "Use /family add <telegram_id> to start one."
                )
                return
            family = await user_store.get_family(caller_row.family_id)
            members = await user_store.list_family_members(caller_row.family_id)
        except Exception:
            logger.warning("family manage failed", exc_info=True)
            await message.reply_text("Could not load your family. Please try again.")
            return
        if family is None:
            await message.reply_text("Your family record is missing.")
            return
        if storage:
            await storage.log_bot_event(
                caller_id,
                "family_manage",
                {"family_id": family.id, "count": len(members)},
            )
        await message.reply_text(
            format_family(family, members, self.settings.family_max_members),
            parse_mode="HTML",
        )

    async def _confirmation_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resolve a destructive-op approve/deny button press."""
        query = update.callback_query
        if query is None:
            return
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[0] != "confirm":
            try:
                await query.answer()
            except Exception:
                logger.debug("Failed to answer confirmation callback query")
            return

        _, token, decision = parts
        approved = decision == "y"
        cm = context.bot_data.get("confirmation_manager")
        matched = cm.resolve(token, approved) if cm else False

        if not matched:
            # Already resolved — timed out (message already rewritten by the
            # timeout handler) or a double-tap. This button press had no
            # effect either way; say so plainly rather than echoing back
            # "Approved" for a tap that changed nothing.
            try:
                await query.answer(
                    "Already resolved (timed out) — this tap had no effect.",
                    show_alert=True,
                )
            except Exception:
                logger.debug("Failed to answer stale confirmation callback query")
            return

        try:
            await query.answer()
        except Exception:
            logger.debug("Failed to answer confirmation callback query")
        verdict = "✅ Approved" if approved else "⛔ Denied"
        try:
            base = query.message.text if query.message else ""
            await query.edit_message_text(
                f"{base}\n\n<b>{verdict}</b>", parse_mode="HTML"
            )
        except Exception:
            logger.debug("Failed to edit confirmation message")

    async def _watchdog_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resolve a session-watchdog hold/dismiss button press (dv-8bb5)."""
        query = update.callback_query
        if query is None:
            return
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[0] != "watchdog":
            try:
                await query.answer()
            except Exception:
                logger.debug("Failed to answer watchdog callback query")
            return

        _, token, action = parts
        wm = context.bot_data.get("watchdog_manager")
        target_user_id = wm.resolve_user(token) if wm else None

        if target_user_id is None or wm is None:
            try:
                await query.answer(
                    "Already resolved — this tap had no effect.",
                    show_alert=True,
                )
            except Exception:
                logger.debug("Failed to answer stale watchdog callback query")
            return

        admin = update.effective_user
        admin_id = admin.id if admin else None
        hold_store = context.bot_data.get("hold_store")

        if action == "10m":
            verdict = await self._apply_watchdog_hold(
                hold_store,
                target_user_id,
                timedelta(minutes=10),
                admin_id,
                "10 minutes",
            )
        elif action == "1d":
            verdict = await self._apply_watchdog_hold(
                hold_store, target_user_id, timedelta(days=1), admin_id, "1 day"
            )
        elif action == "ack":
            verdict = f"✅ Dismissed by <code>{admin_id}</code>"
        else:
            try:
                await query.answer()
            except Exception:
                logger.debug("Failed to answer watchdog callback query")
            return

        wm.clear(token)
        try:
            await query.answer()
        except Exception:
            logger.debug("Failed to answer watchdog callback query")
        try:
            base = query.message.text if query.message else ""
            await query.edit_message_text(
                f"{base}\n\n<b>{verdict}</b>", parse_mode="HTML"
            )
        except Exception:
            logger.debug("Failed to edit watchdog message")

        storage = context.bot_data.get("storage")
        if storage:
            await storage.log_bot_event(
                admin_id or 0,
                "watchdog_action",
                {"target_user_id": target_user_id, "action": action},
            )

    async def _apply_watchdog_hold(
        self,
        hold_store: Optional[Any],
        user_id: int,
        duration: timedelta,
        admin_id: Optional[int],
        label: str,
    ) -> str:
        """Set a hold and return the verdict line for the alert message."""
        if hold_store is None:
            logger.warning("Watchdog hold requested but hold_store unavailable")
            return "⚠️ Hold storage unavailable — no hold was applied."
        until = await hold_store.set_hold(
            user_id, duration, reason="watchdog", set_by=admin_id
        )
        return f"⏸️ Held for {label} (until {until.strftime('%Y-%m-%d %H:%M UTC')})"
