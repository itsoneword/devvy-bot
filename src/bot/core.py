"""Main Telegram bot class.

Features:
- Command registration
- Handler management
- Context injection
- Graceful shutdown
"""

import asyncio
from typing import Any, Callable, Dict, Optional

import structlog
from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from ..config.settings import Settings
from ..exceptions import ClaudeCodeTelegramError
from ..security.auth import fallback_role
from ..notifications.routing import JOBS_HINT
from .features.registry import FeatureRegistry
from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()


class ClaudeCodeBot:
    """Main bot orchestrator."""

    def __init__(self, settings: Settings, dependencies: Dict[str, Any]):
        """Initialize bot with settings and dependencies."""
        self.settings = settings
        self.deps = dependencies
        self.app: Optional[Application] = None
        self.is_running = False
        self.feature_registry: Optional[FeatureRegistry] = None
        self.orchestrator = MessageOrchestrator(settings, dependencies)

    async def initialize(self) -> None:
        """Initialize bot application. Idempotent — safe to call multiple times."""
        if self.app is not None:
            return

        logger.info("Initializing Telegram bot")

        # Create application
        builder = Application.builder()
        builder.token(self.settings.telegram_token_str)
        builder.defaults(Defaults(do_quote=self.settings.reply_quote))
        builder.rate_limiter(AIORateLimiter(max_retries=1))

        # Process updates concurrently so callback queries (e.g. the
        # destructive-op confirmation buttons) can be dispatched while a
        # long-running agentic text turn is still in flight. Without this,
        # PTB serializes all updates and a multi-minute Claude turn blocks
        # the Approve/Deny tap until both the confirmation gate's own
        # timeout and Telegram's callback-query staleness window expire.
        builder.concurrent_updates(True)

        # Configure connection settings
        builder.connect_timeout(30)
        builder.read_timeout(30)
        builder.write_timeout(30)
        builder.pool_timeout(30)

        self.app = builder.build()

        # Initialize feature registry
        self.feature_registry = FeatureRegistry(
            config=self.settings,
            storage=self.deps.get("storage"),
            security=self.deps.get("security"),
        )

        # Add feature registry to dependencies
        self.deps["features"] = self.feature_registry

        # Initialize the underlying Telegram Application so the bot's
        # HTTP client is ready before we make API calls.
        await self.app.initialize()

        # Set bot commands for menu (requires initialized HTTP client)
        await self._set_bot_commands()

        # Register handlers
        self._register_handlers()

        # Add middleware
        self._add_middleware()

        # Set error handler
        self.app.add_error_handler(self._error_handler)

        logger.info("Bot initialization complete")

    async def _set_bot_commands(self) -> None:
        """Set bot command menu via orchestrator.

        The default (global) menu is the owner view. Guests get a reduced
        per-chat menu with owner-only commands hidden — cosmetic only;
        runtime gating in the orchestrator is what actually enforces access.
        """
        if self.app is None:
            return
        commands = await self.orchestrator.get_bot_commands()
        await self.app.bot.set_my_commands(commands)
        logger.info("Bot commands set", commands=[cmd.command for cmd in commands])

        guest_ids = set(self.settings.guest_users or [])
        user_store = self.deps.get("user_store")
        if user_store is not None:
            try:
                guest_ids |= set(user_store.guest_ids())
            except Exception:
                logger.warning("Failed to read DB guest ids for menus")
        if not guest_ids:
            return
        from telegram import BotCommandScopeChat

        guest_commands = await self.orchestrator.get_bot_commands(role="guest")
        for chat_id in sorted(guest_ids):
            try:
                await self.app.bot.set_my_commands(
                    guest_commands, scope=BotCommandScopeChat(chat_id=chat_id)
                )
            except Exception as e:
                # Telegram rejects scoped menus for chats the bot has never
                # seen; the reduced menu appears after the guest's first /start.
                logger.warning(
                    "Failed to set guest command menu",
                    chat_id=chat_id,
                    error=str(e),
                )

    def _register_handlers(self) -> None:
        """Register handlers via the orchestrator."""
        self.orchestrator.register_handlers(self.app)

    def _add_middleware(self) -> None:
        """Add middleware to application."""
        from .middleware.auth import auth_middleware
        from .middleware.rate_limit import rate_limit_middleware
        from .middleware.security import security_middleware

        # Middleware runs in order of group numbers (lower = earlier)
        # Security middleware first (validate inputs)
        self.app.add_handler(
            MessageHandler(
                filters.ALL, self._create_middleware_handler(security_middleware)
            ),
            group=-3,
        )

        # Authentication second
        self.app.add_handler(
            MessageHandler(
                filters.ALL, self._create_middleware_handler(auth_middleware)
            ),
            group=-2,
        )

        # Rate limiting third
        self.app.add_handler(
            MessageHandler(
                filters.ALL, self._create_middleware_handler(rate_limit_middleware)
            ),
            group=-1,
        )

        self._add_reports_chat_handlers()

        logger.info("Middleware added to bot")

    def _add_reports_chat_handlers(self) -> None:
        """Reports group (dv-e215): record membership changes, ignore chatter.

        * my_chat_member (bot added/removed/promoted in any chat) -> structured
          log line + audit entry carrying the chat id, so setup never needs a
          web client. Always on.
        * With REPORTS_CHAT_ID set, every message in that chat is dropped before
          auth and the agent handlers: the group is read-only output, and an
          allowed user typing there must not start an agent turn that shares
          the DM session. Other groups and project threads are untouched.
        """
        from telegram.ext import ApplicationHandlerStop, ChatMemberHandler

        app = self.app
        assert app is not None

        async def on_my_chat_member(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            change = update.my_chat_member
            if change is None:
                return
            chat = change.chat
            audit = self.deps.get("audit_logger")
            try:
                if audit is not None:
                    await audit.log_chat_membership(
                        user_id=change.from_user.id,
                        chat_id=chat.id,
                        chat_type=chat.type,
                        title=chat.title,
                        old_status=change.old_chat_member.status,
                        new_status=change.new_chat_member.status,
                    )
                else:
                    logger.info(
                        "Bot chat membership changed",
                        chat_id=chat.id,
                        chat_type=chat.type,
                        title=chat.title,
                        new_status=change.new_chat_member.status,
                    )
            except Exception:
                logger.warning("chat membership audit failed", exc_info=True)

        app.add_handler(
            ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
            group=-4,
        )

        reports_chat_id = self.settings.reports_chat_id
        if reports_chat_id:

            async def reports_chat_gate(
                update: Update, context: ContextTypes.DEFAULT_TYPE
            ) -> None:
                """Drop everything except text/voice/photo/document in a bound
                topic from an allowed sender."""
                router = self.deps.get("reports_router")
                message = update.effective_message
                user = update.effective_user
                if (
                    router is not None
                    and message is not None
                    and user is not None
                    and (
                        (message.text and not message.text.startswith("/"))
                        or getattr(message, "voice", None)
                        or getattr(message, "photo", None)
                        or getattr(message, "document", None)
                    )
                ):
                    slug = await router.profile_for_message(message)
                    role = None
                    auth = self.deps.get("auth_manager")
                    if auth is not None:
                        role = auth.get_user_role(user.id)
                    if role is None:
                        role = fallback_role(user.id, self.settings)
                    topic = await router.topic_name_for_thread(
                        message.chat_id,
                        message.message_thread_id if message.is_topic_message else None,
                    )
                    if topic is not None and topic in router.reply_only:
                        # Jobs: owner only, whatever the inbound policy says.
                        # A reply to a job message (text, voice, photo or
                        # document — the outer filter already limits the
                        # kinds) goes through; anything else gets a hint.
                        if role == "owner":
                            if slug is not None:
                                return
                            if router.hint_due(topic):
                                await message.reply_text(JOBS_HINT)
                    elif slug is not None and router.sender_allowed(role):
                        return  # continue to auth + the text handler
                logger.debug(
                    "Ignoring message in reports chat", chat_id=reports_chat_id
                )
                raise ApplicationHandlerStop

            app.add_handler(
                MessageHandler(
                    filters.Chat(chat_id=reports_chat_id), reports_chat_gate
                ),
                group=-4,
            )

    def _create_middleware_handler(self, middleware_func: Callable) -> Callable:
        """Create middleware handler that injects dependencies.

        When middleware rejects a request (returns without calling the handler),
        ApplicationHandlerStop is raised to prevent subsequent handler groups
        from processing the update.
        """
        from telegram.ext import ApplicationHandlerStop

        async def middleware_wrapper(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            # Ignore updates generated by bots (including this bot) to avoid
            # self-authentication loops and duplicate processing.
            if update.effective_user and getattr(
                update.effective_user, "is_bot", False
            ):
                logger.debug(
                    "Skipping bot-originated update in middleware",
                    user_id=update.effective_user.id,
                    middleware=middleware_func.__name__,
                )
                raise ApplicationHandlerStop

            # Inject dependencies into context
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings

            # Track whether the middleware allowed the request through
            handler_called = False

            async def dummy_handler(event: Any, data: Any) -> None:
                nonlocal handler_called
                handler_called = True

            # Middleware receives a per-update view: shared deps plus this
            # user's user_data (so auth can stamp the resolved role). Note:
            # top-level mutations of this mapping are not persisted back to
            # bot_data; mutate context.user_data (via "user_data") instead.
            middleware_data: Dict[str, Any] = dict(context.bot_data)
            middleware_data["user_data"] = (
                context.user_data if context.user_data is not None else {}
            )

            # Call middleware with Telegram-style parameters
            await middleware_func(dummy_handler, update, middleware_data)

            # If middleware didn't call the handler, it rejected the request.
            # Raise ApplicationHandlerStop to prevent subsequent handler groups
            # (including the main message handlers) from processing this update.
            if not handler_called:
                raise ApplicationHandlerStop()

        return middleware_wrapper

    async def start(self) -> None:
        """Start the bot."""
        if self.is_running:
            logger.warning("Bot is already running")
            return

        await self.initialize()

        logger.info(
            "Starting bot", mode="webhook" if self.settings.webhook_url else "polling"
        )

        try:
            self.is_running = True

            if self.settings.webhook_url:
                # Webhook mode
                await self.app.run_webhook(
                    listen="0.0.0.0",
                    port=self.settings.webhook_port,
                    url_path=self.settings.webhook_path,
                    webhook_url=self.settings.webhook_url,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )
            else:
                # Polling mode - initialize and start polling manually
                await self.app.initialize()
                await self.app.start()
                await self.app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                )

                # Keep running until manually stopped
                while self.is_running:
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error("Error running bot", error=str(e))
            raise ClaudeCodeTelegramError(f"Failed to start bot: {str(e)}") from e
        finally:
            self.is_running = False

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if not self.is_running:
            logger.warning("Bot is not running")
            return

        logger.info("Stopping bot")

        try:
            self.is_running = False  # Stop the main loop first

            # Shutdown feature registry
            if self.feature_registry:
                self.feature_registry.shutdown()

            if self.app:
                # Stop the updater if it's running
                if self.app.updater.running:
                    await self.app.updater.stop()

                # Stop the application
                await self.app.stop()
                await self.app.shutdown()

            logger.info("Bot stopped successfully")
        except Exception as e:
            logger.error("Error stopping bot", error=str(e))
            raise ClaudeCodeTelegramError(f"Failed to stop bot: {str(e)}") from e

    async def _error_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle errors globally."""
        error = context.error
        logger.error(
            "Global error handler triggered",
            error=str(error),
            update_type=type(update).__name__ if update else None,
            user_id=(
                update.effective_user.id if update and update.effective_user else None
            ),
        )

        # Determine error message for user
        from ..exceptions import (
            AuthenticationError,
            ConfigurationError,
            RateLimitExceeded,
            SecurityError,
        )

        error_messages = {
            AuthenticationError: "🔒 Authentication required. Please contact the administrator.",
            SecurityError: "🛡️ Security violation detected. This incident has been logged.",
            RateLimitExceeded: "⏱️ Rate limit exceeded. Please wait before sending more messages.",
            ConfigurationError: "⚙️ Configuration error. Please contact the administrator.",
            asyncio.TimeoutError: "⏰ Operation timed out. Please try again with a simpler request.",
        }

        error_type = type(error)
        user_message = error_messages.get(
            error_type, "❌ An unexpected error occurred. Please try again."
        )

        # Try to notify user
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(user_message)
            except Exception:
                logger.exception("Failed to send error message to user")

        # Log to audit system if available
        from ..security.audit import AuditLogger

        audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
        if audit_logger and update and update.effective_user:
            try:
                await audit_logger.log_security_violation(
                    user_id=update.effective_user.id,
                    violation_type="system_error",
                    details=f"Error type: {error_type.__name__}, Message: {str(error)}",
                    severity="medium",
                )
            except Exception:
                logger.exception("Failed to log error to audit system")

    async def get_bot_info(self) -> Dict[str, Any]:
        """Get bot information."""
        if not self.app:
            return {"status": "not_initialized"}

        try:
            me = await self.app.bot.get_me()
            return {
                "status": "running" if self.is_running else "initialized",
                "username": me.username,
                "first_name": me.first_name,
                "id": me.id,
                "can_join_groups": me.can_join_groups,
                "can_read_all_group_messages": me.can_read_all_group_messages,
                "supports_inline_queries": me.supports_inline_queries,
                "webhook_url": self.settings.webhook_url,
                "webhook_port": (
                    self.settings.webhook_port if self.settings.webhook_url else None
                ),
            }
        except Exception as e:
            logger.error("Failed to get bot info", error=str(e))
            return {"status": "error", "error": str(e)}

    async def health_check(self) -> bool:
        """Perform health check."""
        try:
            if not self.app:
                return False

            # Try to get bot info
            await self.app.bot.get_me()
            return True
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return False
