"""Configuration management using Pydantic Settings.

Features:
- Environment variable loading
- Type validation
- Default values
- Computed properties
- Environment-specific settings
"""

import json
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.utils.constants import (
    DEFAULT_ACTIVITY_LOG_MAX_LINES,
    DEFAULT_CLAUDE_MAX_COST_PER_REQUEST,
    DEFAULT_CLAUDE_MAX_COST_PER_USER,
    DEFAULT_CLAUDE_MAX_COST_PER_USER_GUEST,
    DEFAULT_CLAUDE_MAX_TURNS,
    DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    DEFAULT_DATABASE_URL,
    DEFAULT_MAX_SESSIONS_PER_USER,
    DEFAULT_MEMORY_PATH,
    DEFAULT_PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS,
    DEFAULT_RATE_LIMIT_BURST,
    DEFAULT_RATE_LIMIT_REQUESTS,
    DEFAULT_RATE_LIMIT_WINDOW,
    DEFAULT_SESSION_TIMEOUT_HOURS,
    DEFAULT_SOUL_MD_PATH,
    DEFAULT_UNMETERED_MAX_TOKENS_PER_USER,
    DEFAULT_UNMETERED_MAX_TOKENS_PER_USER_GUEST,
    DEFAULT_USER_DATABASE_URL,
    DEFAULT_WATCHDOG_MAX_COST_PER_HOUR,
    DEFAULT_WATCHDOG_MAX_MESSAGES_PER_HOUR,
    DEFAULT_WATCHDOG_MAX_TOKENS_PER_HOUR,
    DEFAULT_WATCHDOG_MAX_TURNS_PER_HOUR,
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Bot settings
    telegram_bot_token: SecretStr = Field(
        ..., description="Telegram bot token from BotFather"
    )
    telegram_bot_username: str = Field(..., description="Bot username without @")

    # Security
    approved_directory: Optional[Path] = Field(
        None,
        description="Base directory for projects. If unset, denylist mode is used.",
    )
    allowed_users: Optional[List[int]] = Field(
        None, description="Allowed Telegram user IDs (owner role)"
    )
    guest_users: Optional[List[int]] = Field(
        None,
        description=(
            "Guest Telegram user IDs (restricted role). Effective whitelist is "
            "the union with allowed_users; an ID in both lists is an owner."
        ),
    )
    family_max_members: int = Field(
        3,
        description=(
            "Maximum members per family (owner-protection cap for guest-"
            "initiated /family add)"
        ),
    )
    enable_token_auth: bool = Field(
        False, description="Enable token-based authentication"
    )
    auth_token_secret: Optional[SecretStr] = Field(
        None, description="Secret for auth tokens"
    )

    # Security relaxation (for trusted environments)
    disable_security_patterns: bool = Field(
        False,
        description=(
            "Disable dangerous pattern validation (pipes, redirections, etc.)"
        ),
    )
    disable_tool_validation: bool = Field(
        False,
        description="Allow all Claude tools by bypassing tool validation checks",
    )

    # Claude settings
    claude_binary_path: Optional[str] = Field(
        None, description="Path to Claude CLI binary (deprecated)"
    )
    claude_cli_path: Optional[str] = Field(
        None, description="Path to Claude CLI executable"
    )
    anthropic_api_key: Optional[SecretStr] = Field(
        None,
        description="Anthropic API key for SDK (optional if CLI logged in)",
    )
    claude_model: Optional[str] = Field(
        None, description="Claude model to use (defaults to CLI default if unset)"
    )
    claude_max_turns: int = Field(
        DEFAULT_CLAUDE_MAX_TURNS, description="Max conversation turns"
    )
    claude_timeout_seconds: int = Field(
        DEFAULT_CLAUDE_TIMEOUT_SECONDS, description="Claude timeout"
    )
    # The SDK reads the CLI's stream-json on stdout and dies on any single
    # message over this limit (default 1 MiB). One Read of a ~700 KB PNG
    # base64-encodes past that and kills the whole turn, so raise the ceiling.
    claude_max_buffer_size: int = Field(
        16 * 1024 * 1024,
        description="Max bytes for a single SDK stream-json message (stdout)",
    )
    claude_max_cost_per_user: float = Field(
        DEFAULT_CLAUDE_MAX_COST_PER_USER, description="Max cost per user"
    )
    claude_max_cost_per_user_guest: float = Field(
        DEFAULT_CLAUDE_MAX_COST_PER_USER_GUEST,
        description="Max daily cost for guest-role users",
    )
    claude_max_cost_per_request: float = Field(
        DEFAULT_CLAUDE_MAX_COST_PER_REQUEST,
        description="Max cost per individual request (SDK budget cap)",
    )
    # Per-person budget overrides, beating the role-tier caps above. Needed
    # because role tiers can't distinguish two owner-role users (e.g. give
    # the primary owner $100/day and their spouse $50/day).
    # Optional is load-bearing: pydantic-settings JSON-decodes complex env
    # values BEFORE field validators, and only Optional fields are allowed to
    # fail that parse and fall through to our validator (same trick as
    # allowed_users).
    claude_cost_limit_overrides: Optional[Dict[int, float]] = Field(
        None,
        description=(
            "Per-user daily budget overrides as 'telegram_id:usd' pairs, "
            "comma-separated (e.g. '111:100,222:50'). Takes precedence over "
            "the role-tier caps."
        ),
    )

    # Session watchdog (dv-8bb5). Soft, hourly thresholds -- crossing one
    # sends the admin an alert with hold buttons, it never blocks the
    # request itself (the daily cost cap above already does the hard
    # blocking). Applies to every role, owner included: unlike the daily
    # cap, this is about catching a runaway pattern, not budgeting spend.
    watchdog_max_cost_per_hour: float = Field(
        DEFAULT_WATCHDOG_MAX_COST_PER_HOUR,
        description="Alert admin if a user's spend in a rolling hour exceeds this",
    )
    watchdog_max_messages_per_hour: int = Field(
        DEFAULT_WATCHDOG_MAX_MESSAGES_PER_HOUR,
        description="Alert admin if a user's completed turns in a rolling hour exceed this",
    )
    watchdog_max_turns_per_hour: int = Field(
        DEFAULT_WATCHDOG_MAX_TURNS_PER_HOUR,
        description="Alert admin if a user's summed SDK turns in a rolling hour exceed this",
    )

    # Unmetered-backend token budget (dv-0722). A Codex turn on a ChatGPT
    # subscription reports no USD price (ClaudeResponse.cost is None), so
    # the daily budget and the watchdog count its tokens instead. Same role
    # tiers, owner exemption and per-user-override precedence as the USD
    # caps above; a user is refused when EITHER ledger is exhausted.
    unmetered_max_tokens_per_user: int = Field(
        DEFAULT_UNMETERED_MAX_TOKENS_PER_USER,
        description="Max daily tokens on unmetered (no-USD-price) backends per user",
    )
    unmetered_max_tokens_per_user_guest: int = Field(
        DEFAULT_UNMETERED_MAX_TOKENS_PER_USER_GUEST,
        description="Max daily tokens on unmetered backends for guest-role users",
    )
    watchdog_max_tokens_per_hour: int = Field(
        DEFAULT_WATCHDOG_MAX_TOKENS_PER_HOUR,
        description=(
            "Alert admin if a user's unmetered-backend tokens in a rolling hour "
            "exceed this"
        ),
    )

    # Agent backend selection. ``claude`` preserves the current Claude Code path;
    # ``openai_codex`` uses the local ChatGPT/Codex device-code auth backend.
    agent_backend: Literal["claude", "openai_codex"] = Field(
        "claude", description="Default agent backend for Telegram turns"
    )
    agent_model: Optional[str] = Field(
        None, description="Default model override for the selected agent backend"
    )
    agent_timeout_seconds: int = Field(
        600, description="Timeout for non-Claude agent backend requests", ge=10
    )
    # Agent-tools bridge (dv-ffcb). A subprocess backend (Codex) cannot reach
    # the in-process SDK MCP server, so the same tools are served over
    # loopback HTTP with a per-turn bearer token. The bind address is NOT
    # configurable: it is always 127.0.0.1.
    enable_agent_bridge: bool = Field(
        True,
        description=(
            "Serve the agent tools (memory, user data) to subprocess backends "
            "over a loopback HTTP MCP endpoint. Off = a Codex turn has no bot "
            "tools at all."
        ),
    )
    agent_bridge_port: int = Field(
        8770,
        description="Loopback port for the agent-tools MCP bridge",
        ge=1,
        le=65535,
    )
    agent_bridge_token_ttl_seconds: int = Field(
        1800,
        description=(
            "Backstop expiry for a per-turn agent-bridge token. Tokens are "
            "normally revoked when the turn returns; this only bounds one "
            "that never did. Keep above AGENT_TIMEOUT_SECONDS."
        ),
        ge=60,
    )
    # Memory evaluator (fact extraction). A setting rather than a literal so
    # the model can move without a deploy. The Codex path picks its own model
    # and is unaffected.
    #
    # Default stays sonnet: haiku was tried and did NOT hold up on replay of
    # real extractions (2026-08-04, scripts/evaluator_replay.py, dv-3c8f) — it
    # emitted zero ops on 2 of 5 samples where sonnet found a fact, 3 ops where
    # sonnet found 5-7 on the largest, ran slower per call, and blew a 180s
    # ceiling on the biggest prompt against a 120s production timeout. The cost
    # win came from evaluator_lean_context, not from the model.
    #
    # Re-gated 2026-08-07 for dv-c5d9 with the semantic judge, post-027fd66
    # prompt, 55k-char slices, 9 samples: haiku failed every D10 bar —
    # 3/9 timeouts at the production 120s ceiling, 20% op match (bar >=80%),
    # 75% tier agreement (bar >=90%), pinned recall 0/3 (bar 100%). The
    # earlier rejection was NOT just a harness problem. Flip only after a
    # gate run that clears all four bars (haiku_gate artifacts in dv-c5d9).
    evaluator_model: str = Field(
        "sonnet",
        description=(
            "Model alias for the memory evaluator's Claude CLI call "
            "(e.g. 'sonnet', 'haiku', or a full model id)."
        ),
    )
    # An extraction subprocess is a bare `claude -p`, so by default it inherits
    # the whole Claude Code harness for its cwd: this repo's CLAUDE.md, the
    # skill listing, MCP tool definitions and the agent system prompt. Measured
    # 2026-08-04: that floor is ~42k tokens on a run whose answer was "[]".
    # None of it is relevant to extracting facts from a transcript.
    evaluator_lean_context: bool = Field(
        True,
        description=(
            "Strip the Claude Code harness (project settings, skills, MCP "
            "servers, tools, default system prompt) from evaluator CLI calls."
        ),
    )
    # Runaway guard, not a cost lever. The delta embeds everything since
    # last_checkpoint_message_id and that cursor only advances on success, so
    # repeated failures grow the next prompt without bound. Default sits above
    # every delta observed to date (max ~85k chars) so normal runs are untouched.
    evaluator_max_delta_chars: int = Field(
        120_000,
        ge=4_000,
        description=(
            "Hard cap on the conversation slice sent to one evaluator run. "
            "Over this, the oldest messages are dropped (most recent kept)."
        ),
    )
    # A setting (dv-c5d9) because the haiku gate may show haiku needs a higher
    # ceiling at 50k slices — raising it must be an .env edit, not a deploy.
    # On timeout the subprocess is killed (it used to leak) and the failure is
    # recorded with an error_message prefixed 'timeout' so the rate is
    # queryable from memory_extractions.
    evaluator_timeout_seconds: int = Field(
        120,
        ge=10,
        description=(
            "Ceiling for one evaluator/reconciler CLI call; timeouts kill "
            "the subprocess and are recorded as failures, never as []."
        ),
    )
    # Deterministic episodic backstop behind the extraction prompt's
    # durability rules (dv-7517 step 3). Keyword heuristic over mixed RU/EN
    # text — kill switch kept for the first weeks in case it misfires on live
    # phrasing; every rejection is logged with full content and counted in
    # op_stats.rejected_episodic either way.
    evaluator_episodic_guard: bool = Field(
        True,
        description=(
            "Reject episodic (meal/day-summary-shaped) evaluator candidates "
            "before reconciliation (EVALUATOR_EPISODIC_GUARD)."
        ),
    )
    # Daily fact-quality check (dv-404f): report-only review of the facts
    # saved in the last 24h — the episodic guard plus one cheap LLM judge call
    # per scope, delivered to the owner in Telegram. Default OFF: it costs a
    # model call a day per active scope and it is inert until the owner has
    # seen a report they want.
    fact_quality_check_enabled: bool = Field(
        False,
        description=(
            "Run the daily guard+judge review of newly-saved memory facts and "
            "report to the owner (FACT_QUALITY_CHECK_ENABLED). Never writes."
        ),
    )
    # Standard 5-field crontab, UTC (build_cron_trigger translates day-of-week).
    # 07:00 puts the report in the morning without competing with the 06:00
    # Monday consolidation pass.
    fact_quality_check_cron: str = Field(
        "0 7 * * *",
        description=(
            "Cron schedule for the daily fact-quality check "
            "(FACT_QUALITY_CHECK_CRON)."
        ),
    )
    # A setting, like evaluator_model, so the judge can move without a deploy.
    # Haiku by default: judging short standalone sentences is the reconciler's
    # class of task, where haiku held up (owner D4).
    fact_quality_check_model: str = Field(
        "haiku",
        description=(
            "Model alias for the daily fact-quality judge call "
            "(FACT_QUALITY_CHECK_MODEL)."
        ),
    )
    # Save-honesty check (dv-7783): flags replies that claim a durable write
    # («записал», "I'll remember") with no write-ish tool call in the turn.
    # Telemetry only — audit_log rows + one owner Telegram inform per
    # offending turn. Kill switch in case the detector turns out noisy in
    # production (FP bound measured offline: <5% of flagged turns, D11).
    save_claim_check_enabled: bool = Field(
        True,
        description=(
            "Detect reply text claiming a memory/file write with no backing "
            "tool call; log + inform the owner (SAVE_CLAIM_CHECK_ENABLED)."
        ),
    )
    openai_codex_credentials_path: Path = Field(
        Path("data/model_auth/openai_codex.json"),
        description="Local JSON token store for OpenAI Codex OAuth credentials",
    )
    codex_session_store_path: Path = Field(
        Path("data/codex_sessions.json"),
        description="JSON store of Codex thread ids per (user, working_dir)",
    )
    # NOTE: When changing this list, also update docs/tools.md,
    # docs/configuration.md, .env.example,
    # src/claude/facade.py (_get_admin_instructions),
    # and src/bot/orchestrator.py (_TOOL_ICONS).
    claude_allowed_tools: Optional[List[str]] = Field(
        default=[
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
            "LS",
            "Task",
            "TaskOutput",
            "MultiEdit",
            "NotebookRead",
            "NotebookEdit",
            "WebFetch",
            "TodoRead",
            "TodoWrite",
            "WebSearch",
            "Skill",
        ],
        description="List of allowed Claude tools",
    )
    claude_disallowed_tools: Optional[List[str]] = Field(
        # AskUserQuestion renders an interactive option picker that only
        # exists in the CLI harness. In a Telegram session the call succeeds
        # with a void answer, so the question never reaches the user and the
        # model concludes they declined to choose (dv-1106 follow-up,
        # 2026-08-11 coach incident). Blocked so questions go out as plain
        # chat text instead.
        default=["AskUserQuestion"],
        description="List of explicitly disallowed Claude tools/commands",
    )

    # Sandbox settings
    sandbox_enabled: bool = Field(
        True,
        description="Enable OS-level bash sandboxing for approved dir",
    )
    sandbox_excluded_commands: Optional[List[str]] = Field(
        default=["git", "npm", "pip", "poetry", "make", "docker"],
        description="Commands that run outside the sandbox (need system access)",
    )

    # Rate limiting
    rate_limit_requests: int = Field(
        DEFAULT_RATE_LIMIT_REQUESTS, description="Requests per window"
    )
    rate_limit_window: int = Field(
        DEFAULT_RATE_LIMIT_WINDOW, description="Rate limit window seconds"
    )
    rate_limit_burst: int = Field(
        DEFAULT_RATE_LIMIT_BURST, description="Burst capacity"
    )

    # Storage
    database_url: str = Field(
        DEFAULT_DATABASE_URL, description="Database connection URL"
    )
    user_database_url: str = Field(
        DEFAULT_USER_DATABASE_URL,
        description="Per-user data store (data.db, dv-5f3a): logs, products, "
        "portions, issues, docs — separate file from bot.db",
    )
    # The host runs UTC; "today" for a tracking log is the user's local day,
    # so a UTC boundary would roll the digest mid-evening (dv-df45).
    user_timezone: str = Field(
        "Europe/Lisbon",
        description="IANA timezone defining the user's day boundary for the "
        "today-state digest and the evening gap-ping",
    )
    # The task board (dv-1327). Markdown files, not a table: agents, the owner's
    # kanban UI and the taskpump all work the same files. Relative paths resolve
    # against the bot's working directory (the repo).
    deviz_tasks_dir: Path = Field(
        Path(".deviz/tasks"),
        description="Directory holding the deviz task board markdown files",
    )
    session_timeout_hours: int = Field(
        DEFAULT_SESSION_TIMEOUT_HOURS, description="Session timeout"
    )
    session_timeout_minutes: int = Field(
        default=120,
        description="Session timeout in minutes",
        ge=10,
        le=1440,  # Max 24 hours
    )
    max_sessions_per_user: int = Field(
        DEFAULT_MAX_SESSIONS_PER_USER, description="Max concurrent sessions"
    )

    # Features
    enable_mcp: bool = Field(False, description="Enable Model Context Protocol")
    mcp_config_path: Optional[Path] = Field(
        None, description="MCP configuration file path"
    )
    enable_file_uploads: bool = Field(True, description="Enable file upload handling")
    enable_voice_messages: bool = Field(
        True, description="Enable voice message transcription"
    )
    voice_provider: Literal["mistral", "openai", "whisper", "elevenlabs"] = Field(
        "mistral",
        description=(
            "Voice transcription provider: 'mistral', 'openai', 'elevenlabs', "
            "or 'whisper' (local)"
        ),
    )
    whisper_model: str = Field(
        "base",
        description=(
            "Local Whisper model size: 'tiny', 'base', 'small', 'medium', 'large-v3'. "
            "Smaller models are faster but less accurate."
        ),
    )
    mistral_api_key: Optional[SecretStr] = Field(
        None, description="Mistral API key for voice transcription"
    )
    openai_api_key: Optional[SecretStr] = Field(
        None, description="OpenAI API key for Whisper voice transcription"
    )
    elevenlabs_api_key: Optional[SecretStr] = Field(
        None, description="ElevenLabs API key for Scribe voice transcription"
    )
    voice_transcription_model: Optional[str] = Field(
        None,
        description=(
            "Model for voice transcription. "
            "Defaults to 'voxtral-mini-latest' (Mistral), 'whisper-1' (OpenAI) "
            "or 'scribe_v2' (ElevenLabs)"
        ),
    )
    voice_max_file_size_mb: int = Field(
        20,
        description=(
            "Maximum Telegram voice message size (MB) that will be downloaded "
            "for transcription"
        ),
        ge=1,
        le=200,
    )
    # dv-38ef: domain terms passed to each provider's native hint/bias
    # parameter (faster-whisper hotwords, Mistral context_bias, OpenAI
    # prompt) so recurring proper nouns don't get mistranscribed as
    # homophones ("Claude" -> "cloud").
    # NoDecode: pydantic-settings would otherwise JSON-decode the env value
    # before the CSV validator runs and crash on VOICE_VOCABULARY_HINTS=a,b.
    voice_vocabulary_hints: Annotated[List[str], NoDecode] = Field(
        default=["Claude", "Anthropic", "Sonnet", "Opus", "Haiku"],
        description=(
            "Domain vocabulary passed to the transcription provider's "
            "hint/bias parameter to reduce misrecognition of jargon and "
            "proper nouns"
        ),
    )
    voice_language: Optional[str] = Field(
        None,
        description=(
            "ISO-639-1 language code of voice messages (e.g. 'en'). Pinning the "
            "language skips auto-detection and improves accuracy on short clips. "
            "None = auto-detect."
        ),
    )
    enable_soul_memory: bool = Field(False, description="Enable soul and memory system")
    memory_path: Path = Field(
        Path(DEFAULT_MEMORY_PATH),
        description="Path to memory directory (relative to approved_directory or absolute)",
    )
    soul_md_path: Path = Field(
        Path(DEFAULT_SOUL_MD_PATH),
        description="Path to soul.md personality file (relative to approved_directory or absolute)",
    )
    activity_log_max_lines: int = Field(
        DEFAULT_ACTIVITY_LOG_MAX_LINES,
        description="Maximum lines in activity.md before rotation",
        ge=50,
    )

    # Context healing: re-inject lost assistant prose from SQLite on resume.
    heal_lost_context: bool = Field(
        True,
        description=(
            "When a resumed CLI transcript dropped a pure-prose assistant turn "
            "(no committed text and no tool_use; back-filled with a synthetic "
            "placeholder), prepend a recap of recent stored responses so the "
            "model keeps continuity. Targets chat-style personas; tool-driven "
            "turns are unaffected."
        ),
    )

    # Conversation window thresholds, in REAL context tokens (input +
    # cache_read + cache_creation), not the uncached-input-only counter these
    # replaced. Calibrated 2026-08-04 (dv-9f31) so the effective cadence
    # matches what the old, wrong counter produced in practice — the old 50k /
    # 150k values were firing at a real ~125k / ~300k. Tunable at runtime
    # because the right numbers are a lived-experience judgement, not a
    # derivation.
    #
    # 2026-08-04 correction: 200k -> 320k, on the theory that the window had
    # been silently halved. It had not. Both numbers were fine; what was being
    # compared against them was a per-turn SUM, so heavy profiles rolled every
    # turn at either value (devvy_bot rolled 7x in 5 hours; coach 5x). Fixed
    # 2026-08-05 in ClaudeResponse.peak_context_tokens — these thresholds are
    # now read against a real level and are left where they are, because 320k
    # is where the old (accidentally correct) cadence actually rotated and it
    # sits above the CLI's own auto-compact point: compaction, which
    # summarises, handles overflow first; the hard roll, which discards all
    # but the tail, stays the last resort it was meant to be.
    window_eval_interval_tokens: int = Field(
        120_000,
        ge=10_000,
        description=(
            "Run the memory evaluator each time the conversation's real "
            "context crosses another multiple of this many tokens."
        ),
    )
    window_refresh_threshold_tokens: int = Field(
        320_000,
        ge=20_000,
        description=(
            "Roll the conversation window (fresh backend session seeded with "
            "the recent tail) once real context reaches this many tokens."
        ),
    )
    # Pressure valve, not a cap (dv-5e2a, owner D6): when a window bundle's
    # facts snapshot exceeds this, the bundle is composed and injected anyway
    # and a consolidation run is requested -- never truncation, never
    # rejection. Default sits below the worst measured scope (devops,
    # 17,313 ch on 2026-08-05) so the valve demonstrably fires once.
    bundle_facts_budget_chars: int = Field(
        12_000,
        ge=1_000,
        description=(
            "Character budget for the facts snapshot inside a frozen window "
            "bundle; exceeding it triggers a memory consolidation pass."
        ),
    )

    # Reply quoting
    reply_quote: bool = Field(
        True,
        description=(
            "Quote the original user message when replying. "
            "Set to false for cleaner thread-based conversations."
        ),
    )

    # Output verbosity (0=quiet, 1=normal, 2=detailed)
    verbose_level: int = Field(
        1,
        description=(
            "Bot output verbosity: 0=quiet (final response only), "
            "1=normal (tool names + reasoning), "
            "2=detailed (tool inputs + longer reasoning)"
        ),
        ge=0,
        le=2,
    )

    # Streaming drafts (Telegram sendMessageDraft)
    enable_stream_drafts: bool = Field(
        False,
        description="Stream partial responses via sendMessageDraft (private chats only)",
    )
    stream_draft_interval: float = Field(
        0.3,
        description="Minimum seconds between draft updates (0.1-5.0)",
        ge=0.1,
        le=5.0,
    )

    # Monitoring
    log_level: str = Field("INFO", description="Logging level")
    enable_telemetry: bool = Field(False, description="Enable anonymous telemetry")
    sentry_dsn: Optional[str] = Field(None, description="Sentry DSN for error tracking")

    # Development
    debug: bool = Field(False, description="Enable debug mode")
    development_mode: bool = Field(False, description="Enable development features")
    dangerously_allow_all_users: bool = Field(
        False,
        description=(
            "Accept EVERY Telegram user as an owner. There is no other gate"
            " behind this — anyone who finds the bot gets full access. Only"
            " ever true on a throwaway local bot; DEVELOPMENT_MODE alone no"
            " longer does this (dv-5c83)."
        ),
    )

    # Webhook settings (optional)
    webhook_url: Optional[str] = Field(None, description="Webhook URL for bot")
    webhook_port: int = Field(8443, description="Webhook port")
    webhook_path: str = Field("/webhook", description="Webhook path")

    # Agentic platform settings
    enable_api_server: bool = Field(False, description="Enable FastAPI webhook server")
    api_server_host: str = Field(
        "127.0.0.1",
        description=(
            "Webhook API bind address. Set 0.0.0.0 to accept external "
            "webhooks (put a reverse proxy in front)."
        ),
    )
    api_server_port: int = Field(8080, description="Webhook API server port")
    enable_scheduler: bool = Field(False, description="Enable job scheduler")
    github_webhook_secret: Optional[str] = Field(
        None, description="GitHub webhook HMAC secret"
    )
    webhook_api_secret: Optional[str] = Field(
        None, description="Shared secret for generic webhook providers"
    )
    notification_chat_ids: Optional[List[int]] = Field(
        None, description="Default Telegram chat IDs for proactive notifications"
    )
    health_export_profile_slug: str = Field(
        "coach",
        description=(
            "Profile slug that receives 'health' provider webhooks (e.g. Apple "
            "Health via Health Auto Export). Ignored if that slug doesn't exist "
            "in the profile registry -- falls back to the default working dir."
        ),
    )
    health_export_user_id: Optional[int] = Field(
        None,
        description=(
            "Telegram user id that owns imported health data (memory scope + "
            "reply target). In a private chat this doubles as the chat id. "
            "Unset falls back to the first ALLOWED_USERS entry."
        ),
    )
    enable_project_threads: bool = Field(
        False,
        description="Enable strict routing by Telegram forum project threads",
    )
    project_threads_mode: Literal["private", "group"] = Field(
        "private",
        description="Project thread mode: private chat topics or group forum topics",
    )
    project_threads_chat_id: Optional[int] = Field(
        None, description="Telegram forum chat ID where project topics are managed"
    )
    projects_config_path: Optional[Path] = Field(
        None, description="Path to YAML project registry for thread mode"
    )
    profiles_config_path: Optional[Path] = Field(
        default=None,
        description="Path to profiles YAML registry. If unset, single-profile mode.",
    )
    project_threads_sync_action_interval_seconds: float = Field(
        DEFAULT_PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS,
        description=(
            "Minimum delay between Telegram API calls during project topic sync"
        ),
        ge=0.0,
    )

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    @field_validator(
        "allowed_users", "guest_users", "notification_chat_ids", mode="before"
    )
    @classmethod
    def parse_int_list(cls, v: Any) -> Optional[List[int]]:
        """Parse comma-separated integer lists."""
        if v is None:
            return None
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        if isinstance(v, list):
            return [int(uid) for uid in v]
        return v  # type: ignore[no-any-return]

    @field_validator("claude_cost_limit_overrides", mode="before")
    @classmethod
    def parse_cost_overrides(cls, v: Any) -> Optional[Dict[int, float]]:
        """Parse 'id:usd,id:usd' into {telegram_id: usd}."""
        if v is None or v == "":
            return None
        if isinstance(v, dict):
            return {int(k): float(val) for k, val in v.items()}
        if isinstance(v, str):
            out: Dict[int, float] = {}
            for pair in v.split(","):
                pair = pair.strip()
                if not pair:
                    continue
                uid, _, cap = pair.partition(":")
                out[int(uid.strip())] = float(cap.strip())
            return out
        return v  # type: ignore[no-any-return]

    @field_validator("claude_allowed_tools", mode="before")
    @classmethod
    def parse_claude_allowed_tools(cls, v: Any) -> Optional[List[str]]:
        """Parse comma-separated tool names."""
        if v is None:
            return None
        if isinstance(v, str):
            return [tool.strip() for tool in v.split(",") if tool.strip()]
        if isinstance(v, list):
            return [str(tool) for tool in v]
        return v  # type: ignore[no-any-return]

    @field_validator("sandbox_excluded_commands", mode="before")
    @classmethod
    def parse_sandbox_excluded_commands(cls, v: Any) -> Optional[List[str]]:
        """Parse comma-separated command names/paths that bypass the sandbox."""
        if v is None:
            return None
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        if isinstance(v, list):
            return [str(c) for c in v]
        return v  # type: ignore[no-any-return]

    @field_validator("voice_vocabulary_hints", mode="before")
    @classmethod
    def parse_voice_vocabulary_hints(cls, v: Any) -> List[str]:
        """Parse comma-separated domain terms for ASR hint biasing."""
        if isinstance(v, str):
            return [term.strip() for term in v.split(",") if term.strip()]
        if isinstance(v, list):
            return [str(term) for term in v]
        return v  # type: ignore[no-any-return]

    @field_validator("approved_directory", mode="before")
    @classmethod
    def validate_approved_directory(cls, v: Any) -> Any:
        """Validate approved directory exists and is accessible, if provided."""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            v = Path(v)

        path = v.resolve()
        if not path.exists():
            raise ValueError(f"Approved directory does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"Approved directory is not a directory: {path}")
        return path

    @field_validator("mcp_config_path", mode="before")
    @classmethod
    def validate_mcp_config(cls, v: Any, info: Any) -> Optional[Path]:
        """Validate MCP configuration path if MCP is enabled."""
        if not v:
            return v  # type: ignore[no-any-return]
        if isinstance(v, str):
            v = Path(v)
        if not v.exists():
            raise ValueError(f"MCP config file does not exist: {v}")
        # Validate that the file contains valid JSON with mcpServers
        try:
            with open(v) as f:
                config_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"MCP config file is not valid JSON: {e}")
        if not isinstance(config_data, dict):
            raise ValueError("MCP config file must contain a JSON object")
        if "mcpServers" not in config_data:
            raise ValueError(
                "MCP config file must contain a 'mcpServers' key. "
                'Format: {"mcpServers": {"name": {"command": ...}}}'
            )
        if not isinstance(config_data["mcpServers"], dict):
            raise ValueError(
                "'mcpServers' must be an object mapping server names to configurations"
            )
        if not config_data["mcpServers"]:
            raise ValueError(
                "'mcpServers' must contain at least one server configuration"
            )
        return v  # type: ignore[no-any-return]

    @field_validator("projects_config_path", mode="before")
    @classmethod
    def validate_projects_config_path(cls, v: Any) -> Optional[Path]:
        """Validate projects config path if provided."""
        if not v:
            return None
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return None
            v = Path(value)
        if not v.exists():
            raise ValueError(f"Projects config file does not exist: {v}")
        if not v.is_file():
            raise ValueError(f"Projects config path is not a file: {v}")
        return v  # type: ignore[no-any-return]

    @field_validator("project_threads_mode", mode="before")
    @classmethod
    def validate_project_threads_mode(cls, v: Any) -> str:
        """Validate project thread mode."""
        if v is None:
            return "private"
        mode = str(v).strip().lower()
        if mode not in {"private", "group"}:
            raise ValueError("project_threads_mode must be one of ['private', 'group']")
        return mode

    @field_validator("voice_provider", mode="before")
    @classmethod
    def validate_voice_provider(cls, v: Any) -> str:
        """Validate and normalize voice transcription provider."""
        if v is None:
            return "mistral"
        provider = str(v).strip().lower()
        if provider not in {"mistral", "openai", "whisper", "elevenlabs"}:
            raise ValueError(
                "voice_provider must be one of "
                "['mistral', 'openai', 'whisper', 'elevenlabs']"
            )
        return provider

    @field_validator("project_threads_chat_id", mode="before")
    @classmethod
    def validate_project_threads_chat_id(cls, v: Any) -> Optional[int]:
        """Allow empty chat ID for private mode by treating blank values as None."""
        if v is None:
            return None
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return None
            return int(value)
        if isinstance(v, int):
            return v
        return v  # type: ignore[no-any-return]

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: Any) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")
        return v.upper()  # type: ignore[no-any-return]

    @model_validator(mode="after")
    def validate_cross_field_dependencies(self) -> "Settings":
        """Validate dependencies between fields."""
        # Check auth token requirements
        if self.enable_token_auth and not self.auth_token_secret:
            raise ValueError(
                "auth_token_secret required when enable_token_auth is True"
            )

        # Check MCP requirements
        if self.enable_mcp and not self.mcp_config_path:
            raise ValueError("mcp_config_path required when enable_mcp is True")

        if self.enable_project_threads:
            if (
                self.project_threads_mode == "group"
                and self.project_threads_chat_id is None
            ):
                raise ValueError(
                    "project_threads_chat_id required when "
                    "project_threads_mode is 'group'"
                )
            if not self.projects_config_path:
                raise ValueError(
                    "projects_config_path required when enable_project_threads is True"
                )

        return self

    @model_validator(mode="after")
    def validate_soul_memory_paths(self) -> "Settings":
        """Validate and resolve soul/memory paths against approved_directory."""
        if not self.enable_soul_memory:
            return self

        for attr in ("memory_path", "soul_md_path"):
            path = getattr(self, attr)
            if not path.is_absolute():
                if self.approved_directory is None:
                    # In denylist mode, relative paths resolve against cwd
                    resolved = Path.cwd() / path
                else:
                    resolved = (self.approved_directory / path).resolve()
            else:
                resolved = path.resolve()

            # Ensure path is within approved_directory (allowlist mode only)
            if self.approved_directory is not None:
                try:
                    resolved.relative_to(self.approved_directory)
                except ValueError:
                    raise ValueError(
                        f"{attr} '{path}' resolves to '{resolved}' which is "
                        f"outside approved directory boundary "
                        f"'{self.approved_directory}'"
                    )

            object.__setattr__(self, attr, resolved)

        return self

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return not (self.debug or self.development_mode)

    @property
    def database_path(self) -> Optional[Path]:
        """Extract path from SQLite database URL."""
        if self.database_url.startswith("sqlite:///"):
            db_path = self.database_url.replace("sqlite:///", "")
            return Path(db_path).resolve()
        return None

    @property
    def telegram_token_str(self) -> str:
        """Get Telegram token as string."""
        return self.telegram_bot_token.get_secret_value()

    @property
    def auth_secret_str(self) -> Optional[str]:
        """Get auth token secret as string."""
        if self.auth_token_secret:
            return self.auth_token_secret.get_secret_value()
        return None

    @property
    def anthropic_api_key_str(self) -> Optional[str]:
        """Get Anthropic API key as string."""
        return (
            self.anthropic_api_key.get_secret_value()
            if self.anthropic_api_key
            else None
        )

    @property
    def mistral_api_key_str(self) -> Optional[str]:
        """Get Mistral API key as string."""
        return self.mistral_api_key.get_secret_value() if self.mistral_api_key else None

    @property
    def openai_api_key_str(self) -> Optional[str]:
        """Get OpenAI API key as string."""
        return self.openai_api_key.get_secret_value() if self.openai_api_key else None

    @property
    def elevenlabs_api_key_str(self) -> Optional[str]:
        """Get ElevenLabs API key as string."""
        return (
            self.elevenlabs_api_key.get_secret_value()
            if self.elevenlabs_api_key
            else None
        )

    @property
    def resolved_voice_model(self) -> str:
        """Get the voice transcription model, with provider-specific defaults."""
        if self.voice_transcription_model:
            return self.voice_transcription_model
        if self.voice_provider == "whisper":
            return self.whisper_model
        if self.voice_provider == "openai":
            return "whisper-1"
        if self.voice_provider == "elevenlabs":
            return "scribe_v2"
        return "voxtral-mini-latest"

    @property
    def voice_max_file_size_bytes(self) -> int:
        """Maximum allowed voice message size in bytes."""
        return self.voice_max_file_size_mb * 1024 * 1024

    @property
    def voice_provider_api_key_env(self) -> str:
        """API key environment variable required for the configured voice provider."""
        if self.voice_provider == "whisper":
            return "WHISPER_MODEL"  # No API key needed; shown for guidance only
        if self.voice_provider == "openai":
            return "OPENAI_API_KEY"
        if self.voice_provider == "elevenlabs":
            return "ELEVENLABS_API_KEY"
        return "MISTRAL_API_KEY"

    @property
    def voice_provider_display_name(self) -> str:
        """Human-friendly label for the configured voice provider."""
        if self.voice_provider == "whisper":
            return f"Local Whisper ({self.whisper_model})"
        if self.voice_provider == "openai":
            return "OpenAI Whisper"
        if self.voice_provider == "elevenlabs":
            return "ElevenLabs Scribe"
        return "Mistral Voxtral"

    @property
    def is_denylist_mode(self) -> bool:
        """True when no APPROVED_DIRECTORY is set -- uses denylist validation."""
        return self.approved_directory is None
