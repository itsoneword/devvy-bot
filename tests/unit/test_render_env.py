"""`.env` rendering for the installer (dv-7d92 step 5).

The two failure modes worth tests:

* A fresh install that copies `.env.example` verbatim -- `APPROVED_DIRECTORY`
  is then `/path/to/your/projects`, which puts the bot in allowlist mode
  against a directory that does not exist and raises at startup.
* A re-run that overwrites a live `.env` -- the acceptance criterion for this
  task is "re-running loses no data", and this file holds the bot token, the
  owner id and any API key.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "render_env", REPO_ROOT / "scripts" / "render_env.py"
)
render_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_env)

EXAMPLE = """\
# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_BOT_USERNAME=your_bot_username

# Security
APPROVED_DIRECTORY=/path/to/your/projects
ALLOWED_USERS=
AUTH_TOKEN_SECRET=
WEBHOOK_API_SECRET=

# Behaviour
CLAUDE_MAX_TURNS=10
DEVELOPMENT_MODE=false
"""


def _values(text):
    return render_env.read_values(text)


# --- placeholders -------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "your_bot_token_here",
        "your-bot-username",
        "/path/to/your/projects",
        "<token>",
        "changeme",
        "CHANGEME",
        "xxxx",
    ],
)
def test_placeholder_values_are_recognised(value):
    assert render_env.is_placeholder(value)


@pytest.mark.parametrize(
    "value",
    ["", "false", "10", "123456:AAH-real-token", "/srv/devvy", "sk-ant-api03-abc"],
)
def test_real_values_are_not_placeholders(value):
    assert not render_env.is_placeholder(value)


def test_fresh_render_blanks_the_placeholder_values():
    text, notes = render_env.render(
        existing=None, example=EXAMPLE, overrides={}, secret_factory=lambda: "S"
    )

    values = _values(text)
    # The one that actually breaks startup.
    assert values["APPROVED_DIRECTORY"] == ""
    assert values["TELEGRAM_BOT_TOKEN"] == ""
    assert any("blanked placeholder" in n for n in notes)


def test_fresh_render_keeps_real_defaults_and_comments():
    text, _ = render_env.render(
        existing=None, example=EXAMPLE, overrides={}, secret_factory=lambda: "S"
    )

    assert _values(text)["CLAUDE_MAX_TURNS"] == "10"
    assert "# Telegram" in text


# --- overrides and secrets ---------------------------------------------


def test_installer_values_are_applied():
    text, _ = render_env.render(
        existing=None,
        example=EXAMPLE,
        overrides={
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "TELEGRAM_BOT_USERNAME": "mybot",
            "PROFILES_CONFIG_PATH": "/srv/devvy/config/profiles.yaml",
        },
        secret_factory=lambda: "S",
    )

    values = _values(text)
    assert values["TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert values["TELEGRAM_BOT_USERNAME"] == "mybot"
    # A key absent from .env.example is appended, not dropped.
    assert values["PROFILES_CONFIG_PATH"] == "/srv/devvy/config/profiles.yaml"


def test_secrets_are_generated_when_empty():
    text, notes = render_env.render(
        existing=None, example=EXAMPLE, overrides={}, secret_factory=lambda: "generated"
    )

    values = _values(text)
    assert values["AUTH_TOKEN_SECRET"] == "generated"
    assert values["WEBHOOK_API_SECRET"] == "generated"
    assert any("generated:" in n for n in notes)


def test_existing_secrets_are_never_regenerated():
    """Rotating AUTH_TOKEN_SECRET on a re-run invalidates every issued token."""
    existing = "AUTH_TOKEN_SECRET=keep-me\nWEBHOOK_API_SECRET=also-keep\n"

    text, _ = render_env.render(
        existing=existing, example=EXAMPLE, overrides={}, secret_factory=lambda: "NEW"
    )

    values = _values(text)
    assert values["AUTH_TOKEN_SECRET"] == "keep-me"
    assert values["WEBHOOK_API_SECRET"] == "also-keep"


def test_generated_secrets_are_random_and_long():
    a, b = render_env.new_secret(), render_env.new_secret()
    assert a != b
    assert len(a) >= 32


# --- the re-run contract ------------------------------------------------


def test_rerun_keeps_every_existing_value_and_comment():
    existing = """\
# my notes
TELEGRAM_BOT_TOKEN=123:abc
ALLOWED_USERS=4242
CLAUDE_MAX_TURNS=99
ANTHROPIC_API_KEY=sk-ant-mine
AUTH_TOKEN_SECRET=keep
WEBHOOK_API_SECRET=keep
"""

    text, notes = render_env.render(
        existing=existing,
        example=EXAMPLE,
        overrides={"TELEGRAM_BOT_USERNAME": "mybot"},
        secret_factory=lambda: "NEW",
    )

    values = _values(text)
    assert values["ALLOWED_USERS"] == "4242"
    assert values["CLAUDE_MAX_TURNS"] == "99"
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-mine"
    assert values["TELEGRAM_BOT_USERNAME"] == "mybot"
    assert "# my notes" in text
    assert any("existing values kept" in n for n in notes)


def test_rerun_does_not_merge_new_keys_from_the_example():
    """Every settings field has a default; appending the whole example over a
    tuned config is how an 'idempotent' installer changes behaviour."""
    existing = "TELEGRAM_BOT_TOKEN=123:abc\nAUTH_TOKEN_SECRET=k\nWEBHOOK_API_SECRET=k\n"

    text, _ = render_env.render(
        existing=existing, example=EXAMPLE, overrides={}, secret_factory=lambda: "NEW"
    )

    assert "CLAUDE_MAX_TURNS" not in _values(text)
    assert "APPROVED_DIRECTORY" not in _values(text)


def test_a_commented_key_is_not_treated_as_set():
    existing = "# ALLOWED_USERS=99\nTELEGRAM_BOT_TOKEN=t\n"

    text, _ = render_env.render(
        existing=existing,
        example=EXAMPLE,
        overrides={"ALLOWED_USERS": "4242"},
        secret_factory=lambda: "S",
    )

    assert _values(text)["ALLOWED_USERS"] == "4242"
    assert "# ALLOWED_USERS=99" in text  # documentation survives


# --- CLI ----------------------------------------------------------------


def test_parse_set_accepts_values_containing_equals():
    assert render_env.parse_set(["K=a=b=c"]) == {"K": "a=b=c"}


@pytest.mark.parametrize("bad", ["novalue", "=v", "not a name=v"])
def test_parse_set_rejects_junk(bad):
    with pytest.raises(ValueError):
        render_env.parse_set([bad])


def test_cli_writes_mode_600_and_backs_up_on_rerun(tmp_path):
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE, encoding="utf-8")
    base = ["--env", str(env), "--example", str(example)]

    assert render_env.main(base + ["--set", "TELEGRAM_BOT_TOKEN=123:abc"]) == 0
    assert env.stat().st_mode & 0o777 == 0o600
    first = env.read_text(encoding="utf-8")

    assert render_env.main(base + ["--set", "ALLOWED_USERS=42"]) == 0

    backups = list(tmp_path.glob(".env.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == first
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert _values(env.read_text(encoding="utf-8"))["ALLOWED_USERS"] == "42"


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE, encoding="utf-8")

    rc = render_env.main(
        [
            "--env",
            str(env),
            "--example",
            str(example),
            "--dry-run",
            "--set",
            "TELEGRAM_BOT_TOKEN=123:abc",
        ]
    )

    assert rc == 0
    assert not env.exists()
    assert "would write" in capsys.readouterr().out


def test_cli_fails_when_there_is_nothing_to_render_from(tmp_path):
    rc = render_env.main(
        ["--env", str(tmp_path / ".env"), "--example", str(tmp_path / "missing")]
    )
    assert rc == 2


def test_the_shipped_example_renders_into_a_usable_env(tmp_path):
    """Against the real .env.example, not a fixture: it is what ships."""
    text, _ = render_env.render(
        existing=None,
        example=(REPO_ROOT / ".env.example").read_text(encoding="utf-8"),
        overrides={"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_BOT_USERNAME": "mybot"},
        secret_factory=lambda: "S",
    )

    values = _values(text)
    assert values["TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert values["TELEGRAM_BOT_USERNAME"] == "mybot"
    # Nothing that would abort startup or silently open the bot up.
    assert values.get("APPROVED_DIRECTORY", "") == ""
    assert values.get("DEVELOPMENT_MODE", "false") == "false"
    assert values.get("DANGEROUSLY_ALLOW_ALL_USERS", "false") == "false"
    assert values["AUTH_TOKEN_SECRET"] == "S"


def test_a_no_op_rerun_leaves_no_backup(tmp_path):
    """Every .env.bak-* holds the bot token; a re-run that changes nothing
    must not produce one."""
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE, encoding="utf-8")
    base = ["--env", str(env), "--example", str(example), "--set", "ALLOWED_USERS=42"]
    assert render_env.main(base) == 0
    first = env.read_text(encoding="utf-8")

    assert render_env.main(base) == 0

    assert env.read_text(encoding="utf-8") == first
    assert list(tmp_path.glob(".env.bak-*")) == []
    assert env.stat().st_mode & 0o777 == 0o600
