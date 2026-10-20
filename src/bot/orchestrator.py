"""Message orchestrator — single entry point for all Telegram updates.

Registers the conversational (agentic) interface: commands plus text, file,
photo and voice handlers. The pre-agentic "classic" terminal-in-Telegram
interface was removed — see docs/DECISIONS.md 2026-08-11.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional

import structlog
from telegram import (
    BotCommand,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..profiles.registry import ensure_workspace_for
from .orchestration.callbacks import CallbacksMixin
from .orchestration.commands.admin import AdminCommandsMixin
from .orchestration.commands.info import InfoCommandsMixin
from .orchestration.routing import _PROFILE_BADGES
from .orchestration.streaming import StreamingMixin
from .orchestration.turns import TurnsMixin
from .utils.html_format import escape_html

logger = structlog.get_logger()


# Commands only the owner role may run (guests get a short refusal). Kept in
# code, not env, by design — see docs/plans/2026-07-19-multiuser-guest-mvp.md §2.
OWNER_ONLY_COMMANDS = frozenset(
    {
        "stop",
        "resume",
        "restart",
        "auth",
        "repo",
        "model",
        "servercheck",
        "diag",
        "identity",
        "limits",
        "adduser",
        "rmuser",
        "setkey",
        "keys",
        "delkey",
        "voice",
        "setup",
    }
)


@dataclass(frozen=True)
class CommandSpec:
    """One bot command, described once for all three consumers.

    Registration, the Telegram menu and ``/help`` all read this table — see
    CLAUDE.md "Adding a New Bot Command". The point of a single table is that
    most commands are deliberately *not* in the menu (dv-a1c8): a command that
    fell out of ``/help`` would become invisible rather than merely unlisted.
    """

    name: str
    description: str
    #: ``/help`` section this appears under; keys of ``_HELP_SECTIONS``.
    group: str
    #: Roles whose Telegram menu shows it. Empty = registered but unlisted.
    #: TABLE ORDER IS MENU ORDER — setMyCommands preserves send order.
    menu_roles: FrozenSet[str] = field(default_factory=frozenset)
    #: ``"profiles"``: only exists when configured.
    requires: Optional[str] = None


_OWNER = frozenset({"owner"})
_EVERYONE = frozenset({"owner", "guest"})

# The order of this table is the order of the Telegram menu (D5, 2026-08-11):
# core + safety first, then the owner's four at the bottom.
COMMAND_TABLE: tuple[CommandSpec, ...] = (
    CommandSpec("start", "Start the bot", "core", _EVERYONE),
    CommandSpec("new", "Start a fresh session", "core", _EVERYONE),
    CommandSpec(
        "agents", "Pick active agent (keyboard)", "core", _EVERYONE, requires="profiles"
    ),
    CommandSpec(
        "stop", "Stop this chat's agent (/stop all = killswitch)", "safety", _OWNER
    ),
    CommandSpec(
        "resume", "Clear killswitch: resume runs + scheduler", "safety", _OWNER
    ),
    CommandSpec("restart", "Restart the bot", "safety", _OWNER),
    CommandSpec("auth", "Show / add backend logins", "safety", _OWNER),
    CommandSpec("help", "List every command you can run", "core", _EVERYONE),
    CommandSpec("status", "Show session status", "session", _OWNER),
    CommandSpec("usage", "Show cost spent and budget caps", "session", _EVERYONE),
    CommandSpec("limits", "Show OpenAI/Codex account limits", "session", _OWNER),
    CommandSpec("model", "Choose model backend / log in", "session", _OWNER),
    CommandSpec("tasks", "Show the shared task board", "session", _OWNER),
    # --- Unlisted from here down: registered and working, surfaced by /help.
    CommandSpec("family", "Manage your family members", "people", frozenset({"guest"})),
    CommandSpec("verbose", "Set output verbosity (0/1/2)", "session"),
    CommandSpec("memory", "Show loaded memory and context size", "session"),
    CommandSpec("repo", "List repos / switch workspace", "session"),
    CommandSpec("schedules", "List scheduled agent jobs", "session"),
    CommandSpec("reminders", "Your reminders & todos: list, cancel", "session"),
    CommandSpec("voice", "Voice input: pick speech-to-text or text only", "session"),
    CommandSpec("setup", "First-run setup interview (re-run any time)", "session"),
    CommandSpec(
        "sessions", "Show each profile's last activity", "session", requires="profiles"
    ),
    CommandSpec("servercheck", "Run server health check now", "diagnostics"),
    CommandSpec(
        "diag",
        "Show profile lifecycle diagnostics",
        "diagnostics",
        requires="profiles",
    ),
    CommandSpec(
        "identity",
        "Show a profile's identity prompt",
        "diagnostics",
        requires="profiles",
    ),
    CommandSpec("setkey", "Store a tool API key (message auto-deleted)", "safety"),
    CommandSpec("keys", "List stored tool key names", "safety"),
    CommandSpec("delkey", "Delete a stored tool key", "safety"),
    CommandSpec("adduser", "Grant a Telegram user guest access", "people"),
    CommandSpec("rmuser", "Revoke a user's access", "people"),
)

_HELP_SECTIONS: tuple[tuple[str, str], ...] = (
    ("core", "Basics"),
    ("session", "Session &amp; workspace"),
    ("safety", "Safety — these work when the agent path does not"),
    ("diagnostics", "Diagnostics"),
    ("people", "People &amp; access"),
)


class MessageOrchestrator(
    AdminCommandsMixin, InfoCommandsMixin, StreamingMixin, CallbacksMixin, TurnsMixin
):
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def register_handlers(self, app: Application) -> None:
        """Register the agentic handler set."""
        self._register_agentic_handlers(app)

    def _command_handlers(self) -> Dict[str, Callable]:  # type: ignore[type-arg]
        """Map every COMMAND_TABLE name to its handler.

        Kept next to the table, not merged into it, so specs stay a plain
        frozen dataclass. ``test_orchestrator`` asserts the two agree exactly —
        a spec without a handler (or the reverse) is a test failure, not a
        runtime KeyError.
        """
        from .features import server_monitor
        from .orchestration.commands import auth, keys, reminders, setup, system, voice

        return {
            "start": self.agentic_start,
            "new": self.agentic_new,
            "agents": self.agentic_agents,
            "stop": self.agentic_stop,
            "resume": self.agentic_resume,
            "restart": system.restart_command,
            "auth": auth.auth_command,
            "reminders": reminders.reminders_command,
            "help": self.agentic_help,
            "status": self.agentic_status,
            "usage": self.agentic_usage,
            "limits": self.agentic_limits,
            "model": self.agentic_model,
            "tasks": self.agentic_tasks,
            "family": self.agentic_family,
            "verbose": self.agentic_verbose,
            "memory": self.agentic_memory,
            "repo": self.agentic_repo,
            "schedules": self.agentic_schedules,
            "sessions": self.agentic_sessions,
            "servercheck": server_monitor.servercheck_command,
            "diag": self.agentic_diag,
            "identity": self.agentic_identity,
            "adduser": self.agentic_adduser,
            "rmuser": self.agentic_rmuser,
            "setkey": keys.setkey_command,
            "keys": keys.keys_command,
            "delkey": keys.delkey_command,
            "voice": voice.voice_command,
            "setup": setup.setup_command,
        }

    def _available_commands(self) -> List[CommandSpec]:
        """COMMAND_TABLE filtered by what this deployment actually has."""
        have_profiles = bool(self.deps.get("profile_registry"))
        available = []
        for spec in COMMAND_TABLE:
            if spec.requires == "profiles" and not have_profiles:
                continue
            available.append(spec)
        return available

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register handlers: commands + text/file/photo/voice."""
        handlers = self._command_handlers()

        # /stop, /resume and /restart are in this loop, unconditionally and
        # ahead of anything profile-dependent, on purpose: they are the
        # commands that must still work when the agent path is broken, so they
        # never depend on a registry being loaded and never route through
        # agentic_text. /auth (dv-6a17) inherits the same requirement.
        for spec in self._available_commands():
            handler = handlers[spec.name]
            if spec.name in OWNER_ONLY_COMMANDS:
                handler = self._owner_only(handler, spec.name)
            app.add_handler(CommandHandler(spec.name, self._inject_deps(handler)))

        # Dynamic per-profile switch commands (/devops, /channel, ...) — from
        # the live registry, never the table. /help reads the same source.
        registry = self.deps.get("profile_registry")
        if registry:
            for profile in registry.enabled_profiles:
                app.add_handler(
                    CommandHandler(
                        profile.slug,
                        self._inject_deps(self._make_switch_handler(profile.slug)),
                    )
                )

        # Text messages -> Claude
        # Edits excluded (dv-b7e2): handlers read update.message directly,
        # which is None for edited_message updates -> AttributeError.
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL & ~filters.UpdateType.EDITED_MESSAGE,
                self._inject_deps(self.agentic_document),
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.PHOTO & ~filters.UpdateType.EDITED_MESSAGE,
                self._inject_deps(self.agentic_photo),
            ),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(
                filters.VOICE & ~filters.UpdateType.EDITED_MESSAGE,
                self._inject_deps(self.agentic_voice),
            ),
            group=10,
        )

        # Selection callbacks, scoped by pattern. EVERY prefix dispatched on
        # inside _agentic_callback must appear here or the branch is dead code
        # that silently does nothing — test_callback_pattern_covers_every_prefix
        # is what keeps the two in sync.
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^(cd:|model:|agent:|agents:|auth:|rem:|vset:|onb:|qi:)",
            )
        )

        # Destructive-op confirmation gate (approve/deny) buttons.
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._confirmation_callback),
                pattern=r"^confirm:",
            )
        )

        # Session watchdog (hold/dismiss) buttons.
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._watchdog_callback),
                pattern=r"^watchdog:",
            )
        )

        logger.info("Agentic handlers registered")

    async def get_bot_commands(  # type: ignore[type-arg]
        self, role: str = "owner"
    ) -> list:
        """Return the Telegram command menu for *role*, in table order.

        Most registered commands are deliberately absent (dv-a1c8): the menu
        is the short list, ``/help`` is the complete one. The visible menu is
        cosmetic either way — runtime gating via ``_owner_only`` is
        authoritative.
        """
        return [
            BotCommand(spec.name, spec.description)
            for spec in self._available_commands()
            if role in spec.menu_roles
            and not (role != "owner" and spec.name in OWNER_ONLY_COMMANDS)
        ]

    async def agentic_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List every command the caller can actually run.

        This is the contract that makes the short menu safe: eleven working
        commands are unlisted, and this is the one place that names them. It
        is DERIVED — from COMMAND_TABLE for commands and from the live profile
        registry for the switches — so a command added later cannot silently
        go missing from it. Role filtering uses OWNER_ONLY_COMMANDS and
        ``profile.allows_role``, never a hand-written list.
        """
        user = update.effective_user
        role = self._resolve_role(user.id if user else None, context)
        is_owner = role == "owner"
        menu = {
            spec.name for spec in self._available_commands() if role in spec.menu_roles
        }

        lines: List[str] = ["<b>Commands</b>"]
        for group, title in _HELP_SECTIONS:
            entries = [
                spec
                for spec in self._available_commands()
                if spec.group == group
                and (is_owner or spec.name not in OWNER_ONLY_COMMANDS)
            ]
            if not entries:
                continue
            lines.append("")
            lines.append(f"<b>{title}</b>")
            for spec in entries:
                mark = "" if spec.name in menu else " ·"
                lines.append(f"/{spec.name}{mark} — {escape_html(spec.description)}")

        registry = context.bot_data.get("profile_registry") or self.deps.get(
            "profile_registry"
        )
        if registry:
            switches = [p for p in registry.enabled_profiles if p.allows_role(role)]
            if switches:
                active = context.user_data.get("active_profile")
                lines.append("")
                lines.append("<b>Agents</b> — switch, or prefix a message with @slug")
                for profile in switches:
                    marker = " ←" if profile.slug == active else ""
                    lines.append(
                        f"/{escape_html(profile.slug)} — "
                        f"{escape_html(profile.name)}{marker}"
                    )

        lines.append("")
        lines.append("<i>· = not in the Telegram menu, but works if you type it.</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    def _make_switch_handler(self, slug: str) -> Callable:  # type: ignore[type-arg]
        """Build a /<slug> command handler that switches the active profile."""

        async def _switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            registry = context.bot_data.get("profile_registry")
            profile = registry.get_enabled_by_slug(slug) if registry else None
            if not profile:
                await update.message.reply_text(f"Unknown profile: {slug}")
                return
            user = update.effective_user
            user_id = user.id if user else None
            role = self._resolve_role(user_id, context)
            if not profile.allows_role(role):
                if update.message:
                    await update.message.reply_text(
                        f"🚫 The '{slug}' profile is owner-only."
                    )
                storage = context.bot_data.get("storage")
                if storage and user_id is not None:
                    await storage.log_bot_event(
                        user_id,
                        "profile_access_denied",
                        {"profile": slug, "role": role, "via": "switch"},
                        success=False,
                    )
                return
            # Inline text after the command (e.g. "/channel read comments").
            text = (update.message.text or "").partition(" ")[2].strip()
            working_dir = ensure_workspace_for(profile, role, user_id)
            context.user_data["active_profile"] = slug
            context.user_data["current_directory"] = working_dir
            if update.effective_user:
                await self._save_user_prefs(
                    context, update.effective_user.id, active_profile=slug
                )
            badge = _PROFILE_BADGES.get(slug, slug)
            if not text:
                sent = await update.message.reply_text(
                    f"{badge} — switched. You're now talking to {profile.name}."
                )
                if user_id is not None:
                    await self._pin_switch_message(context, user_id, sent)
                return
            # Re-dispatch the inline text through the normal text path.
            await self.agentic_text(update, context, override_text=text)

        return _switch
