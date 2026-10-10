"""Keep bot secrets out of agent subprocesses (dv-2976).

Agents run as the bot's uid with a Bash tool, so anything in an agent CLI's
environment is readable by the model (``env``). Two layers:

1. :func:`scrub_process_env` -- called once at startup, after Settings has
   read the secrets into memory. Removes every secret-looking name from
   ``os.environ``. This is what protects the Claude path: the Agent SDK
   builds the CLI env as ``{**os.environ, **options.env}`` and has no option
   to drop inherited keys (claude_agent_sdk 0.2.123,
   ``_internal/transport/subprocess_cli.py`` ``connect``). It also covers
   every other spawn site that inherits the bot env (evaluator, bgjob,
   cli_login, self_modify, orchestration).
2. :func:`agent_child_env` -- a strict allowlist for spawns the bot builds
   itself (Codex). Only :data:`PASSTHROUGH_VARS` / :data:`PASSTHROUGH_PREFIXES`
   survive, plus explicit per-call vars.

Standard library only, so scripts can import it without Settings.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Mapping, MutableMapping, Optional

# The bot's own secrets, by name. Matched in addition to the pattern below so
# a renamed-but-obvious key is still caught.
KNOWN_SECRET_VARS = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "OPENROUTER_API_KEY",
        "ELEVENLABS_API_KEY",
        "MISTRAL_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CHANNEL_INTEL_TOKEN",
        "HIRELI_SERVICE_KEY",
        "WEBHOOK_API_SECRET",
        "AUTH_TOKEN_SECRET",
        "GITHUB_WEBHOOK_SECRET",
    }
)

# Whole-word match on the underscore-separated name, so
# WINDOW_REFRESH_THRESHOLD_TOKENS (config) is not a secret but
# TELEGRAM_BOT_TOKEN is.
_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|APIKEY|CREDENTIAL|CREDENTIALS"
    r"|PRIVATE)(?:_|$)",
    re.IGNORECASE,
)

# The agent CLIs' OWN credentials when supplied by env instead of their
# credential files. The CLI must hold them to work at all, so they are never
# scrubbed and always passed through. Verified 2026-09-27 against the
# binaries: Claude CLI 2.1.283 reads CLAUDE_CODE_OAUTH_TOKEN /
# ANTHROPIC_AUTH_TOKEN; codex-cli 0.157.1 reads CODEX_API_KEY /
# CODEX_ACCESS_TOKEN (before $CODEX_HOME/auth.json). Neither is set in prod,
# which uses the OAuth files. ANTHROPIC_API_KEY is deliberately NOT here: it
# is a Settings field, and sdk_integration re-exports it only when configured.
AGENT_CLI_CREDENTIAL_VARS = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
    }
)

# Everything an agent CLI and its tools need from the parent. How each CLI
# finds its auth (read from the binaries, not guessed):
# * Claude CLI: ``$CLAUDE_SECURESTORAGE_CONFIG_DIR`` or ``$CLAUDE_CONFIG_DIR``
#   or ``$HOME/.claude`` -> ``.credentials.json``; global config
#   ``$HOME/.claude.json``. So HOME + the two CLAUDE_* dirs.
# * Codex CLI: ``$CODEX_HOME`` (default ``$HOME/.codex``) -> ``auth.json``;
#   ``codex`` is a node wrapper (``#!/usr/bin/env node``), so PATH must find
#   node.
PASSTHROUGH_VARS = frozenset(
    {
        # process basics
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TZ",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "VIRTUAL_ENV",
        # systemd --user / dbus (bgjob, agents running systemctl --user)
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "DBUS_SESSION_BUS_ADDRESS",
        # TLS / proxies
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        # agent CLI config / auth locations
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "RUST_LOG",
    }
)
# LC_* locale; CODEX_* (CODEX_HOME and codex's own settings); CLAUDE_CODE_*
# (CLI behaviour flags the bot sets in its env, e.g.
# CLAUDE_CODE_DISABLE_AUTO_MEMORY).
PASSTHROUGH_PREFIXES = ("LC_", "CODEX_", "CLAUDE_CODE_")


def is_secret_name(name: str) -> bool:
    """True when ``name`` looks like a credential and must not reach agents."""
    if name in AGENT_CLI_CREDENTIAL_VARS:
        return False
    return name.upper() in KNOWN_SECRET_VARS or bool(_SECRET_NAME_RE.search(name))


def is_passthrough(name: str) -> bool:
    if name in AGENT_CLI_CREDENTIAL_VARS:
        return True
    if is_secret_name(name):
        return False
    return name in PASSTHROUGH_VARS or name.startswith(PASSTHROUGH_PREFIXES)


def scrub_process_env(
    extra_names: Iterable[str] = (),
    environ: Optional[MutableMapping[str, str]] = None,
) -> List[str]:
    """Remove secrets from ``environ`` (default ``os.environ``) in place.

    Removes every :func:`is_secret_name` match plus ``extra_names`` (the keys
    of the secrets file, and Settings' secret fields). Returns the removed
    NAMES, sorted, for logging; never the values.

    This does not change ``/proc/<pid>/environ``, which is the env block the
    process was exec'd with: only dropping systemd ``EnvironmentFile=`` for
    the secrets does that (dv-4030).
    """
    env = os.environ if environ is None else environ
    extra = {n for n in extra_names if n not in AGENT_CLI_CREDENTIAL_VARS}
    removed = [k for k in list(env) if k in extra or is_secret_name(k)]
    for key in removed:
        env.pop(key, None)
    return sorted(removed)


def agent_child_env(
    extra: Optional[Mapping[str, str]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    """Allowlisted environment for an agent CLI the bot spawns itself.

    ``extra`` holds this call's explicit vars (bridge token, an MCP bearer the
    CLI reads by name); they are added as given, secret-looking names
    included, because the caller chose them for this child.
    """
    env = os.environ if environ is None else environ
    child = {k: v for k, v in env.items() if is_passthrough(k)}
    if extra:
        child.update({str(k): str(v) for k, v in extra.items()})
    return child


def sdk_child_env_names(
    options_env: Optional[Mapping[str, str]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """NAMES the Claude CLI gets via the Agent SDK: its merge, reproduced.

    ``{**os.environ minus CLAUDECODE, CLAUDE_CODE_ENTRYPOINT,
    **options.env, CLAUDE_AGENT_SDK_VERSION}`` (plus PWD). For the check
    script; tests assert against the real transport instead.
    """
    env = os.environ if environ is None else environ
    names = {k for k in env if k != "CLAUDECODE"}
    names |= {"CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION", "PWD"}
    names |= set(options_env or {})
    return sorted(names)


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_env_name(name: str) -> bool:
    """A plain env var name, safe inside ``${...}`` or a TOML key."""
    return bool(_ENV_NAME_RE.match(name or ""))


def carries_secret(name: str, value: str, secret_values: Iterable[str] = ()) -> bool:
    """True when an MCP ``env`` entry must not be written into argv.

    Argv is world-readable (``ps``, ``/proc/<pid>/cmdline``), unlike the
    environment. Secret by name, or by value: a known secret embedded in an
    innocently named var still counts.
    """
    if is_secret_name(name) or name in AGENT_CLI_CREDENTIAL_VARS:
        return True
    return any(s and s in value for s in secret_values)


def split_mcp_env(
    env: Mapping[str, str], secret_values: Iterable[str] = ()
) -> "tuple[dict, dict]":
    """Split an MCP server ``env`` into (argv-safe, must-go-by-environment)."""
    secrets = [s for s in secret_values if s]
    safe: dict = {}
    routed: dict = {}
    for name, value in env.items():
        name, value = str(name), str(value)
        if carries_secret(name, value, secrets):
            routed[name] = value
        else:
            safe[name] = value
    return safe, routed
