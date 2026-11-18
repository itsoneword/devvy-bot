# Changelog

Release notes for Devvy. Newest first.

## 1.8 — 2026-11-17

### Design pipeline
- Design elements and mockups: images are drawn with FLUX (free, self-hosted
  or hosted queue; retries on gateway errors) with an optional paid
  OpenRouter fallback capped per run (`design.mockups.paid_max_usd`).
- Design map: `MAP.md` is generated after every elements/mockups run:
  screens with their next step, built marks, pump variants and reports; a
  built mark wins over a waiting card.
- A second profile can draw for a board (`design.profiles`); element folders
  carry the author's prefix.
- Overnight design rounds: a critic scores each round and feeds back, and a
  rerun resumes. The critic's rows record the round and variant (migration
  0016).
- `pump2 design run --mockup FILE` builds to a mockup you picked outside the
  run. Mockups read your earlier verdicts and references; when the critic
  rejects every variant, NOT READY comes first. Full-page renders are cut
  into screen-high columns for the collage.
- Review PDF: design pictures go into one PDF (`send_review` tool, automatic
  paths) instead of a stream of photos.

### Pump and jobs
- Analyze decides easily reversible questions with the default and asks only
  about irreversible ones; the 3-open-tasks cap per agent is dropped.
- Job `limit_fallback`: a background job that hits a usage limit reruns once
  on another Claude model.
- A job's card is named by its title in the pin, finish pings and `job_list`.
- Board cards can live in a separate boards repo (`cards_dir`).

### Bot
- The busy notice of a background turn goes to that turn's profile topic, not
  the DM.
- Telegram over IPv4 only (IPv6 connects to api.telegram.org hung ~20 % of
  the time); getFile/download of photos, documents and voice retry; photo and
  document sends survive upload timeouts and retry over a fresh connection,
  and undelivered files are said in chat. `TELEGRAM_FILE_API_URL` points
  media retries at a local Bot API server.
- A failing bot-command menu update no longer aborts startup.
- A document sent in a group topic is saved under that topic's profile.
- `/stop` works in bound reports topics.
- Voice: ElevenLabs retries on connect failures and 429/5xx.
- Web forms: the page opens on load, `networkidle` best effort.
- Incoming photos, voice and documents are kept and retried in the background,
  also after a restart; a failed one can be retried with a tap, and `/stop`
  cancels a download in progress (migration 0013).
- Replies and markdown notifications are sent as Telegram rich messages
  (`sendRichMessage`): real tables, headings and lists. Any error or an
  over-limit message falls back to the HTML path (`ENABLE_RICH_MESSAGES`).
- A Telegram message delivered twice starts only one agent turn.
- `/limits` can render as a Telegram widget in the owner's DM with a Refresh
  button (`LIMITS_WIDGET_ENABLED`); when Telegram refuses the widget it stays
  text.
- Web form tools `webform_fill` / `webform_submit`: a headless browser fills
  a form and the owner approves each submit (`WEBFORMS_BROWSERS_PATH`).
- Devlog channel agent: post drafts with a picture; the owner taps the
  preview to publish (migration 0014).
- GitHub Issues intake: `github_issues` / `github_issue_read` /
  `github_issue_file` tools and `scripts/gh_intake`; pump2 posts status
  comments back to the issue.
- Scheduled jobs reply as the profile that created them, in its topic
  (migration 0015).
- Notify API: card inline buttons; dropped pictures are named in the message.
- Local TUI chat with agents: an internal chat endpoint and a chat pane in
  `scripts/devvy-tui` (chat is no longer Telegram-only).
- Memory: project decisions are stored as links to `docs/DECISIONS.md`;
  per-task decisions go to the task card as comments, not into the memory
  prompt.
- Memory: changing a pinned or core fact refreshes that window's bundle on
  the next turn (migration 0009).

### Jobs and limits
- Fewer false-positive owner asks for background jobs: `systemctl`/`nc` inside
  heredoc text and `rm` inside the job's own tree no longer ask.
- Background-job fast-tier asks: a low-stakes ask unanswered after
  `JOB_FAST_ASK_TIMEOUT_SECONDS` is denied and the job is told nobody
  answered.
- Codex usage limit: no more retries; the limit is recorded and reported.
- Board card author and assignee are set by code; the pump picks up only
  cards assigned to this install.

## 1.7 — 2026-11-09

### Pump v2 replaces the old pump
- A rewrite of the task pipeline (`scripts/pump2`, a timer tick every 2 min):
  analyze → tests → build → review → merge, each stage a fresh background job
  in the card's own git worktree, all state in the card's `pump2:` block.
  The old `scripts/taskpump` is removed.
- The merge needs no model: pump2 brings the integration branch in, runs the
  board's test command under a memory cap and fast-forwards on green. A
  `mergefix` agent stage runs only when that fails. Heavy runs are limited per
  server and per project.
- Custom stages: a board's `pipeline` list adds your own stages (a copywriter,
  a designer) with their own prompt, report and `return_to`; an example prompt
  ships as `config/taskpump/copy.example.md`.
- Fewer questions: minor questions are decided by the board's agent; the owner
  is asked only about money, keys, production, deletion and real product
  choices. Every merge, return and stop is reported on the card.
- Visibility: one pinned summary per board in the agent's chat (stage per card,
  why a held card waits, a clean-run counter); `scripts/pump2 status`.
- Feedback: review remarks carry a criterion and score, stored for
  analysis; a stage's own remarks without a criterion count as advice.
- Design loop for `designloop` cards (`scripts/pump2 design run`): rendered
  variants, a critic, the owner picks one.
- A card's worktree is removed after it merges (never one with uncommitted
  files; branches are kept).
- Docs: `docs/pump.md` (set up from scratch, how it works, writing a card).

### Bot, feedback and memory
- Message reactions: a reaction on an agent reply is stored as a rating of
  that answer (migration 0010).
- Feature feedback survey: score buttons in a Feedback topic of the owner's
  group, `FEEDBACK_*` settings (migration 0011).
- Boot safe mode: the last known good config is kept; a profile with bad
  paths is switched off for that run and says why.
- Restart gate: `/restart` and `restart_bot` run the install preflight first
  and refuse on a failure (`/restart force` skips it).
- `VOICE_ECHO_TRANSCRIPT`: a voice message is answered with its transcript.
- Memory: snapshot caps raised (general and pointer facts 60 → 100, bundle
  facts budget 24k → 40k chars); the sweep no longer closes an old fact that
  the newer one does not fully cover.
- A `monitoring` profile template ships for a separate Monitoring topic.
- `/internal/prompt` accepts `user_id` and `cwd` (the owner's workspace) for
  jobs.

### Menu and configuration
- `/menu` reorganised: a Models screen, a Commands page and a Pump screen with
  a draft answer per pending question; `/status`, `/usage` and `/auth` are off
  the owner's menu.
- New profile key `project_instructions:`: an outside project guide added to
  the shared first-turn bundle.
- `.env.example`, `.env.sample` and `.env.template` are no longer blocked as
  secret files.

### Local mode: the Pump on a laptop, without Telegram (beta)
- `./install.sh --local` (implied on macOS) installs Devvy with no bot token: a
  headless server (`CHANNEL=local`) and a terminal UI, `scripts/devvy-tui`, that
  shows the pump pin, the questions waiting for you (answered verbatim, like
  `pump2 answer`), Approve/Deny for a job's risky action, running jobs with
  their output, and a New task form. Beta: the chat with the
  agents stays Telegram-only for now.
- macOS: two launchd agents (server, 5-minute pump tick), Homebrew fixes in the
  preflight table, background jobs boxed by `sandbox-exec` with two deny-default
  Seatbelt profiles. Run `scripts/seatbelt_probe` once on your Mac; until it
  prints all PASS the box is untested there. No box tool = no background job.
- Jobs run under a portable process launcher where systemd is absent (macOS,
  WSL2 without systemd), with a memory cap reported like a cgroup OOM. Local
  installs run one pump job at a time (`PUMP_MAX_CONCURRENT=1`, ~2 GB a job).
- The job's risky-action ask is answered only through a TUI credential that
  jobs cannot read; the inbox and callback endpoints exist only in local mode.
- Windows: through WSL2 (documented); native Windows is not supported.

### Upgrade
- From 1.5 or 1.6: follow `docs/upgrading.md` (pull, `./install.sh`,
  `scripts/pump2 migrate --apply`, timers, `pump2 doctor`, restart).

### Enable
- Pump v2 on a board: `"pump2": true` in its `config/taskpump/boards.json`
  entry (`scripts/pump2 migrate --apply` adds it, plus `pump2_home` for this
  repo's own board); check it with `scripts/pump2 doctor`.
- The pump2 timer: `systemctl --user enable --now pump2.timer` (install steps:
  `docs/upgrading.md`); `taskpump.timer` is retired.
- Custom stages: a board's `pipeline` list (`docs/pump.md`, Custom stages).
- Local mode: `./install.sh --local` (beta, no bot token).

## 1.6 — 2026-10-28

### Owner menu and limits
- Owner-only `/menu` (no model call) is a four-button hub: **Usage**, **Pump**,
  **Models**, **Settings**. The Pump screen is fast (one status call) and lists
  what waits for you first, with actions per task (run first, urgent, freeze).
  Settings edit the Pump caps, background-job slots and per-stage models as
  `name: value [−] [+]` rows, validated, with a log line per change.
- `/limits` is the outage panel: live Claude subscription usage (5 h / weekly %),
  Codex account limits, whether the Pump is frozen, and a switch that sends
  background jobs to Claude, Codex or OpenRouter. Spend per day and model is
  in Usage / `/usage`.
- `/model` shows short labels, a tick on the current pick and three Codex
  models next to the Claude row. Fable is selectable like Opus and Sonnet.

### Models and routing
- `config/models.yaml` has a `jobs:` table: each background-job purpose (and
  each Pump stage) gets its own harness and model, with the same JSON contract
  on every route. `config/models.example.yaml` documents every key. Codex can
  run background jobs (`codex exec` with a job-local policy endpoint).
- A model-limit hit ends a job with a dedicated exit code; the Pump freezes,
  keeps unfinished work in a WIP commit, and thaws itself when the limit lifts.
  Jobs and tasks refuse to start new model runs while the shared limit record
  is set.
- The "Confirm risky action" explainer runs on Sonnet and sees the task context.

### Task pump
- **Design loop** for UI work: a card labelled `designloop` is never taken by
  the tick; `taskpump design run <board:id>` produces Claude design variants
  at phone width, passes them through a render and checks gate, runs critic
  rounds (Codex, with images) and stops on "ready", three rounds, or the same
  notes twice. The brief comes from the project's own design docs; you pick one
  of the finished variants in chat. `scripts/designloop_calibrate` calibrates the critic.
- Parallel builds: the L-size hold is gone, L tasks build in parallel with the
  rest and conflicts are resolved at merge. The launch cap counts only launches
  in a row that produced no new commit, so a progressing task is not capped.
- Hold alerts, review age in the pin and an hourly "waiting on you" summary
  (`PUMP_REVIEW_HOURS`); a daily per-board digest of what reached production
  and what is merged but not deployed.
- Freeze / urgent lane / thaw (`taskpump freeze|thaw|urgent`), a `pump_first`
  mark that takes the first free slot, titles next to ids in status, pin and
  digest.
- Acceptance: the Pump runs the acceptance command itself before the agent,
  and separates environment failures from a real acceptance failure. Mechanically
  failed stages (tests failing, no report) retry automatically. Jobs run only
  the tests a task needs and keep scratch files out of commits.
- Before review the Pump renders the bot screens a task changed and checks them
  at phone width; the reviewer sees the images.

### Screens, images and tools
- Bot screens have a model: `scripts/screens check|render` truncation-checks
  every handler's text and keyboard at phone width and renders a PNG.
- `view_image` is the only way agents look at images (Claude and Codex, bot
  turns and background jobs); a raw `Read` of an image is blocked.
- `send_image_to_user` works from scheduled turns.
- `restart_bot` restarts the service after the turn's reply is saved; the turn
  is no longer lost. `/restart` no longer gets swallowed by the embedded API
  server's signal handling.

### Memory
- Facts injected per window are settings: `MEMORY_SNAPSHOT_GENERAL_CAP` and
  `MEMORY_POINTER_CAP`, both default 60 (older rows stay reachable via `recall`).
- Task-status chatter ("merged", "started") is no longer stored as a fact.
- A stale-fact sweep runs after each window rotation and closes facts
  superseded by newer ones on the same subject; owner rules and pinned facts
  are never closed.

### Observability and operations
- A 1-minute host and container sampler, and per-source attribution of usage
  (agent / Pump / job / CLI / other) in the Overview.
- Daily `/tmp` sweep timer template (`scripts/tmpsweep`); Pump worktrees and
  test databases no longer leak disk.
- Dependencies upgraded; the policy hook is faster on simple Bash commands.

### Upgrade
- One new schema migration for `tool_calls` (status and input columns) applies on start.
- New optional keys in `config/models.yaml` (`jobs:`); no change needed if unset.

### Enable
- Design loop: label a UI card `designloop`.
- Review age in the pin and the "waiting on you" summary: `PUMP_REVIEW_HOURS`.
- Background-job model routes: a `jobs:` block in `config/models.yaml`.
- /tmp sweep: install `tmpsweep.timer` (`docs/systemd-operations.md`).

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

### Enable
- `/trace` needs nothing: `tool_calls` fills from the first turn on.
- Test memory cap: run suites as `scripts/testrun -- <cmd>`
  (`PUMP_TEST_MEMORY_MAX`).

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
