# Changelog

Release notes for Devvy. Newest first.

## 1.4 — 2026-10-19

### Models and cost
- OpenRouter as a per-profile provider on the Claude harness (token-priced,
  `/model` limited to the profile's `allowed_models`). A per-request cost cap
  stops a turn that runs past its budget; costs are reconciled daily against
  OpenRouter's own numbers. Price table: `scripts/gen_openrouter_pricing.py`.
- Router `decide()` call kind: typed yes/no questions answered by TypeSafe Jev
  (optional, `TYPESAFE_API_KEY`), with a Haiku fallback.
- Memory: optional Jev gate before the reconciler (`RECONCILE_JEV_GATE_ENABLED`,
  off) and a Jev fact screen that only runs beside the normal extraction
  (`MEMORY_SCREEN_MODE=shadow`, writes nothing; `off` disables it).
  `scripts/screen_shadow_report.py` reads the results. No key = no effect.
- `CONFIRMATION_TIMEOUT_SECONDS`: one setting for how long an approval prompt
  waits; related hook and idle timeouts derive from it.

### Topics and routing
- Reports group: report-type notifications go to a forum group's topics by
  source, and messages (text, voice, photo, document) in a bound topic talk to
  that agent in the same conversation.
- Jobs topic: reply to a job notification and the answer reaches the agent that
  ran the job. Other messages in the topic get a short, throttled hint.
- Non-DM turns carry a `[via: group › topic]` source tag.
- **Removed:** the legacy project topics (`/sync_threads`, `PROJECT_THREADS_*`);
  the reports topics are the one topic mechanism.
- Per-(user, agent) turn locks: `/stop` stops the current chat's agent only,
  `/stop all` stops everything. The busy notice says who holds the turn.
- Scheduled replies, task-pump and notify messages are rendered as markdown.

### Jobs and tasks
- Task pump ships (opt-in, timer not enabled by the installer): analyse, ask,
  design tests, implement on a branch; answers come via chat. Other boards go
  in your own `config/taskpump/boards.json`. README documents it and deviz
  (the `.deviz/tasks/*.md` format and Claude Code plugin).
- Queued-message button interrupts the current turn and runs that message next.
- Per-turn Claude cost is recorded (delta of the session total).
- Jobs topic replies also work with voice, photo and document.
- `scripts/bgjob run --cmd "<command>"` (and `job_start` with `command`) runs a
  plain command detached, with a ✅/❌ ping carrying exit code and test verdict.
  Cheaper job context: `task_id`, a `summary.md` per job, compact `job_list`.
- Agents can comment on any open board task (marked "from <agent>", 10 per day,
  treated as untrusted input by the task pump).

### Security
- Sandboxed profiles run Bash inside a bwrap box (workspace + `extra_dirs`
  writable, `sandbox_read_dirs` read-only, no `~/.claude` or `data/`).
- The bot token is redacted from file-download URLs in logs.

### Analytics
- LLM Analytics: per-call context growth (tool-result sizes, window breakdown)
  and a 24h/7d/30d/all or from–to date filter on the chain view.

### Upgrade
- Four new schema migrations apply on start. If you used project topics,
  re-bind those topics as reports topics before updating.

## 1.3 — 2026-10-12

### First-run onboarding
- A fresh install is now configured through a short conversation in Telegram
  instead of hand-editing files. Until it is done, every owner message goes to
  the onboarding interview; `/start` offers a "Настроить" button.
- Step 0 sets up voice input (speech-to-text only): local Whisper or a cloud
  provider, via `/voice`.
- At most 8 questions, every one skippable. Pick a scenario (developer/server,
  personal assistant/family, food and sport) or just describe what you want
  in free text or voice; the bot shows a summary and waits for confirmation.
- Results go to the existing places: tone and identity to the profile
  persona, facts about you to memory, your timezone to settings, chosen
  agents to `config/profiles.yaml` (with a Restart button to activate them).
- `/setup` reruns it at any time; a tuned persona is backed up first.
- Family members and guests inherit the owner's setup.
- Release templates are neutral and carry an anti-sycophancy baseline; the old
  `soul.md` / `user.md` templates are gone.

### Keys for your tools
- `/setkey <service> NAME value`, `/keys`, `/delkey`: store API keys for tools
  the agents run. The message with the key is deleted before the model sees
  it; `scripts/withkeys <service> -- <command>` runs a tool with those keys
  in its environment and redacts their values from all output.

### Installer
- Missing Poetry / claude CLI are offered and installed again (the offer was
  silently skipped before).
- The claude CLI is installed with Anthropic's native installer, which needs
  no root (`npm install -g` failed for non-root users).
- Running the installer after `sudo -iu <user>` no longer renders the service
  for the previous account.

### Fixes
- Confirmation buttons show their result as a toast, and previews with
  special characters no longer swallow the feedback.
- Background jobs resolve profiles through the registry and get the profile's
  `extra_dirs`.

## 1.2 — 2026-10-10

### Database: SQLAlchemy + Alembic
- The schema of all four SQLite databases (`bot.db`, `data.db`,
  `llm_analytics.db`, `channel_intel.db`) is now declared as SQLAlchemy models
  and versioned with Alembic. Queries stay plain SQL.
- Migrations run automatically at startup. A WAL-safe backup of each database
  is written to `data/db/backups/` before any upgrade (last 3 kept).
- Existing installs upgrade in place: on first boot the old migration chain is
  brought up to date, the schema is verified, and the database is stamped at
  the Alembic baseline. No manual step.
- Full-text search tables, their triggers and views are kept as raw-SQL
  revisions. See `docs/database-migrations.md` for how to change the schema.
- `scripts/schema_diff.py` compares two databases structurally.
- The hand-rolled connection pool is replaced by SQLAlchemy's pool: an
  unfinished transaction is rolled back when a connection is returned, the
  number of connections is bounded, and dead connections are replaced.
- Raw SQL outside the storage layer was moved into repositories; a test keeps
  it that way.

### Conversations and models
- Switching the model or backend (Claude Code <-> Codex) marks the switch in
  the conversation, so the new model knows which earlier replies it did not
  write.
- A backend switch no longer starts a new context window: the new backend's
  first turn is rebuilt from the full stored conversation of the current
  window, with the tools used per turn. The window limit is unchanged.
- Sessions no longer expire on a timer; resume failures are classified and
  shown in the reply footer together with the window id.
- One model router for every model call, with a per-call record of model,
  tokens and cost.

### Backends
- Codex backend parity: turns are persisted, the bot's tools are available,
  the destructive-command confirmation gate and denylist apply, and the
  evaluator works on Codex.
- One tool-call policy (allow / deny / ask) shared by both backends.
- Codex can be pointed at OpenRouter or another OpenAI-compatible provider.
- A backend conformance suite and a live smoke test (`scripts/backend_smoke.py`)
  every backend and profile type must pass.
- Per-user cost caps now apply on every backend.
- Evaluator and consolidation can run on a cheaper model.

### Agents and features
- Sandboxed profiles can get extra writable directories (`extra_dirs`).
- Reminders can be scheduled for another family member, and are scheduled in
  the user's time zone, not UTC.
- Recipe answers written without a web search are re-checked against sources;
  a confirmation or correction now edits the original message instead of
  sending a second one.
- Answer-quality metrics in the analytics dashboard.

### Security and storage
- All data lives under one data root (`DEVVY_DATA_ROOT`), with a migration
  script (`scripts/migrate_data_root.py`) for older layouts.
- Bot secrets are scrubbed from the environment agents run in.
- The scheduler checks authorization: users can only see and change their own
  jobs.
- A confirmation-gate bypass via CLI allow rules was closed.

### Fixes
- Failed or denied tool calls were recorded as successful.
- `send_image_to_user` was broken by a stale path in `mcp.json`.
- Notification delivery crashed on some emoji/Unicode input.
- One-command install script rewritten to be idempotent; README and setup docs
  regenerated.

## 1.1 — 2026-09-09
- Agents can be managed and created from Telegram.
- Codex backend parity.

## 1.0 — 2026-09-02
- Telegram front-end for Claude Code / Codex, one-command Linux install.
