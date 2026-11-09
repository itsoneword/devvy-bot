"""dv-fe4d phase 1: launcher choice, the macOS boxes as wired into bgjob and
the chat box, and the /proc-less process tree. Runs on Linux: sandbox-exec is
faked by name, the profiles are checked as text."""

from __future__ import annotations

import argparse
import importlib.util
import json
import py_compile
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from src.claude import process_tree
from src.platform import launcher
from src.security import bash_box

_SCRIPTS = Path(__file__).parents[2] / "scripts"


def _load(name: str):
    loader = SourceFileLoader(f"{name}_platform_test", str(_SCRIPTS / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# --- launcher choice --------------------------------------------------------


@pytest.mark.parametrize(
    "forced, systemd_run, want",
    [
        ("process", "/usr/bin/systemd-run", "process"),
        ("systemd", None, "systemd"),
        ("", "/usr/bin/systemd-run", "systemd"),
        ("", None, "process"),  # macOS, WSL2 without systemd
        ("bogus", None, "process"),
    ],
)
def test_launcher_choice(tmp_path, monkeypatch, forced, systemd_run, want) -> None:
    monkeypatch.setenv("DEVVY_LAUNCHER", forced)
    monkeypatch.setattr(
        launcher.shutil, "which", lambda n: systemd_run if n == "systemd-run" else None
    )
    monkeypatch.setattr(launcher, "SYSTEMD_BOOTED", tmp_path)  # systemd is init
    assert launcher.kind() == want


def test_systemd_run_without_a_running_systemd_is_process(
    tmp_path, monkeypatch
) -> None:
    """WSL2 without systemd / a container: the binary is there, no manager."""
    monkeypatch.delenv("DEVVY_LAUNCHER", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda n: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "SYSTEMD_BOOTED", tmp_path / "absent")
    assert launcher.kind() == launcher.PROCESS


def test_outcome_mapping(tmp_path) -> None:
    def put(**rec):
        (tmp_path / launcher.OUTCOME).write_text(json.dumps(rec))
        return launcher.outcome(tmp_path)

    assert launcher.outcome(tmp_path) == ("", "")
    assert put(result="memcap", peak="300M") == ("oom-kill", "300M")
    assert put(result="signal", signal=9, peak="") == ("signal-9", "")
    assert put(result="exit", rc=0, peak="12M") == ("", "12M")


def test_finished_job_is_not_active(tmp_path) -> None:
    (tmp_path / launcher.SUPERVISOR_PID).write_text("1")  # alive, but done
    (tmp_path / launcher.OUTCOME).write_text("{}")
    assert not launcher.active(tmp_path)
    assert not launcher.active(tmp_path / "missing")


# --- the macOS job box in bgjob ---------------------------------------------


def _agent_args() -> argparse.Namespace:
    return argparse.Namespace(
        prompt="do it",
        cmd=None,
        profile="general",
        name="t",
        allow_dir=None,
        model="sonnet",
        quiet=True,
        force=True,
        cwd=None,
        task=None,
        queue=False,
        batch=None,
        route=None,
        pre_cmd=None,
        pre_log=None,
        setting_sources=None,
    )


def test_bgjob_uses_seatbelt_when_there_is_no_bwrap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BGJOB_ROOT", str(tmp_path))
    monkeypatch.delenv("DEVVY_LAUNCHER", raising=False)
    bgjob = _load("bgjob")
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    tools = {"sandbox-exec": "/usr/bin/sandbox-exec", "systemd-run": "/bin/sr"}
    monkeypatch.setattr(bgjob.shutil, "which", lambda n: tools.get(n))

    class R:
        returncode = 0
        stderr = ""
        stdout = '{"harness": "claude"}'  # the `agentjob --print-route` probe

    monkeypatch.setattr(bgjob.subprocess, "run", lambda *a, **k: R())
    bgjob.cmd_run(_agent_args())

    run_sh = next(tmp_path.glob("*/run.sh")).read_text()
    profile = next(tmp_path.glob("*/box.sb"))
    assert f"/usr/bin/sandbox-exec -f {profile}" in run_sh
    assert "bwrap" not in run_sh
    text = profile.read_text()
    assert "(deny default)" in text and '(literal "/bin/launchctl")' in text
    assert f'(subpath "{profile.parent}")' in text  # the job dir is writable


# --- the macOS chat box -----------------------------------------------------


def test_chat_box_falls_back_to_sandbox_exec(tmp_path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    tools = {"sandbox-exec": "/usr/bin/sandbox-exec"}
    monkeypatch.setattr("shutil.which", lambda n: tools.get(n))
    assert bash_box.BashBox(write_dirs=(ws,)).wrap("true")  # no BoxUnavailable
    seen: dict = {}
    monkeypatch.setattr(
        bash_box.os, "execv", lambda path, argv: seen.update(path=path, argv=argv)
    )
    monkeypatch.setattr(bash_box.os, "chdir", lambda p: seen.update(cwd=str(p)))
    bash_box.main(["--cwd", "/etc", "--rw", str(ws), "--", "echo hi"])
    assert seen["path"] == "/usr/bin/sandbox-exec"
    assert seen["argv"][:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert seen["argv"][-3:] == ["/bin/bash", "-c", "echo hi"]
    assert "--" not in seen["argv"]
    assert "(deny file-read* (subpath " in seen["argv"][2]
    assert seen["cwd"] == str(ws)  # outside the grants: back to the workspace


def test_chat_box_without_any_tool_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda n: None)
    with pytest.raises(bash_box.BoxUnavailable):
        bash_box.BashBox(write_dirs=(tmp_path,)).wrap("true")


# --- process tree without /proc ---------------------------------------------


def test_process_tree_uses_ps_without_proc(monkeypatch) -> None:
    monkeypatch.setattr(process_tree, "_PROC", "/nonexistent-proc")

    class R:
        stdout = "  10     1\n  11    10\n  12    11\n  13     1\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert process_tree.descendants(10) == [12, 11]


def test_seatbelt_probe_compiles(tmp_path) -> None:
    py_compile.compile(
        str(_SCRIPTS / "seatbelt_probe"), cfile=str(tmp_path / "p.pyc"), doraise=True
    )
