# Contributing to Devvy

Devvy is a self-hosted Telegram front-end for the Claude Code and OpenAI Codex CLIs. It is
built and maintained for a small number of real installations, so the bar for a change is
"does it work on someone's box", not "does it look good in a diff".

Before you start on anything substantial, open an issue. A lot of things that look missing
are deliberate — see the trust model in the [README](README.md) and the architecture notes
in `CLAUDE.md`.

## Development setup

```bash
git clone https://github.com/itsoneword/Devvy.git
cd Devvy
poetry install          # dependencies, including dev tools
cp .env.example .env    # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME, ALLOWED_USERS
make test
make lint
```

You need Python 3.11+, Poetry and git. To actually run the bot you also need Node and the
`claude` CLI — see [docs/installation.md](docs/installation.md).

> **Poetry on Linux:** if `poetry install` fails with `DBusErrorResponse` /
> `ItemNotFoundException`, that is Poetry's keyring backend. `poetry config
> keyring.enabled false`, then try again.

Run the bot from a checkout without touching your installed service:

```bash
poetry run python -m src.main          # or: make run-debug
```

## The loop

```bash
make format    # black + isort
make lint      # .env.example drift check + black + isort + flake8 + mypy
make test      # pytest with coverage
```

CI runs the same three. A PR that fails them will not be reviewed until it passes.

## House rules

- **Type hints on everything.** `mypy` runs with `disallow_untyped_defs = true`.
- **Black, 88 columns**, isort with the black profile. Do not hand-format.
- **structlog, not `print`** — `logger.info("thing happened", user_id=123)`.
- **Timezone-aware UTC datetimes**: `datetime.now(UTC)`, never `datetime.utcnow()`.
- **Custom exceptions** from `src/exceptions.py`, not bare `Exception`.
- **Tests with the change**, in `tests/unit/` (or `tests/integration/`). New behaviour
  without a test is not done.
- **No personal data in the tree** — no home paths, no Telegram user ids, no names, not
  even in test fixtures. `scripts/scan_public_leaks.py` and
  `tests/unit/test_scripts/test_export_public.py` enforce this.

### Configuration changes

`src/config/settings.py` is the source of truth. After adding or changing a field:

```bash
make env-example    # regenerates .env.example; commit the result
```

`.env.example` is a generated file. Editing it by hand fails `make lint` and CI.

### Adding a bot command

`COMMAND_TABLE` in `src/bot/orchestrator.py` is the single source of truth: registration,
the Telegram menu and `/help` all derive from it. Add one `CommandSpec` and one entry in
the handler map — `tests/unit/test_orchestrator.py` fails if the two disagree. Owner-only
commands go in `OWNER_ONLY_COMMANDS`.

## Pull requests

- One change per PR, with a clear description of what and why.
- Reference the issue it closes.
- Conventional commit subjects: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.
- Say how you verified it. For anything touching the installer, systemd, MCP daemons or
  migrations, "unit tests pass" is not verification — run it end to end and say so.

## Security

Do not open a public issue for a vulnerability. Use GitHub's private advisory flow — see
[SECURITY.md](SECURITY.md).

Devvy runs commands as its service user by design, so "the bot can read a file" is not a
vulnerability. What matters: a user escaping their role or profile, a guest reaching
another person's data, a path that bypasses `SecurityValidator` or `ToolMonitor`, or a
credential leaking into logs or a shipped file.

## Scope

Things that will be declined unless the conversation happens first:

- **Docker or macOS support.** Background jobs need `bubblewrap` and transient
  `systemd --user` units; a container or launchd path means porting that subsystem, not
  adding a file. Linux + systemd is the supported target.
- **Per-user LLM credentials.** One installation, one account, one bill — see the README.
- **Anything that makes the bot safe for strangers.** It is a household appliance. The
  answer to "what if an untrusted user…" is "do not give an untrusted user access".
