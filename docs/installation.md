# Installation

`./install.sh` takes a fresh clone to a running bot. This page is the detail behind it:
what it needs, what it changes on your machine, how to undo it, and what to do when a
step fails.

For the two-minute version, see the [README quick start](../README.md#quick-start).

```bash
git clone https://github.com/itsoneword/Devvy.git
cd Devvy
./install.sh --token <token-from-@BotFather>
```

Run `./install.sh --dry-run --token <token>` first if you want to see every command it
would run without it running any of them.

## Prerequisites

The installer's first step checks all of these and prints a ✔/✘ table with the exact fix
command for anything missing. It offers to run the fixes one at a time; it never installs
anything without a yes. This table mirrors `scripts/preflight.py` — that script is the
authority.

| | Needed | Why | If missing |
|---|---|---|---|
| **Linux** | required | The systemd unit, the sudoers rule and the bubblewrap sandbox are Linux-only | No macOS or Windows path in v1 |
| **systemd** | required | The bot runs as a service; background jobs use `systemd --user` | Use `--no-service` and run `poetry run python -m src.main` yourself |
| **Python ≥ 3.11** | required | The project targets 3.11+ | `sudo apt-get install -y python3 python3-venv python3-pip` |
| **git** | required | The bot updates itself from git | `sudo apt-get install -y git` |
| **Poetry** | required | Dependency install, and the service's `ExecStart` | `curl -sSL https://install.python-poetry.org \| python3 -` |
| **Node ≥ 18** | required | The `claude` CLI is a Node application | `curl -fsSL https://deb.nodesource.com/setup_22.x \| sudo -E bash - && sudo apt-get install -y nodejs` |
| **claude CLI** | required | Every agent turn runs through it | `npm install -g @anthropic-ai/claude-code` |
| **sudo** | required | Installs the unit and the `/restart` sudoers rule | Install the two files by hand as root — `--dry-run` prints them |
| **bubblewrap** | optional | `scripts/bgjob` refuses to start background jobs without it | `sudo apt-get install -y bubblewrap` |
| **visudo** | optional | Validates the sudoers rule before installing it | `sudo apt-get install -y sudo` |

You also need, and the installer cannot get for you:

- A **Telegram bot token** — talk to [@BotFather](https://t.me/botfather), `/newbot`.
- A **Claude subscription or Anthropic API key**. This can wait; see
  [Claude login](#claude-login) below.

## What the installer does

Ten steps, each printed as it runs, with a summary at the end saying which ones actually
succeeded. It does not use `set -e`: a failure at step 9 must not hide that steps 1–8
worked.

| Step | What happens |
|---|---|
| 1 | **Preflight** — the table above |
| 2 | **Token** — validated with Telegram's `getMe`; your bot's username is read from the reply |
| 3 | **`.env`** — seeded from `.env.example`, your values applied, secrets generated, `chmod 600` |
| 4 | **Owner capture** — drains the pending update backlog, waits for your `/start`, confirms the identity, writes `ALLOWED_USERS` |
| 5 | **Profiles** — writes `config/profiles.yaml` with `devops` + `general` |
| 6 | **Dependencies** — `poetry install` |
| 7 | **Verification** — loads the real settings, creates workspaces, personas and the database schema, seeds the owner row |
| 8 | **Claude login** — offers terminal login, an API key, or skip |
| 9 | **Service** — renders and installs the systemd unit and sudoers rule, `daemon-reload`, `enable`, enables user lingering |
| 10 | **Start** — restarts the service and waits until it is genuinely up |

Step 10 is stricter than `systemctl is-active`. The unit is `Restart=always`, so a bot that
crashes on every boot still reads as "active" most of the time. The installer requires
`active` **and** zero restarts **and** the startup banner in the journal since the restart.
If it cannot read the journal (that needs `adm` / `systemd-journal` group membership) it
says so and continues — a missing banner alone is a warning, not a failure.

### Files it touches

Inside the clone:

| Path | |
|---|---|
| `.env` | Created if absent; on a re-run only the values it is explicitly setting change. Always mode 600 — it holds your bot token and possibly your API key. |
| `.env.bak-<timestamp>` | A backup, written only when `.env` actually changes. These contain the token; delete old ones. |
| `config/profiles.yaml` | Created; existing profiles are kept. |
| `workspaces/`, `data/` | Workspaces, memory directories, personas, `bot.db`. Never overwritten. |
| `deploy/rendered/` | The unit and sudoers files rendered for this machine, before installation. |
| `.venv/` (Poetry) | Dependencies. |

Outside the clone:

| Path | |
|---|---|
| `/etc/systemd/system/claude-bot.service` | The service. Name it differently with `--service-name`. |
| `/etc/sudoers.d/claude-bot` | Narrow, command-scoped rule so `/restart` works without a password. Validated with `visudo -cf` before *and* after installation. |
| user lingering | `loginctl enable-linger <you>`, so background jobs survive your logout. |
| `~/.claude/` | Only if you choose to log in during step 8. This is the `claude` CLI's own store, shared with your interactive CLI. |

### What it does **not** touch

- Your shell profile. Nothing is appended to `.bashrc`, `.zshrc` or `.profile` — if Poetry
  lands in `~/.local/bin`, the installer puts it on `PATH` for its own run only.
- Anything global except the two files above. No system Python packages, no `pip install`
  into system site-packages.
- Your data. `--uninstall` removes the service, never the database, memory or workspaces.
- Other systemd units, and other users' anything.

## Claude login

The bot starts without an LLM login; it just cannot answer until it has one. Three ways,
pick any:

1. **During install** — choose "log in now" at step 8. This needs a real terminal, and it
   is the only route that works without any key: `claude auth login` prints a URL, you
   approve it in a browser, and the credentials land in `~/.claude`.
2. **From Telegram** — send `/auth`, press the button for the backend, follow the prompts.
   Also how you re-log in after a token expires. Owner only.
3. **By hand** — `ANTHROPIC_API_KEY=sk-ant-...` in `.env`, then
   `sudo systemctl restart claude-bot`. This bills the API directly rather than using a
   subscription.

## Re-running and uninstalling

`./install.sh` is idempotent. Re-run it after a `git pull`, or to add the systemd unit to
an install you first made with `--no-service`. It re-uses the token already in `.env`,
skips owner capture if `ALLOWED_USERS` is set, keeps existing profiles, and leaves
`.env` alone apart from the values it is setting.

```bash
./install.sh --uninstall
```

stops and disables the service, removes the unit and the sudoers file, reloads systemd —
and then prints everything it deliberately left behind (`.env`, `config/profiles.yaml`,
`data/`, `workspaces/`, `~/.claude`, user lingering) so you can decide what to delete. It
never deletes data.

To remove Devvy entirely afterwards: delete the clone, and — if nothing else on the box
uses the `claude` CLI — `rm -rf ~/.claude`, which logs you out everywhere on that machine.

## Command-line flags

| Flag | |
|---|---|
| `--token <token>` | Bot token. Also read from `$TELEGRAM_BOT_TOKEN`, or prompted for. |
| `--owner-id <id>` | Skip interactive owner capture (headless installs). |
| `--profiles <list>` | Which profiles to install. Default `devops,general`. |
| `--service-name <n>` | systemd unit name. Default `claude-bot`. |
| `--skip-auth` | Do not ask about the Claude login. |
| `--no-service` | Configure everything, install no unit. Run with `poetry run python -m src.main`. |
| `--dry-run` | Print every action, change nothing. |
| `--uninstall` | Remove the service. Deletes no data. |
| `-y`, `--yes` | Answer yes to every confirmation. |

## Troubleshooting

### Preflight failures

Each row below is a ✘ in the preflight table. The installer prints the same fix; this is
the longer version.

**Linux** — v1 is Linux + systemd only. The bot itself is portable Python, but the service
management, the `/restart` sudoers rule and the `bubblewrap` sandbox for background jobs
are not. On another OS you can still run it in the foreground with `--no-service`, but
background jobs and `/restart` will not work.

**systemd** — `systemctl` exists but `/run/systemd/system` does not: you are in a
container, or WSL1, where the client is installed and there is no init to talk to.
`--no-service` and a process manager of your choice.

**Python** — the version *running the installer* is what is checked. If `python3` is 3.10,
installing 3.11 is not enough; make it the `python3` on `PATH`, or invoke the installer's
steps with the newer interpreter.

**Poetry** — after `curl -sSL https://install.python-poetry.org | python3 -`, Poetry is in
`~/.local/bin`, which a non-login shell often does not have on `PATH`. That is the classic
"it installed but is not found". Add it to `PATH` and re-run.

**Node / claude CLI** — `npm install -g` needs a writable global prefix. If it fails with
`EACCES`, either use a Node version manager or set a user-level prefix; do not run `npm -g`
under `sudo` and end up with root-owned files in your home directory.

**bubblewrap** (optional) — without it the bot runs fine, but `job_start` background jobs
refuse to run. Install it if you plan to use them.

**sudo / visudo** (optional) — without `visudo` the sudoers rule cannot be validated before
installation, and a malformed sudoers file can lock you out of `sudo` entirely. The
installer refuses to install an unvalidated rule; install `sudo` (which provides `visudo`)
or place the rule by hand.

### Later steps

**Step 2, "getMe rejected it"** — the token is wrong, revoked, or has whitespace in it.
Get a fresh one from @BotFather with `/token`.

**Step 4 never sees your `/start`** — you must message the bot whose token you just pasted,
in a private chat, from the account you want as owner. If the bot has an old update backlog
the installer discards it first and tells you how many it dropped. Alternatively pass
`--owner-id <your id>`; find it via [@userinfobot](https://t.me/userinfobot).

**Step 6, `poetry install` fails** — on Linux this is usually Poetry's keyring backend
throwing `DBusErrorResponse`. `poetry config keyring.enabled false` and re-run.

**Step 9, sudoers `PermissionError`** — an older install left `/etc/sudoers.d/claude-bot`
mode 0440; current versions unlink before writing. Update, or
`sudo rm /etc/sudoers.d/claude-bot` and re-run.

**Step 10, "the service is failed"** — the installer prints the last 30 journal lines.
Read them first; the usual causes are a missing owner (`ALLOWED_USERS` empty — the bot
refuses to start with no owner and no guest, deliberately) and a bad value in `.env`.

**Step 10, "it is crash-looping"** — the unit restarted at least once during the wait, so
the bot is failing after startup. `journalctl -u claude-bot -n 100`.

**Step 10, "could not confirm the startup banner"** — usually just journal permissions:
`sudo usermod -aG adm,systemd-journal <you>`, then log out and back in. The service is
active and has not restarted, so this is a warning only.

**The bot is running but says nothing** — check that you are messaging from the account
recorded in `ALLOWED_USERS`, and that a Claude login exists (`/auth` in Telegram, or
`claude auth status`).

More on operating the service — logs, restart loops, log rotation — in
[systemd-operations.md](systemd-operations.md).
