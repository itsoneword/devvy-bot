"""Regressions found during review of the local macOS install."""

import os
import plistlib
import pty
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = (REPO / "install.sh").read_text()


def _section(start, end):
    return INSTALL.split(start, 1)[1].split(end, 1)[0]


def test_apple_silicon_launchd_path_finds_brew_tools(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/render_deploy.py"),
            "--launchd",
            "--out",
            str(tmp_path),
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "SERVICE_USER": "tester",
            "DEVVY_PLATFORM": "Darwin",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for name in ("server", "pump2"):
        plist = plistlib.loads((tmp_path / f"com.devvy.{name}.plist").read_bytes())
        path = plist["EnvironmentVariables"]["PATH"].split(":")
        assert "/opt/homebrew/bin" in path
        assert path.index("/opt/homebrew/bin") < path.index("/usr/bin")


def test_tui_is_locked_and_local_dependency_failure_is_fatal():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    lock = tomllib.loads((REPO / "poetry.lock").read_text())
    assert "textual" in project["tool"]["poetry"]["extras"]["tui"]
    assert any(package["name"] == "textual" for package in lock["package"])
    dependencies = _section("# 6. Dependencies\n", "# 7. Verify config + database")
    script = """
LOCAL=1; DRY_RUN=0; FAILED=0
info() { :; }
ok() { :; }
failed() { FAILED=1; }
run() { printf '%s\\n' "$*"; return 1; }
poetry() { :; }
""" + dependencies + '\nprintf "FAILED=%s\\n" "$FAILED"\n'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "poetry install --extras tui" in result.stdout
    assert "FAILED=1" in result.stdout


@pytest.mark.parametrize("choice", ["1", "3"])
def test_interactive_local_login_prompt_has_no_telegram_path(choice):
    auth = _section(
        "# 8. Claude / Anthropic auth -- always skippable\n", "# 9. systemd service"
    )
    script = """
LOCAL=1; SKIP_AUTH=0; DRY_RUN=0; ENV_FILE=/nonexistent
info() { printf '%s\\n' "$*"; }
say() { printf '%s\\n' "$*"; }
skipped() { :; }
failed() { :; }
ok() { :; }
step() { :; }
claude() { return 1; }
""" + auth.replace(
        "claude_logged_in() {",
        "claude_logged_in() { return 1; }\nunused_claude_logged_in() {",
    )
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["bash", "-c", script],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave)
        slave = -1
        os.write(master, f"{choice}\n".encode())
        out, err = proc.communicate(timeout=10)
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)
    assert proc.returncode == 0, err
    assert "claude auth login" in out
    assert "/auth" not in out
    assert "Telegram" not in out
