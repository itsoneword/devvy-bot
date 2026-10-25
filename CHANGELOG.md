# Changelog

Release notes for Devvy. Newest first.

## 1.5 — 2026-10-25

### Tracing
- `tool_calls` records every tool call of a turn as it happens: bot turns,
  background jobs and task-pump runs, subagent calls linked to the call that
  started them, with a short result preview. A turn that is interrupted or
  crashes keeps what it did. Rows are kept 90 days. It replaces `tool_usage`.
- Owner-only `/trace` (and `scripts/trace`) prints a turn's question, answer and
  tool-call tree: `/trace` (newest), `/trace <message_id>`, `/trace call:<id>`
  (jobs, pump), `/trace last [N]`.
- Per-turn activity ledger: when a turn stalls, the log names what it was
  waiting on (API, a hook or MCP request, or the bot not reading its stream).
  An event-loop lag monitor warns when the loop runs late.

### Safety and approvals
- The "Confirm risky action" card now carries an independent plain-language
  explanation of the command and a verdict, from a cheap model that sees only
  the rule reason and the command, never the agent's reasoning. The command is
  treated as untrusted. The verdict is advisory: only your tap approves, and if
  the explainer fails the card is unchanged.
- `sandbox_keys` (profile key-store services injected into a sandboxed
  profile's Bash, redacted from output) now works on the Codex backend too.
- Closed a path where background jobs could leave the bwrap box by starting a
  new systemd unit.

### Task pump
- New stages: **architect** (checks the plan before you see it), **review**
  (a separate job that can send a build back once) and **acceptance** (runs the
  product as a user would). Small tasks skip what they do not need; a task can
  carry `size: S|M|L`, and an L build runs alone on its repo.
- `taskpump start <id>`, and `job_start` with `task_id`, start a task directly.
  Each board runs under its own profile; cards go to that agent's chat.
- Launch holds: `deps:` not yet done, or `start_at:` in the future, keep a task
  from launching. The reason shows in the status pin.
- One pinned live status post (running / needs you / waiting / today).
- A per-task launch cap (`PUMP_TASK_CAP`, 15) replaces the daily cap as the
  runaway guard; a capped task continues on "go" or `taskpump retry <id> --more N`.
  `PUMP_DAILY_CAP` stays as a fuse. `taskpump retry <id>` re-runs a failed step.
- `PUMP_MAX_ACCEPTANCE` (default 1) limits concurrent acceptance runs.
  Question and result cards go to the board's agent chat; reply there to answer.
- Tick interval is 5 minutes. The repo feature map (`docs/features.yaml`) is
  passed to the stages when present.
- `scripts/testrun -- <cmd>` runs tests under their own memory cap
  (`PUMP_TEST_MEMORY_MAX`), so a heavy suite is OOM-killed alone instead of
  taking its job down.

### Telegram reliability
- Telegram network timeouts are retried with backoff; if all retries fail you
  get a "not processed, resend" notice instead of a security warning.
- Progress edits, drafts and tool lines run on a background updater (newest
  wins), so a rate-limited Telegram call can no longer stall the model stream.
  The typing indicator no longer counts against the group rate limit.
- One turn per Telegram album (caption first); text sent while an album is open
  joins it.
- `/stop` cuts a run that is still streaming at the grace deadline, ends the
  CLI's whole process tree, and ignores stale callback errors. Detached
  background jobs are unaffected. The interrupt button always gives feedback.
- A text-only final message counts as the answer, with a 60 s grace for
  trailing events.

### Jobs
- Jobs topic is a log: one batch digest per agent turn goes to the agent's chat,
  and replies in the Jobs topic are answered in the agent chat. Duplicate-job
  guard and clearer `task_id` errors.
- `extra_dirs` count as image roots, and job finish notifications attach images.
  The job stream buffer is 16 MiB like bot turns.
- `scripts/bgjob` lists only real job directories.

### Upgrade
- One new schema migration (`tool_calls`) applies on start.
- `tool_usage` readers now read `tool_calls`.

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
