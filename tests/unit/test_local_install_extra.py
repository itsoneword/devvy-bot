"""Local-mode install details beyond the phase-4 scenarios (dv-fe4d).

``render_deploy.py --launchd`` renders only the launchd agents with the
laptop job cap; the Mac uninstall boots the agents out; ``--local`` on WSL2
has no launchd and says how to run the server and tick by hand; platform
detection keeps a WSL2 distro with systemd on the Linux matrix.
"""

import importlib.util
import os
import plistlib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "preflight_local_extra", REPO_ROOT / "scripts" / "preflight.py"
)
preflight = importlib.util.module_from_spec(_spec)
sys.modules["preflight_local_extra"] = preflight
_spec.loader.exec_module(preflight)


def _env(tmp_path, **extra):
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "NO_COLOR": "1",
        **extra,
    }


def test_render_launchd_only_writes_the_agents_with_the_job_cap(tmp_path):
    r = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "render_deploy.py"),
            "--launchd",
            "--out",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=_env(tmp_path, SERVICE_USER="tester", PUMP_MAX_CONCURRENT="1"),
    )
    assert r.returncode == 0, r.stderr
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["com.devvy.pump2.plist", "com.devvy.server.plist"]
    assert "sudoers" not in r.stdout
    tick = plistlib.loads((tmp_path / "com.devvy.pump2.plist").read_bytes())
    assert tick["EnvironmentVariables"]["BGJOB_MAX_CONCURRENT"] == "1"


def _install(tmp_path, *args, platform_name):
    return subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh"), *args],
        capture_output=True,
        text=True,
        env=_env(tmp_path, DEVVY_PLATFORM=platform_name),
        stdin=subprocess.DEVNULL,
        timeout=120,
        cwd=tmp_path,
    )


def test_mac_uninstall_boots_the_agents_out(tmp_path):
    r = _install(tmp_path, "--uninstall", "--dry-run", platform_name="Darwin")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "launchctl bootout" in out and "com.devvy.pump2" in out
    assert "systemctl" not in out and "sudoers" not in out.lower()


def test_mac_without_local_flag_is_local_mode(tmp_path):
    r = _install(tmp_path, "--dry-run", "--yes", "--skip-auth", platform_name="Darwin")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "CHANNEL=local" in out and "launchctl bootstrap" in out


def test_wsl_local_install_has_no_launchd_and_says_how_to_run(tmp_path):
    r = _install(
        tmp_path, "--local", "--dry-run", "--yes", "--skip-auth", platform_name="WSL"
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "launchctl" not in out and "systemctl enable" not in out
    assert "scripts/pump2 tick" in out and "process launcher" in out
    assert "botfather" not in out.lower()


def test_wsl_with_systemd_is_the_linux_matrix(monkeypatch, tmp_path):
    osrelease = tmp_path / "osrelease"
    osrelease.write_text("6.6.87.2-microsoft-standard-WSL2\n")
    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/sys/kernel/osrelease":
            return real_open(osrelease, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.delenv("DEVVY_PLATFORM", raising=False)
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setitem(preflight.__builtins__, "open", fake_open)
    monkeypatch.setattr(preflight.os.path, "exists", lambda p: True)
    assert preflight.detect_platform() == "Linux"
    monkeypatch.setattr(preflight.os.path, "exists", lambda p: False)
    assert preflight.detect_platform() == "WSL"
