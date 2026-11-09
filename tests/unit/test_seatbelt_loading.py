"""dv-fe4d: seatbelt.py as its real callers load it, by file path.

scripts/bgjob, bash_box.py (run as a script) and scripts/seatbelt_probe all
load seatbelt.py without the ``src`` package imported, so its own load of
bash_box.py must work standalone. Each case runs in a fresh ``python -I``
so nothing this pytest session already imported can mask a failure (the
review of phase 1 found test_seatbelt.py passing only after test_bash_box).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from src.security import seatbelt

REPO = Path(__file__).resolve().parents[2]


def _fresh(code: str, tmp_path: Path) -> str:
    r = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(code), str(REPO), str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_bgjob_builds_the_mac_job_box_in_a_fresh_process(tmp_path) -> None:
    out = _fresh(
        """
        import os, sys
        from importlib.machinery import SourceFileLoader
        import importlib.util
        from pathlib import Path
        repo, tmp = Path(sys.argv[1]), Path(sys.argv[2])
        os.environ["BGJOB_ROOT"] = str(tmp)
        loader = SourceFileLoader("bgjob_fresh", str(repo / "scripts" / "bgjob"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        bgjob = importlib.util.module_from_spec(spec)
        loader.exec_module(bgjob)
        assert "src.security.bash_box" not in sys.modules
        tools = {"sandbox-exec": "/usr/bin/sandbox-exec"}
        bgjob.shutil.which = lambda n: tools.get(n)
        d = tmp / "job"
        d.mkdir()
        print(bgjob.job_box(d, [str(d)]))
        print((d / "box.sb").read_text())
        """,
        tmp_path,
    )
    job = tmp_path / "job"
    assert f"/usr/bin/sandbox-exec -f {job / 'box.sb'}" in out
    assert "(deny default)" in out and '(literal "/bin/launchctl")' in out
    assert f'(allow file-write* (subpath "{job}"))' in out


def test_bash_box_builds_the_mac_chat_box_when_loaded_by_path(tmp_path) -> None:
    out = _fresh(
        """
        import sys, shutil
        import importlib.util
        from pathlib import Path
        repo, tmp = Path(sys.argv[1]), Path(sys.argv[2])
        path = repo / "src" / "security" / "bash_box.py"
        spec = importlib.util.spec_from_file_location("__box_main__", path)
        box = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = box  # as python runs it: __main__ is registered
        spec.loader.exec_module(box)
        assert "src.security.bash_box" not in sys.modules
        tools = {"sandbox-exec": "/usr/bin/sandbox-exec"}
        shutil.which = lambda n: tools.get(n)
        ws = tmp / "ws"
        ws.mkdir()
        argv = box.build_seatbelt_argv(write_dirs=[ws], home=Path("/Users/t"))
        assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"], argv
        print(argv[2])
        """,
        tmp_path,
    )
    assert '(deny file-read* (subpath "/Users/t"))' in out
    assert f'(allow file-write* (subpath "{tmp_path / "ws"}"))' in out


def test_seatbelt_loads_standalone_in_a_fresh_process(tmp_path) -> None:
    out = _fresh(
        """
        import sys
        import importlib.util
        from pathlib import Path
        repo = Path(sys.argv[1])
        spec = importlib.util.spec_from_file_location(
            "_bgjob_seatbelt", repo / "src" / "security" / "seatbelt.py"
        )
        sb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sb)
        print(sb._HOME_RO)
        """,
        tmp_path,
    )
    assert ".local/bin" in out


def test_chat_profile_stats_only_home_and_grant_ancestors() -> None:
    """realpath/getcwd of a granted dir need stat on each ancestor; a
    non-granted sibling under $HOME stays unstat-able."""
    text = seatbelt.bash_profile(
        write_dirs=["/Users/t/code/ws"], read_dirs=[], home="/Users/t"
    )
    deny = '(deny file-read* (subpath "/Users/t"))'
    after = text[text.index(deny) :]
    meta = [ln for ln in after.splitlines() if "file-read-metadata" in ln]
    assert '(allow file-read-metadata (literal "/Users/t"))' in meta
    assert '(allow file-read-metadata (literal "/Users/t/code"))' in meta
    assert '(allow file-read-metadata (literal "/Users/t/.local"))' in meta
    assert all("(literal " in ln for ln in meta)  # never a subpath under $HOME
    assert not any("/Users/t/code/ws" in ln for ln in meta)  # subpath covers it
