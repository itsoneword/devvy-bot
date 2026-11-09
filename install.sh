#!/usr/bin/env bash
#
# install.sh -- one command from a fresh clone to a running Devvy bot.
#
#   git clone <repo> && cd devvy_bot && ./install.sh
#
# The Telegram bot token is the only thing you must supply. Everything else --
# owner identity, .env, profiles, workspaces, personas, database, systemd unit,
# sudoers rule -- is set up here, and every step is safe to run again.
#
# Linux + systemd for the Telegram bot. `--local` (dv-fe4d, beta) is the
# laptop install instead: no Telegram, a headless server + the Pump TUI, and on
# macOS two launchd agents (server, 2-minute pump tick). See --help.
#
# ---------------------------------------------------------------------------
# Why there is no `set -e`
#
# The previous version ran `set -euo pipefail` and then a sequence of `sudo cp`
# / `systemctl` calls. Any failure aborted mid-way with no output and left
# systemd half-configured -- a unit file installed but not enabled, or enabled
# but pointing at a .env that was never written. This script checks every step
# explicitly, records what happened, and prints a summary of what actually
# succeeded, so a partial install is legible instead of silent.
# `set -u` and `pipefail` stay: those catch typos, not runtime failures.
# ---------------------------------------------------------------------------
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

SERVICE_NAME="claude-bot"
ENV_FILE="$SCRIPT_DIR/.env"
PROFILES_CONFIG="$SCRIPT_DIR/config/profiles.yaml"
RENDERED_DIR="$SCRIPT_DIR/deploy/rendered"
DEFAULT_PROFILES="devops,general"

BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
BOT_USERNAME=""
OWNER_ID=""
PROFILES="$DEFAULT_PROFILES"
DRY_RUN=0
DO_UNINSTALL=0
ASSUME_YES=0
SKIP_AUTH=0
SKIP_SERVICE=0
LOCAL=0
FAILED=0

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD=$'\033[1m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_DIM=""; C_OFF=""
fi

say()  { printf '%s\n' "$*"; }
info() { printf '     %s\n' "$*"; }
warn() { printf '%s\n' "${C_YELLOW}  !  $*${C_OFF}" >&2; }
err()  { printf '%s\n' "${C_RED}  ✘  $*${C_OFF}" >&2; }
step() { printf '\n%s\n' "${C_BOLD}==> $*${C_OFF}"; }

# Summary lines: "status<TAB>step<TAB>detail". Printed by print_summary on exit,
# including on failure -- knowing which of ten steps completed is the whole
# point of not using `set -e`.
SUMMARY=()
record()  { SUMMARY+=("$1	$2	${3:-}"); }
ok()      { printf '%s\n' "${C_GREEN}  ✔  $1${C_OFF}${2:+  ${C_DIM}$2${C_OFF}}"
            record ok "$1" "${2:-}"; }
skipped() { printf '%s\n' "${C_DIM}  -  $1 (skipped${2:+: $2})${C_OFF}"
            record skip "$1" "${2:-}"; }
failed()  { err "$1${2:+ -- $2}"; record fail "$1" "${2:-}"; FAILED=1; }

print_summary() {
    [ "${#SUMMARY[@]}" -eq 0 ] && return 0
    printf '\n%s\n' "${C_BOLD}Summary${C_OFF}"
    local line status name detail mark
    for line in "${SUMMARY[@]}"; do
        IFS=$'\t' read -r status name detail <<<"$line"
        case "$status" in
            ok)   mark="${C_GREEN}✔${C_OFF}" ;;
            skip) mark="${C_DIM}-${C_OFF}" ;;
            *)    mark="${C_RED}✘${C_OFF}" ;;
        esac
        printf '  %s %s%s\n' "$mark" "$name" "${detail:+  ($detail)}"
    done
}
trap print_summary EXIT

# run <cmd...> -- honours --dry-run. Returns the command's exit status.
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '%s\n' "${C_DIM}     would run: $*${C_OFF}"
        return 0
    fi
    printf '%s\n' "${C_DIM}     + $*${C_OFF}"
    "$@"
}

# run_sh <string> -- same, for a command that needs a shell (pipes, &&).
run_sh() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '%s\n' "${C_DIM}     would run: $1${C_OFF}"
        return 0
    fi
    printf '%s\n' "${C_DIM}     + $1${C_OFF}"
    bash -c "$1"
}

# confirm <question> -- default no. --yes and a missing TTY both answer for you.
confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    local reply
    read -r -p "     $1 [y/N] " reply
    [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

usage() {
    cat <<'USAGE'
Usage: ./install.sh [options]

  --local              Local mode (beta): no Telegram. A headless server and
                       the Pump TUI (scripts/devvy-tui); on macOS launchd
                       runs the server and the 2-minute pump tick. The
                       default (and only) mode on macOS.
  --token <token>      Telegram bot token from @BotFather.
                       Also read from $TELEGRAM_BOT_TOKEN, or prompted for.
  --owner-id <id>      Your Telegram user id. Skips the interactive owner
                       capture (headless installs).
  --profiles <list>    Profiles to install (default: devops,general).
                       Others ship as templates: see
                       python3 scripts/install_profiles.py --list
  --service-name <n>   systemd unit name (default: claude-bot).
  --skip-auth          Do not ask about Claude/Anthropic login. Finish later
                       with claude auth login (local) or /auth (Telegram).
  --no-service         Configure everything, install no systemd unit. Run the
                       bot by hand with: poetry run python -m src.main
  --dry-run            Print every action, change nothing.
  --uninstall          Stop and remove the service, unit and sudoers rule.
                       Deletes no data -- it tells you what it left behind.
  -y, --yes            Answer yes to every confirmation (non-interactive).
  -h, --help           This text.

Re-running is safe. .env, the database, memory and workspaces are never
overwritten; existing files are backed up before they are changed.
USAGE
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --token)          BOT_TOKEN="${2:-}"; shift 2 ;;
        --token=*)        BOT_TOKEN="${1#*=}"; shift ;;
        --owner-id)       OWNER_ID="${2:-}"; shift 2 ;;
        --owner-id=*)     OWNER_ID="${1#*=}"; shift ;;
        --profiles)       PROFILES="${2:-}"; shift 2 ;;
        --profiles=*)     PROFILES="${1#*=}"; shift ;;
        --service-name)   SERVICE_NAME="${2:-}"; shift 2 ;;
        --service-name=*) SERVICE_NAME="${1#*=}"; shift ;;
        --skip-auth)      SKIP_AUTH=1; shift ;;
        --no-service)     SKIP_SERVICE=1; shift ;;
        --local)          LOCAL=1; shift ;;
        --dry-run)        DRY_RUN=1; shift ;;
        --uninstall)      DO_UNINSTALL=1; shift ;;
        -y|--yes)         ASSUME_YES=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *) err "Unknown option: $1 (try --help)"; exit 2 ;;
    esac
done

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
SUDOERS_PATH="/etc/sudoers.d/${SERVICE_NAME}"

# Darwin / Linux / WSL (WSL = WSL2 without systemd). DEVVY_PLATFORM overrides,
# the same variable scripts/preflight.py reads, so the Mac path is testable.
PLATFORM="${DEVVY_PLATFORM:-}"
if [ -z "$PLATFORM" ]; then
    case "$(uname -s)" in
        Darwin) PLATFORM=Darwin ;;
        *)  if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null &&
               [ ! -d /run/systemd/system ]; then
                PLATFORM=WSL
            else
                PLATFORM=Linux
            fi ;;
    esac
fi
# The Telegram service install needs systemd; a Mac gets local mode only.
[ "$PLATFORM" = Darwin ] && LOCAL=1

# Local mode (dv-fe4d): launchd agents on macOS.
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LAUNCHD_AGENTS="server pump2"
# One pump job at a time on a laptop: an implement job peaks around 2 GB.
LOCAL_PUMP_MAX=1
LOCAL_API_PORT=8080

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
uninstall() {
    say "${C_BOLD}Removing the ${SERVICE_NAME} service${C_OFF}"
    say ""
    if command -v systemctl >/dev/null 2>&1; then
        run sudo systemctl stop "$SERVICE_NAME" 2>/dev/null
        run sudo systemctl disable "$SERVICE_NAME" 2>/dev/null
    fi
    [ -f "$UNIT_PATH" ]    && run sudo rm -f "$UNIT_PATH"
    [ -f "$SUDOERS_PATH" ] && run sudo rm -f "$SUDOERS_PATH"
    command -v systemctl >/dev/null 2>&1 && run sudo systemctl daemon-reload
    ok "service, unit and sudoers rule removed"

    say ""
    say "${C_BOLD}Left in place -- delete by hand if you mean to:${C_OFF}"
    local p
    for p in "$ENV_FILE" "$PROFILES_CONFIG" "$SCRIPT_DIR/data/bot.db" \
             "$SCRIPT_DIR/data/memory" "$SCRIPT_DIR/workspaces" \
             "$RENDERED_DIR" "$HOME/.claude"; do
        [ -e "$p" ] && info "$p"
    done
    say ""
    info "Your Claude/Anthropic login lives in ~/.claude and is shared with the"
    info "claude CLI -- removing it logs you out everywhere on this machine."
    say ""
    info "User lingering (loginctl enable-linger) was left on: other services"
    info "may depend on it. Turn it off with:"
    info "  sudo loginctl disable-linger ${USER:-$(id -un)}"
}

uninstall_local() {
    say "${C_BOLD}Removing the local-mode launchd agents${C_OFF}"
    say ""
    local agent uid
    uid="$(id -u)"
    for agent in $LAUNCHD_AGENTS; do
        run launchctl bootout "gui/$uid/com.devvy.$agent" 2>/dev/null
        run rm -f "$LAUNCH_AGENTS/com.devvy.$agent.plist"
    done
    ok "launchd agents removed"
    say ""
    say "${C_BOLD}Left in place -- delete by hand if you mean to:${C_OFF}"
    local p
    for p in "$ENV_FILE" "$PROFILES_CONFIG" "$SCRIPT_DIR/data" \
             "$SCRIPT_DIR/workspaces" "$SCRIPT_DIR/logs" "$RENDERED_DIR"; do
        [ -e "$p" ] && info "$p"
    done
}

if [ "$DO_UNINSTALL" = 1 ]; then
    if [ "$PLATFORM" = Darwin ]; then
        uninstall_local
    else
        uninstall
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
say "${C_BOLD}Devvy -- install${C_OFF}"
[ "$DRY_RUN" = 1 ] && say "${C_YELLOW}--dry-run: nothing will be changed.${C_OFF}"
info "root:    $SCRIPT_DIR"
if [ "$LOCAL" = 1 ]; then
    info "mode:    local (beta) on $PLATFORM -- no Telegram"
else
    info "service: $SERVICE_NAME"
fi

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
step "1/10  Preflight"

if ! command -v python3 >/dev/null 2>&1; then
    err "python3 is not installed, and this installer is written in it."
    if [ "$PLATFORM" = Darwin ]; then
        err "  fix: brew install python@3.12"
    else
        err "  fix: sudo apt-get update && sudo apt-get install -y python3"
    fi
    exit 1
fi

offer_autofixes() {
    # Ask, one dependency at a time, before running anything. An installer that
    # apt-installs on its own is one people are right to refuse to run.
    local fixes
    # preflight exits 1 when something is missing; under pipefail that would
    # fail the whole pipeline and skip every offer.
    fixes="$( { python3 scripts/preflight.py --json 2>/dev/null || true; } | python3 -c '
import json, sys
for item in json.load(sys.stdin):
    if not item["ok"] and item["autofix"]:
        print("%s\t%s\t%s" % (item["key"], item["label"], item["autofix"]))
')" || return 0
    [ -z "$fixes" ] && return 0
    local key label cmd
    # Read the list on fd 3 so confirm() still reads the answer from the terminal.
    while IFS=$'\t' read -r -u 3 key label cmd; do
        [ -z "$key" ] && continue
        say ""
        info "$label is missing. Suggested fix:"
        info "  $cmd"
        if confirm "Run it now?"; then
            run_sh "$cmd" || warn "$label install failed; fix it by hand and re-run."
        else
            info "Skipped. Install $label yourself, then re-run ./install.sh."
        fi
    done 3<<<"$fixes"
    # Poetry's installer lands in ~/.local/bin, which a non-login shell may not
    # have on PATH -- the single most common "it installed but is not found".
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
}

export DEVVY_PLATFORM="$PLATFORM"
python3 scripts/preflight.py
PREFLIGHT_RC=$?
if [ "$PREFLIGHT_RC" != 0 ]; then
    offer_autofixes
    python3 scripts/preflight.py
    PREFLIGHT_RC=$?
fi
if [ "$PREFLIGHT_RC" != 0 ]; then
    if [ "$DRY_RUN" = 1 ]; then
        skipped "preflight" "unmet dependencies, continuing because --dry-run"
    else
        failed "preflight" "install the missing dependencies above, then re-run"
        exit 1
    fi
else
    ok "preflight"
fi

# ---------------------------------------------------------------------------
# 2. Bot token
# ---------------------------------------------------------------------------
if [ "$LOCAL" = 1 ]; then
    step "2/10  Channel"
    info "local: no Telegram. The Pump TUI (scripts/devvy-tui) is the interface."
    ok "channel" "local"
else
step "2/10  Telegram bot token"

if [ -z "$BOT_TOKEN" ] && [ -f "$ENV_FILE" ]; then
    # A re-run should not ask again for something already recorded.
    BOT_TOKEN="$(sed -n 's/^[[:space:]]*TELEGRAM_BOT_TOKEN[[:space:]]*=[[:space:]]*//p' \
        "$ENV_FILE" | tail -n1)"
    [ -n "$BOT_TOKEN" ] && info "using the token already in .env"
fi

if [ -z "$BOT_TOKEN" ]; then
    if [ -t 0 ]; then
        say ""
        info "Create a bot with @BotFather in Telegram (/newbot) and paste its token."
        read -r -p "     Token: " BOT_TOKEN
    fi
fi

if [ -z "$BOT_TOKEN" ]; then
    failed "bot token" "pass --token, set \$TELEGRAM_BOT_TOKEN, or run interactively"
    exit 1
fi

if [ "$DRY_RUN" = 1 ]; then
    BOT_USERNAME="your_bot"
    info "would validate the token with getMe and read the bot username"
    skipped "token validated"
else
    BOT_USERNAME="$(python3 scripts/capture_owner.py --token "$BOT_TOKEN" --validate-only)"
    if [ -z "$BOT_USERNAME" ]; then
        failed "bot token" "getMe rejected it -- check the token from @BotFather"
        exit 1
    fi
    ok "token valid" "@$BOT_USERNAME"
fi
fi  # LOCAL

# ---------------------------------------------------------------------------
# 3. Configuration (.env)
#
# Ordering note: the task lists owner capture as step 3 and .env as step 5.
# They are swapped here for one mechanical reason -- capture_owner.py writes
# ALLOWED_USERS *into .env*, so if it ran first it would create a four-line
# .env and the seed-from-.env.example step would then see an "existing" file
# and leave it alone. The invariant that matters (owner recorded before the
# service ever starts) is unaffected.
# ---------------------------------------------------------------------------
step "3/10  Configuration (.env)"

if [ "$LOCAL" = 1 ]; then
    # One owner, id 1: the local channel's only address (LOCAL_CHAT_ID). The
    # API server carries the inbox the TUI reads, bound to 127.0.0.1.
    ENV_ARGS=(--env "$ENV_FILE"
              --set "CHANNEL=local"
              --set "ENABLE_API_SERVER=true"
              --set "PROFILES_CONFIG_PATH=$PROFILES_CONFIG"
              --set "DEVELOPMENT_MODE=false")
    grep -qE '^[[:space:]]*ALLOWED_USERS[[:space:]]*=[[:space:]]*[0-9]' "$ENV_FILE" 2>/dev/null ||
        ENV_ARGS+=(--set "ALLOWED_USERS=1")
    info "CHANNEL=local, ENABLE_API_SERVER=true (127.0.0.1:$LOCAL_API_PORT)"
else
ENV_ARGS=(--env "$ENV_FILE"
          --set "TELEGRAM_BOT_TOKEN=$BOT_TOKEN"
          --set "TELEGRAM_BOT_USERNAME=$BOT_USERNAME"
          --set "PROFILES_CONFIG_PATH=$PROFILES_CONFIG"
          --set "DEVELOPMENT_MODE=false")
fi
[ "$DRY_RUN" = 1 ] && ENV_ARGS+=(--dry-run)

if python3 scripts/render_env.py "${ENV_ARGS[@]}"; then
    ok ".env" "mode 600, secrets generated, existing values kept"
else
    failed ".env" "could not be written"
    exit 1
fi

# ---------------------------------------------------------------------------
# 4. Owner capture (dv-5c83)
# ---------------------------------------------------------------------------
step "4/10  Owner"

if [ "$LOCAL" = 1 ]; then
    ok "owner" "local: this machine's user (ALLOWED_USERS=1)"
elif grep -qE '^[[:space:]]*ALLOWED_USERS[[:space:]]*=[[:space:]]*[0-9]' "$ENV_FILE" 2>/dev/null; then
    skipped "owner capture" "ALLOWED_USERS already set in .env"
elif [ "$DRY_RUN" = 1 ]; then
    info "would drain pending updates, wait for your /start, and confirm your id"
    skipped "owner capture" "--dry-run"
else
    CAPTURE_ARGS=(--token "$BOT_TOKEN" --env-file "$ENV_FILE")
    [ -n "$OWNER_ID" ] && CAPTURE_ARGS+=(--owner-id "$OWNER_ID")
    [ "$ASSUME_YES" = 1 ] && CAPTURE_ARGS+=(--yes)
    if python3 scripts/capture_owner.py "${CAPTURE_ARGS[@]}"; then
        ok "owner recorded"
    else
        # Not fatal: everything else can still be set up, and the owner can be
        # added by hand. The service start below refuses without one.
        failed "owner capture" "set ALLOWED_USERS in .env, or re-run with --owner-id"
    fi
fi

# ---------------------------------------------------------------------------
# 5. Profiles
# ---------------------------------------------------------------------------
step "5/10  Profiles"

PROFILE_ARGS=(--config "$PROFILES_CONFIG" --install "$PROFILES")
[ "$DRY_RUN" = 1 ] && PROFILE_ARGS+=(--dry-run)
if python3 scripts/install_profiles.py "${PROFILE_ARGS[@]}"; then
    ok "profiles" "$PROFILES"
    info "others available: python3 scripts/install_profiles.py --list"
else
    failed "profiles" "config/profiles.yaml was not written"
fi

# ---------------------------------------------------------------------------
# 6. Dependencies
# ---------------------------------------------------------------------------
step "6/10  Dependencies (poetry install)"

if ! command -v poetry >/dev/null 2>&1 && [ "$DRY_RUN" = 1 ]; then
    # Same rule as preflight: --dry-run reports the gap and keeps going.
    info "would run: poetry install --no-interaction --no-ansi"
    skipped "poetry install" "poetry is not on PATH, continuing because --dry-run"
elif ! command -v poetry >/dev/null 2>&1; then
    failed "poetry install" "poetry is not on PATH"
elif [ "$LOCAL" = 1 ] && run poetry install --extras tui --no-interaction --no-ansi; then
    ok "dependencies installed"
elif [ "$LOCAL" != 1 ] && run poetry install --no-interaction --no-ansi; then
    ok "dependencies installed"
else
    failed "poetry install" "see the output above"
fi

# ---------------------------------------------------------------------------
# 7. Verify config + database
# ---------------------------------------------------------------------------
step "7/10  Config, workspaces and database"

if [ "$DRY_RUN" = 1 ]; then
    info "would run: poetry run python scripts/check_install.py"
    info "  (loads .env, materialises every profile's workspace, memory dir and"
    info "   persona from templates, creates the database schema, seeds the owner)"
    skipped "verification" "--dry-run"
elif ! command -v poetry >/dev/null 2>&1; then
    skipped "verification" "poetry unavailable"
elif poetry run python scripts/check_install.py; then
    ok "config, workspaces and database"
else
    failed "verification" "fix the items marked ✘ above and re-run"
fi

# ---------------------------------------------------------------------------
# 8. Claude / Anthropic auth -- always skippable
# ---------------------------------------------------------------------------
step "8/10  Claude login"

claude_logged_in() {
    command -v claude >/dev/null 2>&1 || return 1
    claude auth status --json 2>/dev/null | python3 -c '
import json, sys
try:
    print("yes" if json.load(sys.stdin).get("loggedIn") else "no")
except Exception:
    print("no")
' | grep -q yes
}

auth_instructions() {
    if [ "$LOCAL" = 1 ]; then
        info "Finish the login later -- jobs need it, the server starts without it:"
        info "  * run: claude auth login"
        info "  * or put ANTHROPIC_API_KEY=... in $ENV_FILE"
        return 0
    fi
    info "Finish the login later, whenever you like -- the bot starts without it:"
    info "  * send /auth in Telegram and follow the buttons, or"
    info "  * run: claude auth login"
    info "  * or put ANTHROPIC_API_KEY=... in $ENV_FILE and restart the service"
}

if [ "$SKIP_AUTH" = 1 ]; then
    skipped "Claude login" "--skip-auth"
    auth_instructions
elif [ "$DRY_RUN" = 1 ]; then
    info "would check 'claude auth status --json' and offer: log in now / paste"
    info "an ANTHROPIC_API_KEY / skip"
    skipped "Claude login" "--dry-run"
elif grep -qE '^[[:space:]]*ANTHROPIC_API_KEY[[:space:]]*=[[:space:]]*.+' "$ENV_FILE" 2>/dev/null; then
    ok "Claude auth" "ANTHROPIC_API_KEY already in .env"
elif claude_logged_in; then
    ok "Claude auth" "the claude CLI is already logged in"
elif [ ! -t 0 ]; then
    skipped "Claude login" "not a terminal"
    auth_instructions
else
    say ""
    if [ "$LOCAL" = 1 ]; then
        info "Local jobs need a Claude subscription or an Anthropic API key."
    else
        info "The bot needs a Claude subscription or an Anthropic API key to answer."
    fi
    info "  1) Log in now in this terminal (opens a browser link)"
    info "  2) Paste an ANTHROPIC_API_KEY"
    if [ "$LOCAL" = 1 ]; then
        info "  3) Skip -- finish later with claude auth login"
    else
        info "  3) Skip -- finish later with /auth in Telegram"
    fi
    read -r -p "     Choice [1/2/3, default 3]: " AUTH_CHOICE
    case "${AUTH_CHOICE:-3}" in
        1)
            if claude auth login; then
                ok "Claude login"
            else
                if [ "$LOCAL" = 1 ]; then
                    failed "Claude login" "finish later with claude auth login"
                else
                    failed "Claude login" "you can still finish later with /auth"
                fi
                auth_instructions
            fi
            ;;
        2)
            read -r -s -p "     ANTHROPIC_API_KEY: " ANTHROPIC_KEY; say ""
            if [ -n "$ANTHROPIC_KEY" ] && \
               python3 scripts/render_env.py --env "$ENV_FILE" \
                   --set "ANTHROPIC_API_KEY=$ANTHROPIC_KEY" >/dev/null; then
                ok "Claude auth" "ANTHROPIC_API_KEY written to .env"
            else
                failed "Claude auth" "no key written"
                auth_instructions
            fi
            unset ANTHROPIC_KEY
            ;;
        *)
            skipped "Claude login" "chosen: finish later"
            auth_instructions
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# 9. systemd service
# ---------------------------------------------------------------------------
step "9/10  Service"

install_service() {
    # Render first. render_deploy.py validates the sudoers output with
    # `visudo -cf` and exits non-zero if it does not parse, so a malformed rule
    # can never reach /etc/sudoers.d -- which is a lock-yourself-out bug.
    run python3 scripts/render_deploy.py --out "$RENDERED_DIR" \
        --service-name "$SERVICE_NAME" || return 1

    run sudo install -m 644 -o root -g root \
        "$RENDERED_DIR/claude-bot.service" "$UNIT_PATH" || return 1
    run sudo install -m 440 -o root -g root \
        "$RENDERED_DIR/sudoers-claude-bot" "$SUDOERS_PATH" || return 1
    # Validate the installed copy too: that file, not the rendered one, is what
    # sudo actually reads.
    run sudo visudo -cf "$SUDOERS_PATH" || return 1
    run sudo systemctl daemon-reload || return 1
    run sudo systemctl enable "$SERVICE_NAME" || return 1

    # bgjob runs background work as transient systemd --user units; without
    # lingering they die the moment the login session ends.
    run sudo loginctl enable-linger "${USER:-$(id -un)}" ||
        warn "could not enable user lingering; background jobs will not survive logout"
    return 0
}

install_launchd() {
    # Only the com.devvy.* agents: no unit, no root-owned files. The tick's
    # job cap goes into its plist (BGJOB_MAX_CONCURRENT, read by scripts/bgjob).
    local uid agent plist
    uid="$(id -u)"
    info "PUMP_MAX_CONCURRENT=$LOCAL_PUMP_MAX (one job at a time, about 2 GB each)"
    run env PUMP_MAX_CONCURRENT="$LOCAL_PUMP_MAX" python3 scripts/render_deploy.py \
        --launchd --out "$RENDERED_DIR" || return 1
    run mkdir -p "$SCRIPT_DIR/logs" "$LAUNCH_AGENTS" || return 1
    for agent in $LAUNCHD_AGENTS; do
        plist="$LAUNCH_AGENTS/com.devvy.$agent.plist"
        run cp "$RENDERED_DIR/com.devvy.$agent.plist" "$plist" || return 1
        # bootout first so a re-run loads the new plist; not loaded = fine.
        run launchctl bootout "gui/$uid/com.devvy.$agent" 2>/dev/null
        run launchctl bootstrap "gui/$uid" "$plist" || return 1
    done
    return 0
}

local_by_hand() {
    info "No launchd here: run the server and the pump tick yourself:"
    info "  poetry run python -m src.main     # CHANNEL=local is in the config"
    info "  crontab: */5 * * * * cd $SCRIPT_DIR && BGJOB_MAX_CONCURRENT=$LOCAL_PUMP_MAX scripts/pump2 tick"
    info "Jobs use the process launcher when systemd is not running."
}

if [ "$LOCAL" = 1 ]; then
    if [ "$SKIP_SERVICE" = 1 ]; then
        skipped "launchd agents" "--no-service"
        local_by_hand
    elif [ "$PLATFORM" != Darwin ]; then
        skipped "launchd agents" "not macOS"
        local_by_hand
    elif install_launchd; then
        ok "launchd agents" "com.devvy.server, com.devvy.pump2 (tick every 2 min)"
    else
        failed "launchd agents" "see the output above"
    fi
elif [ "$SKIP_SERVICE" = 1 ]; then
    skipped "systemd service" "--no-service"
    info "Run the bot by hand: poetry run python -m src.main"
elif ! command -v systemctl >/dev/null 2>&1; then
    skipped "systemd service" "systemd not available"
    info "Run the bot by hand: poetry run python -m src.main"
elif install_service; then
    ok "systemd unit + sudoers installed" "$UNIT_PATH"
else
    failed "systemd service" "unit or sudoers not installed"
fi

# ---------------------------------------------------------------------------
# 10. Start and verify
# ---------------------------------------------------------------------------
step "10/10  Start"

HAVE_OWNER=0
grep -qE '^[[:space:]]*ALLOWED_USERS[[:space:]]*=[[:space:]]*[0-9]' "$ENV_FILE" 2>/dev/null && HAVE_OWNER=1

# The line src/bot/core.py logs once the application is built and polling is
# about to start. Seeing it is the difference between "systemd started a
# process" and "the bot came up".
STARTUP_BANNER="Starting bot"

start_and_verify() {
    local since
    since="$(date '+%Y-%m-%d %H:%M:%S')"
    run sudo systemctl restart "$SERVICE_NAME" || return 1
    [ "$DRY_RUN" = 1 ] && return 0

    # `is-active` alone is not enough: the unit is Restart=always, so a bot
    # that boots, raises, and is restarted forever reads as "active" or
    # "activating" at almost any sampling moment. Wait for the banner, and
    # treat a restart count above zero as the crash loop it is.
    local i state banner="" restarts=0
    for i in $(seq 1 40); do
        state="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null)"
        [ "$state" = "failed" ] && break
        restarts="$(systemctl show -p NRestarts --value "$SERVICE_NAME" 2>/dev/null)"
        [ -n "${restarts:-}" ] && [ "$restarts" -gt 0 ] 2>/dev/null && break
        if journalctl -u "$SERVICE_NAME" --since "$since" --no-pager 2>/dev/null |
                grep -qF "$STARTUP_BANNER"; then
            banner="seen"
            break
        fi
        sleep 1
    done

    state="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null)"
    restarts="$(systemctl show -p NRestarts --value "$SERVICE_NAME" 2>/dev/null)"
    if [ "$state" != "active" ]; then
        err "the service is '$state' -- last 30 log lines:"
        journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null | sed 's/^/     /'
        return 1
    fi
    if [ -n "${restarts:-}" ] && [ "$restarts" -gt 0 ] 2>/dev/null; then
        err "the service restarted $restarts time(s) already -- it is crash-looping:"
        journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null | sed 's/^/     /'
        return 1
    fi
    if [ -z "$banner" ]; then
        # Reading the journal needs group membership (adm / systemd-journal).
        # Without it the grep above finds nothing on a perfectly healthy bot,
        # so this is a warning, never a failure.
        warn "could not confirm '$STARTUP_BANNER' in the journal within 40s."
        info "The service is active and has not restarted, so this is probably"
        info "only missing journal access. Check with:"
        info "  journalctl -u $SERVICE_NAME -n 50"
        info "  sudo usermod -aG adm,systemd-journal ${USER:-$(id -un)}   # then re-login"
    fi
    return 0
}

local_health() {
    # launchd started the server at bootstrap (RunAtLoad); wait for /health.
    local url="http://127.0.0.1:$LOCAL_API_PORT/health" i
    for i in $(seq 1 40); do
        curl -fsS "$url" >/dev/null 2>&1 && return 0
        sleep 1
    done
    err "no answer from $url within 40s -- last 30 log lines:"
    tail -n 30 "$SCRIPT_DIR/logs/server.log" 2>/dev/null | sed 's/^/     /'
    return 1
}

if [ "$LOCAL" = 1 ]; then
    if [ "$SKIP_SERVICE" = 1 ] || [ "$PLATFORM" != Darwin ]; then
        skipped "start" "no launchd agents installed"
    elif [ "$DRY_RUN" = 1 ]; then
        info "would wait for http://127.0.0.1:$LOCAL_API_PORT/health"
        skipped "start" "--dry-run"
    elif local_health; then
        ok "server running" "127.0.0.1:$LOCAL_API_PORT"
    else
        failed "server did not come up" "tail -n 50 $SCRIPT_DIR/logs/server.log"
    fi
elif [ "$SKIP_SERVICE" = 1 ] || ! command -v systemctl >/dev/null 2>&1; then
    skipped "start" "no service installed"
elif [ "$HAVE_OWNER" = 0 ] && [ "$DRY_RUN" != 1 ]; then
    # Starting an ownerless bot is the failure mode dv-5c83 exists to remove:
    # it refuses to boot, and the reason is buried in the journal.
    skipped "start" "no owner in .env"
    info "Set ALLOWED_USERS=<your telegram id> in $ENV_FILE (or re-run this"
    info "installer), then: sudo systemctl start $SERVICE_NAME"
elif start_and_verify; then
    ok "service running"
else
    failed "service did not come up" "journalctl -u $SERVICE_NAME -n 50"
fi

# ---------------------------------------------------------------------------
if [ "$LOCAL" = 1 ]; then
    say ""
    if [ "$FAILED" = 0 ]; then
        say "${C_BOLD}${C_GREEN}Done (local mode, beta).${C_OFF}"
    else
        say "${C_BOLD}${C_YELLOW}Finished with problems -- see the summary below.${C_OFF}"
        say "  Fix what is marked ✘ and run ./install.sh --local again."
    fi
    say ""
    say "  Pump TUI: scripts/devvy-tui"
    say "  Logs:     tail -f $SCRIPT_DIR/logs/server.log $SCRIPT_DIR/logs/pump2.log"
    if [ "$PLATFORM" = Darwin ]; then
        say "  Restart:  launchctl kickstart -k gui/\$(id -u)/com.devvy.server"
        say "  Once:     scripts/seatbelt_probe   (checks the job box on this Mac)"
    fi
    say "  Docs:     docs/installation.md (Local mode)"
    exit "$FAILED"
fi

say ""
if [ "$FAILED" = 0 ]; then
    say "${C_BOLD}${C_GREEN}Done.${C_OFF}"
    [ -n "$BOT_USERNAME" ] && say "  Message https://t.me/${BOT_USERNAME} to begin."
else
    say "${C_BOLD}${C_YELLOW}Finished with problems -- see the summary below.${C_OFF}"
    say "  Fix what is marked ✘ and run ./install.sh again; it is idempotent."
fi
say ""
say "  Logs:    journalctl -u ${SERVICE_NAME} -f"
say "  Status:  systemctl status ${SERVICE_NAME}"
say "  Restart: sudo systemctl restart ${SERVICE_NAME}"
say "  Profiles: python3 scripts/install_profiles.py --list"

exit "$FAILED"
