# Devvy

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

A Telegram front-end for the [Claude Code](https://claude.ai/code) and OpenAI Codex CLIs.
You self-host it on your own Linux box; you and the handful of people you allow talk to it in
Telegram, and it does real work on that machine — reads and edits files, runs commands, keeps
per-person memory, answers from your phone.

It is a **household appliance, not a service**: one owner, one machine, one LLM login, a few
trusted users. Read the next section before you install it.

---

## The money and trust model — read this first

**One LLM account serves the whole installation.** Devvy runs as a single OS user with a single
Claude (or Anthropic API) login. There is no per-user credential: every Telegram user you allow
spends **your** subscription quota or **your** API bill. That is by design, and it is not going
to change — it is what makes "here, use my bot" a one-line action.

**The bot has your filesystem.** It runs as an ordinary service account with that account's
full access (a denylist blocks obvious system paths like `/etc/shadow` and SSH keys; there is an
optional single-directory allowlist mode). Anyone allowed to talk to the bot can, in principle,
get it to read or change anything that account can.

**Spending caps exist, but do not treat them as a wall.** `CLAUDE_MAX_COST_PER_USER` and the
guest tier cap a rolling 24h spend per person, and the watchdog warns you hourly. They are
enforced from the cost the Claude backend reports; the OpenAI Codex backend reports none, so on
that backend the caps do not bite.

**Guests are isolated by convention, not by a sandbox.** A guest gets a restricted role, their
own workspace and their own memory scope. That protects against accident, not against someone
who is trying. Devvy is not hardened against a hostile user who has been let in.

**So: install it for yourself and your household.** Do not hand the username to strangers, and
do not put it in a public channel. See [SECURITY.md](SECURITY.md).

---

## Quick Start

You need a Linux machine with systemd (a small VPS is plenty) and a Telegram bot token from
[@BotFather](https://t.me/botfather). Everything else — Python, Poetry, Node, the `claude` CLI —
is checked by the installer, which prints the exact command for anything missing.

```bash
git clone https://github.com/itsoneword/Devvy.git
cd Devvy
./install.sh --token 1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
```

The installer then walks you through the rest:

1. **Preflight** — a ✔/✘ table of dependencies, and an offer to install the missing ones
   (nothing is installed without you saying yes).
2. **"Send `/start` to @yourbot"** — it waits for your message and records you as the owner.
   Nobody else can use the bot until you add them.
3. **Claude login** — log in right there in the terminal, paste an `ANTHROPIC_API_KEY`, or skip
   and do it later. See below.
4. It writes `.env`, creates `config/profiles.yaml`, installs the systemd unit, starts the
   service and checks that the bot actually came up.

Then message your bot. Re-running `./install.sh` is safe: it never overwrites `.env`, the
database, memory or workspaces.

Useful flags: `--dry-run` (print every action, change nothing), `--owner-id <id>` (headless),
`--no-service`, `--skip-auth`, `--uninstall`, `--help`.

Full detail — prerequisites, what the installer touches, uninstall, troubleshooting keyed to
each preflight failure — is in [docs/installation.md](docs/installation.md).

> Installed **from source only**. Devvy is not on PyPI and there is no `pip install` route.

### Giving the bot an LLM login

Three ways, all equivalent — pick one:

| When | How |
|---|---|
| During install | Choose "log in now"; the installer runs `claude auth login` in your terminal. |
| Later, from Telegram | Send `/auth` and follow the buttons. Works for Claude and Codex. |
| By hand | Put `ANTHROPIC_API_KEY=sk-ant-...` in `.env` and restart the service. |

The bot starts and runs without a login — it just cannot answer until one of the three is done.

## Talking to it

There is one interaction mode: conversation. No command syntax to learn.

```
You: What changed in this repo since Friday?
Bot: 📖 Read  💻 Bash: git log --since=friday
     [summary]

You: Add a retry decorator to the HTTP client and run the tests
Bot: ✏️ Edit: http_client.py
     💻 Bash: poetry run pytest tests/ -v
     [diff + test results]
```

Commands, in the Telegram menu: `/start`, `/new`, `/agents`, `/stop`, `/resume`, `/restart`,
`/help`, `/status`, `/usage`, `/model`, `/tasks`.

Registered but unlisted — type them and they work: `/auth`, `/verbose`, `/memory`, `/repo`,
`/servercheck`, `/schedules`, `/sessions`, `/adduser`, `/rmuser`, `/family`, `/diag`,
`/identity`, `/sync_threads`, plus one switch command per profile (`/devops`, `/general`, …).

`/help` lists exactly what *you* can run, generated from the same table that builds the menu, so
it cannot drift. Owner-only commands (`/restart`, `/auth`, `/adduser`, …) are hidden from guests.

`/verbose 0|1|2` controls how much of Claude's tool use is shown: quiet, normal, or detailed.
`/model` switches between the Claude Code and OpenAI Codex backends; Codex choices come from the account's live entitlement catalog.

## Profiles

A profile is an isolated persona: its own working directory, its own memory, its own
auto-resuming session. Switching profiles switches all three. The installer sets up two:

| Profile | For |
|---|---|
| `devops` | The bot's own repository — deploys, logs, code changes. Owner only. |
| `general` | Everything else: notes, questions, scratch work. |

Four more ship as templates — `chef`, `coach`, `pm`, `channel`:

```bash
python3 scripts/install_profiles.py --list          # what's available
python3 scripts/install_profiles.py --add coach     # instantiate one
sudo systemctl restart claude-bot                   # profiles load at boot
```

Each arrives already isolated and already prompted. See
[`data/templates/profiles/README.md`](data/templates/profiles/README.md) to write your own.

Or do it from Telegram: `/agents → ➕ New agent` hands you to `devops`, which
interviews you (purpose, who may use it, which folders outside its workspace it
may write to) and creates the profile. `/agents → ⚙ Manage` turns profiles on
and off without deleting anything. A profile that needs to write to a notes
vault or another checkout gets `extra_dirs:` — the sandbox stays on.

## Beyond chat

Optional and off by default:

- **Webhooks** — a FastAPI server takes GitHub events (HMAC-SHA256) or generic Bearer-auth
  posts and routes them through the agent (`ENABLE_API_SERVER`).
- **Scheduler** — recurring agent jobs on a cron schedule (`ENABLE_SCHEDULER`).
- **Notifications** — proactive Telegram delivery, rate limited (`NOTIFICATION_CHAT_IDS`).
- **Voice messages** — transcription via Mistral Voxtral, OpenAI Whisper, or local
  faster-whisper (`ENABLE_VOICE_MESSAGES`).
- **Project threads** — one Telegram topic per project, routed strictly
  (`ENABLE_PROJECT_THREADS`).
- **Places knowledge base** — a standalone MCP daemon (hikes, restaurants, hotels) that the
  `channel` profile mounts. Self-contained; see
  [`src/channel_intel/README.md`](src/channel_intel/README.md).

## Configuration

`.env` holds everything. It is generated by the installer, and
[`.env.example`](.env.example) is generated from `src/config/settings.py`, so it always lists
every variable with its real default.

The ones people actually change:

```bash
ALLOWED_USERS=123456789            # owners (full access)
GUEST_USERS=987654321              # guests (restricted role, isolated workspace)
CLAUDE_MAX_COST_PER_USER=10.0      # rolling 24h USD cap per owner
CLAUDE_MAX_COST_PER_USER_GUEST=2.0 # ... and per guest
VERBOSE_LEVEL=1                    # 0 quiet, 1 normal, 2 detailed
APPROVED_DIRECTORY=                # set = lock the bot to one directory
```

Find your Telegram user id by messaging [@userinfobot](https://t.me/userinfobot). Full
reference: [docs/configuration.md](docs/configuration.md).

## Requirements and limits

- **Linux with systemd only.** The bot ships as a systemd unit, self-restarts through a sudoers
  rule, and runs background jobs as transient `systemd --user` units inside a `bubblewrap`
  sandbox. macOS and Docker have none of those three, so supporting them means porting the
  background-job subsystem, not writing a Dockerfile. Not done, not planned for v1.
- Python 3.11+, Node 18+ (the `claude` CLI is a Node app), Poetry, git.
- A Claude subscription or an Anthropic API key.

## Documentation

| | |
|---|---|
| [Installation](docs/installation.md) | Prerequisites, what the installer touches, uninstall, troubleshooting |
| [Setup](docs/setup.md) | First run, optional features, the manual install route |
| [Configuration](docs/configuration.md) | Every environment variable |
| [Tools](docs/tools.md) | Which tools the agent may use, and how to restrict them |
| [Systemd operations](docs/systemd-operations.md) | Logs, restarts, service troubleshooting |
| [Project overview](docs/project-overview.md) | Architecture and request flow |
| [Development](docs/development.md) · [Contributing](CONTRIBUTING.md) | Working on Devvy |

## Development

```bash
poetry install     # dependencies, including dev
make test          # pytest with coverage
make lint          # env-example drift + black + isort + flake8 + mypy
make format        # black + isort
make env-example   # regenerate .env.example after changing settings.py
```

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- [Claude](https://claude.ai) by Anthropic
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
