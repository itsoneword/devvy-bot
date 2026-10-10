"""dv-2976: bot secrets never reach agent subprocesses.

Covers the three layers: secrets load into Settings without touching
os.environ (both layouts), the startup scrub the Claude SDK path relies on,
and the allowlisted env + argv guard on the Codex path.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest
from pydantic import SecretStr

from src.config import load_config
from src.config.loader import scrub_secrets_from_environ, secrets_file_for
from src.config.settings import Settings
from src.utils.agent_env import (
    AGENT_CLI_CREDENTIAL_VARS,
    agent_child_env,
    is_passthrough,
    is_secret_name,
    scrub_process_env,
    split_mcp_env,
)

# What every agent CLI needs from the parent. Removing one of these from the
# allowlist breaks a CLI (auth, node lookup, locale, systemd --user), so the
# list is pinned here on purpose: change it only with a reason.
REQUIRED_PASSTHROUGH = (
    "PATH",  # node for the codex wrapper, every tool binary
    "HOME",  # ~/.claude/.credentials.json, ~/.claude.json, ~/.codex
    "USER",
    "LANG",
    "TERM",
    "XDG_RUNTIME_DIR",  # systemctl --user (bgjob)
    "CLAUDE_CONFIG_DIR",  # Claude CLI config/credentials dir override
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",  # Claude CLI credentials dir override
    "CODEX_HOME",  # codex auth.json + sessions
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",  # CLI flag the bot sets in its env
    "LC_ALL",
)

# Real names from the prod .env (2026-09-27), values fake.
SECRETS = {
    "TELEGRAM_BOT_TOKEN": "123456:tg-fake-token-value",
    "OPENROUTER_API_KEY": "sk-or-fake-openrouter-value",
    "ELEVENLABS_API_KEY": "el-fake-elevenlabs-value",
    "CHANNEL_INTEL_TOKEN": "ci-fake-channel-intel-value",
    "WEBHOOK_API_SECRET": "wh-fake-webhook-secret-value",
}
CONFIG = {
    "TELEGRAM_BOT_USERNAME": "test_bot",
    "CLAUDE_MODEL": "sonnet",
    "WINDOW_REFRESH_THRESHOLD_TOKENS": "220000",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}


@pytest.fixture(autouse=True)
def _restore_environ(monkeypatch):
    snapshot = dict(os.environ)
    for key in (*SECRETS, *CONFIG, "DEVVY_DATA_ROOT", "APPROVED_DIRECTORY"):
        monkeypatch.delenv(key, raising=False)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _write_env(path: Path, values: Dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


# --- classification -------------------------------------------------------


def test_prod_secret_names_are_secret_and_config_is_not():
    for name in SECRETS:
        assert is_secret_name(name), name
    for name in CONFIG:
        assert not is_secret_name(name), name
    assert is_secret_name("SOME_NEW_PRIVATE_KEY")
    assert is_secret_name("DB_PASSWORD")


def test_required_passthrough_vars_are_allowlisted():
    for name in REQUIRED_PASSTHROUGH:
        assert is_passthrough(name), name
    env = {name: "v" for name in REQUIRED_PASSTHROUGH}
    assert set(agent_child_env(environ=env)) == set(REQUIRED_PASSTHROUGH)


def test_agent_cli_credentials_survive_scrub_and_allowlist():
    env = {name: "x" for name in AGENT_CLI_CREDENTIAL_VARS}
    assert scrub_process_env(environ=env) == []
    assert set(agent_child_env(environ=env)) == set(AGENT_CLI_CREDENTIAL_VARS)


# --- loading: secrets reach Settings, never os.environ -------------------


def test_root_mode_reads_secrets_file_into_settings_only(tmp_path):
    root = tmp_path / "data"
    env_file = _write_env(
        tmp_path / ".env",
        {
            **CONFIG,
            "DEVVY_DATA_ROOT": str(root),
            "APPROVED_DIRECTORY": str(tmp_path),
        },
    )
    secrets_file = _write_env(
        root / "secrets" / "bot.env", {**SECRETS, "CUSTOM_UPSTREAM": "zzz-custom"}
    )
    secrets_file.chmod(0o600)
    assert secrets_file_for(env_file) == secrets_file

    settings = load_config(env="development", config_file=env_file)

    assert settings.telegram_token_str == SECRETS["TELEGRAM_BOT_TOKEN"]
    assert (
        settings.secret_value("CHANNEL_INTEL_TOKEN") == SECRETS["CHANNEL_INTEL_TOKEN"]
    )
    assert settings.webhook_api_secret == SECRETS["WEBHOOK_API_SECRET"]
    assert settings.elevenlabs_api_key_str == SECRETS["ELEVENLABS_API_KEY"]
    for name in (*SECRETS, "CUSTOM_UPSTREAM"):
        assert name not in os.environ, name
    # config still exported, as load_dotenv did
    assert os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_legacy_mode_still_loads_everything_from_dotenv(tmp_path):
    env_file = _write_env(
        tmp_path / ".env",
        {**CONFIG, **SECRETS, "APPROVED_DIRECTORY": str(tmp_path)},
    )
    assert secrets_file_for(env_file) is None

    settings = load_config(env="development", config_file=env_file)

    assert settings.telegram_token_str == SECRETS["TELEGRAM_BOT_TOKEN"]
    assert settings.telegram_bot_username == "test_bot"
    assert (
        settings.secret_value("CHANNEL_INTEL_TOKEN") == SECRETS["CHANNEL_INTEL_TOKEN"]
    )
    assert settings.claude_model == "sonnet"
    for name in SECRETS:
        assert name not in os.environ, name
    assert os.environ["WINDOW_REFRESH_THRESHOLD_TOKENS"] == "220000"


def test_scrub_removes_systemd_exported_secrets(tmp_path):
    """Today EnvironmentFile= exports the whole .env; the scrub undoes that."""
    root = tmp_path / "data"
    env_file = _write_env(
        tmp_path / ".env",
        {**CONFIG, "DEVVY_DATA_ROOT": str(root), "APPROVED_DIRECTORY": str(tmp_path)},
    )
    _write_env(root / "secrets" / "bot.env", {**SECRETS, "ODD_NAME": "q"})
    os.environ.update({**SECRETS, "ODD_NAME": "q"})  # as systemd would

    settings = load_config(env="development", config_file=env_file)
    removed = scrub_secrets_from_environ(settings, env_file)

    assert set(SECRETS) | {"ODD_NAME"} <= set(removed)
    for name in (*SECRETS, "ODD_NAME"):
        assert name not in os.environ
    assert settings.telegram_token_str == SECRETS["TELEGRAM_BOT_TOKEN"]
    assert os.environ["CLAUDE_MODEL"] == "sonnet"


# --- Claude SDK path: the real transport's merge ------------------------


async def test_sdk_spawned_env_has_no_secret(monkeypatch):
    """Drive the SDK's own SubprocessCLITransport.connect() and capture the env
    it would exec the CLI with. Not a re-implementation of its merge."""
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport import subprocess_cli

    os.environ.update(SECRETS)
    os.environ.update(CONFIG)
    os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"
    scrub_process_env(SECRETS)

    captured: Dict[str, Any] = {}

    class _Stop(Exception):
        pass

    async def fake_open_process(cmd: Any, **kwargs: Any) -> Any:
        captured.update(kwargs["env"])
        captured["__argv__"] = " ".join(cmd)
        raise _Stop

    monkeypatch.setattr(subprocess_cli.anyio, "open_process", fake_open_process)

    async def _no_prompt():  # pragma: no cover - never iterated
        if False:
            yield {}

    transport = subprocess_cli.SubprocessCLITransport(
        prompt=_no_prompt(),
        options=ClaudeAgentOptions(
            cli_path="/bin/true",
            env={"CHANNEL_INTEL_TOKEN": SECRETS["CHANNEL_INTEL_TOKEN"]},
            mcp_servers={
                "channel-intel": {
                    "type": "http",
                    "url": "http://127.0.0.1:1/mcp",
                    "headers": {"Authorization": "Bearer ${CHANNEL_INTEL_TOKEN}"},
                }
            },
        ),
    )
    with pytest.raises(Exception):
        await transport.connect()

    argv = captured.pop("__argv__")
    leaked = [n for n in SECRETS if n in captured and n != "CHANNEL_INTEL_TOKEN"]
    assert leaked == []
    # the one explicit per-call var arrives; nothing secret sits in argv
    assert captured["CHANNEL_INTEL_TOKEN"] == SECRETS["CHANNEL_INTEL_TOKEN"]
    for value in SECRETS.values():
        assert value not in argv
    assert captured["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


# --- Codex path: allowlisted env, argv guard ------------------------------


class _Registry:
    def __init__(self, profiles):
        self._profiles = profiles

    def get_by_working_dir(self, path):
        for p in self._profiles:
            if p.working_dir == path:
                return p
        return None


class _Profile:
    def __init__(self, working_dir: Path, mcp: Path):
        self.working_dir = working_dir
        self.mcp = mcp
        self.slug = "channel"


def _codex(tmp_path: Path, **settings_kw: Any):
    from src.agent_backends.codex_cli import CodexCliIntegration
    from src.storage.codex_sessions import InMemoryCodexSessionStore

    settings = Settings(
        _env_file=None,
        telegram_bot_token=SECRETS["TELEGRAM_BOT_TOKEN"],
        telegram_bot_username="x",
        **settings_kw,
    )
    return CodexCliIntegration(
        config=settings,
        codex_bin="codex",
        session_store=InMemoryCodexSessionStore(),
    )


def test_codex_child_env_is_allowlisted(tmp_path):
    wd = tmp_path / "chan"
    wd.mkdir()
    seam = tmp_path / "channel-intel.json"
    seam.write_text(
        json.dumps(
            {"type": "http", "url": "http://x/mcp", "auth_env": "CHANNEL_INTEL_TOKEN"}
        )
    )
    integ = _codex(
        tmp_path,
        enable_mcp=False,
        channel_intel_token=SecretStr(SECRETS["CHANNEL_INTEL_TOKEN"]),
    )
    integ.profile_registry = _Registry([_Profile(wd, seam)])
    os.environ.update(SECRETS)  # worst case: nothing was scrubbed
    os.environ.update({"CODEX_HOME": "/tmp/ch", "RANDOM_CONFIG": "1"})

    env = integ._child_env(wd, {"AGENT_BRIDGE_TOKEN": "per-turn"})

    for name in SECRETS:
        if name != "CHANNEL_INTEL_TOKEN":
            assert name not in env, name
    # codex reads the seam bearer by NAME, so the child needs exactly that one
    assert env["CHANNEL_INTEL_TOKEN"] == SECRETS["CHANNEL_INTEL_TOKEN"]
    assert env["AGENT_BRIDGE_TOKEN"] == "per-turn"
    assert env["CODEX_HOME"] == "/tmp/ch"
    assert "PATH" in env and "RANDOM_CONFIG" not in env


def test_codex_argv_never_carries_mcp_secret_values(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    mcp = tmp_path / "mcp.json"
    mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "svc": {
                        "command": "svc-server",
                        "env": {
                            "SVC_API_TOKEN": "named-secret-abcdef",
                            "INNOCENT": SECRETS["TELEGRAM_BOT_TOKEN"],
                            "SAFE_PATH": "/srv/data",
                        },
                    }
                }
            }
        )
    )
    integ = _codex(tmp_path, enable_mcp=True, mcp_config_path=mcp)

    argv = " ".join(integ._mcp_overrides(wd))

    assert "named-secret-abcdef" not in argv
    assert SECRETS["TELEGRAM_BOT_TOKEN"] not in argv
    assert 'mcp_servers.svc.env.SAFE_PATH="/srv/data"' in argv
    assert 'mcp_servers.svc.env_vars=["INNOCENT", "SVC_API_TOKEN"]' in argv
    env = integ._child_env(wd)
    assert env["SVC_API_TOKEN"] == "named-secret-abcdef"
    assert env["INNOCENT"] == SECRETS["TELEGRAM_BOT_TOKEN"]


def test_split_mcp_env_matches_by_name_and_value():
    safe, routed = split_mcp_env(
        {"A_TOKEN": "t", "PATHS": "/x", "EMBED": "pre-SECRETVALUE-post"},
        ["SECRETVALUE"],
    )
    assert safe == {"PATHS": "/x"}
    assert set(routed) == {"A_TOKEN", "EMBED"}
