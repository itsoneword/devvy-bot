"""Pump v2 freeze/urgent (dv-44b1): one module, one tick hook."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.pump2 import freeze
from src.pump2.state import find_card, set_pump2

from .test_pump2 import TASKS, Fake, board, card
from .test_pump2 import repo as _repo

repo = _repo
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def sp(tmp_path: Path) -> Path:
    return tmp_path / "pump2" / "freeze.json"


def mk(repo: Path, cid: str, stage: str = "tests") -> "object":
    p = card(repo, cid)
    set_pump2(p, stage=stage)
    return find_card(board(repo, "true"), cid)


# ---- module ----------------------------------------------------------------
def test_no_state_file_launches(repo: Path, sp: Path) -> None:
    assert freeze.may_launch(mk(repo, "dv-0001"), sp) is True  # type: ignore[arg-type]
    assert freeze.status_line(sp) is None


def test_freeze_blocks_and_thaw_releases(repo: Path, sp: Path) -> None:
    c = mk(repo, "dv-0001")
    freeze.freeze("devops", sp)
    assert freeze.may_launch(c, sp) is False  # type: ignore[arg-type]
    st = freeze.read(sp)
    assert st.frozen and st.frozen["by"] == "devops" and st.frozen["at"]
    assert "FROZEN since" in (freeze.status_line(sp) or "")
    freeze.thaw("devops", sp)
    assert freeze.may_launch(c, sp) is True  # type: ignore[arg-type]
    assert freeze.status_line(sp) is None


def test_urgent_passes_only_its_card(repo: Path, sp: Path) -> None:
    a, b = mk(repo, "dv-0001"), mk(repo, "dv-0002")
    freeze.urgent("t:dv-0001", a.path, "owner", sp)  # type: ignore[attr-defined]
    assert freeze.may_launch(a, sp) is True  # type: ignore[arg-type]
    assert freeze.may_launch(b, sp) is False  # type: ignore[arg-type]
    assert "urgent: t:dv-0001" in (freeze.status_line(sp) or "")
    freeze.thaw("owner", sp)
    assert freeze.may_launch(b, sp) is True  # type: ignore[arg-type]


@pytest.mark.parametrize("end", ["done", "stopped", "gone"])
def test_urgent_clears_but_board_stays_frozen(repo: Path, sp: Path, end: str) -> None:
    a, b = mk(repo, "dv-0001"), mk(repo, "dv-0002")
    freeze.urgent("t:dv-0001", a.path, "owner", sp)  # type: ignore[attr-defined]
    if end == "gone":
        a.path.unlink()  # type: ignore[attr-defined]
    else:
        set_pump2(a.path, stage=end)  # type: ignore[attr-defined]
    assert freeze.may_launch(b, sp) is False  # type: ignore[arg-type]
    st = freeze.read(sp)
    assert st.frozen and not st.urgent
    assert "urgent" not in (freeze.status_line(sp) or "")


def test_corrupt_state_fails_closed_and_reports(repo: Path, sp: Path) -> None:
    sp.parent.mkdir(parents=True)
    sp.write_text("{not json")
    assert freeze.may_launch(mk(repo, "dv-0001"), sp) is False  # type: ignore[arg-type]
    assert "error" in (freeze.status_line(sp) or "").lower()


# ---- tick ------------------------------------------------------------------
@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "devvy"
    monkeypatch.setenv("DEVVY_ROOT", str(r))
    return r


def test_tick_holds_model_stage_and_merge(
    repo: Path, tmp_path: Path, root: Path
) -> None:
    b, f = board(repo, "true"), Fake()
    mk(repo, "dv-0001", "tests")
    p2 = card(repo, "dv-0002")
    set_pump2(p2, stage="merge", branch="pump2/dv-0002")
    freeze.freeze("owner")
    out = f.tick(b, tmp_path)
    assert "t:dv-0001 held: frozen" in out
    assert "t:dv-0002 held: frozen" in out
    assert f.launched == []
    assert not find_card(b, "dv-0002").pump2.get("job")
    freeze.thaw("owner")
    assert not any("held: frozen" in ln for ln in f.tick(b, tmp_path))


def test_tick_still_reaps_running_job(repo: Path, tmp_path: Path, root: Path) -> None:
    b, f = board(repo, "true"), Fake()
    p = card(repo, "dv-0001")
    set_pump2(p, stage="tests", job="job-9")
    f.running = True
    freeze.freeze("owner")
    out = f.tick(b, tmp_path)
    assert out and not any("held: frozen" in ln for ln in out)


def test_tick_urgent_card_launches(repo: Path, tmp_path: Path, root: Path) -> None:
    b, f = board(repo, "true"), Fake()
    set_pump2(card(repo, "dv-0001"), stage="analyze")
    set_pump2(card(repo, "dv-0002"), stage="analyze")
    freeze.urgent("t:dv-0002", repo / TASKS / "dv-0002.md", "owner")
    f.running = True
    out = f.tick(b, tmp_path)
    assert "t:dv-0001 held: frozen" in out
    assert [x[0] for x in f.launched] == ["dv-0002"]


# ---- CLI -------------------------------------------------------------------
def run(root: Path, boards: Path, *a: str) -> "subprocess.CompletedProcess[str]":
    import os

    env = {**os.environ, "DEVVY_ROOT": str(root)}
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/pump2"), "--boards", str(boards), *a],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


def test_cli_freeze_urgent_thaw_status(repo: Path, tmp_path: Path) -> None:
    mk(repo, "dv-0001", "tests")
    bf = tmp_path / "boards.json"
    bf.write_text(json.dumps({"boards": [{"name": "t", "repo": str(repo)}]}))
    r = tmp_path / "root"
    assert run(r, bf, "freeze", "--by", "tester").returncode == 0
    s = run(r, bf, "status")
    assert s.stdout.splitlines()[0].startswith("FROZEN since ")
    assert "by tester" in s.stdout.splitlines()[0]
    assert run(r, bf, "urgent", "t:dv-0001", "--by", "tester").returncode == 0
    assert "urgent: t:dv-0001" in run(r, bf, "status").stdout.splitlines()[0]
    js = run(r, bf, "status", "--json")
    assert isinstance(json.loads(js.stdout), list)
    bad = run(r, bf, "urgent", "t:dv-9999", "--by", "tester")
    assert bad.returncode == 2 and (bad.stderr or bad.stdout).strip()
    assert run(r, bf, "thaw", "--by", "tester").returncode == 0
    assert "FROZEN" not in run(r, bf, "status").stdout


def test_only_freeze_module_knows_the_state() -> None:
    hits = []
    for p in (ROOT / "src/pump2").rglob("*.py"):
        if p.name == "freeze.py":
            continue
        for ln in p.read_text().splitlines():
            if "freeze.json" in ln:
                hits.append(f"{p.name}: {ln}")
    assert hits == []


@pytest.mark.parametrize(
    "content, flagged",
    [
        (None, False),
        ('{"frozen": {"at": "x", "by": "owner"}, "urgent": null}', True),
        ("{broken", True),  # unreadable state fails closed
    ],
)
def test_release_tick_passes_the_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
    flagged: bool,
) -> None:
    """dv-81a7 review: `scripts/release tick` gets --frozen when pump2 is
    frozen, so a frozen pump starts no release check (as the old tick)."""
    from src.pump2 import cli

    state = tmp_path / "freeze.json"
    monkeypatch.setattr(freeze, "default_path", lambda: state)
    release = tmp_path / "release"  # scripts/release is not shipped publicly
    release.write_text("")
    monkeypatch.setattr(cli, "RELEASE", release)
    if content is not None:
        state.write_text(content)
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.release_tick() == []
    assert seen and seen[0][-1] == ("--frozen" if flagged else "tick")
