"""Telegram bot authentication middleware."""

from datetime import UTC, datetime
from typing import Any, Callable, Dict

import structlog

from ...security.auth import fallback_role

logger = structlog.get_logger()


def _stamp_role(auth_manager: Any, user_id: int, data: Dict[str, Any]) -> None:
    """Record the authenticated user's role in per-user state.

    ``data["user_data"]`` is this update's ``context.user_data`` (injected by
    the middleware wrapper in core.py). Handlers read ``user_data["role"]``
    next to the rest of the per-user auth state. Missing role information
    fails closed: env-configured owners (``ALLOWED_USERS``) stay owner —
    the explicit single-owner install case — everyone else becomes guest.
    """
    user_data = data.get("user_data")
    if not isinstance(user_data, dict):
        return
    getter = getattr(auth_manager, "get_user_role", None)
    role = getter(user_id) if callable(getter) else None
    if not isinstance(role, str):
        role = fallback_role(user_id, data.get("settings"))
    user_data["role"] = role


async def auth_middleware(handler: Callable, event: Any, data: Dict[str, Any]) -> Any:
    """Check authentication before processing messages.

    This middleware:
    1. Checks if user is authenticated
    2. Attempts authentication if not authenticated
    3. Updates session activity
    4. Logs authentication events
    """
    # Extract user information
    user_id = event.effective_user.id if event.effective_user else None
    username = (
        getattr(event.effective_user, "username", None)
        if event.effective_user
        else None
    )

    if not user_id:
        logger.warning("No user information in update")
        return

    # Session watchdog hold (dv-8bb5): an admin suspended this user from a
    # watchdog alert's hold buttons. Commands and callback buttons pass
    # through untouched -- same carve-out as the rate limiter's cost gate
    # (src/bot/middleware/rate_limit.py) -- so a held user can still run
    # /help or /usage to see why, they just can't spend another turn.
    hold_store = data.get("hold_store")
    if hold_store is not None:
        held_until = hold_store.held_until(user_id)
        if held_until is not None:
            message = event.effective_message
            message_text = (message.text or "") if message else ""
            is_command = message_text.startswith("/")
            is_callback = getattr(event, "callback_query", None) is not None
            if not is_command and not is_callback:
                logger.info("Message blocked: user on hold", user_id=user_id)
                if event.effective_message:
                    await event.effective_message.reply_text(
                        "⏸️ <b>On hold</b>\n\n"
                        f"An admin put this account on hold until "
                        f"{held_until.strftime('%Y-%m-%d %H:%M UTC')}. "
                        "Commands like /help and /usage still work.",
                        parse_mode="HTML",
                    )
                return

    # Get dependencies from context
    auth_manager = data.get("auth_manager")
    audit_logger = data.get("audit_logger")

    if not auth_manager:
        logger.error("Authentication manager not available in middleware context")
        if event.effective_message:
            await event.effective_message.reply_text(
                "🔒 Authentication system unavailable. Please try again later."
            )
        return

    # Check if user is already authenticated
    if auth_manager.is_authenticated(user_id):
        # Update session activity
        if auth_manager.refresh_session(user_id):
            session = auth_manager.get_session(user_id)
            logger.debug(
                "Session refreshed",
                user_id=user_id,
                username=username,
                auth_provider=session.auth_provider if session else None,
            )

        # Continue to handler
        _stamp_role(auth_manager, user_id, data)
        return await handler(event, data)

    # User not authenticated - attempt authentication
    logger.info(
        "Attempting authentication for user", user_id=user_id, username=username
    )

    # Try to authenticate (providers will check whitelist and tokens)
    authentication_successful = await auth_manager.authenticate_user(user_id)

    # Log authentication attempt
    if audit_logger:
        await audit_logger.log_auth_attempt(
            user_id=user_id,
            success=authentication_successful,
            method="automatic",
            reason="message_received",
        )

    if authentication_successful:
        session = auth_manager.get_session(user_id)
        logger.info(
            "User authenticated successfully",
            user_id=user_id,
            username=username,
            auth_provider=session.auth_provider if session else None,
        )

        # Welcome message for new session
        if event.effective_message:
            await event.effective_message.reply_text(
                f"🔓 Welcome! You are now authenticated.\n"
                f"Session started at {datetime.now(UTC).strftime('%H:%M:%S UTC')}"
            )

        # Continue to handler
        _stamp_role(auth_manager, user_id, data)
        return await handler(event, data)

    else:
        # Authentication failed
        logger.warning("Authentication failed", user_id=user_id, username=username)

        if event.effective_message:
            await event.effective_message.reply_text(
                "🔒 <b>Authentication Required</b>\n\n"
                "You are not authorized to use this bot.\n"
                "Please contact the administrator for access.\n\n"
                f"Your Telegram ID: <code>{user_id}</code>\n"
                "Share this ID with the administrator to request access.",
                parse_mode="HTML",
            )
        return  # Stop processing


async def require_auth(handler: Callable, event: Any, data: Dict[str, Any]) -> Any:
    """Decorator-style middleware that requires authentication.

    This is a stricter version that only allows authenticated users.
    """
    user_id = event.effective_user.id if event.effective_user else None
    auth_manager = data.get("auth_manager")

    if not auth_manager or not auth_manager.is_authenticated(user_id):
        if event.effective_message:
            await event.effective_message.reply_text(
                "🔒 Authentication required to use this command."
            )
        return

    return await handler(event, data)


async def admin_required(handler: Callable, event: Any, data: Dict[str, Any]) -> Any:
    """Middleware that requires admin privileges.

    Note: This is a placeholder - admin privileges would need to be
    implemented in the authentication system.
    """
    user_id = event.effective_user.id if event.effective_user else None
    auth_manager = data.get("auth_manager")

    if not auth_manager or not auth_manager.is_authenticated(user_id):
        if event.effective_message:
            await event.effective_message.reply_text("🔒 Authentication required.")
        return

    session = auth_manager.get_session(user_id)
    if not session or not session.user_info:
        if event.effective_message:
            await event.effective_message.reply_text(
                "🔒 Session information unavailable."
            )
        return

    # Check for admin permissions (placeholder logic)
    permissions = session.user_info.get("permissions", [])
    if "admin" not in permissions:
        if event.effective_message:
            await event.effective_message.reply_text(
                "🔒 <b>Admin Access Required</b>\n\n"
                "This command requires administrator privileges.",
                parse_mode="HTML",
            )
        return

    return await handler(event, data)
