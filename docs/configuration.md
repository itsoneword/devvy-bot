# Configuration Guide

This document provides comprehensive information about configuring Devvy.

`src/config/settings.py` is the source of truth, and [`.env.example`](../.env.example) is
generated from it — that file always lists every variable with its real default. If this
page and the model disagree, the model wins; fix the page.

## Overview

The bot uses a configuration system built with Pydantic Settings v2 that provides:

- **Type Safety**: All configuration values are validated and type-checked
- **Environment Support**: Automatic environment-specific overrides
- **Feature Flags**: Dynamic enabling/disabling of functionality
- **Validation**: Cross-field validation and runtime checks

## Configuration Sources

Configuration is loaded in this order (later sources override earlier ones):

1. **Default values** defined in the Settings class
2. **Environment variables**
3. **`.env` file** (if present)
4. **Environment-specific overrides** (development/testing/production)

## Environment Variables

### Required Settings

```bash
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_BOT_USERNAME=your_bot_name

# Security
APPROVED_DIRECTORY=/path/to/your/projects
```

### Optional Settings

#### User Access Control

```bash
# Comma-separated list of allowed Telegram user IDs
ALLOWED_USERS=123456789,987654321

# Enable token-based authentication (requires AUTH_TOKEN_SECRET)
ENABLE_TOKEN_AUTH=false
AUTH_TOKEN_SECRET=your-secret-key-here
```

#### Security Relaxation (Trusted Environments Only)

```bash
# Disable dangerous pattern validation in SecurityValidator (default: false)
# WARNING: This allows characters such as pipes and redirections in validated paths.
DISABLE_SECURITY_PATTERNS=false

# Disable ToolMonitor allowlist/disallowlist checks (default: false)
# WARNING: This only skips tool-name allow/disallow checks; path and Bash safety checks still apply.
DISABLE_TOOL_VALIDATION=false
```

#### Claude Configuration

```bash
# Authentication
ANTHROPIC_API_KEY=sk-ant-api03-...    # Optional: API key for SDK (uses CLI auth if omitted)

# Maximum conversation turns before requiring new session
CLAUDE_MAX_TURNS=10

# Timeout for Claude operations in seconds
CLAUDE_TIMEOUT_SECONDS=300

# Max bytes for one stream-json message on the CLI's stdout. The SDK's own
# default is 1 MiB, which a single image Read overflows and kills the turn.
CLAUDE_MAX_BUFFER_SIZE=16777216

# Maximum cost per user in USD (lifetime budget for rate limiter)
CLAUDE_MAX_COST_PER_USER=10.0

# Maximum cost per individual request in USD (SDK-level hard cap)
CLAUDE_MAX_COST_PER_REQUEST=5.0

# Allowed Claude tools (comma-separated list; see docs/tools.md for descriptions)
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,LS,Task,TaskOutput,MultiEdit,NotebookRead,NotebookEdit,WebFetch,TodoRead,TodoWrite,WebSearch
```

#### Agent Backend Selection

```bash
# Default backend for agentic text turns: claude or openai_codex
AGENT_BACKEND=claude

# Optional model override for the selected backend
# Current live-tested Codex choices: openai-codex/gpt-5.5, openai-codex/gpt-5.4-mini
AGENT_MODEL=

# Timeout for non-Claude backend requests in seconds
AGENT_TIMEOUT_SECONDS=600

# Local OAuth token store used by /model -> Login OpenAI Codex
OPENAI_CODEX_CREDENTIALS_PATH=data/model_auth/openai_codex.json
```

#### Memory Evaluator

The evaluator extracts memory ops from conversations at each window
checkpoint. Its model is a separate setting from the conversation model.

```bash
# Model alias for the evaluator's Claude CLI call (sonnet, haiku, or a full id)
EVALUATOR_MODEL=sonnet

# Strip the Claude Code harness from evaluator CLI calls
EVALUATOR_LEAN_CONTEXT=true

# Hard cap on the conversation slice one run may carry (chars)
EVALUATOR_MAX_DELTA_CHARS=120000

# Ceiling for one evaluator/reconciler CLI call; on timeout the subprocess is
# killed and the failure is recorded with an error_message prefixed 'timeout'
EVALUATOR_TIMEOUT_SECONDS=120
```

Since dv-c5d9 each run is a pipeline: extraction emits *candidates* (with
optional `supersedes_id` hints), a second haiku call reconciles them against
the scope's full active fact set (verdicts: new / duplicate / contradicts /
unsure), and deterministic code maps verdicts to add/supersede/flag_conflict
ops. The per-run breakdown lands in `memory_extractions.op_stats` and in the
user-facing "Memory: N found — …" report.

**Do not switch this to `haiku` without re-measuring.** It was tried and
rejected on 2026-08-04 (`scripts/evaluator_replay.py`, dv-3c8f): replaying 5
real extractions, haiku emitted zero ops on 2 samples where sonnet found a
fact, 3 ops where sonnet found 5–7 on the largest, ran *slower* per call than
sonnet, and exceeded a 180s ceiling on the biggest prompt — against a 120s
production timeout in `_invoke_claude_cli`. The setting exists so that verdict
can be revisited with data, not so it can be flipped on intuition.

Revisited with data on 2026-08-07 (dv-c5d9, semantic judge, post-`027fd66`
prompt, 55k-char slices, 9 samples): haiku failed every D10 gate bar — 3/9
timeouts at 120s, 20% op match vs ≥80%, 75% tier agreement vs ≥90%, pinned
recall 0/3 vs 100%. Extraction stays on sonnet; only the *reconciler* (the
second, small call of the dv-c5d9 pipeline) runs on haiku.

`EVALUATOR_LEAN_CONTEXT` matters more than the model. An extraction is a bare
`claude -p` in the bot's working directory, so by default it inherits that
directory's whole Claude Code context — project `CLAUDE.md`, the skill
listing, MCP tool definitions, the agent system prompt. Measured 2026-08-04,
that floor was ~42k tokens on runs that returned `[]`. Lean mode passes
`--system-prompt`, `--tools ""`, `--setting-sources ""`, `--strict-mcp-config`
and `--disable-slash-commands`, leaving only the extraction prompt itself.
Set it to `false` to reproduce the old invocation.

`EVALUATOR_MAX_DELTA_CHARS` is a runaway guard, not a cost lever: the
checkpoint cursor only advances after a *successful* run, so a wedged
extractor would otherwise grow the next prompt without bound. Truncation drops
the oldest messages and logs a warning — those facts are never re-sent.

The Codex backend picks its own model and ignores all three.

#### Daily fact-quality check (dv-404f)

A scheduled review of the facts saved in the last 24 hours: the deterministic
episodic guard plus one cheap judge call per scope (durable / episodic /
malformed), reported to the owner chat(s) as e.g.
`coach#u111111111: 5 new facts, 2 look like trash (ids 512, 514)`.

**Report-only.** It never closes, edits or deletes a fact — fixes stay
owner-approved via `scripts/cleanup_episodic.py`. Off by default; it needs
`ENABLE_SCHEDULER=true` as well, since it registers as a native scheduler job
alongside the weekly consolidation pass.

```bash
# Run the daily check and report to the owner (default off)
FACT_QUALITY_CHECK_ENABLED=false

# Cron schedule, standard 5 fields, UTC
FACT_QUALITY_CHECK_CRON=0 7 * * *

# Model alias for the judge call
FACT_QUALITY_CHECK_MODEL=haiku
```

The guard and the judge are deliberately independent: either one calling a
fact junk lists it, and a disagreement between them is itself signal for the
next round of extraction-prompt tuning. A judge outage degrades to a
guard-only report that says so, rather than a clean-looking day. Every flagged
fact is also logged with full content as `fact_quality_verdict`, which is the
labeled corpus for that tuning.

In agentic mode, `/model` can switch per user between Claude Code and OpenAI Codex without changing the global default. OpenAI Codex login uses device-code authentication and stores the resulting token JSON with user-only file permissions. Unsupported Codex model selections, including `*-pro` variants rejected by ChatGPT-account Codex auth, fall back to `openai-codex/gpt-5.4-mini`. Current limitation: Codex is wired for the agentic text path; document, photo, and voice workflows still use the Claude integration.

#### Rate Limiting

```bash
# Number of requests allowed per window
RATE_LIMIT_REQUESTS=10

# Rate limit window in seconds
RATE_LIMIT_WINDOW=60

# Burst capacity for rate limiting
RATE_LIMIT_BURST=20
```

#### Session Watchdog (dv-8bb5)

Soft, hourly thresholds -- crossing one alerts the owner chat(s) with
hold-10-min / hold-1-day / dismiss buttons instead of blocking the request.
Applies to every role, owner included. See `src/security/session_watchdog.py`
and `RateLimiter.record_watchdog_turn`.

```bash
# Alert if a user's real spend in a rolling hour exceeds this
WATCHDOG_MAX_COST_PER_HOUR=20.0

# Alert if a user's completed turns in a rolling hour exceed this
WATCHDOG_MAX_MESSAGES_PER_HOUR=60

# Alert if a user's summed SDK turns in a rolling hour exceed this
WATCHDOG_MAX_TURNS_PER_HOUR=100
```

#### Storage & Database

```bash
# Database URL (SQLite by default)
DATABASE_URL=sqlite:///data/bot.db

# Per-user data store (dv-5f3a): logs, products, portions, issues, docs.
# Separate file from bot.db so user data and analytics have separate blast
# radii; own migration chain (src/storage/user_data.py).
USER_DATABASE_URL=sqlite:///data/data.db

# Session management
SESSION_TIMEOUT_HOURS=24           # Session timeout in hours
MAX_SESSIONS_PER_USER=5            # Max concurrent sessions per user
```

#### Feature Flags

```bash
# Enable Model Context Protocol
ENABLE_MCP=false
MCP_CONFIG_PATH=/path/to/mcp/config.json

# Enable file upload handling
ENABLE_FILE_UPLOADS=true

# Enable voice message transcription
ENABLE_VOICE_MESSAGES=true
VOICE_PROVIDER=mistral              # 'mistral' (default), 'openai', 'elevenlabs' or 'whisper'
MISTRAL_API_KEY=                     # Required when VOICE_PROVIDER=mistral
OPENAI_API_KEY=                      # Required when VOICE_PROVIDER=openai
ELEVENLABS_API_KEY=                  # Required when VOICE_PROVIDER=elevenlabs
VOICE_VOCABULARY_HINTS=Claude,Anthropic,Sonnet,Opus,Haiku  # CSV of domain terms to bias recognition
VOICE_LANGUAGE=                      # ISO-639-1 code (e.g. en); empty = auto-detect
VOICE_TRANSCRIPTION_MODEL=           # Default: voxtral-mini-latest (Mistral) or whisper-1 (OpenAI)
VOICE_MAX_FILE_SIZE_MB=20            # Max Telegram voice file size to download (1-200MB)
```

#### Agentic Platform

```bash
# Webhook API Server
ENABLE_API_SERVER=false               # Enable FastAPI webhook server
API_SERVER_PORT=8080                  # Server port (default: 8080)

# Webhook Authentication
GITHUB_WEBHOOK_SECRET=your-secret    # GitHub HMAC-SHA256 secret
WEBHOOK_API_SECRET=your-secret       # Bearer token for generic providers

# Job Scheduler
ENABLE_SCHEDULER=false                # Enable cron job scheduler

# Notifications
NOTIFICATION_CHAT_IDS=123456,789012  # Default Telegram chat IDs for proactive notifications
```

#### Project Thread Mode

```bash
# Strict project routing via Telegram project topics
ENABLE_PROJECT_THREADS=false

# Mode: private (default) or group
PROJECT_THREADS_MODE=private

# YAML registry file with project slugs/names/paths
PROJECTS_CONFIG_PATH=config/projects.yaml

# Required only for PROJECT_THREADS_MODE=group
PROJECT_THREADS_CHAT_ID=-1001234567890

# Minimum delay (seconds) between Telegram API calls during topic sync
# Set 0 to disable pacing
PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS=1.1
```

`PROJECTS_CONFIG_PATH` schema:

```yaml
projects:
  - slug: my-app
    name: My App
    path: my-app
    enabled: true
```

When `ENABLE_PROJECT_THREADS=true`:
- `PROJECT_THREADS_MODE=private`:
  - `/start` and `/sync_threads` are allowed outside topics in private chat.
  - all other updates must be inside mapped project topics.
- `PROJECT_THREADS_MODE=group`:
  - behavior remains forum-topic based using `PROJECT_THREADS_CHAT_ID`.

#### Monitoring & Logging

```bash
# Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL=INFO

# Enable anonymous telemetry
ENABLE_TELEMETRY=false

# Sentry DSN for error tracking
SENTRY_DSN=https://your-sentry-dsn@sentry.io/project
```

#### Development

```bash
# Enable debug mode
DEBUG=false

# Enable development features
DEVELOPMENT_MODE=false

# Environment override (development, testing, production)
ENVIRONMENT=development
```

#### Webhook (Telegram Polling vs Webhook)

```bash
# Webhook URL for bot (leave empty for polling mode)
WEBHOOK_URL=https://your-domain.com/webhook

# Webhook port
WEBHOOK_PORT=8443

# Webhook path
WEBHOOK_PATH=/webhook
```

#### Profiles (Personas)

```bash
# YAML registry of personas. Unset = single-profile mode.
PROFILES_CONFIG_PATH=config/profiles.yaml
```

Each profile has its own `working_dir`, `memory_dir` and `persona` file, which
together give it an isolated auto-resuming session — the existing
`(user, working_dir)` session key provides that isolation for free. Route to a
profile with a sticky switch (`/devops`, `/channel`, `/general`) or a one-shot
prefix (`@channel <msg>`). Pick one from a keyboard with `/agents`; inspect
state with `/sessions`, `/diag`, `/identity`. (`/profiles` was deleted — it was
a strict subset of `/agents`.)

Profiles work under both denylist (systemd) and allowlist (container) modes;
each profile's `working_dir` must be reachable in whichever mode is active.
Per-profile keys include `sandbox`, `max_turns`, `max_cost_per_request`,
`permission_policy.allowed_roles`, `family_memory`, `personal_memory` and
`mcp`. See `config/profiles.example.yaml`.

Note that a profile's persona comes from its `persona` file, injected by
`PromptComposer`. A `CLAUDE.md` in the profile's `working_dir` is loaded
separately by the CLI itself via the `project` setting source — the bot does
not inject it.

#### Output Verbosity

```bash
# 0 = quiet (final response only), 1 = normal, 2 = detailed
VERBOSE_LEVEL=1
```

Controls how much of Claude's background activity is streamed to the user
while a turn runs. `1` shows tool names plus reasoning snippets; `2` adds tool
input summaries and longer reasoning text. Users override per session with
`/verbose 0|1|2`. A typing indicator refreshes every ~2s at every level.

#### Voice Transcription

```bash
ENABLE_VOICE_MESSAGES=true
VOICE_PROVIDER=mistral          # mistral | openai | elevenlabs | whisper
MISTRAL_API_KEY=...
OPENAI_API_KEY=...
ELEVENLABS_API_KEY=...
VOICE_VOCABULARY_HINTS=Claude,Anthropic,Sonnet,Opus,Haiku
VOICE_LANGUAGE=en
VOICE_TRANSCRIPTION_MODEL=...

# whisper provider only — runs locally, no API key
WHISPER_MODEL=base              # tiny | base | small | medium | large-v3
```

Implemented in `src/bot/features/voice_handler.py`. The `whisper` provider runs
locally through `faster-whisper` (CPU, `compute_type=int8`, `vad_filter` on).
`faster-whisper` is a main Poetry dependency, so `poetry install` / `make
install` pulls it in; on the first transcription it downloads model weights
(~480 MB for `small`) into the HuggingFace cache before returning a result.

#### Channel Knowledge Base (MCP)

```bash
CHANNEL_INTEL_HOST=127.0.0.1
CHANNEL_INTEL_PORT=8765
CHANNEL_INTEL_DB_PATH=data/channel_intel.db
CHANNEL_INTEL_TOKEN=            # if set, daemon requires Bearer auth
```

`src/channel_intel/server.py` runs as the standalone `channel-intel.service`
HTTP MCP daemon. The bot mounts it **only** for the `channel` profile, through
that profile's `mcp` seam (`config/mcp/channel-intel.json`), which reads the
same token env var and injects the header. Other profiles do not receive these
tools unless their profile config explicitly points at an MCP seam.

## Cost Limits

Three separate mechanisms, easily confused:

| Limit | Set by | Scope | On hit |
|---|---|---|---|
| Daily rolling budget | `CLAUDE_MAX_COST_PER_USER` / `_GUEST`, per-user `CLAUDE_COST_LIMIT_OVERRIDES` (`id:usd,...`) | per user, 24h in-memory window | request refused before the model runs |
| Per-request SDK cap | `CLAUDE_MAX_COST_PER_REQUEST`, or per-profile `max_cost_per_request` | per turn | **turn aborted mid-run**, partial output labelled as such |
| Turn limit | `CLAUDE_MAX_TURNS`, or per-profile `max_turns` | per turn | turn stopped, labelled |

Owners are exempt from the daily budget and the global per-request cap
**unless** an explicit per-user override (daily) or per-profile
`max_cost_per_request` (per-request) names them — the specific setting always
beats the role exemption.

`/usage` reads the enforced number from `RateLimiter.effective_daily_cap`, so
the readout and the gate cannot drift apart. An aborted turn always produces a
user-visible notice; see `_ABORT_SUBTYPES` in `src/claude/sdk_integration.py`
and `docs/FINDINGS.md` 2026-08-03 for why silence there was a bug.

## Environment-Specific Configuration

The bot automatically applies different settings based on the environment:

### Development Environment

Activated when `ENVIRONMENT=development` or when `DEBUG=true`:

- `debug = true`
- `development_mode = true`
- `log_level = "DEBUG"`
- `rate_limit_requests = 100` (more lenient)
- `claude_timeout_seconds = 600` (longer timeout)
- `enable_telemetry = false`

### Testing Environment

Activated when `ENVIRONMENT=testing`:

- `debug = true`
- `database_url = "sqlite:///:memory:"` (in-memory database)
- `approved_directory = "/tmp/test_projects"`
- `claude_timeout_seconds = 30` (faster timeout)
- `rate_limit_requests = 1000` (no effective rate limiting)

### Production Environment

Activated when `ENVIRONMENT=production`:

- `debug = false`
- `log_level = "INFO"`
- `enable_telemetry = true`
- `claude_max_cost_per_user = 5.0` (stricter cost limit)
- `claude_max_cost_per_request = 2.0` (per-request SDK cap)
- `rate_limit_requests = 5` (stricter rate limiting)
- `session_timeout_hours = 12` (shorter session timeout)

## Feature Flags

Feature flags allow you to enable or disable functionality dynamically:

```python
from src.config import load_config, FeatureFlags

config = load_config()
features = FeatureFlags(config)

if features.api_server_enabled:
    # Start webhook API server
    pass
```

Available feature flags:

- `api_server_enabled`: Webhook API server
- `scheduler_enabled`: Cron job scheduler
- `mcp_enabled`: Model Context Protocol support
- `file_uploads_enabled`: File upload handling
- `telemetry_enabled`: Anonymous usage telemetry
- `token_auth_enabled`: Token-based authentication
- `webhook_enabled`: Telegram webhook mode (vs polling)
- `voice_messages_enabled`: Voice message transcription (default: true)
- `development_features_enabled`: Development-only features

## Validation

The configuration system performs extensive validation:

### Path Validation

- `APPROVED_DIRECTORY` must exist and be accessible
- `MCP_CONFIG_PATH` must exist if MCP is enabled

### Cross-Field Validation

- `AUTH_TOKEN_SECRET` is required when `ENABLE_TOKEN_AUTH=true`
- `MCP_CONFIG_PATH` is required when `ENABLE_MCP=true`
- `MISTRAL_API_KEY` is required when `VOICE_PROVIDER=mistral`
- `OPENAI_API_KEY` is required when `VOICE_PROVIDER=openai`
- `ELEVENLABS_API_KEY` is required when `VOICE_PROVIDER=elevenlabs` (model `scribe_v2`; `VOICE_VOCABULARY_HINTS` become Scribe keyterms, +20% cost)
- `VOICE_VOCABULARY_HINTS` feeds each provider's native bias parameter: faster-whisper `hotwords`, Mistral `context_bias`, OpenAI `prompt`, ElevenLabs `keyterms`

### Value Validation

- `LOG_LEVEL` must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL
- Numeric values must be positive where appropriate
- User IDs in `ALLOWED_USERS` must be valid integers

## Claude Integration Options

### Authentication Options

#### Option 1: Use Existing Claude CLI Authentication (Recommended)
```bash
# No ANTHROPIC_API_KEY needed - SDK will use CLI credentials
# Ensure Claude CLI is installed and authenticated: claude auth login
```

#### Option 2: Direct API Key
```bash
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
```

## Troubleshooting

### Common Issues

1. **"Approved directory does not exist"**
   - Ensure the path in `APPROVED_DIRECTORY` exists
   - Use absolute paths, not relative paths
   - Check file permissions

2. **"auth_token_secret required"**
   - Set `AUTH_TOKEN_SECRET` when using `ENABLE_TOKEN_AUTH=true`
   - Generate a secure secret: `openssl rand -hex 32`

3. **"MCP config file does not exist"**
   - Ensure `MCP_CONFIG_PATH` points to an existing file
   - Or disable MCP with `ENABLE_MCP=false`

## Security Considerations

- **Never commit secrets** to version control
- **Use environment variables** for sensitive data
- **Rotate tokens regularly** if using token-based auth
- **Restrict `APPROVED_DIRECTORY`** to only necessary paths
- **Monitor logs** for configuration errors and security events
