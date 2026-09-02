# Setup

Getting Devvy running, and turning on the optional pieces afterwards.

The install itself is one command — see [installation.md](installation.md) for the full
walkthrough, prerequisites and troubleshooting. This page picks up at "the bot is
installed" and covers first run, the optional subsystems, and the manual route for people
who would rather not run an installer.

## 1. Install

```bash
git clone https://github.com/itsoneword/Devvy.git
cd Devvy
./install.sh --token <token-from-@BotFather>
```

The installer checks dependencies, waits for you to send `/start` so it can record you as
the owner, writes `.env` and `config/profiles.yaml`, installs the systemd unit, and starts
the service. Re-running it is safe.

## 2. Give it a Claude login

The bot starts without one but cannot answer until it has one. Any of the three:

- **At install time** — pick "log in now" at the Claude login step; the installer runs
  `claude auth login` in your terminal (needs a real TTY).
- **From Telegram** — send `/auth`, pick the backend, follow the buttons. This is also how
  you re-authenticate later, and it works for OpenAI Codex too.
- **By hand** — put `ANTHROPIC_API_KEY=sk-ant-...` in `.env` and
  `sudo systemctl restart claude-bot`.

Check the current state any time with `/auth` in Telegram or `claude auth status` on the
box.

## 3. First run

1. Open your bot in Telegram and send `/start`.
2. Ask it something about your machine or a project.
3. `/status` shows the session, `/usage` shows what has been spent, `/help` lists every
   command you can run.

Logs while you get your bearings:

```bash
journalctl -u claude-bot -f
```

## 4. Add the people you trust

```bash
# in .env
ALLOWED_USERS=111111111,222222222   # owners: full access
GUEST_USERS=333333333               # guests: restricted role, isolated workspace
```

or `/adduser <id>` in Telegram. Ids come from
[@userinfobot](https://t.me/userinfobot). Everyone you add spends **your** LLM quota — read
the trust model in the [README](../README.md#the-money-and-trust-model--read-this-first)
before adding anyone.

Changes to `.env` need a restart: `/restart` in Telegram, or
`sudo systemctl restart claude-bot`.

## Optional subsystems

All of these are off by default. Every variable below is documented in
[`.env.example`](../.env.example) and [configuration.md](configuration.md).

### Profiles

Two are installed (`devops`, `general`); `chef`, `coach`, `pm` and `channel` ship as
templates:

```bash
python3 scripts/install_profiles.py --list
python3 scripts/install_profiles.py --add coach
sudo systemctl restart claude-bot
```

Each profile has its own working directory, memory and session. `/agents` switches between
them, or use the per-profile command (`/general`, `/coach`, …).

### Voice messages

```bash
ENABLE_VOICE_MESSAGES=true
VOICE_PROVIDER=mistral        # mistral | openai | elevenlabs | whisper (local)
MISTRAL_API_KEY=...           # or OPENAI_API_KEY / ELEVENLABS_API_KEY
```

The hosted providers need their extra dependency: `poetry install --extras voice`. The
`whisper` provider runs locally with no API key. `VOICE_TRANSCRIPTION_MODEL` overrides the
per-provider default; `VOICE_VOCABULARY_HINTS` biases recurring proper nouns.

### Webhook server

Receives external events and routes them through the agent.

```bash
ENABLE_API_SERVER=true
API_SERVER_HOST=127.0.0.1     # 0.0.0.0 only behind a reverse proxy
API_SERVER_PORT=8080
NOTIFICATION_CHAT_IDS=111111111
```

**GitHub:** generate a secret with `openssl rand -hex 32`, put it in
`GITHUB_WEBHOOK_SECRET`, and add a webhook in the repository pointing at
`https://your-host/webhooks/github` with content type `application/json` and that secret.
Signatures are verified with HMAC-SHA256; deliveries are deduplicated.

**Anything else:** set `WEBHOOK_API_SECRET` (the installer generates one) and post with a
bearer token:

```bash
curl -X POST http://localhost:8080/webhooks/custom \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $WEBHOOK_API_SECRET" \
  -H "X-Event-Type: deployment" \
  -H "X-Delivery-ID: unique-id-123" \
  -d '{"status": "success", "environment": "production"}'
```

### Scheduler

```bash
ENABLE_SCHEDULER=true
NOTIFICATION_CHAT_IDS=111111111
```

Recurring agent jobs on cron schedules, persisted in SQLite. `/schedules` lists them.

### Project threads

One Telegram topic per project, with strict routing:

```bash
ENABLE_PROJECT_THREADS=true
PROJECT_THREADS_MODE=private          # or: group
PROJECTS_CONFIG_PATH=config/projects.yaml
PROJECT_THREADS_CHAT_ID=-1001234567890  # group mode only
```

In strict group mode only `/start` and `/sync_threads` work outside a mapped topic. In
private mode `/start` syncs the topics in your private chat. Topics must be enabled for
your bot in BotFather (**Bot Settings → Threaded mode**).

### Spending controls

```bash
CLAUDE_MAX_COST_PER_USER=10.0        # rolling 24h cap, owners
CLAUDE_MAX_COST_PER_USER_GUEST=2.0   # ... guests
CLAUDE_COST_LIMIT_OVERRIDES=111111111:100,222222222:50
WATCHDOG_MAX_COST_PER_HOUR=20.0      # alerts you, does not block
```

Caps are enforced against the cost the Claude backend reports. The OpenAI Codex backend
reports no cost, so they do not bite there.

## Advanced: installing by hand

You do not need `install.sh`. It automates these steps and nothing more; doing them
yourself is reasonable if you want the bot under a different process manager, or you are
packaging it.

1. **Dependencies**

   ```bash
   poetry install                  # add --extras voice if you want voice
   ```

2. **Configuration** — copy the example and edit it. Everything except the bot token,
   username and owner list has a working default:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

   ```bash
   TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
   TELEGRAM_BOT_USERNAME=my_devvy_bot
   ALLOWED_USERS=111111111          # your Telegram id; without one the bot refuses to start
   ```

   `.env.example` is generated from `src/config/settings.py`, so it lists every variable
   with its real default. Do not edit it by hand — run `make env-example`.

3. **Profiles** (optional; skip for single-profile mode)

   ```bash
   python3 scripts/install_profiles.py --install devops,general
   # then: PROFILES_CONFIG_PATH=config/profiles.yaml in .env
   ```

4. **Claude login** — `claude auth login`, or `ANTHROPIC_API_KEY` in `.env`.

5. **Check it** — this loads the real settings, creates the workspaces, personas and
   database schema, and tells you what is missing:

   ```bash
   poetry run python scripts/check_install.py
   ```

6. **Run it**

   ```bash
   poetry run python -m src.main
   ```

   For a service, render the unit and sudoers rule for this machine and install them:

   ```bash
   python3 scripts/render_deploy.py --out deploy/rendered --service-name claude-bot
   sudo install -m 644 -o root -g root deploy/rendered/claude-bot.service \
       /etc/systemd/system/claude-bot.service
   sudo install -m 440 -o root -g root deploy/rendered/sudoers-claude-bot \
       /etc/sudoers.d/claude-bot
   sudo visudo -cf /etc/sudoers.d/claude-bot
   sudo systemctl daemon-reload && sudo systemctl enable --now claude-bot
   ```

   `./install.sh --dry-run --token <token>` prints exactly this sequence with your paths
   filled in, which is a good way to check you have not missed anything.

## Troubleshooting

Install-time problems, step by step: [installation.md](installation.md#troubleshooting).
Service problems — logs, restart loops: [systemd-operations.md](systemd-operations.md).

**The bot ignores me.** Your Telegram id is not in `ALLOWED_USERS` or `GUEST_USERS`, or
`.env` changed without a restart. Unauthorised attempts are in the journal.

**It replies that it cannot reach Claude.** No LLM login: `/auth`, or `claude auth status`
on the box. Note the credentials belong to the *service* user, not to you — check with
`sudo -u <service-user> claude auth status` if you are unsure which account the service is
using.

**Permission errors on files.** The bot has exactly the access its service account has.
Either widen that account's access or move the work into a directory it owns. If
`APPROVED_DIRECTORY` is set, everything outside that one directory is refused by design.

## Getting help

- [README](../README.md) — what Devvy is, and the trust model
- [configuration.md](configuration.md) — every environment variable
- [SECURITY.md](../SECURITY.md) — reporting a vulnerability
