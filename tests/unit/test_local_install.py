"""macOS / local-mode install path (dv-fe4d phase 4).

Interfaces the build must provide (invented here from the analysis):
  * ``DEVVY_PLATFORM`` env var (Darwin / Linux / WSL) overrides OS detection in
    ``scripts/preflight.py`` and ``install.sh`` so the Mac path is testable.
  * ``preflight.build_specs()`` follows the platform; on WSL (no systemd) the
    local-mode matrix needs no systemd/sudo/visudo.
  * ``install.sh --local``: no Telegram questions, launchd on Darwin,
    CHANNEL=local and PUMP_MAX_CONCURRENT=1 in the install plan / env file.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "preflight_local", REPO_ROOT / "scripts" / "preflight.py"
)
preflight = importlib.util.module_from_spec(_spec)
sys.modules["preflight_local"] = preflight
_spec.loader.exec_module(preflight)


def _stub_host(monkeypatch, system, have, machine="arm64"):
    """Pretend to be ``system`` with exactly the binaries in ``have``."""
    monkeypatch.setattr(preflight.platform, "system", lambda: system)
    monkeypatch.setattr(preflight.platform, "machine", lambda: machine)
    monkeypatch.setenv("DEVVY_PLATFORM", system)
    monkeypatch.setattr(
        preflight.shutil, "which", lambda n: f"/usr/bin/{n}" if n in have else None
    )

    def fake_version(cmd):
        if cmd[0] not in have:
            return None
        return {
            "node": "v22.1.0",
            "poetry": "Poetry (version 2.1.0)",
            "git": "git version 2.43.0",
            "claude": "2.1.257 (Claude Code)",
        }.get(cmd[0], "1.0")

    monkeypatch.setattr(preflight, "run_version", fake_version)


def _results():
    return {r.check.key: r for r in preflight.run_checks(preflight.build_specs())}


MAC_HAVE = {"node", "poetry", "git", "claude", "sandbox-exec", "brew"}


def test_preflight_passes_on_darwin_with_mac_tools(monkeypatch):
    _stub_host(monkeypatch, "Darwin", MAC_HAVE)
    res = _results()
    assert not preflight.missing_required(list(res.values())), {
        k: v.note for k, v in res.items() if not v.ok
    }
    for key in ("systemd", "bwrap", "sudo", "visudo"):
        assert key not in res
    assert res["os"].ok


def test_darwin_fixes_use_brew_not_apt(monkeypatch):
    _stub_host(monkeypatch, "Darwin", {"claude", "sandbox-exec"})
    res = _results()
    missing = [r for r in res.values() if not r.ok]
    assert missing, "git/node/poetry were stubbed absent"
    for r in missing:
        cmds = r.check.fix + " " + (r.check.autofix or "")
        assert "apt" not in cmds and "sudo -E bash" not in cmds
    assert any("brew install" in (r.check.autofix or r.check.fix) for r in missing)


def test_darwin_without_sandbox_exec_fails_required(monkeypatch):
    _stub_host(monkeypatch, "Darwin", MAC_HAVE - {"sandbox-exec"})
    bad = preflight.missing_required(list(_results().values()))
    assert any(
        "sandbox" in r.check.key or "sandbox" in r.check.label.lower() for r in bad
    )


def test_linux_systemd_matrix_unchanged(monkeypatch):
    _stub_host(monkeypatch, "Linux", {"node"}, "x86_64")
    monkeypatch.delenv("DEVVY_PLATFORM", raising=False)
    keys = [s.check.key for s in preflight.build_specs()]
    assert keys == [
        "os",
        "systemd",
        "python",
        "git",
        "poetry",
        "node",
        "claude",
        "bwrap",
        "sudo",
        "visudo",
    ]


def test_wsl2_without_systemd_needs_no_systemd(monkeypatch):
    _stub_host(monkeypatch, "Linux", {"node", "poetry", "git", "claude", "bwrap"})
    monkeypatch.setenv("DEVVY_PLATFORM", "WSL")
    monkeypatch.setattr(preflight.os.path, "exists", lambda p: False)
    res = _results()
    assert "systemd" not in res or not res["systemd"].check.required
    assert not preflight.missing_required(list(res.values()))


def test_wsl2_without_systemd_falls_back_to_process_launcher(monkeypatch):
    from src.platform import launcher

    monkeypatch.delenv("DEVVY_LAUNCHER", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(launcher, "SYSTEMD_BOOTED", Path("/nonexistent/run/systemd"))
    assert launcher.kind() == launcher.PROCESS


# --- install.sh --local --dry-run ----------------------------------------


def _install(tmp_path, *args, platform_name="Darwin"):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "DEVVY_PLATFORM": platform_name,
        "NO_COLOR": "1",
    }
    return subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=120,
        cwd=tmp_path,
    )


def test_mac_local_install_dry_run_plan(tmp_path):
    r = _install(tmp_path, "--local", "--dry-run", "--yes", "--skip-auth")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "launchctl" in out
    assert "com.devvy.server" in out and "com.devvy.pump2" in out
    assert "PUMP_MAX_CONCURRENT=1" in out
    assert "CHANNEL=local" in out
    assert "systemctl enable" not in out and "sudoers" not in out.lower()
    assert "apt-get" not in out


def test_local_install_asks_no_telegram_questions(tmp_path):
    r = _install(tmp_path, "--local", "--dry-run", "--yes", "--skip-auth")
    out = (r.stdout + r.stderr).lower()
    assert r.returncode == 0, out
    assert "botfather" not in out
    assert "bot token" not in out
    assert "owner capture" not in out.replace("owner capture  (", "")


def test_linux_install_dry_run_still_uses_systemd(tmp_path):
    r = _install(
        tmp_path,
        "--dry-run",
        "--yes",
        "--skip-auth",
        "--token",
        "123:abc",
        "--owner-id",
        "5",
        platform_name="Linux",
    )
    out = r.stdout + r.stderr
    assert "systemctl enable claude-bot" in out
    assert "launchctl" not in out
    assert "PUMP_MAX_CONCURRENT=1" not in out


def test_local_install_dry_run_twice_is_the_same_plan(tmp_path):
    a = _install(tmp_path, "--local", "--dry-run", "--yes", "--skip-auth")
    b = _install(tmp_path, "--local", "--dry-run", "--yes", "--skip-auth")
    assert a.returncode == b.returncode == 0
    assert a.stdout.count("launchctl") == b.stdout.count("launchctl") > 0
