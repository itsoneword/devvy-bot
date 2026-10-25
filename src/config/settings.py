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
    DEFAULT_CLAUDE_MAX_BUFFER_SIZE,
    DEFAULT_CLAUDE_MAX_COST_PER_REQUEST,
    DEFAULT_CLAUDE_MAX_COST_PER_USER,
    DEFAULT_CLAUDE_MAX_COST_PER_USER_GUEST,
    DEFAULT_CLAUDE_MAX_TURNS,
    CONFIRMATION_MARGIN_SECONDS,
    DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    DEFAULT_DATABASE_URL,
    DEFAULT_MAX_SESSIONS_PER_USER,
    DEFAULT_MEMORY_PATH,
    DEFAULT_RATE_LIMIT_BURST,
    DEFAULT_RATE_LIMIT_REQUESTS,
    DEFAULT_RATE_LIMIT_WINDOW,
    DEFAULT_REPORTS_ROUTES,
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
from src.utils.paths import DataLayout, sqlite_url


# Single source of the window-refresh default; window_repository, llm_analytics
# and the session state import it instead of repeating the literal.
DEFAULT_WINDOW_REFRESH_THRESHOLD_TOKENS = 150_000


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
    confirmation_timeout_seconds: float = Field(
        600.0,
        ge=10,
        description=(
            "How long the owner has to tap Approve/Deny on a risky tool call "
            "before it is auto-denied. Every timeout that must outlive the "
            "wait (PreToolUse hook, Codex hook, turn idle limits) derives "
            "from this value."
        ),
    )
    claude_timeout_seconds: int = Field(
        DEFAULT_CLAUDE_TIMEOUT_SECONDS,
        description=(
            "Idle timeout for one SDK turn: seconds of silence from the CLI "
            "before the turn is killed. Not a wall-clock cap — a turn that "
            "keeps streaming tool calls runs as long as it needs."
        ),
    )
    # The SDK reads the CLI's stream-json on stdout and dies on any single
    # message over this limit (default 1 MiB). One Read of a ~700 KB PNG
    # base64-encodes past that and kills the whole turn, so raise the ceiling.
    claude_max_buffer_size: int = Field(
        DEFAULT_CLAUDE_MAX_BUFFER_SIZE,
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
    # the daily budget and the watchdog count its tokens instead. It uses the
    # same role tiers as the USD caps, but owners are always exempt from this
    # local hard gate: a USD override does not change that. A non-owner is
    # refused when either applicable ledger is exhausted.
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
    # win came from stripping the harness (now always on), not from the model.
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
    # Extraction on the Codex backend used to pass no model and ran whatever
    # ~/.codex/config.toml named. The router always passes one (dv-ef90); the
    # default is what that file named on 2026-10-04.
    evaluator_codex_model: str = Field(
        "openai-codex/gpt-6-sol",
        description=(
            "Model for the memory evaluator's extraction call when the active "
            "backend is Codex."
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
    # Jev gate in front of the reconciler (dv-7e11 phase 3, owner decisions
    # 2026-10-17): per candidate one typed decide() call on TypeSafe Jev;
    # confident new/duplicate are accepted, everything else (every
    # contradicts, low confidence, Jev down) goes to the haiku reconciler in
    # one batch. Default OFF until reviewed; thresholds per label so each can
    # be tuned from the op_stats.reconcile_gate data.
    reconcile_jev_gate_enabled: bool = Field(
        False,
        description=(
            "Gate reconciler candidates through Jev before the haiku call "
            "(RECONCILE_JEV_GATE_ENABLED). Off = every candidate to haiku."
        ),
    )
    # Defaults from the labelled re-run (dv-7e11 ticket, 2026-10-17).
    reconcile_jev_min_conf_new: float = Field(
        0.8,
        ge=0.0,
        le=1.0,
        description="Min Jev confidence to accept 'new' without haiku.",
    )
    # No reliable duplicate band yet: above 1.0 = duplicates always escalate.
    reconcile_jev_min_conf_duplicate: float = Field(
        1.01,
        ge=0.0,
        le=1.01,
        description=(
            "Min Jev confidence to accept 'duplicate' without haiku; the "
            "related_fact answer must clear it too. Above 1.0 = never."
        ),
    )
    # Jev's systematic miss: a state change (open -> resolved, newer value)
    # answered "new". A yes/no question in the same call; above this, escalate.
    reconcile_jev_max_state_change: float = Field(
        0.55,
        ge=0.0,
        le=1.01,
        description=(
            "Escalate when Jev's P(candidate updates/resolves a saved fact) "
            "is above this (RECONCILE_JEV_MAX_STATE_CHANGE)."
        ),
    )
    # Jev's context overflows at ~165 facts (shadow test), so the prefilter
    # is mandatory; the cited fact was in the top 15 in 90% of cases.
    reconcile_jev_top_k: int = Field(
        15,
        ge=1,
        le=100,
        description="Saved facts shown to Jev per candidate (lexical top-k).",
    )
    # TypeSafe Jev key (dv-e690). Primary source for the bot process: the
    # dotenv / secrets file, handed to the router at startup. The
    # TYPESAFE_API_KEY env var overrides it (jobs get it from `withkeys`).
    typesafe_api_key: Optional[SecretStr] = Field(
        None,
        description=(
            "TypeSafe Jev API key (TYPESAFE_API_KEY). Unset = router decide() "
            "falls back to its LLM leg."
        ),
    )
    # Jev screen in front of memory extraction (dv-7e11 step c). Jev picks
    # which saved facts Sonnet sees; it is never a skip gate.
    memory_screen_mode: Literal["off", "shadow", "on"] = Field(
        "shadow",
        description=(
            "off = today's extraction only. shadow = today's extraction writes "
            "memory as before; the screen runs beside it, writes no facts and "
            "records its proposed ops in op_stats.screen. on = reserved for the "
            "cutover step (currently behaves as shadow)."
        ),
    )
    memory_screen_chunk_tokens: int = Field(
        12_000,
        ge=2_000,
        le=30_000,
        description="Jev-token estimate per screened chunk (MEMORY_SCREEN_CHUNK_TOKENS).",
    )
    memory_screen_fact_send_min: float = Field(
        0.3,
        ge=0.0,
        le=1.0,
        description=(
            "A saved fact scoring at least this on Jev's touched or mentioned "
            "question goes to Sonnet (MEMORY_SCREEN_FACT_SEND_MIN)."
        ),
    )
    memory_screen_fact_send_max: int = Field(
        40,
        ge=1,
        le=200,
        description="Cap on score-selected facts per chunk (pinned/remember facts are extra).",
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
    # Recipe search-retry (dv-300d / dv-5d5f follow-up): the chef profile
    # measurably writes recipes from memory instead of searching (78 of 81
    # historical chef recipe turns, zero Web* calls). The trigger is
    # mechanical (see recipe_retry.needs_search_retry), not judge-scored — a
    # blind LLM judge cannot separate a confidently wrong recipe from a
    # correct one (dv-300d validation gate). Default OFF: a second SDK turn
    # per flagged answer has a real cost, so this ships opt-in, one retry
    # per turn at most.
    recipe_retry_enabled: bool = Field(
        False,
        description=(
            "Re-run a recipe-shaped answer with a forced web search when the "
            "original turn made zero WebSearch/WebFetch calls "
            "(RECIPE_RETRY_ENABLED)."
        ),
    )
    recipe_retry_profiles: str = Field(
        "chef",
        description=(
            "Comma-separated profile slugs the recipe search-retry applies "
            "to (RECIPE_RETRY_PROFILES)."
        ),
    )
    # Verification budget (dv-9f41): how much searching one turn may do.
    # Both poles measured on coach — zero data reads (dv-7a3c) and a 20-call
    # literature review for one chest session (turn 1817). Counted per turn in
    # the PreToolUse hook (src/claude/verification_budget.py). Ships
    # count-only (owner, 2026-10-13): over-budget turns are logged and show up
    # in quality_daily.deep_research_turns; set ENFORCE once those counters
    # show capping costs no quality. The 1/3 budgets are untuned starting
    # points — calibrate from the counters, not from feel.
    verification_budget_enforce: bool = Field(
        False,
        description=(
            "Deny search-class tool calls (WebSearch/WebFetch, subagent = 2) "
            "past the per-turn budget instead of only logging them "
            "(VERIFICATION_BUDGET_ENFORCE)."
        ),
    )
    verification_budget_profiles: str = Field(
        "coach,chef",
        description=(
            "Comma-separated profile slugs the per-turn verification budget "
            "applies to (VERIFICATION_BUDGET_PROFILES)."
        ),
    )
    verification_budget_draft_max: int = Field(
        1,
        description=(
            "Search-class budget units for an ordinary turn "
            "(VERIFICATION_BUDGET_DRAFT_MAX)."
        ),
    )
    verification_budget_confirm_max: int = Field(
        3,
        description=(
            "WebSearch calls allowed in the follow-up confirm turn; WebFetch "
            "and subagents are not part of it (VERIFICATION_BUDGET_CONFIRM_MAX)."
        ),
    )
    # Confirm/escalate follow-up (dv-9f41): the coach-side twin of
    # recipe_retry. A training-programme answer grounded in nothing (no data
    # read, no search) gets ONE cheap confirm turn; only a contradiction it
    # reports earns ONE deeper escalate turn. Default OFF for the same reason
    # as recipe_retry: each follow-up is a real extra SDK turn.
    verification_check_enabled: bool = Field(
        False,
        description=(
            "Run a capped confirm turn after an unchecked training-programme "
            "answer, escalating once on a reported contradiction "
            "(VERIFICATION_CHECK_ENABLED)."
        ),
    )
    verification_check_profiles: str = Field(
        "coach",
        description=(
            "Comma-separated profile slugs the confirm/escalate follow-up "
            "applies to (VERIFICATION_CHECK_PROFILES)."
        ),
    )
    openai_codex_credentials_path: Path = Field(
        Path("data/model_auth/openai_codex.json"),
        description="Local JSON token store for OpenAI Codex OAuth credentials",
    )
    codex_session_store_path: Path = Field(
        Path("data/codex_sessions.json"),
        description=(
            "Legacy Codex thread JSON (pre dv-ebde). Read-only: imported once "
            "into the bot.db sessions table at startup, never written"
        ),
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

    # One data root (dv-c66a): unset = legacy layout, every default below is
    # what it always was. Set = db/, secrets/, state/ under it; any path
    # variable set explicitly still wins. See src/utils/paths.py.
    devvy_data_root: Optional[Path] = Field(
        None,
        description="Data root holding db/, workspaces/, secrets/, state/ "
        "(unset = legacy data/ layout inside the checkout)",
    )
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
    # DEPRECATED (2026-09-10), read by nothing that decides anything. Kept so
    # an existing SESSION_TIMEOUT_HOURS in a deployment's env stays harmless.
    # Conversations end by context SIZE (window rotation), never by elapsed
    # time — see docs/DECISIONS.md.
    session_timeout_hours: int = Field(
        DEFAULT_SESSION_TIMEOUT_HOURS, description="Deprecated, unused"
    )
    session_timeout_minutes: int = Field(
        default=120,
        description="Deprecated, unused",
        ge=10,
        le=1440,  # Max 24 hours
    )
    max_sessions_per_user: int = Field(
        DEFAULT_MAX_SESSIONS_PER_USER,
        description="Max sessions kept per user PER working directory",
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
    # now read against a real level.  The later 320k default was invalid for
    # Codex models whose catalog window is 272k (about 258.4k usable input in
    # the rollout).  220k was the backend-neutral default until 2026-10-04,
    # when it was lowered to the 150k the live .env had long been running.
    # Codex orchestration also clamps it to 80% of the selected catalog
    # model's window, leaving headroom instead of relying on invisible
    # provider compaction.
    # 0 since 2026-10-04 (owner): the window-roll extraction re-reads the whole
    # window, so a mid-window checkpoint ~30k before the roll was a second,
    # mostly redundant run. Rolls, /new and "remember" still evaluate.
    window_eval_interval_tokens: int = Field(
        0,
        ge=0,
        description=(
            "Run the memory evaluator each time the conversation's real "
            "context crosses another multiple of this many tokens. "
            "0 = off (evaluate only at window roll, /new and 'remember')."
        ),
    )
    window_refresh_threshold_tokens: int = Field(
        DEFAULT_WINDOW_REFRESH_THRESHOLD_TOKENS,
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
    # Bearer for the channel-intel MCP seam (config/mcp/channel-intel.json
    # names it by env var, ``auth_env``). Read here, never from os.environ:
    # the bot scrubs secrets from its env at startup (dv-2976).
    channel_intel_token: Optional[SecretStr] = Field(
        None, description="Bearer token for the channel-intel MCP server"
    )
    # Hireli trusted-client MCP (dv-9f02). The bot itself is the MCP client
    # (src/claude/hireli_tools.py); the key never reaches an agent. Must be
    # the direct local URL: Hireli refuses the service key on proxied requests.
    hireli_mcp_url: Optional[str] = Field(
        None, description="Hireli MCP endpoint, e.g. http://127.0.0.1:8001/mcp"
    )
    hireli_service_key: Optional[SecretStr] = Field(
        None, description="Hireli trusted-client service key"
    )
    notification_chat_ids: Optional[List[int]] = Field(
        None, description="Default Telegram chat IDs for proactive notifications"
    )
    # Reports group (dv-e215). Out-of-band messages (NotificationService) whose
    # `source` matches a key of reports_routes go to a topic of this forum
    # supergroup instead of the DM. Unset = everything stays in the DM.
    reports_chat_id: Optional[int] = Field(
        None,
        description=(
            "Forum supergroup (bot is admin with Manage Topics) that receives "
            "report-type notifications, one topic per source type. Unset = off."
        ),
    )
    # NoDecode: accept 'prefix=Topic,prefix=Topic' as well as JSON.
    reports_routes: Annotated[Dict[str, str], NoDecode] = Field(
        default_factory=lambda: dict(DEFAULT_REPORTS_ROUTES),
        description=(
            "Source prefix -> topic name ('server-monitor=Monitoring,...' or a "
            "JSON object). A source matches a key when equal to it or starting "
            "with '<key>:'; longest key wins. Replaces the defaults entirely."
        ),
    )
    # Inbound side of the reports group (dv-e215 v2): topic name -> profile slug.
    # A message in a bound topic is a turn with that profile (same (user, agent)
    # conversation as in the DM). Empty = the group stays read-only.
    reports_bindings: Annotated[Dict[str, str], NoDecode] = Field(
        default_factory=dict,
        description=(
            "Topic name -> profile slug ('Monitoring=devops,Coach=coach' or a "
            "JSON object). Bound topics are created on startup."
        ),
    )
    reports_reply_only: List[str] = Field(
        default_factory=list,
        description=(
            "Bound topics that only accept a reply to a job message "
            "(owner only, whatever REPORTS_INBOUND_POLICY says)."
        ),
    )
    reports_inbound_policy: str = Field(
        "owner",
        description=(
            "Who may write to bound topics: 'owner' (default) or 'allowed' "
            "(any authenticated user; profile access rules still apply)."
        ),
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
    projects_config_path: Optional[Path] = Field(
        None, description="Path to YAML project registry for thread mode"
    )
    profiles_config_path: Optional[Path] = Field(
        default=None,
        description="Path to profiles YAML registry. If unset, single-profile mode.",
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

    @field_validator("reports_routes", mode="before")
    @classmethod
    def parse_reports_routes(cls, v: Any) -> Any:
        """Parse JSON or 'prefix=Topic,prefix=Topic' into a dict."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("{"):
                return json.loads(v)
            out: Dict[str, str] = {}
            for pair in v.split(","):
                key, _, topic = pair.partition("=")
                if key.strip() and topic.strip():
                    out[key.strip().lower()] = topic.strip()
            return out
        if isinstance(v, dict):
            return {str(k).strip().lower(): str(t).strip() for k, t in v.items()}
        return v

    @field_validator("reports_bindings", mode="before")
    @classmethod
    def parse_reports_bindings(cls, v: Any) -> Any:
        """Parse JSON or 'Topic=slug,Topic=slug' into a dict."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("{"):
                return json.loads(v)
            out: Dict[str, str] = {}
            for pair in v.split(","):
                topic, _, slug = pair.partition("=")
                if topic.strip() and slug.strip():
                    out[topic.strip()] = slug.strip().lower()
            return out
        if isinstance(v, dict):
            return {str(k).strip(): str(t).strip().lower() for k, t in v.items()}
        return v

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

        return self

    @field_validator("devvy_data_root", mode="before")
    @classmethod
    def blank_data_root_is_unset(cls, v: Any) -> Any:
        """``DEVVY_DATA_ROOT=`` (empty) means legacy layout, not the cwd."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def derive_data_root_paths(self) -> "Settings":
        """Fill path defaults from DEVVY_DATA_ROOT; explicit values win."""
        if self.devvy_data_root is None:
            return self
        root = Path(self.devvy_data_root).expanduser()
        object.__setattr__(self, "devvy_data_root", root)
        layout = self.data_layout
        derived: Dict[str, Any] = {
            "database_url": sqlite_url(layout.bot_db),
            "user_database_url": sqlite_url(layout.data_db),
            "openai_codex_credentials_path": layout.codex_credentials,
            # The global soul/memory files (activity log, /memory) are the
            # devops agent's, so they follow its workspace (dv-4030): left on
            # data/memory they would keep writing into the retired tree.
            "memory_path": layout.devops_workspace / "memory",
            "soul_md_path": layout.devops_workspace / "soul.md",
        }
        for attr, value in derived.items():
            if attr not in self.model_fields_set:
                object.__setattr__(self, attr, value)
        return self

    @property
    def confirmation_hook_timeout_seconds(self) -> int:
        """Timeout for a PreToolUse hook that may wait on the owner."""
        return int(self.confirmation_timeout_seconds) + CONFIRMATION_MARGIN_SECONDS

    @property
    def claude_idle_limit_seconds(self) -> int:
        """Claude turn idle limit; never shorter than a confirmation wait."""
        return max(
            self.claude_timeout_seconds,
            self.confirmation_hook_timeout_seconds + CONFIRMATION_MARGIN_SECONDS,
        )

    @property
    def agent_idle_limit_seconds(self) -> int:
        """Codex stdout-silence limit; never shorter than a confirmation wait."""
        return max(
            self.agent_timeout_seconds,
            self.confirmation_hook_timeout_seconds + CONFIRMATION_MARGIN_SECONDS,
        )

    @property
    def data_layout(self) -> DataLayout:
        """Every runtime path for this deployment (legacy when root unset)."""
        return DataLayout(root=self.devvy_data_root)

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

    def secret_values(self) -> List[str]:
        """Every configured secret value (for leak guards; never log these).

        Settings' SecretStr fields plus secret-named str fields
        (webhook_api_secret, ...). Values under 8 chars are skipped: too
        short to match on without false positives."""
        from src.utils.agent_env import is_secret_name

        out: List[str] = []
        for name, field in type(self).model_fields.items():
            if "SecretStr" not in str(field.annotation) and not is_secret_name(name):
                continue
            value = getattr(self, name, None)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if isinstance(value, str) and len(value) >= 8:
                out.append(value)
        return out

    def secret_value(self, env_name: str) -> Optional[str]:
        """The value of the setting named by env var ``env_name``, or None.

        For config that names a secret by env var (an MCP seam's
        ``auth_env``). Looks up the Settings field only: secrets are not in
        os.environ once the bot has started (dv-2976)."""
        if not env_name:
            return None
        value = getattr(self, env_name.lower(), None)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str) and value:
            return value
        return None

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
