"""dv-fe4d phase 2: the local TUI's credential stays out of every box.

The architect's RETURN (c)/(d): ``/internal/callback`` answers owner prompts,
so its bearer must be unreadable by the jobs and chat commands that raise
those prompts. It lives in its own file (``DataLayout.tui_secret_file``), not
in bot.env, which jobs read for agentctl's bearer.

(c) the Linux job box covers the file with /dev/null;
(d) both Seatbelt profiles deny it by name, after every allow.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from src.security import seatbelt
from src.utils.paths import DataLayout, ensure_tui_secret

REPO = Path(__file__).resolve().parents[2]


def _bgjob(monkeypatch, tmp_path: Path):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("BGJOB_ROOT", str(tmp_path / "jobs"))
    loader = SourceFileLoader("bgjob_tui_secret", str(REPO / "scripts" / "bgjob"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def data_root(tmp_path, monkeypatch) -> Path:  # type: ignore[no-untyped-def]
    root = tmp_path / "data"
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(root))
    return root


# --- the file -------------------------------------------------------------


def test_tui_secret_is_its_own_file_beside_bot_env(tmp_path) -> None:
    layout = DataLayout(root=tmp_path)
    assert layout.tui_secret_file == tmp_path / "secrets" / "tui.secret"
    assert layout.tui_secret_file != layout.bot_secrets_file


def test_ensure_tui_secret_creates_0600_once(tmp_path) -> None:
    path = tmp_path / "secrets" / "tui.secret"
    first = ensure_tui_secret(path)
    assert len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ensure_tui_secret(path) == first  # read back, not regenerated


# --- (c) the Linux job box -------------------------------------------------


def test_job_box_covers_the_tui_secret_with_dev_null(
    tmp_path, monkeypatch, data_root
) -> None:
    secret = data_root / "secrets" / "tui.secret"
    ensure_tui_secret(secret)
    bgjob = _bgjob(monkeypatch, tmp_path)
    monkeypatch.setattr(bgjob.shutil, "which", lambda n: "/usr/bin/bwrap")
    d = tmp_path / "job"
    d.mkdir()
    prefix = bgjob.job_box(d, [str(d)])
    assert prefix.endswith(f"--ro-bind /dev/null {secret}")
    # After every bind, so a write path containing it cannot expose it again.
    assert prefix.index(f"--bind {d} {d}") < prefix.index("/dev/null")


def test_job_box_skips_a_tui_secret_that_does_not_exist(
    tmp_path, monkeypatch, data_root
) -> None:
    bgjob = _bgjob(monkeypatch, tmp_path)
    monkeypatch.setattr(bgjob.shutil, "which", lambda n: "/usr/bin/bwrap")
    d = tmp_path / "job"
    d.mkdir()
    assert "/dev/null" not in bgjob.job_box(d, [str(d)]).split("--dev /dev")[1]


def _bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    r = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "true"],
        capture_output=True,
        timeout=20,
    )
    return r.returncode == 0


@pytest.mark.skipif(not _bwrap_works(), reason="bwrap cannot run on this host")
def test_command_in_the_linux_job_box_cannot_read_the_tui_secret(
    tmp_path, monkeypatch, data_root
) -> None:
    secret = data_root / "secrets" / "tui.secret"
    value = ensure_tui_secret(secret)
    (data_root / "secrets" / "bot.env").write_text("WEBHOOK_API_SECRET=x\n")
    bgjob = _bgjob(monkeypatch, tmp_path)
    d = tmp_path / "job"
    d.mkdir()
    prefix = bgjob.job_box(d, [str(d)]).replace("$box_cg", "")
    script = (
        f"{prefix} /bin/sh -c "
        f"'cat {secret}; echo; cat {data_root / 'secrets' / 'bot.env'}'"
    )
    r = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert value not in r.stdout
    assert "WEBHOOK_API_SECRET=x" in r.stdout  # jobs still read bot.env


# --- (d) the Seatbelt profiles ---------------------------------------------


def _deny_line(path: Path) -> str:
    return f'(deny file-read* file-write* (subpath "{path}"))'


def test_job_profile_denies_the_tui_secret_after_its_read_allow(tmp_path) -> None:
    secret = tmp_path / "secrets" / "tui.secret"
    text = seatbelt.job_profile([str(tmp_path)], hidden=[str(secret)])
    assert _deny_line(secret) in text
    # Seatbelt applies the last matching rule: the deny must follow the allows.
    assert text.index(_deny_line(secret)) > text.index("(allow file-read*)")
    assert text.index(_deny_line(secret)) > text.index(
        f'(allow file-write* (subpath "{tmp_path}"))'
    )


def test_bash_profile_denies_secrets_even_inside_a_grant(tmp_path) -> None:
    root = tmp_path / "data"
    hidden = [root / "secrets", root / "secrets" / "tui.secret"]
    text = seatbelt.bash_profile(
        write_dirs=[str(root)], home=str(tmp_path), hidden=[str(p) for p in hidden]
    )
    for p in hidden:
        assert text.index(_deny_line(p)) > text.index(
            f'(allow file-read* (subpath "{root}"))'
        )


def test_bgjob_mac_box_profile_hides_the_tui_secret(
    tmp_path, monkeypatch, data_root
) -> None:
    bgjob = _bgjob(monkeypatch, tmp_path)
    tools = {"sandbox-exec": "/usr/bin/sandbox-exec"}
    monkeypatch.setattr(bgjob.shutil, "which", lambda n: tools.get(n))
    d = tmp_path / "job"
    d.mkdir()
    bgjob.job_box(d, [str(d)])
    profile = (d / "box.sb").read_text()
    assert _deny_line(data_root / "secrets" / "tui.secret") in profile


def test_mac_chat_box_launcher_hides_secret_dirs_and_tui_secret(tmp_path) -> None:
    """bash_box.secret_paths, as the launcher (run as a script) computes it."""
    code = textwrap.dedent("""
        import sys, importlib.util
        from pathlib import Path
        path = Path(sys.argv[1]) / "src" / "security" / "bash_box.py"
        spec = importlib.util.spec_from_file_location("__box_main__", path)
        box = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = box
        spec.loader.exec_module(box)
        for p in box.secret_paths():
            print(p)
        """)
    root = tmp_path / "data"
    env = {**os.environ, "DEVVY_DATA_ROOT": str(root)}
    r = subprocess.run(
        [sys.executable, "-I", "-c", code, str(REPO)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    printed = r.stdout.split()
    assert str(root / "secrets") in printed
    assert str(root / "secrets" / "tui.secret") in printed


# --- review R1: the credential never comes from a file a job reads ---------


def _local_cfg(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / "config.env"
    cfg.write_text(f"CHANNEL=local\nAPPROVED_DIRECTORY={tmp_path}\n{extra}")
    return cfg


def test_boot_refuses_a_tui_secret_in_bot_env(tmp_path, monkeypatch, data_root) -> None:
    from src.config import load_config
    from src.exceptions import ConfigurationError

    monkeypatch.delenv("LOCAL_TUI_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    (data_root / "secrets").mkdir(parents=True)
    bot_env = data_root / "secrets" / "bot.env"
    bot_env.write_text("LOCAL_TUI_SECRET=from-bot-env\n")
    bot_env.chmod(0o600)
    with pytest.raises(ConfigurationError, match="LOCAL_TUI_SECRET"):
        load_config(config_file=_local_cfg(tmp_path))


def test_boot_refuses_a_tui_secret_in_the_config_file(tmp_path, monkeypatch) -> None:
    from src.config import load_config
    from src.exceptions import ConfigurationError

    monkeypatch.delenv("LOCAL_TUI_SECRET", raising=False)
    monkeypatch.delenv("DEVVY_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError, match="LOCAL_TUI_SECRET"):
        load_config(config_file=_local_cfg(tmp_path, "LOCAL_TUI_SECRET=x\n"))


def test_process_env_override_still_loads(tmp_path, monkeypatch) -> None:
    from src.config import load_config

    monkeypatch.delenv("DEVVY_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCAL_TUI_SECRET", "from-process-env")
    monkeypatch.chdir(tmp_path)
    config = load_config(config_file=_local_cfg(tmp_path))
    assert config.local_tui_secret == "from-process-env"


def test_env_example_does_not_offer_the_tui_secret() -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import gen_env_example
    finally:
        sys.path.remove(str(REPO / "scripts"))
    assert "LOCAL_TUI_SECRET" not in gen_env_example.generate()


# --- review R3: covered by the secret-dir deny in both layouts -------------


@pytest.mark.parametrize("root", [None, "data-root"])
def test_tui_secret_sits_inside_a_secret_dir(tmp_path, root) -> None:
    from src.security.denylist import build_protected_dirs

    layout = DataLayout(root=None if root is None else tmp_path / root)
    path = layout.tui_secret_file
    assert any(d == path.parent or d in path.parents for d in layout.secret_dirs)
    from src.utils.paths import tui_secret_path

    absolute = tui_secret_path(layout)
    assert absolute.is_absolute()
    everyone = build_protected_dirs(layout, archive=tmp_path / "archive").everyone
    assert any(d in absolute.parents for d in everyone)


def test_legacy_tui_secret_path_is_the_checkout_not_the_cwd(
    tmp_path, monkeypatch
) -> None:
    from src.utils.paths import REPO_ROOT, tui_secret_path

    monkeypatch.chdir(tmp_path)
    p = tui_secret_path(DataLayout(root=None))
    assert p == REPO_ROOT / "data" / "model_auth" / "tui.secret"
