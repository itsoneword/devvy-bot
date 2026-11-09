"""Main entry point for Claude Code Telegram Bot."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

import structlog

from src import __version__
from src.agent_backends import AgentBackend, CodexCliIntegration, CodexSessionStore
from src.claude import (
    ClaudeIntegration,
    SessionManager,
)
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.features import FeatureFlags
from src.config.settings import Settings
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.middleware import EventSecurityMiddleware
from src.exceptions import ConfigurationError
from src.notifications.service import NotificationService
from src.notifications.transport import LocalTransport, TelegramTransport, Transport
from src.scheduler.scheduler import JobScheduler
from src.security.audit import AuditLogger, InMemoryAuditStorage
from src.security.auth import (
    AuthenticationManager,
    InMemoryTokenStorage,
    TokenAuthProvider,
    WhitelistAuthProvider,
)
from src.security.rate_limiter import RateLimiter
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.hold_repository import HoldStore
from src.storage.model_calls import ModelCallRepository
from src.storage.session_storage import SQLiteSessionStorage
from src.utils.log_redaction import install_secret_redaction
from src.utils.paths import DataLayout
from src.utils.turn_locks import report_dropped_messages

if TYPE_CHECKING:
    # The Telegram side is imported only when channel=telegram: a local
    # install (dv-fe4d) never loads the telegram package.
    from src.bot.core import ClaudeCodeBot
    from src.notifications.routing import ReportsRouter


def setup_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    level = logging.DEBUG if debug else logging.INFO

    # Configure standard logging
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    # httpx logs the full request URL at INFO, which embeds the Telegram bot
    # token on every API call. Redact secrets from all handlers as a safety net.
    install_secret_redaction()

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            (
                structlog.processors.JSONRenderer()
                if not debug
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Claude Code Telegram Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version", action="version", version=f"Claude Code Telegram Bot {__version__}"
    )

    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    parser.add_argument("--config-file", type=Path, help="Path to configuration file")

    return parser.parse_args()


async def create_application(config: Settings) -> Dict[str, Any]:
    """Create and configure the application components."""
    logger = structlog.get_logger()
    logger.info("Creating application components")

    features = FeatureFlags(config)

    # Bootstrap soul/memory files from templates if feature enabled
    if features.soul_memory_enabled:
        from src.boot.memory import bootstrap_memory

        templates_dir = Path(__file__).parent.parent / "data" / "templates"
        await bootstrap_memory(
            memory_path=config.memory_path,
            soul_md_path=config.soul_md_path,
            templates_dir=templates_dir,
        )

        # Generate project memory stubs
        if config.projects_config_path:
            from src.boot.memory import bootstrap_project_stubs

            await bootstrap_project_stubs(
                memory_path=config.memory_path,
                projects_config_path=config.projects_config_path,
            )

    # Initialize storage system
    storage = Storage(
        config.database_url,
        tasks_dir=config.deviz_tasks_dir,
        user_database_url=config.user_database_url,
        trace_retention_days=config.trace_retention_days,
    )
    await storage.initialize()

    # DB-backed user store (users & families v1). GUEST_USERS env ids are
    # seeded as (guest, source=env) — idempotent, never downgrades an
    # admin-added row. The store keeps a sync in-memory role snapshot for the
    # auth hot path.
    from src.storage.user_repository import UserStore

    user_store = UserStore(storage.bot_users)
    # ALLOWED_USERS ids become owner rows (dv-5c83). The installer writes the
    # captured owner into .env before first boot; without this the row only
    # ever existed in env, so nothing survived an ALLOWED_USERS edit and
    # /adduser had no first owner to attribute rows to.
    await user_store.seed_env_owners(config.allowed_users or [])
    await user_store.seed_env_guests(config.guest_users or [])
    await user_store.refresh()

    # Create security components
    providers = []

    # Add whitelist provider if users are configured. allowed_users = owners,
    # guest_users + DB-managed users = restricted guest role; union is the
    # effective whitelist.
    if (
        config.allowed_users
        or config.guest_users
        or user_store.guest_ids()
        or user_store.owner_ids()
    ):
        providers.append(
            WhitelistAuthProvider(
                config.allowed_users or [],
                guest_users=config.guest_users,
                user_store=user_store,
            )
        )

    # Add token provider if enabled
    if config.enable_token_auth:
        token_storage = InMemoryTokenStorage()  # TODO: Use database storage
        providers.append(TokenAuthProvider(config.auth_token_secret, token_storage))

    # Last resort: accept everyone. This used to trigger on DEVELOPMENT_MODE
    # alone, and .env.example shipped DEVELOPMENT_MODE=true — so the documented
    # first run produced a bot open to anyone who found it (dv-5c83). It now
    # needs a flag whose name cannot be set by accident.
    if not providers and config.dangerously_allow_all_users:
        logger.warning(
            "DANGEROUSLY_ALLOW_ALL_USERS is set: EVERY Telegram user is an"
            " owner of this bot. Never run this on a bot anyone else can find."
        )
        providers.append(WhitelistAuthProvider([], allow_all_dev=True))
    elif not providers:
        raise ConfigurationError(
            "No authentication providers configured: set ALLOWED_USERS to your"
            " Telegram user id (install.sh captures it for you), or enable"
            " token auth. DEVELOPMENT_MODE no longer opens the bot to everyone."
        )

    auth_manager = AuthenticationManager(providers)
    security_validator = SecurityValidator(
        config.approved_directory,
        disable_security_patterns=config.disable_security_patterns,
    )
    rate_limiter = RateLimiter(config)
    # Restore today's spend into the budget tracker — it lives in memory and
    # a restart would otherwise silently restart the budget window at $0.
    try:
        rate_limiter.seed_costs(await storage.costs.get_costs_for_date())
    except Exception as e:
        logger.warning("Could not seed cost tracker from storage", error=str(e))

    # Session watchdog holds (dv-8bb5). Loaded now (needs only storage) so a
    # hold set before a restart survives it; the watchdog alert itself is
    # wired later, once the Telegram bot instance exists.
    hold_store = HoldStore(storage.holds)
    try:
        await hold_store.load()
    except Exception as e:
        logger.warning("Could not load session holds from storage", error=str(e))

    # Create audit storage and logger
    audit_storage = InMemoryAuditStorage()  # TODO: Use database storage in production
    audit_logger = AuditLogger(audit_storage)

    # Create Claude integration components with persistent storage
    session_storage = SQLiteSessionStorage(storage.db_manager)
    session_manager = SessionManager(config, session_storage)

    # Shared write lock for memory file operations (soul/memory feature)
    memory_write_lock = asyncio.Lock() if features.soul_memory_enabled else None

    # Activity logger (soul/memory feature)
    activity_logger = None
    if features.soul_memory_enabled:
        from src.activity import ActivityLogger

        activity_logger = ActivityLogger(
            memory_path=config.memory_path,
            max_lines=config.activity_log_max_lines,
            write_lock=memory_write_lock,
        )

    # Prompt composer (soul/memory feature)
    prompt_composer = None
    if features.soul_memory_enabled:
        from src.claude.prompt_composer import PromptComposer

        agent_md_path = config.soul_md_path.parent / "agent.md"
        prompt_composer = PromptComposer(
            memory_path=config.memory_path,
            agent_md_path=agent_md_path,
        )

    # Phase 5: Token tracking + evaluator services.
    # DB-backed agent memory is always on (this replaces the file-memory
    # feature, so it no longer rides the file-oriented soul_memory flag).
    refresh_service = None

    from src.evaluator.decision_refs import DecisionRefChecker
    from src.evaluator.jev_gate import JevGateConfig
    from src.evaluator.screen import ScreenConfig
    from src.evaluator.service import EvaluatorService
    from src.evaluator.writer import MemoryWriter
    from src.token.tracker import TokenTracker
    from src.utils.boards import agent_repos

    token_tracker = TokenTracker(db=storage.db_manager)
    # dv-32fe: agents with their own repo (boards.json profiles + devops)
    # store per-task decisions as pointers, checked against that repo.
    decision_refs = DecisionRefChecker(agent_repos())
    from src.storage.window_bundle_repository import WindowBundleRepository

    # dv-ef59: every writer marks the window bundle stale on a pinned/core
    # change, so the rule binds from the next turn, not the next roll.
    memory_writer = MemoryWriter(
        storage.memory_repository,
        decision_refs=decision_refs,
        bundle_repo=WindowBundleRepository(storage.db_manager),
    )
    evaluator_service = EvaluatorService(
        memory_repo=storage.memory_repository,
        writer=memory_writer,
        token_tracker=token_tracker,
        db=storage.db_manager,
        max_delta_chars=config.evaluator_max_delta_chars,
        timeout_seconds=config.evaluator_timeout_seconds,
        episodic_guard_enabled=config.evaluator_episodic_guard,
        jev_gate=(
            JevGateConfig(
                min_conf_new=config.reconcile_jev_min_conf_new,
                min_conf_duplicate=config.reconcile_jev_min_conf_duplicate,
                max_state_change=config.reconcile_jev_max_state_change,
                top_k=config.reconcile_jev_top_k,
            )
            if config.reconcile_jev_gate_enabled
            else None
        ),
        screen=ScreenConfig(
            mode=config.memory_screen_mode,
            chunk_tokens=config.memory_screen_chunk_tokens,
            fact_send_min=config.memory_screen_fact_send_min,
            fact_send_max=config.memory_screen_fact_send_max,
        ),
        repo_agents=decision_refs.agents,
    )

    # DB-backed agent memory retriever (always on; replaces file memory).
    # Selects pinned + FTS-matched general facts per agent_id each turn.
    from src.storage.memory_repository import MemoryRetriever

    storage.memory_repository.bundle_caps = (
        config.memory_snapshot_general_cap,
        config.memory_pointer_cap,
    )
    memory_retriever = MemoryRetriever(
        storage.memory_repository,
        general_cap=config.memory_snapshot_general_cap,
        pointer_cap=config.memory_pointer_cap,
    )

    # Profile registry (multi-persona routing). Unset = single-profile mode.
    # Safe mode (dv-86f4): after a crash loop deploy/crashwatch.sh sets
    # <data root>/lkg/USE and this boot loads profiles/models from the
    # last-known-good snapshot instead of the live files.
    from src.boot import safemode

    profiles_path, models_path = safemode.config_paths(
        config.data_layout, config.profiles_config_path, config.model_routes_path
    )
    safe_mode_alert: Optional[str] = None
    profile_registry = None
    if profiles_path:
        from src.boot.profiles import bootstrap_profiles
        from src.profiles import load_profile_registry, validate_registry_paths

        profile_registry = load_profile_registry(
            profiles_path,
            sandbox_default=config.sandbox_enabled,
            data_root=config.devvy_data_root,
        )
        # First run materialises each profile's working_dir, memory_dir and
        # persona from templates; re-runs never overwrite. Must precede
        # validation, which would otherwise abort on the files being created.
        await bootstrap_profiles(
            profile_registry,
            Path(__file__).parent.parent / "data" / "templates",
        )
        # A missing persona or unwritable working_dir must fail here, naming
        # the profile, rather than at the first turn that uses it. Only the
        # default profile is fatal (dv-86f4): any other bad profile is off for
        # this run and the owner is told once the bot is up (run_application).
        for slug, reason in validate_registry_paths(
            profile_registry, degrade=True
        ).items():
            logger.warning("Profile disabled for this run", profile=slug, reason=reason)
        if safemode.is_active(config.data_layout):
            # Only the default profile, owner-only: the snapshot may carry
            # grants revoked since. After validation, so /agents shows "safe
            # mode" as the reason for every agent it turns off.
            safe_mode_alert = safemode.apply(
                profile_registry,
                config.data_layout,
                config.profiles_config_path,
                models_from_snapshot=models_path != config.model_routes_path,
                sandbox_default=config.sandbox_enabled,
                data_root=config.devvy_data_root,
            )
        logger.info("Profile registry loaded", count=len(profile_registry.profiles))

    # Lifecycle observability — records resume/fresh/memory events per profile.
    from src.observability import LifecycleLogger

    lifecycle_logger = LifecycleLogger(storage.db_manager)
    await lifecycle_logger.ensure_schema()

    # Bot-owned conversation windows (BPC): per user+working_dir token
    # accounting + evaluator/compaction checkpoints, backend-agnostic.
    # Created before the backends because the window bundle provider needs it.
    from src.storage.window_repository import WindowRepository

    window_repo = WindowRepository(
        storage.db_manager,
        eval_interval_tokens=config.window_eval_interval_tokens,
        refresh_threshold_tokens=config.window_refresh_threshold_tokens,
    )

    # Frozen per-window context bundle (dv-5e2a): one system prompt per
    # conversation window, materialized on the window's first turn and reused
    # byte-identical until the next roll or /new. Injected into both backends
    # so a codex turn cannot un-freeze the prompt.
    from src.claude.window_bundle import WindowBundleProvider

    bundle_provider = WindowBundleProvider(
        window_repo=window_repo,
        bundle_repo=WindowBundleRepository(storage.db_manager),
        memory_repo=storage.memory_repository,
        profile_registry=profile_registry,
        default_composer=prompt_composer,
        facts_budget_chars=config.bundle_facts_budget_chars,
        general_cap=config.memory_snapshot_general_cap,
        pointer_cap=config.memory_pointer_cap,
        sandbox_default=config.sandbox_enabled,
        sweep_wait_seconds=config.memory_sweep_wait_seconds,
    )

    # Create Claude SDK manager and integration facade
    logger.info("Using Claude Python SDK integration")
    sdk_manager = ClaudeSDKManager(
        config,
        security_validator=security_validator,
        prompt_composer=prompt_composer,
        profile_registry=profile_registry,
        memory_retriever=memory_retriever,
        bundle_provider=bundle_provider,
    )

    # Per-turn today-state digest (dv-df45). Only profiles flagged
    # `today_digest: true` in profiles.yaml ever receive it.
    digest_provider = None
    if storage.user_data is not None:
        from src.daystate.db_source import DbTodayStateSource

        digest_provider = DbTodayStateSource(storage.user_data)

    claude_integration = ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
        session_manager=session_manager,
        activity_logger=activity_logger,
        token_tracker=token_tracker,
        evaluator_service=evaluator_service,
        profile_registry=profile_registry,
        lifecycle_logger=lifecycle_logger,
        digest_provider=digest_provider,
    )

    # Populate account entitlements before synchronous model-policy reads can
    # validate (and potentially repair) a persisted preference.  Without this
    # boot refresh, the cold fallback catalog used to rewrite a saved Sol pick
    # to the obsolete mini default before the first Codex turn.
    from src.agent_backends.codex_catalog import CODEX_CATALOG

    await CODEX_CATALOG.get(force=True)

    # dv-ebde: Codex thread bookkeeping moved from data/codex_sessions.json
    # onto the `sessions` row. One-shot, idempotent, read-only on the file
    # (kept as the rollback copy for one release).
    from src.storage.codex_sessions import import_codex_sessions_json

    try:
        await import_codex_sessions_json(
            storage.db_manager, config.codex_session_store_path
        )
    except Exception as e:  # a failed import must not stop the bot
        logger.warning("Codex session JSON import failed", error=str(e))

    codex_integration = CodexCliIntegration(
        config=config,
        session_store=CodexSessionStore(storage.db_manager),
        prompt_composer=prompt_composer,
        profile_registry=profile_registry,
        memory_retriever=memory_retriever,
        bundle_provider=bundle_provider,
        # Same session manager as the Claude path: a Codex thread has to exist
        # in `sessions` before its `messages` row is written (dv-0c01).
        session_manager=session_manager,
    )

    # Wire refresh service (needs claude_integration for conversation helper)
    if features.soul_memory_enabled and evaluator_service:
        from src.evaluator.refresh import ContextRefreshService

        refresh_service = ContextRefreshService(
            evaluator_service=evaluator_service,
            session_manager=session_manager,
            get_conversation=lambda session: claude_integration._get_conversation_for_eval(
                session
            ),
        )
        claude_integration.refresh_service = refresh_service

    # --- Event bus and agentic platform components ---
    event_bus = EventBus()

    # Event security middleware
    event_security = EventSecurityMiddleware(
        event_bus=event_bus,
        security_validator=security_validator,
        auth_manager=auth_manager,
    )
    event_security.register()

    # One turn at a time per user, across ALL entry points (Telegram messages
    # in the orchestrator + event-driven turns here).
    from src.utils.turn_locks import TurnLocks

    turn_locks = TurnLocks()

    # Agent handler — translates events into Claude executions
    agent_handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=claude_integration,
        default_working_directory=config.approved_directory or Path.home(),
        default_user_id=config.allowed_users[0] if config.allowed_users else 0,
        profile_registry=profile_registry,
        health_export_profile_slug=config.health_export_profile_slug,
        health_export_user_id=config.health_export_user_id,
        turn_locks=turn_locks,
        user_store=user_store,
        storage=storage,
        window_repo=window_repo,
        default_agent_model=config.agent_model,
    )
    agent_handler.register()

    # Backend registry (dv-5368): user_data["agent_backend"] is the key the
    # orchestrator looks up; each value satisfies AgentBackend
    # (src/agent_backends/protocol.py). A third backend registers its
    # instance here and its BackendPolicy in protocol.BACKEND_POLICIES.
    backends: Dict[str, AgentBackend] = {
        claude_integration.backend_id: claude_integration,
        codex_integration.backend_id: codex_integration,
    }

    # Model router (dv-11a5): every model call goes through it and leaves a
    # `model_calls` row. It owns the registry above; plain calls made outside
    # a handler (consolidation, fact-quality) reach it via get_router().
    from src.model_router import HarnessVersions, ModelRouter, set_router
    from src.model_router.claude_plain import ClaudePlainCaller
    from src.model_router.codex_plain import CodexPlainCaller
    from src.model_router.openrouter_plain import OpenRouterPlainCaller
    from src.model_router.routes import boot_call_routes, legacy_route_models
    from src.model_router.types import CLAUDE_HARNESS, CODEX_HARNESS, OPENROUTER_HARNESS
    from src.model_router.typesafe_decide import (
        TypesafeDecider,
        configure_typesafe_key,
    )

    def _profile_slug_for(working_dir: Path) -> Optional[str]:
        if profile_registry is None:
            return None
        profile = profile_registry.get_by_working_dir(working_dir)
        return profile.slug if profile is not None else None

    # Plain-call routes (dv-781f): built-in rows < the deprecated scalar
    # settings < models.yaml. A bad file never stops the boot; the owner is
    # told once the bot can send (run_application).
    call_routes, model_routes_alert = boot_call_routes(
        models_path, legacy=legacy_route_models(config)
    )
    model_router = ModelRouter(
        backends=backends,
        call_routes=call_routes,
        plain_callers={
            CLAUDE_HARNESS: ClaudePlainCaller(cli_path=config.claude_cli_path or None),
            CODEX_HARNESS: CodexPlainCaller(codex_bin=codex_integration.codex_bin),
            OPENROUTER_HARNESS: OpenRouterPlainCaller(),
        },
        recorder=ModelCallRepository(storage.db_manager),
        tool_calls=storage.tool_calls,
        versions=HarnessVersions(
            claude_path=config.claude_cli_path or None,
            codex_bin=codex_integration.codex_bin,
        ),
        profile_resolver=_profile_slug_for,
        decider=TypesafeDecider(),
    )
    set_router(model_router)
    # dv-e690: the key lives in Settings (scrubbed from os.environ), so hand
    # it to the Jev adapter once; the env var stays an override.
    configure_typesafe_key(
        config.typesafe_api_key.get_secret_value() if config.typesafe_api_key else None
    )

    # Create bot with all dependencies
    dependencies = {
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "rate_limiter": rate_limiter,
        "audit_logger": audit_logger,
        "backends": backends,
        "model_router": model_router,
        # Pre-registry keys, kept for the dv-5368 transition; orchestration
        # reads bot_data["backends"] and only falls back to these.
        "claude_integration": claude_integration,
        "codex_integration": codex_integration,
        "storage": storage,
        "event_bus": event_bus,
        "prompt_composer": prompt_composer,
        "token_tracker": token_tracker,
        "profile_registry": profile_registry,
        "lifecycle_logger": lifecycle_logger,
        "window_repo": window_repo,
        "evaluator_service": evaluator_service,
        "user_store": user_store,
        "turn_locks": turn_locks,
        "hold_store": hold_store,
    }

    # The channel (dv-fe4d). Telegram: the bot, whose Telegram Bot instance
    # exists only after bot.initialize(), so the notification service and the
    # owner prompts are wired in run_application(). Local: no bot at all; the
    # same services deliver into the local inbox.
    bot: Optional["ClaudeCodeBot"] = None
    if config.channel == "telegram":
        from src.bot.core import ClaudeCodeBot
        from src.bot.features.self_modify import SelfModificationGuard

        # Self-modification guard for native service mode
        dependencies["self_modification_guard"] = SelfModificationGuard(
            bot_root=Path(__file__).resolve().parent.parent,
        )
        bot = ClaudeCodeBot(config, dependencies)

    logger.info("Application components created successfully")

    return {
        "bot": bot,
        # bot.deps on a Telegram install; the same dict when there is no bot.
        "deps": dependencies,
        "backends": backends,
        "claude_integration": claude_integration,
        "storage": storage,
        "config": config,
        "features": features,
        "event_bus": event_bus,
        "agent_handler": agent_handler,
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "evaluator_service": evaluator_service,
        "refresh_service": refresh_service,
        "bundle_provider": bundle_provider,
        "model_routes_alert": model_routes_alert,
        "safe_mode_alert": safe_mode_alert,
        # Separate writer instance for the in-session `remember` tool: same
        # repo and the same duplicate suppression, but stamped
        # source="agent_tool" and counted separately from the batch evaluator.
        "agent_memory_writer": MemoryWriter(
            storage.memory_repository,
            source="agent_tool",
            decision_refs=decision_refs,
            bundle_repo=WindowBundleRepository(storage.db_manager),
        ),
        "decision_refs": decision_refs,
        # And a third for the audit tools (dv-a9e1): forget/correct close rows
        # on the user's explicit ask, so invalidated_by must say so.
        "audit_memory_writer": MemoryWriter(
            storage.memory_repository,
            source="user_request",
            bundle_repo=WindowBundleRepository(storage.db_manager),
        ),
    }


def crashwatch_recovery(layout: DataLayout) -> Optional[str]:
    """The «снова работает» message after a crash-loop alert, or None (dv-86f4).

    ``deploy/crashwatch.sh`` (ExecStopPost) counts crashes in
    ``<data root>/crashwatch/fails`` and stamps ``alerted`` when it told the
    owner. A boot that gets this far after such an alert clears both, so the
    next loop alerts afresh, and says how many crashes it took.
    """
    base = layout.root if layout.root is not None else layout.legacy_dir
    state = Path(base) / "crashwatch"
    alerted = state / "alerted"
    try:
        stamp = alerted.read_text(encoding="utf-8").split()
    except OSError:
        return None
    crashes = stamp[1] if len(stamp) > 1 and stamp[1].isdigit() else "?"
    for name in ("alerted", "fails"):
        try:
            (state / name).unlink()
        except OSError:
            pass
    return f"✅ Бот снова работает после {crashes} падений подряд."


# How long a boot must stay up before it counts as the end of a crash loop.
CRASHWATCH_GRACE_S = 60.0


async def crashwatch_recovery_after(
    layout: DataLayout,
    notify: Callable[[str], Awaitable[None]],
    grace: float = CRASHWATCH_GRACE_S,
) -> None:
    """After ``grace`` s up, send the «снова работает» message if there is one.

    The grace keeps a loop that dies late in startup from getting an alert /
    «снова работает» pair per few restarts.
    """
    await asyncio.sleep(grace)
    recovered = crashwatch_recovery(layout)
    if recovered:
        structlog.get_logger().info("Crash loop over", message=recovered)
        await notify(recovered)


async def run_application(app: Dict[str, Any]) -> None:
    """Run the application with graceful shutdown handling."""
    logger = structlog.get_logger()
    bot: Optional["ClaudeCodeBot"] = app["bot"]
    deps: Dict[str, Any] = app["deps"]
    claude_integration: ClaudeIntegration = app["claude_integration"]
    storage: Storage = app["storage"]
    config: Settings = app["config"]
    features: FeatureFlags = app["features"]
    event_bus: EventBus = app["event_bus"]

    notification_service: Optional[NotificationService] = None
    scheduler: Optional[JobScheduler] = None
    recovery_task: Optional[asyncio.Task[None]] = None
    snapshot_task: Optional[asyncio.Task[bool]] = None
    agent_bridge: Optional[Any] = None

    # Set up signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(signum: int, frame: Any) -> None:
        logger.info("Shutdown signal received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting Claude Code Telegram Bot", channel=config.channel)

        # Where notifications and owner prompts go (dv-fe4d).
        transport: Transport
        telegram_bot: Optional[Any] = None
        if bot is not None:
            # Initialize the bot first (creates the Telegram Application)
            await bot.initialize()
            assert bot.app is not None

            # Now wire up components that need the Telegram Bot instance
            telegram_bot = bot.app.bot
            app["agent_handler"].bot = telegram_bot
            transport = TelegramTransport(telegram_bot)
        else:
            # Local channel: the inbox the TUI reads, behind its own secret
            # (created on first boot, never in bot.env: jobs read that file,
            # and the loader refuses LOCAL_TUI_SECRET there). Only a
            # process-env override (tests) takes the place of the file.
            transport = LocalTransport(storage.db_manager, desktop_notify=True)
            if not config.local_tui_secret:
                from src.utils.paths import ensure_tui_secret, tui_secret_path

                config.local_tui_secret = ensure_tui_secret(
                    tui_secret_path(config.data_layout)
                )
            logger.info("Local channel: no Telegram, inbox at /internal/inbox")

        # Wire notification callbacks for evaluator/refresh services.
        #
        # dv-c5d9 step 5 + review R6: a system notification must not be a
        # bare send. It is (a) ledgered in sent_messages as kind=
        # 'system_notify' so a Telegram reply to it resolves to "text the
        # agent never wrote" (dv-7783), and (b) persisted as a messages row
        # in the profile's current session/window so the agent can see that
        # the user was told — previously the text existed nowhere.
        _notify_registry = deps.get("profile_registry")
        _notify_window_repo = deps.get("window_repo")

        async def _persist_notification(
            user_id: int, message: str, agent_id: str
        ) -> None:
            profile = (
                _notify_registry.get_by_slug(agent_id) if _notify_registry else None
            )
            if profile is None:
                return
            from src.storage.delivered import persist_delivered_text

            await persist_delivered_text(
                storage,
                _notify_window_repo,
                user_id,
                profile.working_dir,
                prompt="",
                response=f"[memory system] {message}",
            )

        async def _notify_user(
            user_id: int, message: str, agent_id: Optional[str] = None
        ) -> None:
            try:
                message_id = await transport.send(user_id, message)
            except Exception as e:
                logger.warning("Notification failed", user_id=user_id, error=str(e))
                return
            try:
                await storage.sent_messages.record(
                    telegram_chat_id=user_id,
                    telegram_message_id=message_id,
                    kind="system_notify",
                    profile_slug=agent_id,
                )
            except Exception as e:  # record() never raises; belt and braces
                logger.warning("Notification ledger failed", error=str(e))
            if agent_id:
                try:
                    await _persist_notification(user_id, message, agent_id)
                except Exception as e:
                    logger.warning(
                        "Notification persistence failed",
                        agent_id=agent_id,
                        error=str(e),
                    )

        evaluator_service = app.get("evaluator_service")
        if evaluator_service:
            evaluator_service._notify = _notify_user

        refresh_service = app.get("refresh_service")
        if refresh_service:
            refresh_service._notify = _notify_user

        # A bad models.yaml booted on the built-in routes (dv-781f): say so.
        if app.get("model_routes_alert"):
            for chat_id in config.notification_chat_ids or []:
                await _notify_user(chat_id, app["model_routes_alert"])

        # Safe-mode boot (dv-86f4): one 🛟 message names every agent it turned
        # off, instead of one «выключен» per profile.
        if app.get("safe_mode_alert"):
            for chat_id in config.notification_chat_ids or []:
                await _notify_user(chat_id, app["safe_mode_alert"])
        # A profile the boot switched off over bad paths (dv-86f4): one message
        # per profile, so the owner learns now rather than from its silence.
        elif _notify_registry is not None:
            for slug, reason in _notify_registry.boot_disabled.items():
                for chat_id in config.notification_chat_ids or []:
                    await _notify_user(
                        chat_id,
                        f"⚠️ Профиль {slug} выключен до рестарта: {reason}",
                    )

        # Start event bus
        await event_bus.start()

        # Notification service
        reports_router: Optional["ReportsRouter"] = None
        if config.reports_chat_id and telegram_bot is not None:
            from src.notifications import routing

            reports_router = routing.ReportsRouter(
                chat_id=config.reports_chat_id,
                routes=config.reports_routes,
                repository=storage.project_threads,
                owner_chat_ids=config.notification_chat_ids or [],
                bindings=config.reports_bindings,
                inbound_policy=config.reports_inbound_policy,
                reply_only=config.reports_reply_only,
                ledger=storage.sent_messages,
            )
            deps["reports_router"] = reports_router
            await reports_router.ensure_bound_topics(telegram_bot)
            logger.info(
                "Reports routing on",
                chat_id=config.reports_chat_id,
                routes=config.reports_routes,
            )
        notification_service = NotificationService(
            event_bus=event_bus,
            bot=telegram_bot,
            transport=transport,
            default_chat_ids=config.notification_chat_ids or [],
            pin_store=storage.pinned_messages,
            sent_messages=storage.sent_messages,
            reports_router=reports_router,
        )
        notification_service.register()
        await notification_service.start()

        # Collect concurrent tasks
        tasks: list[asyncio.Task[Any]] = []

        # Bot task — use start() which handles its own initialization check
        if bot is not None:
            bot_task = asyncio.create_task(bot.start())
            tasks.append(bot_task)

        # Stale-fact sweep (dv-cf07; was the dv-4c6f report-only pass). Not
        # scheduler-bound: window rotations (refresh_window / /new, via
        # bot_data) and the bundle valve call it; the scheduler below only
        # adds the weekly all-scope run.
        from src.consolidation.service import ConsolidationService

        async def _notify_owner(message: str) -> None:
            for chat_id in config.notification_chat_ids or []:
                await _notify_user(chat_id, message)

        consolidation_service = ConsolidationService(
            memory_repo=storage.memory_repository,
            db=storage.db_manager,
            notify_callback=_notify_owner,
            board=storage.tasks,
            report_dir=config.data_layout.consolidation_dir,
            decision_refs=app.get("decision_refs"),
        )
        app["consolidation_service"] = consolidation_service
        deps["consolidation_service"] = consolidation_service

        # Bundle (dv-5e2a): a facts snapshot over budget requests a sweep
        # instead of being truncated, and a new window's snapshot waits for
        # the rotation's sweep.
        bundle_provider = app.get("bundle_provider")
        if bundle_provider is not None:
            bundle_provider.attach_consolidation(consolidation_service)

        # Scheduler (if enabled) — created before the API server so the
        # /internal/schedule endpoints can register jobs live.
        if features.scheduler_enabled:
            scheduler = JobScheduler(
                event_bus=event_bus,
                db_manager=storage.db_manager,
                default_working_directory=config.approved_directory or Path.home(),
            )
            await scheduler.start()
            logger.info("Job scheduler enabled")

            # Weekly sweep over every scope (dv-cf07), native async job:
            # catches scopes that rarely rotate.
            async def _weekly_consolidation() -> None:
                try:
                    await consolidation_service.run()
                except Exception as e:
                    logger.error("Weekly consolidation failed", error=str(e))

            scheduler.add_native_job(
                "memory-consolidation", "0 6 * * 1", _weekly_consolidation
            )

            # Daily fact-quality check (dv-404f): guard + one cheap judge call
            # over the facts saved in the last 24h, reported to the owner.
            # Report-only, and off unless FACT_QUALITY_CHECK_ENABLED is set.
            from src.evaluator.fact_quality import register_daily_check

            app["fact_quality_check"] = register_daily_check(
                scheduler=scheduler,
                db=storage.db_manager,
                enabled=config.fact_quality_check_enabled,
                cron_expression=config.fact_quality_check_cron,
                notify_callback=_notify_owner,
            )

            # Daily OpenRouter backstop (dv-d2f6): our recorded OR total vs
            # the key's usage_daily; drift alerts the owner. Idle without key.
            from src.model_router.or_reconcile import register_daily_reconcile

            app["openrouter_reconcile"] = register_daily_reconcile(
                scheduler=scheduler,
                model_calls=ModelCallRepository(storage.db_manager),
                notify_callback=_notify_owner,
            )

        # devvy -> Phoenix (dv-87d8): settled model_calls + tool_calls rows as
        # OpenInference spans, every minute. Off while PHOENIX_URL is unset.
        from src.observability.phoenix import register_phoenix_export

        register_phoenix_export(
            scheduler,
            storage.db_manager,
            config.phoenix_url,
            project=config.phoenix_project,
        )

        # Wire in-process agent MCP tools (schedule/notify/task). Done here —
        # not at SDK-manager construction — because the scheduler is created
        # now. These tools run outside the Bash sandbox, so agents no longer
        # need the network-blocked HTTP agentctl path.
        agent_services = None
        try:
            from src.claude.agent_tools import AgentServices

            agent_services = AgentServices(
                event_bus=event_bus,
                task_repo=storage.tasks,
                scheduler=scheduler,
                profile_registry=deps.get("profile_registry"),
                default_chat_ids=config.notification_chat_ids or [],
                memory_writer=app.get("agent_memory_writer"),
                memory_repo=storage.memory_repository,
                message_repo=storage.messages,
                audit_memory_writer=app.get("audit_memory_writer"),
                user_data=storage.user_data,
                user_timezone=config.user_timezone,
                storage=storage,
                window_repo=_notify_window_repo,
                reports_chat_id=config.reports_chat_id,
            )

            # restart_bot (dv-da50): drain on the one process-wide TurnLocks
            # the orchestrator and AgentHandler both hold, then SIGTERM.
            _restart_locks = app["agent_handler"].turn_locks

            async def _request_restart(reason: str, ctx: Any) -> str:
                # Imported on use: the module is a Telegram command module.
                from src.bot.orchestration.commands.system import request_restart

                return await request_restart(
                    _restart_locks,
                    reason=reason,
                    user_id=ctx.user_id,
                    notify=_notify_owner,
                )

            agent_services.request_restart = _request_restart
            if config.hireli_mcp_url and config.hireli_service_key:
                from src.claude.hireli_tools import HireliClient

                agent_services.hireli = HireliClient(
                    config.hireli_mcp_url,
                    config.hireli_service_key.get_secret_value(),
                )
            # exchange_* tools (dv-8664): run the owner's fetchcrypto /
            # fetchmexc scripts. Same ~/tools root the Bash money-script gate
            # watches (src/security/destructive.py).
            from src.claude.exchange_tools import VENUES, ExchangeClient

            exchange_root = Path.home() / "tools"
            if any((exchange_root / v.directory).is_dir() for v in VENUES.values()):
                from src.security.keystore import default_keystore

                agent_services.exchange = ExchangeClient(
                    exchange_root, keystore=default_keystore()
                )
        except Exception as e:  # pragma: no cover - defensive wiring guard
            logger.warning("Failed to build agent MCP services", error=str(e))

        # Family todos & reminders (dv-752a): one ItemService shared by the
        # item_* tools, the scheduler fire path, the per-turn injection on
        # both backends, and /reminders.
        if agent_services is not None:
            try:
                from src.items.repository import ItemRepository
                from src.items.service import ItemService

                _user_store = deps.get("user_store")
                item_service = ItemService(
                    ItemRepository(storage.db_manager),
                    scheduler=scheduler,
                    profile_registry=deps.get("profile_registry"),
                    default_tz=config.user_timezone,
                    role_lookup=(_user_store.get_role_cached if _user_store else None),
                )
                agent_services.items = item_service
                deps["item_service"] = item_service
                app["agent_handler"].items = item_service
                for backend in app.get("backends", {}).values():
                    backend.items_service = item_service
            except Exception as e:  # pragma: no cover - defensive wiring guard
                logger.warning("Failed to build item service", error=str(e))

        # The same tools, second transport (dv-ffcb): a loopback HTTP MCP
        # endpoint so the out-of-process Codex backend can reach them. Started
        # unconditionally, not only when AGENT_BACKEND is codex: the backend is
        # a per-user runtime choice (/model writes user_data["agent_backend"]),
        # so gating on the process-wide default would leave the tools missing
        # for exactly the user who just switched to Codex from Telegram.
        if config.enable_agent_bridge and agent_services is not None:
            try:
                from src.agent_bridge import AgentBridge

                agent_bridge = AgentBridge(
                    services=agent_services,
                    port=config.agent_bridge_port,
                    token_ttl_seconds=config.agent_bridge_token_ttl_seconds,
                )
                await agent_bridge.start()
                app["agent_bridge"] = agent_bridge
            except Exception as e:  # pragma: no cover - defensive wiring guard
                agent_bridge = None
                logger.warning("Failed to start agent bridge", error=str(e))

        # Every registered backend gets both transports and keeps what it can
        # use (dv-5368): Claude mounts the in-process services, Codex takes
        # the bridge. One failure must not cost the other backend its tools.
        if agent_services is not None:
            for backend in app.get("backends", {}).values():
                try:
                    backend.attach_tools(agent_services, agent_bridge)
                except Exception as e:  # pragma: no cover - defensive wiring guard
                    logger.warning(
                        "Failed to attach agent tools",
                        backend=getattr(backend, "backend_id", "?"),
                        error=str(e),
                    )

        # Destructive-op confirmation gate (full-access profiles). Prompts go
        # to the owner chat(s) through the channel's transport: Telegram
        # buttons, or an inbox card the TUI answers on /internal/callback.
        try:
            from src.claude.confirmation import CALLBACK_PREFIX, ConfirmationManager
            from src.security import ask_explain

            def _confirm_buttons(token: str) -> list[tuple[str, str]]:
                return [
                    ("✅ Approve", f"{CALLBACK_PREFIX}:{token}:y"),
                    ("⛔ Deny", f"{CALLBACK_PREFIX}:{token}:n"),
                ]

            async def _send_confirm_prompt(chat_id: int, text: str, token: str) -> int:
                return await transport.send(
                    chat_id, text, parse_mode="HTML", buttons=_confirm_buttons(token)
                )

            # dv-af70: the explanation is edited into a still-pending card,
            # so the same buttons go back on with it.
            async def _update_confirm_prompt(
                chat_id: int, message_id: int, text: str, token: str
            ) -> None:
                await transport.edit(
                    chat_id,
                    message_id,
                    text,
                    parse_mode="HTML",
                    buttons=_confirm_buttons(token),
                )

            async def _edit_confirm_prompt(
                chat_id: int, message_id: int, text: str
            ) -> None:
                await transport.edit(chat_id, message_id, text, parse_mode="HTML")

            confirmation_manager = ConfirmationManager(
                send_prompt=_send_confirm_prompt,
                chat_ids=config.notification_chat_ids or [],
                edit_prompt=_edit_confirm_prompt,
                timeout_seconds=config.confirmation_timeout_seconds,
                explain=ask_explain.explain,
                update_prompt=_update_confirm_prompt,
            )
            claude_integration.sdk_manager.attach_confirmation_manager(
                confirmation_manager
            )
            deps["confirmation_manager"] = confirmation_manager
        except Exception as e:  # pragma: no cover - defensive wiring guard
            logger.warning("Failed to attach confirmation manager", error=str(e))

        # One tool-call policy for both backends (dv-1601): Codex's
        # PreToolUse hook reaches the rules the Claude hook runs in-process
        # through the bridge. Attached even when the confirmation manager
        # failed to wire: denies still hold, and an "ask" is then denied.
        if agent_bridge is not None:
            from src.security.policy import turn_policy_for

            policy_registry = deps.get("profile_registry")
            agent_bridge.attach_policy(
                lambda ctx: turn_policy_for(
                    policy_registry,
                    config.sandbox_enabled,
                    ctx.working_dir,
                    user_id=ctx.user_id,
                    role=ctx.role,
                ),
                deps.get("confirmation_manager"),
            )

        # Session watchdog alert (dv-8bb5). Wired here for the same reason as
        # the confirmation gate above: needs the channel's transport, and
        # prompts go to the owner chat(s).
        try:
            from src.security.session_watchdog import CALLBACK_PREFIX as WATCHDOG_PREFIX
            from src.security.session_watchdog import WatchdogManager

            async def _send_watchdog_alert(chat_id: int, text: str, token: str) -> int:
                return await transport.send(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    buttons=[
                        ("⏸️ Hold 10 min", f"{WATCHDOG_PREFIX}:{token}:10m"),
                        ("⏸️ Hold 1 day", f"{WATCHDOG_PREFIX}:{token}:1d"),
                        ("✅ Dismiss", f"{WATCHDOG_PREFIX}:{token}:ack"),
                    ],
                )

            async def _edit_watchdog_alert(
                chat_id: int, message_id: int, text: str
            ) -> None:
                await transport.edit(chat_id, message_id, text, parse_mode="HTML")

            watchdog_manager = WatchdogManager(
                send_alert=_send_watchdog_alert,
                chat_ids=config.notification_chat_ids or [],
                edit_alert=_edit_watchdog_alert,
            )
            deps["watchdog_manager"] = watchdog_manager
        except Exception as e:  # pragma: no cover - defensive wiring guard
            logger.warning("Failed to attach watchdog manager", error=str(e))

        # API server (if enabled)
        if features.api_server_enabled:
            from src.api.server import run_api_server

            chat_turn_service = None
            if config.channel == "local":
                from src.chat.local_turns import LocalChatTurns

                chat_turn_service = LocalChatTurns(config, deps)

            api_task = asyncio.create_task(
                run_api_server(
                    event_bus,
                    config,
                    storage.db_manager,
                    scheduler=scheduler,
                    profile_registry=deps.get("profile_registry"),
                    confirmation_manager=deps.get("confirmation_manager"),
                    model_calls=ModelCallRepository(storage.db_manager),
                    tool_calls=storage.tool_calls,
                    chat_turn_service=chat_turn_service,
                )
            )
            tasks.append(api_task)
            logger.info(
                "API server enabled",
                host=config.api_server_host,
                port=config.api_server_port,
            )

        # Back after a crash-loop alert (dv-86f4, deploy/crashwatch.sh): only
        # once polling and every service above have run CRASHWATCH_GRACE_S, so
        # a loop that dies late in startup is not reported as over. Not in
        # `tasks`: its end must not end the run.
        recovery_task = asyncio.create_task(
            crashwatch_recovery_after(config.data_layout, _notify_owner)
        )
        # Last-known-good config for safe mode (dv-86f4): this boot's live
        # profiles/models, once it has run SNAPSHOT_DELAY_S with polling up and
        # the restart preflight passes. Not from a safe-mode boot: that one
        # runs on the snapshot already. Not in `tasks`, like recovery.
        if config.profiles_config_path and not app.get("safe_mode_alert"):
            from src.boot import safemode
            from src.bot.orchestration.commands.system import preflight

            models_live = config.model_routes_path
            if models_live is not None and not models_live.is_absolute():
                models_live = Path(__file__).parent.parent / models_live
            snapshot_task = asyncio.create_task(
                safemode.snapshot_after(
                    config.data_layout,
                    profiles_path=Path(config.profiles_config_path).absolute(),
                    models_path=models_live,
                    check=preflight,
                    sha=safemode.head_sha(),
                )
            )

        # Shutdown task
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        tasks.append(shutdown_task)

        # Wait for any task to complete or shutdown signal
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Check completed tasks for exceptions
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Task failed",
                    task=task.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error("Application error", error=str(e))
        raise
    finally:
        if recovery_task is not None:
            recovery_task.cancel()
        if snapshot_task is not None:
            snapshot_task.cancel()
        # Ordered shutdown: scheduler -> bridge -> API -> notification -> bot
        # -> claude -> storage
        logger.info("Shutting down application")

        try:
            if bot is not None and bot.app is not None:
                await report_dropped_messages(
                    app["agent_handler"].turn_locks, bot.app.bot
                )
            if scheduler:
                await scheduler.stop()
            if agent_bridge:
                await agent_bridge.stop()
            if notification_service:
                await notification_service.stop()
            await event_bus.stop()
            if bot is not None:
                await bot.stop()
            await claude_integration.shutdown()
            await storage.close()
        except Exception as e:
            logger.error("Error during shutdown", error=str(e))

        logger.info("Application shutdown complete")


async def main() -> None:
    """Main application entry point."""
    args = parse_args()
    setup_logging(debug=args.debug)

    logger = structlog.get_logger()
    logger.info("Starting Claude Code Telegram Bot", version=__version__)

    try:
        # Load configuration
        from src.config import FeatureFlags, load_config

        config = load_config(config_file=args.config_file)
        # Settings now holds the secrets; no child process may inherit them
        # (dv-2976). Before create_application: nothing has spawned yet.
        from src.config.loader import scrub_secrets_from_environ

        scrub_secrets_from_environ(config, args.config_file)
        features = FeatureFlags(config)

        logger.info(
            "Configuration loaded",
            environment="production" if config.is_production else "development",
            enabled_features=features.get_enabled_features(),
            debug=config.debug,
        )

        # Initialize bot and Claude integration
        app = await create_application(config)
        # The config file /voice persists its choice into (dv-6b6c).
        app["deps"]["env_file"] = args.config_file or Path(".env")
        await run_application(app)

    except ConfigurationError as e:
        logger.error("Configuration error", error=str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error", error=str(e))
        sys.exit(1)


def run() -> None:
    """Synchronous entry point for setuptools."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
        sys.exit(0)


if __name__ == "__main__":
    run()
