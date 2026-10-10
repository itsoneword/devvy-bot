# Changelog

Release notes for Devvy. Newest first.

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
