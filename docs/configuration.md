# Configuration Guide

This document provides comprehensive information about configuring Devvy.

`src/config/settings.py` is the source of truth for **defaults**, and
[`.env.example`](../.env.example) is generated from it — that file always lists every
variable with its real default. If this page and the model disagree, the model wins; fix
the page.

**Defaults are not live values.** The running bot uses the default overlaid by `.env`,
the secrets file and the environment overrides below. To see what is actually in effect
and where each value comes from:

```bash
poetry run python scripts/show_config.py              # every value that differs from its default
poetry run python scripts/show_config.py window eval  # fields matching any substring
poetry run python scripts/show_config.py --all        # everything
```

**Runtime choices are not config.** The model a user picks with `/model` (a DB
prefs row, loaded into `user_data["agent_model"]`), per-profile selections and
backend switches override these values per turn, and `show_config.py` cannot
see them. For what actually ran, query usage: `llm_analytics.db` →
`api_calls.model` joined to `sessions.profile` / `sessions.cwd`.

## Overview

The bot uses a configuration system built with Pydantic Settings v2 that provides:

- **Type Safety**: All configuration values are validated and type-checked
- **Environment Support**: Automatic environment-specific overrides
- **Feature Flags**: Dynamic enabling/disabling of functionality
- **Validation**: Cross-field validation and runtime checks

## Configuration Sources

Configuration is loaded in this order (later sources override earlier ones):

1. **Default values** defined in the Settings class
2. **`.env` file** (if present)
3. **Secrets file** (`<data root>/secrets/bot.env`; wins over `.env`)
4. **Process environment variables** (win over both files)
5. **Environment-specific overrides** in `src/config/loader.py`, chosen by
   `ENVIRONMENT` (default `development`)

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

# Idle timeout for one SDK turn: seconds of silence from the CLI before the
# turn is killed. NOT a wall-clock cap on the turn — a turn that keeps
# streaming tool calls (sub-agents included) runs as long as it needs. Only a
# single tool call that produces nothing for this long trips it.
CLAUDE_TIMEOUT_SECONDS=300

# Max bytes for one stream-json message on the CLI's stdout. The SDK's own
# default is 1 MiB, which a single image Read overflows and kills the turn.
CLAUDE_MAX_BUFFER_SIZE=16777216

# Maximum cost per user in USD (lifetime budget for rate limiter)
CLAUDE_MAX_COST_PER_USER=10.0

# Maximum cost per individual request in USD (SDK-level hard cap)
CLAUDE_MAX_COST_PER_REQUEST=5.0

# Daily token budget for turns with no USD price (Codex on a subscription);
# same role tiers and owner exemption as the USD caps (see Cost Limits)
UNMETERED_MAX_TOKENS_PER_USER=2500000
UNMETERED_MAX_TOKENS_PER_USER_GUEST=500000

# Allowed Claude tools (comma-separated list; see docs/tools.md for descriptions)
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,LS,Task,TaskOutput,MultiEdit,NotebookRead,NotebookEdit,WebFetch,TodoRead,TodoWrite,WebSearch
```

#### Agent Backend Selection

```bash
# Default backend for agentic text turns: claude or openai_codex
AGENT_BACKEND=claude

# Optional model override for the selected backend. The /model menu is
# populated dynamically by `codex debug models`; the cold fallback is
# openai-codex/gpt-5.6-sol.
AGENT_MODEL=

# Timeout for non-Claude backend requests in seconds
AGENT_TIMEOUT_SECONDS=600

# Local OAuth token store used by /model -> Login OpenAI Codex
OPENAI_CODEX_CREDENTIALS_PATH=data/model_auth/openai_codex.json
```

#### Agent Tools Bridge (Codex)

The Claude path mounts the bot's own tools in-process; a subprocess backend
cannot reach a Python object, so the same tools are served over a loopback
HTTP MCP endpoint (dv-ffcb). The bind address is always `127.0.0.1` and is
deliberately not configurable. Each turn gets its own bearer token, minted
into that child's environment and revoked when the turn returns — there is no
token to configure, and there must never be one (`docs/DECISIONS.md`
2026-09-04).

Which tools the bridge serves is not configured either: it is every tool the
Claude path mounts (`available_tool_specs`, the same per-user filter on both
transports) minus the `NOT_EXPORTED` denylist in `src/agent_bridge/server.py`
— one entry per withheld tool, with its reason. A new tool reaches Codex
without any change to the bridge; only a declared exception keeps one off.

```bash
# Serve the agent tools to subprocess backends. Off = a Codex turn has no
# bot tools at all (memory, food/training log).
ENABLE_AGENT_BRIDGE=true

# Loopback port for the bridge
AGENT_BRIDGE_PORT=8770

# Backstop expiry for a per-turn token that was never revoked (a killed
# turn). Keep above AGENT_TIMEOUT_SECONDS.
AGENT_BRIDGE_TOKEN_TTL_SECONDS=1800
```

#### Memory Evaluator

The evaluator extracts memory ops from conversations. Its model is a separate
setting from the conversation model. Each run reads everything since the last
checkpoint cursor and posts the "Memory: N found — …" report (or "Evaluator
failed: …") to the owner. It runs on exactly these triggers, all in
`src/bot/orchestration/windowing.py` (verified 2026-10-04):

| Trigger | `memory_extractions.session_id` suffix | Condition |
|---|---|---|
| Window roll | `:refresh` | context reaches `WINDOW_REFRESH_THRESHOLD_TOKENS`; also any backend switch |
| Token checkpoint | `:<window_id>` | **off by default** (`WINDOW_EVAL_INTERVAL_TOKENS=0`, 2026-10-04); when >0, context crosses another multiple of it (skipped if the same turn rolls) |
| "Remember" message | `:<window_id>` | user text matches the remember regex («запомни», "remember"); skips the token gate |
| `/new` | `:reset` | conversation text non-empty |

There is no time/idle trigger and no scheduled run: a profile that never
reaches the roll threshold is only evaluated on "remember" or `/new`.
`cumulative_tokens_at_run` is 0 on `:refresh` rows and a flow sum (not the
context level) on the others, so it cannot be compared with the thresholds.

```bash
# Model alias for the evaluator's Claude CLI call (sonnet, haiku, or a full id)
EVALUATOR_MODEL=sonnet

# Model for the extraction call when the active backend is Codex
EVALUATOR_CODEX_MODEL=openai-codex/gpt-6-sol

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

Stripping the harness matters more than the model. A bare `claude -p` in the
bot's working directory inherits that directory's whole Claude Code context:
project `CLAUDE.md`, the skill listing, MCP tool definitions, the agent system
prompt. Measured 2026-08-04, that floor was ~42k tokens on runs that returned
`[]`. Extraction and reconcile calls go through the model router
(`src/model_router/`), which always strips it: no tools, no settings, no MCP
servers, no slash commands. The old `EVALUATOR_LEAN_CONTEXT` switch is gone.

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

#### Recipe search-retry (dv-300d / dv-5d5f)

Mechanical (no-LLM) fix for the chef profile writing recipes from memory:
measured at 78 of 81 historical chef recipe turns with zero
WebSearch/WebFetch calls. After a normal turn is delivered, if the answer
looks like a written-out recipe (`llm_analytics.quality.looks_like_recipe`)
AND the turn made no Web* tool call, the bot silently runs one follow-up turn
in the same profile session asking it to verify with a search, then delivers
that as a second message prefixed `🔁 Попытка 2 из 2 — перепроверил с
поиском:`. Never a third attempt. Off by default — each retry is a real SDK
turn with its own cost.

```bash
# Enable the retry (default off)
RECIPE_RETRY_ENABLED=false

# Comma-separated profile slugs this applies to
RECIPE_RETRY_PROFILES=chef
```

The trigger is deliberately mechanical rather than judge-scored: the dv-300d
validation gate showed a blind LLM judge (no search of its own) scores a
confidently wrong recipe as highly as a correct one, so it cannot be trusted
to decide when a retry is warranted.

In agentic mode, `/model` can switch per user between Claude Code and OpenAI Codex without changing the global default. OpenAI Codex login uses device-code authentication and stores the resulting token JSON with user-only file permissions. The entitled model catalog is refreshed at startup and whenever `/model` opens; unsupported selections fall back to `openai-codex/gpt-5.6-sol`. Current limitation: Codex is wired for the agentic text path; document, photo, and voice workflows still use the Claude integration. Codex sandbox (dv-e38e): profiles with `unrestricted: true` (devops) run `-s danger-full-access`; every other profile runs a codex permission profile that reads everywhere, writes only to its workspace, `extra_dirs` and tmp, and cannot see the data root's `secrets/`, `archive/`, `db/`, credential files, or other users' workspaces (`src/agent_backends/codex_sandbox.py`).

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

# Alert if a user's tokens on an unpriced backend (Codex) in a rolling hour
# exceed this -- those turns have no USD figure to add to the cost metric
WATCHDOG_MAX_TOKENS_PER_HOUR=5000000
```

#### Storage & Database

```bash
# One data root (dv-c66a). Unset = legacy layout: everything flat under the
# checkout's data/ (the defaults below). Set it and every runtime path derives
# from it — explicit per-path variables below still win:
#   <root>/db/          bot.db, data.db, llm_analytics.db, channel_intel.db
#   <root>/workspaces/  per-agent persona.md, memory/, users/, guests/
#   <root>/secrets/     bot.env (0600: TELEGRAM_BOT_TOKEN, API keys, tokens),
#                       model_auth/openai_codex.json
# Secrets (dv-2976) are read into Settings only, never exported: with the
# root set they come from <root>/secrets/bot.env (overrides this file),
# unset from this .env. Either way the bot removes every secret-looking
# name from its os.environ at startup, so agent CLIs never inherit them.
# Check with scripts/agent_env_names.py (prints names only).
# No state/ area (dv-ebde): Codex thread ids + token totals and pinned status
# messages are bot.db tables, consolidation reports go to
# <root>/workspaces/devops/reports/consolidation/, the taskpump counter and
# outbox to <root>/workspaces/devops/taskpump/, and the taskpump tick lock
# is $XDG_RUNTIME_DIR/devvy-taskpump.lock. data/codex_sessions.json is only
# read once at startup (import into bot.db `sessions`), never written.
# Read from the environment or this .env by the bot, the llm_analytics and
# channel_intel daemons and scripts/taskpump (src/utils/paths.py).
# DEVVY_DATA_ROOT=~/Devvybot/data

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

Profile paths: with `DEVVY_DATA_ROOT` set, a relative path resolves under
`<root>/workspaces` (so `coach/persona.md` is that agent's persona) and
`${DEVVY_WORKSPACES}` names the same directory; absolute paths are used as-is
(devops' `working_dir` is the checkout, research's `extra_dirs` are outside).
Unset, relative paths resolve against the config file's directory as before.

Profiles work under both denylist (systemd) and allowlist (container) modes;
each profile's `working_dir` must be reachable in whichever mode is active.
Per-profile keys include `sandbox`, `max_turns`, `max_cost_per_request`,
`permission_policy.allowed_roles`, `family_memory`, `personal_memory`, `mcp`,
`extra_dirs` (extra write roots for a sandboxed profile — a notes vault, another
checkout — instead of `sandbox: false`) and `enabled` (`false` hides a profile
without deleting it). See `config/profiles.example.yaml`.

Owners manage profiles from Telegram: `/agents → ⚙ Manage` toggles `enabled:`
in the YAML and offers the restart (the registry is read once at boot);
`/agents → ➕ New agent` hands you to the `devops` profile, which runs the
`new-agent` skill — a short interview that writes a template under
`data/templates/local/<slug>/`, runs `scripts/install_profiles.py --add`,
validates with `scripts/check_install.py --skip-db`, and offers the restart.

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

**Changing it from Telegram: `/voice`** (owner-only, dv-6b6c). Buttons
ElevenLabs / OpenAI / Mistral / Local Whisper / Только текст. A cloud provider
without a key asks for `/setkey <provider> <PROVIDER>_API_KEY <key>` (e.g.
`/setkey elevenlabs ELEVENLABS_API_KEY …`). The choice is checked with one ~1 s
transcription of silence (`VoiceHandler.probe`), then switched live (no
restart) and written to the config `.env` as `VOICE_PROVIDER` /
`ENABLE_VOICE_MESSAGES`. A rejected key is not kept; the previous provider
keeps running. Keys are resolved by `src/config/voice.py`: the tool key store
first, then the secrets file. Check a provider for real with
`scripts/voice_smoke.py <file.ogg> --provider <p>` (cloud keys from the key
store: prefix `scripts/withkeys <provider> --`).

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
same token env var (from Settings) and hands it to the agent CLI by
environment, referenced as `${CHANNEL_INTEL_TOKEN}`, never in argv. Other profiles do not receive these
tools unless their profile config explicitly points at an MCP seam.

## Cost Limits

Three separate mechanisms, easily confused:

| Limit | Set by | Scope | On hit |
|---|---|---|---|
| Daily rolling budget | `CLAUDE_MAX_COST_PER_USER` / `_GUEST`, per-user `CLAUDE_COST_LIMIT_OVERRIDES` (`id:usd,...`) | per user, 24h in-memory window | request refused before the model runs |
| Per-request SDK cap | `CLAUDE_MAX_COST_PER_REQUEST`, or per-profile `max_cost_per_request` | per turn | **turn aborted mid-run**, partial output labelled as such |
| Turn limit | `CLAUDE_MAX_TURNS`, or per-profile `max_turns` | per turn | turn stopped, labelled |
| Daily unmetered-token budget | `UNMETERED_MAX_TOKENS_PER_USER` / `_GUEST` | per user, same 24h window as the USD budget | request refused when exhausted |

Owners are exempt from the daily budget and the global per-request cap
**unless** an explicit per-user override (daily) or per-profile
`max_cost_per_request` (per-request) names them — the specific setting always
beats the role exemption.

Turns on a backend with no USD price (Codex on a ChatGPT subscription —
`ClaudeResponse.cost is None`, dv-0722) are not free: their
`input_tokens + output_tokens` go to the token budget instead of the USD one,
with the same owner exemption and override precedence. A user is refused when
either ledger is exhausted. The SQLite cost rows record `$0.00` for such turns
(nothing was billed); `/usage` shows the token ledger next to the dollar one.

`/usage` reads the enforced numbers from `RateLimiter.effective_daily_cap` and
`effective_daily_token_cap`, so the readout and the gate cannot drift apart. An aborted turn always produces a
user-visible notice; see `_ABORT_SUBTYPES` in `src/claude/sdk_integration.py`
and `docs/FINDINGS.md` 2026-08-03 for why silence there was a bug.

## Environment-Specific Configuration

`ENVIRONMENT` (set in `.env`; the live bot runs `development` on purpose, see
`docs/DECISIONS.md` 2026-10-04) selects a class in `src/config/environments.py`
whose values `load_config` applies with `setattr` **after** `.env` — they beat
anything the files say. An unknown value logs a warning and applies nothing.
`scripts/show_config.py` marks every value that came from this layer.

| Setting | development | production | What it actually does |
|---|---|---|---|
| `claude_timeout_seconds` | 900 | — (default 300) | Idle timeout (seconds of silence), must stay above the 600s Bash tool ceiling. 300 kills turns with a quiet 5-min tool call. |
| `rate_limit_requests` | 100 | 5 | Per 60s window. 5 blocks normal back-and-forth. |
| `claude_max_cost_per_user` / `_per_request` | default 10 / 5 | 5.0 / 2.0 | Non-owner roles only; owners are uncapped per request. |
| `development_mode` | true | false | Only gates the API server's `/docs` page. |
| `debug` | true | false | Cosmetic (a log line, uvicorn level). |
| `log_level` | DEBUG | INFO | Not read by the bot's logging. Log format and level come from the `--debug` CLI flag only (`src/main.py`); systemd runs without it, so logs are JSON/INFO in every environment. |
| `enable_telemetry` | false | true | Has no consumer — only shown in the features summary. Anthropic CLI telemetry is controlled separately by `DISABLE_TELEMETRY` / `DISABLE_ERROR_REPORTING` in `.env`. |

`testing` (unit tests only): in-memory DB, `/tmp/test_projects`, 30s timeout,
1000 requests/min.

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
