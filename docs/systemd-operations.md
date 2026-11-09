# Systemd Operations Guide

## Overview

The bot runs as a systemd service under your own (non-root) user account with full filesystem access. Security uses a **denylist** model — everything is accessible except sensitive system paths (shadow files, SSH keys, boot, etc.).

## Quick Reference

```bash
# Status / logs
sudo systemctl status claude-bot
journalctl -u claude-bot -f          # live logs
journalctl -u claude-bot --since "1h ago"

# Control
sudo systemctl restart claude-bot
sudo systemctl stop claude-bot
sudo systemctl start claude-bot

# The bot can also restart itself via /restart in Telegram
```

## Security Model

### Denylist Mode (native service)

When `APPROVED_DIRECTORY` is **not set** in `.env`, the bot uses denylist security:

- **Allowed:** Any path the service user can access
- **Blocked:** `/etc/shadow`, `/etc/sudoers`, `/boot/`, `/sys/`, `/dev/`, `/root/`, SSH keys (`id_rsa`, `id_ed25519`), certificate files (`*.pem`, `*.key`)

This replaces the old allowlist model where the bot was locked to a single directory.

### Allowlist Mode (restricted)

Set `APPROVED_DIRECTORY=/path/to/dir` in `.env` to restrict the bot to one directory. Useful when the bot should only ever touch one project.

## Self-Modification

The bot classifies changes to its own files into tiers:

| Tier | Files | Approval | Restart |
|------|-------|----------|---------|
| 0 | Outside bot root | No | No |
| 1 | `data/`, `config/`, `CLAUDE.md`, `.env` | No | No |
| 2 | `src/`, `tests/`, `pyproject.toml`, `deploy/` | Yes | Yes |

- **Tier 1** files (soul, memory, config) can be edited freely
- **Tier 2** files (source code, deps) need user approval and a restart to take effect

## Installation (Fresh Server)

```bash
./install.sh --token <token-from-@BotFather>
```

Steps 9 and 10 are the ones this page is about: the installer renders the unit and the
sudoers rule for this machine, installs them, enables the service and waits until the bot
is genuinely up. See [installation.md](installation.md).

## Installing the unit by hand

`deploy/` holds only `*.template` files — the unit and sudoers rule are rendered per
machine (service user, repo path, Poetry binary), so there is no ready-made file to copy:

```bash
python3 scripts/render_deploy.py --out deploy/rendered --service-name claude-bot

sudo install -m 644 -o root -g root deploy/rendered/claude-bot.service \
    /etc/systemd/system/claude-bot.service
sudo install -m 440 -o root -g root deploy/rendered/sudoers-claude-bot \
    /etc/sudoers.d/claude-bot
sudo visudo -cf /etc/sudoers.d/claude-bot     # never skip this

sudo systemctl daemon-reload
sudo systemctl enable --now claude-bot
```

`render_deploy.py` validates its own sudoers output with `visudo -cf` before writing it;
validate the installed copy too, since that is the file sudo actually reads.

## .env Changes

```bash
# REMOVE or comment out for denylist mode:
# APPROVED_DIRECTORY=/home/youruser/Devvy

# These are now optional but can still be used:
# DISABLE_SECURITY_PATTERNS=true    # skip shell pattern checks
# DISABLE_TOOL_VALIDATION=true      # skip tool name validation
```

## What Needs a Restart

| Change | Restart needed? |
|--------|----------------|
| Source code (`src/`) | Yes |
| `.env` | Yes |
| `pyproject.toml` / `poetry.lock` | Yes (also run `poetry install` first) |
| `data/soul.md`, memory files | No — read at session start |
| `CLAUDE.md` | No — read at session start |

**Boot check before every planned restart (dv-86f4).** `/restart` and the
`restart_bot` tool first run `scripts/check_install.py --preflight` in a
subprocess, from the repo root with a clean environment (it reads the env file
and secrets file itself): `import src.main`, Settings, the profile registry
with strict path validation, and a read-only `alembic_version` check of all
four DBs (a DB stamped with a revision the code does not know = code older
than the DB). It never creates or migrates a DB. Exit 1 → no restart; the
reply quotes the first failing line and offers a **⚠ Force restart** button
(= `/restart force`, no check). `restart_bot` runs the check again after the
drain and sends the owner the reason if it fails then. The check runs ~3 s.

**owner-actions steps.** A step that edits `config/`, the env file or the
secrets file ends with

```bash
(cd ~/Devvybot/devvy_bot && poetry run python scripts/check_install.py --preflight)
```

so `set -e` stops the script at the step that broke the config, not at the
next `/restart` (incident 2026-10-28: an `extra_dirs` entry added before the
dir existed crash-looped the bot for 12 min).

**Crash-loop alert (dv-86f4).** The drop-in
`deploy/claude-bot-crashwatch.conf.template` adds
`ExecStopPost=-<repo>/deploy/crashwatch.sh`. After every stop it reads
systemd's `SERVICE_RESULT`: `success` (clean stop, `/restart`) clears the
count; anything else appends the time to `<data root>/crashwatch/fails`. Three
crashes within 120 s send one message
«🔴 claude-bot падает: … Последняя ошибка: <last error line of the journal>»
through `deploy/notify-fallback.sh` (agentctl notify, else a direct Bot API send
with `agents/server-monitor/.env`; `ssh-login-alert.sh` uses the same sender),
and stamp `crashwatch/alerted`. A boot that stays up 60 s after polling starts
sends «✅ Бот снова работает после N падений подряд» and clears `fails` and
`alerted`. Every threshold hit either switches to safe mode (below) or stops
the bot (`lkg/HALT`), so there is one alert per loop and no quiet window.
There is no systemd restart backoff (see `docs/FINDINGS.md` 2026-10-31).
Install once (owner step, sudo; idempotent, no restart):

```bash
sudo bash ~/Devvybot/devvy_bot/deploy/crashwatch/install.sh
```

**Safe mode (dv-86f4).** A boot that has run 10 min with polling up, and whose
restart preflight then passes, copies the live `profiles.yaml` / `models.yaml`
to `<data root>/lkg/` with `meta.json` (time, git sha) (`src/boot/safemode.py`).
At the crash-loop threshold, with that snapshot present, crashwatch touches
`lkg/USE` and `lkg/tried` and says «🛟 Включаю safe mode» in the alert. The next boot loads the snapshot config, keeps
only the default profile (devops), owner-only, turns every other agent off for
the run, and sends one «🛟 Safe mode: …» message: snapshot date and sha, the
agents it turned off, and how devops differs from the live file (or that the
live file does not parse). Config only, never old code: old code cannot open a
DB already migrated forward. To leave: fix the live config and `/restart`;
a passing check removes `USE` (a forced restart does not). If safe mode crashes
3 times too, crashwatch sends «safe mode failed», touches `lkg/HALT`, and the
drop-in's `ExecCondition=` (`crashwatch.sh condition`) skips every further
start: the bot stays down, no endless restarts. The same stop happens when a
loop hits the threshold and safe mode cannot be entered (no snapshot yet, e.g.
in the first 10 min after a boot or while a profile is degraded so the strict
check fails; or safe mode was already tried): «нет последнего рабочего
конфига — бот остановлен». After fixing, from a shell:

```bash
bash ~/Devvybot/devvy_bot/deploy/crashwatch.sh reset && sudo systemctl start claude-bot
```

**Core subset (dv-5206).** Before every restart run `make test-core` (~1 min:
window roll, evaluator, items, policy parity, export drift; the list is
`CORE_TESTS` in `tests/conftest.py`). Restart only if it passes; a failure is
fixed in the session that sees it or gets a line in dv-5206.

**Backend parity gate (dv-b7c4).** Before restarting with backend, sandbox or
model changes (`src/agent_backends/`, `src/claude/sdk_integration.py`,
`src/agent_bridge/`, `src/security/`, profile sandbox flags, a claude/codex
CLI upgrade), run both layers and restart only if both pass:

```bash
poetry run pytest tests/unit/test_backend_conformance.py   # launch config per backend x profile
poetry run python scripts/backend_smoke.py                 # one real cheap turn per cell, temp data root
```

The smoke prints a PASS/FAIL table (bot tool recorded success=1 and actually
delivered; denied path stays denied) and exits non-zero on any FAIL.

## Background Jobs

Everything that runs without a user message (inventory 2026-10-04; re-check
with `systemctl list-timers --all`, `crontab -l`, and active `scheduled_jobs`
rows before trusting it).

| Job | Where | When | Model call |
|---|---|---|---|
| Memory evaluator | `src/bot/orchestration/windowing.py` | window roll, token checkpoint, "remember", `/new` — see `configuration.md` § Memory Evaluator | `claude` CLI (`EVALUATOR_MODEL`), codex CLI on the codex backend |
| Memory consolidation report | `src/main.py` APScheduler | `0 6 * * 1` + debounced pressure-valve runs | `claude` CLI |
| Fact-quality check | `src/evaluator/fact_quality.py` | `0 7 * * *`, off unless `FACT_QUALITY_CHECK_ENABLED` | `claude` CLI, haiku |
| Reminders / items | `scheduled_jobs` table, `src/scheduler/` | per row | `deliver=agent`: an agent turn; `message`: none |
| Pump v2 | user `pump2.timer` → `scripts/pump2 tick` | every 2 min; only boards with `"pump2": true` in `boards.json` (docs/pump.md) | tick: none; spawns stage jobs, then runs `scripts/release tick` (public-release check / reminders, best-effort). Install: `python3 scripts/render_deploy.py && cp deploy/rendered/pump2.{service,timer} ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now pump2.timer` |
| /tmp sweep | user `tmpsweep.timer` → `scripts/tmpsweep --notify` | every 15 min; sweeps only when /tmp >70% or last sweep ≥23h | none |
| Your own host jobs (backups, monitors) | user crontab or systemd timers | as you set them | outside the bot |

`src/evaluator/refresh.py` (`ContextRefreshService`) is constructed in
`main.py` but never called.

### /tmp sweep (dv-1a02)

/tmp is tmpfs (RAM, user quota); when it fills, the CLI fails with EDQUOT.
`scripts/tmpsweep` removes our top-level entries with nothing modified for
12h. It keeps sockets, `*.lock`, `codex-*`, anything a live process of ours
has open or as cwd, and sweeps only the children of `pytest-of-<user>`,
`claude-<uid>/<slug>/` and `claude_bot_files`. One devops line when >500 MB
was freed or /tmp is still >70% (that alert at most every 6h). Background
jobs get `TMPDIR=<job dir>/tmp` (`scripts/bgjob`), removed when run.sh ends.

Install (read `~/devops/SECURITY.md` first), in the live checkout:

```bash
python3 scripts/render_deploy.py
cp deploy/rendered/tmpsweep.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now tmpsweep.timer
scripts/tmpsweep --dry-run          # eyeball what would go
python3 scripts/intrusion_watch.py --accept
~/devops/security-audit.sh quick
```

## Troubleshooting

```bash
# Check if service is running
sudo systemctl status claude-bot

# Check recent logs for errors
journalctl -u claude-bot --since "5m ago" --no-pager

# Check if the right Python/Poetry is found
sudo -u "$USER" "$HOME/.local/bin/poetry" run python -c "print('ok')"

# Restart after code changes
sudo systemctl restart claude-bot
```

### "ModuleNotFoundError: No module named 'structlog'"

The Poetry virtualenv is missing dependencies. Fix:

```bash
cd ~/Devvy
poetry install
sudo systemctl restart claude-bot
```

This can happen after worktree operations or Poetry cache changes.

### Another process is answering instead of the service

Only one process can poll a Telegram bot token. If you left the bot running by hand
(`poetry run python -m src.main`, a tmux session, a second machine), it grabs the updates
first and the service looks dead while the bot appears alive:

```bash
pgrep -af "src.main"          # find the stray process
sudo systemctl restart claude-bot
```

### Working directory is None

If logs show `working_directory: None` errors, ensure `APPROVED_DIRECTORY` is either commented out (denylist mode, defaults to `$HOME`) or set to a valid path (allowlist mode).
