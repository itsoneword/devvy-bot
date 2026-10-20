"""The bash box (dv-98f2): argv shape, quoting, and a real bwrap round trip."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.security.bash_box import (
    BashBox,
    BoxUnavailable,
    build_bwrap_argv,
    scratch_dir,
)

pytestmark = pytest.mark.skipif(shutil.which("bwrap") is None, reason="no bwrap")


def _box(tmp_path: Path, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws, BashBox(write_dirs=(ws,), **kw)


def test_command_is_one_argv_element(tmp_path) -> None:
    ws, box = _box(tmp_path)
    nasty = "echo 'a b' \"c\"; $(id) `id` && rm -rf / # \\ \n echo end"
    words = shlex.split(box.wrap(nasty))
    assert words[-2:] == ["--", nasty]  # one argv element, after the separator
    assert words[0] == sys.executable and words[1].endswith("bash_box.py")


def test_argv_binds_only_what_was_granted(tmp_path) -> None:
    ws, box = _box(tmp_path)
    ro = tmp_path / "tool"
    ro.mkdir()
    argv = build_bwrap_argv(write_dirs=[ws], read_dirs=[ro], chdir=ws)
    joined = " ".join(argv)
    assert f"--bind {ws} {ws}" in joined
    assert f"--ro-bind {ro} {ro}" in joined
    assert f"--bind {scratch_dir()} {scratch_dir()}" in joined
    assert "--tmpfs /tmp" in joined and "--tmpfs /run" in joined
    assert f"--tmpfs {Path.home()}" in joined
    assert "--unshare-pid" in argv and "--unshare-net" not in argv
    assert not any(a in (".claude", "secrets") for a in argv)
    # No blanket bind of / and nothing outside the grants is writable.
    assert ["--bind", "/", "/"] != argv[:3]
    assert argv.count("--bind") == 2  # workspace + scratch


def test_launcher_execs_bwrap_with_bash_c(tmp_path, monkeypatch) -> None:
    from src.security import bash_box

    ws, _ = _box(tmp_path)
    seen = {}
    monkeypatch.setattr(
        bash_box.os, "execv", lambda path, argv: seen.update(path=path, argv=argv)
    )
    bash_box.main(["--cwd", str(ws), "--rw", str(ws), "--", "echo $HOME"])
    assert seen["path"].endswith("bwrap")
    assert seen["argv"][-4:] == ["--", "/bin/bash", "-c", "echo $HOME"]


def test_chdir_outside_grants_falls_back_to_workspace(tmp_path) -> None:
    ws, _ = _box(tmp_path)
    argv = build_bwrap_argv(write_dirs=[ws], chdir=Path("/etc"))
    assert argv[argv.index("--chdir") + 1] == str(ws)


def test_missing_bwrap_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(BoxUnavailable):
        build_bwrap_argv(write_dirs=[tmp_path])


def _run(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", "-c", command], capture_output=True, text=True, timeout=30
    )


@pytest.fixture
def bwrap_works() -> None:
    probe = _run("bwrap --ro-bind / / true")
    if probe.returncode != 0:
        pytest.skip(f"bwrap cannot start here: {probe.stderr.strip()[:100]}")


def test_round_trip(tmp_path, bwrap_works) -> None:
    ws, box = _box(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    cmd = (
        f"pwd; echo ok > in.txt; cat {outside}/secret.txt; "
        f'touch {outside}/w; touch /etc/w; echo "it\'s $((1+1))"'
    )
    res = _run(box.wrap(cmd, str(ws)))
    assert str(ws) in res.stdout
    assert "it's 2" in res.stdout
    assert (ws / "in.txt").read_text() == "ok\n"
    assert not (outside / "w").exists()
    assert "nope" not in res.stdout
