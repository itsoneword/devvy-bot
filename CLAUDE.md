# CLAUDE.md

Guidance for Claude Code working in this repository.

This file is neutral project documentation. It is loaded by whatever Claude is
working here — the interactive CLI, a subagent, or the bot's own `devops`
profile (whose `working_dir` is this repo). It is **not** the bot's persona:
personas live in each profile's `persona` file and are injected separately by
`PromptComposer`. Keep behaviour and identity out of this file.

> Maintainers: this file is `CLAUDE.public.md` in the private working tree and
> is renamed to `CLAUDE.md` by `scripts/export_public.py`. The private
> `CLAUDE.md` carries workflow that does not ship (board, research docs,
> internal tooling); edit both when the architecture sections drift.

## Project Overview

Telegram bot providing remote access to Claude Code. Python 3.11+, built with
Poetry, using `python-telegram-bot` for Telegram and `claude-agent-sdk` for
Claude Code integration.

## Commands

```bash
make dev              # Install all deps (including dev)
make install          # Production deps only
make run              # Run the bot
make run-debug        # Run with debug logging
make test             # Run tests with coverage
make lint             # Black + isort + flake8 + mypy
make format           # Auto-format with black + isort

# Run a single test
poetry run pytest tests/unit/test_config.py -k test_name -v

# Type checking only
poetry run mypy src
```

## Architecture

### Claude SDK Integration

`ClaudeIntegration` (facade in `src/claude/facade.py`) wraps `ClaudeSDKManager`
(`src/claude/sdk_integration.py`), which uses `claude-agent-sdk` with
`ClaudeSDKClient` for async streaming. Session IDs come from Claude's
`ResultMessage`, not generated locally.

Sessions auto-resume: per user+directory, persisted in SQLite.

### Request Flow

There is one interface — conversational. The pre-agentic "classic" command
interface was deleted; `AGENTIC_MODE` no longer exists.

```
Telegram message -> Security middleware (group -3) -> Auth middleware (group -2)
-> Rate limit (group -1) -> MessageOrchestrator.agentic_text() (group 10)
-> ClaudeIntegration.run_command() -> SDK
-> Response parsed -> Stored in SQLite -> Sent back to Telegram
```

**External triggers** (webhooks, scheduler):

```
Webhook POST /webhooks/{provider} -> Signature verification -> Deduplication
-> Publish WebhookEvent to EventBus -> AgentHandler.handle_webhook()
-> ClaudeIntegration.run_command() -> Publish AgentResponseEvent
-> NotificationService -> Rate-limited Telegram delivery
```

### System Prompt Assembly

`PromptComposer` (`src/claude/prompt_composer.py`) builds the system prompt in
layers: directory constraint -> the profile's persona file -> shared
capabilities -> pinned constraints -> user facts -> family facts.

**CLAUDE.md is deliberately not one of those layers.** `ClaudeAgentOptions`
sets `setting_sources=["project"]`, and the CLI loads `{cwd}/CLAUDE.md` itself
for that source. Reading it into the composed prompt as well duplicates the
entire file in context. The same setting source is what makes `.claude/skills/`
and `.claude/settings.json` visible, so it cannot simply be turned off.

### Dependency Injection

Bot handlers access dependencies via `context.bot_data`:

```python
context.bot_data["auth_manager"]
context.bot_data["claude_integration"]
context.bot_data["storage"]
context.bot_data["security_validator"]
```

### Key Directories

- `src/config/` -- Pydantic Settings v2 config with env detection, feature flags (`features.py`), YAML project loader (`loader.py`)
- `src/bot/middleware/` -- Auth, rate limit, security input validation
- `src/bot/features/` -- File/image/voice handling, server monitor, feature registry, self-modification guard (`self_modify.py`)
- `src/bot/orchestrator.py` -- MessageOrchestrator: the command table (registration + menu + `/help` all derive from it), handler registration, project-topic routing
- `src/bot/orchestration/` -- The orchestrator's method clusters: `turns.py` (text/media pipelines), `commands/` (info, admin, `system.py` = `/restart` + `/sync_threads`), `callbacks.py`, `formatting.py`, `routing.py`, `windowing.py`, `base.py`
- `src/claude/` -- Claude integration facade, SDK/CLI managers, session management, prompt composition, tool monitoring
- `src/projects/` -- Multi-project support: `registry.py` (YAML project config), `thread_manager.py` (Telegram topic sync/routing)
- `src/storage/` -- SQLite via aiosqlite, repository pattern (users, sessions, messages, tool_usage, audit_log, cost_tracking, project_threads)
- `src/channel_intel/` -- Channel place knowledge base: standalone SQLite + repos + `Store` facade behind an HTTP MCP daemon. See `src/channel_intel/README.md`
- `src/security/` -- Multi-provider auth (whitelist + token), input validators, denylist path validator (`denylist.py`), rate limiter, audit logging
- `src/events/` -- EventBus (async pub/sub), event types, AgentHandler, EventSecurityMiddleware
- `src/api/` -- FastAPI webhook server, GitHub HMAC-SHA256 + Bearer token auth
- `src/scheduler/` -- APScheduler cron jobs, persistent storage in SQLite
- `src/notifications/` -- NotificationService, rate-limited Telegram delivery
- `deploy/` -- Systemd unit and sudoers **templates**, rendered by `scripts/render_deploy.py`

### Deployment

Runs as a systemd service (`claude-bot.service`) under an ordinary (non-root)
user account with full user-level filesystem access. `./install.sh` renders and
installs the unit; see `docs/systemd-operations.md`.

- Service control: `sudo systemctl restart|stop|status claude-bot`
- Logs: `journalctl -u claude-bot -f`
- Self-restart: `/restart` in Telegram, or `sudo systemctl restart claude-bot`
- Code changes require a restart; data/config files are read at session start

**Never hardcode a username or an absolute home directory** in `deploy/` or in
tests — `tests/unit/test_render_deploy.py` fails if a template names the
machine it was written on.

### Security Model

Two modes, controlled by `APPROVED_DIRECTORY`:

**Denylist mode** (native service, `APPROVED_DIRECTORY` unset): the bot can
access any path the user can, except sensitive system locations.
`DenylistValidator` (`src/security/denylist.py`) blocks `/etc/shadow`,
`/etc/sudoers`, `/boot/`, `/sys/`, `/dev/`, `/root/`, SSH keys, and certificate
files.

**Allowlist mode** (container/restricted, `APPROVED_DIRECTORY` set): the bot is
locked to one directory.

Both share: authentication (whitelist/token) -> input validation (blocks `..`,
`;`, `&&`, `$()`, …) -> rate limiting (token bucket) -> audit logging.

`ToolMonitor` validates Claude's tool calls against allowlist/disallowlist,
file path boundaries, and dangerous bash patterns.

`SelfModificationGuard` (`src/bot/features/self_modify.py`) classifies edits to
the bot's own files: Tier 0 (external, no gate), Tier 1 (data/config, free
edit), Tier 2 (source/deps, needs approval + restart).

Webhook auth: GitHub HMAC-SHA256 signature verification, generic Bearer token
for other providers, atomic deduplication via the `webhook_events` table.

Two relaxation flags exist for trusted environments only —
`DISABLE_SECURITY_PATTERNS` and `DISABLE_TOOL_VALIDATION`, both default false.

**Trust model:** this is a self-hosted personal/family bot. It assumes the
people it is shared with are not hostile. It is not hardened against an
adversarial user who has been granted access.

### Configuration

Settings load from environment variables via Pydantic Settings. Required:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`.

**`src/config/settings.py` is the source of truth** — grep it rather than
trusting prose. `docs/configuration.md` documents every variable in groups
(access control, Claude, agent backend, cost limits, profiles, project threads,
verbosity, voice, channel KB, agentic platform, webhooks). Feature flags live
in `src/config/features.py`.

### DateTime Convention

All datetimes are timezone-aware UTC: `datetime.now(UTC)`, never
`datetime.utcnow()`. SQLite adapters auto-convert TIMESTAMP/DATETIME columns to
`datetime` objects via `detect_types=PARSE_DECLTYPES`. Model `from_row()`
methods must guard `fromisoformat()` calls with `isinstance(val, str)` checks.

## Code Style

- Black (88 char lines), isort (black profile), flake8, mypy strict, autoflake
- pytest-asyncio with `asyncio_mode = "auto"`
- structlog for all logging (JSON in prod, console in dev)
- Type hints required on all functions (`disallow_untyped_defs = true`)
- `datetime.now(UTC)`, not the deprecated `datetime.utcnow()`

## Adding a New Bot Command

`COMMAND_TABLE` in `src/bot/orchestrator.py` is the **single source of truth**.
Registration, the Telegram menu and `/help` are all derived from it — there is
no second list to keep in sync, and that is deliberate: most listed commands
are unlisted from the menu, so a command missing from `/help` would be
invisible.

1. Write the handler — `orchestration/commands/info.py` (session/status),
   `admin.py` (users), or `system.py` (module-level, process-scoped).
2. Add one `CommandSpec` to `COMMAND_TABLE` and one entry to the handler map
   in `_register_agentic_handlers`. `tests/unit/test_orchestrator.py` fails if
   the two disagree.
   - `menu_roles` — which roles see it in the Telegram menu. Empty = registered
     and working but unlisted (the default for most commands); `/help` still
     shows it. **Table order is menu order** (`setMyCommands` preserves it).
   - `group` — which `/help` section it appears under.
   - `requires` — `"profiles"` or `"project_threads"` to make it conditional.
3. Owner-only? Add the name to `OWNER_ONLY_COMMANDS`. That one frozenset drives
   the runtime gate, the menu filter and the `/help` filter.
4. Add audit logging for the command.

Per-profile switch commands (`/devops`, `/channel`, …) are generated from the
live profile registry, not the table, and `/help` reads them from the same
place filtered by `profile.allows_role(role)`.

## Profiles & Workspaces

The bot runs one or more isolated profiles (`config/profiles.yaml`; unset
`PROFILES_CONFIG_PATH` = single-profile mode). Each has its own `working_dir`,
`memory_dir` and `persona` file, and therefore its own auto-resuming session.
`config/profiles.example.yaml` documents every key, and
`data/templates/profiles/` holds the shipped profile templates a new profile
is instantiated from (`scripts/install_profiles.py --add <name>`).

Profiles are toggled (`enabled:`) from `/agents → ⚙ Manage` and created via
the `new-agent` skill (`.claude/skills/new-agent/`) — an interview run by the
`devops` profile that writes a template under `data/templates/local/<slug>/`,
runs `install_profiles.py --add`, validates with `scripts/check_install.py`,
and offers the restart. A sandboxed profile that must write outside its
workspace gets `extra_dirs:`, never `sandbox: false`.

Configuration reference: `docs/configuration.md`.

## Working in this repo

- **Docs:** `docs/README.md` is the index. `docs/setup.md` for installation,
  `docs/configuration.md` for every environment variable,
  `docs/development.md` for the dev loop, `docs/tools.md` for the tool surface,
  `docs/systemd-operations.md` for running it as a service. When docs and code
  disagree, the code wins — then fix the doc.
- **Concurrency:** several Claude sessions and the live bot may share this
  working tree. Never run `git reset`, `git checkout --`, `git stash` or
  `git stash drop` to tidy up — you will silently destroy another session's
  work. Stage and commit only the files you touched.
- **Verification:** for daemon / MCP / deploy / migration changes, run a real
  end-to-end (restart the actual service against a real DB), not just unit
  tests — fresh-fixture tests miss restart and invocation bugs.
- **No personal data in the tree.** No real usernames, home directories,
  Telegram user ids, tokens or keys — not in `src/`, not in `tests/`, not in
  `docs/`. Use neutral fixtures (`/home/testuser`, `111111111`).
