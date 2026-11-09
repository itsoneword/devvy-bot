"""dv-5354: public design-loop recipe: docs section, example render/gate
scripts, and the ``pump2 doctor`` design checks."""

import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from src.designloop.steps import parse_config
from src.pump2.state import Board

ROOT = Path(__file__).resolve().parents[3]
EX = ROOT / "config" / "taskpump"
RENDER = EX / "design_render.example.py"
GATE = EX / "design_gate.example.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def _fake_python(path: Path, has_pil: bool) -> str:
    path.write_text(
        "#!/bin/sh\n"
        + ('case "$*" in *PIL*) exit 1;; esac\n' if not has_pil else "")
        + "exit 0\n"
    )
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    (r / "f").write_text("x")
    _git(r, "add", "f")
    _git(r, "commit", "-m", "init")
    (r / ".deviz" / "tasks").mkdir(parents=True)
    (r / "scripts").mkdir()
    return r


def _design(py: str, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "render": [py, "scripts/r.py", "{worktree}", "{out}", "{screen}"],
        "gate": [py, "scripts/g.py", "{worktree}", "{renders}", "{refs}", "{screen}"],
        "python": py,
    }
    d.update(kw)
    return d


def _check(repo: Path, design: Any) -> list[str]:
    from src.pump2.doctor import check_board

    b = Board(name="b", repo=repo, test="true", profile="devops", design=design)
    out = check_board(b, None)
    return out


def _stubs(repo: Path) -> None:
    for n in ("r.py", "g.py"):
        (repo / "scripts" / n).write_text("pass\n")


# ---------------------------------------------------------------- C4 doctor


def test_C4_no_design_no_lines(repo: Path) -> None:
    assert _check(repo, None) == []


def test_C4_valid_block_green(repo: Path, tmp_path: Path) -> None:
    _stubs(repo)
    py = _fake_python(tmp_path / "py", True)
    assert _check(repo, _design(py)) == []


def test_C4_malformed_block(repo: Path) -> None:
    out = _check(repo, {"render": "nope"})
    assert out and any("design" in x for x in out)
    assert all("\n" not in x and "--" in x for x in out)


def test_C4_missing_script(repo: Path, tmp_path: Path) -> None:
    (repo / "scripts" / "r.py").write_text("pass\n")
    py = _fake_python(tmp_path / "py", True)
    out = _check(repo, _design(py))
    assert any("g.py" in x for x in out) and not any("r.py" in x for x in out)
    assert all("--" in x for x in out)


def test_C4_first_token_not_executable(repo: Path, tmp_path: Path) -> None:
    _stubs(repo)
    py = _fake_python(tmp_path / "py", True)
    d = _design(py)
    d["render"][0] = "/nonexistent/pythonX"
    out = _check(repo, d)
    assert any("/nonexistent/pythonX" in x for x in out)


def test_C4_render_without_out(repo: Path, tmp_path: Path) -> None:
    _stubs(repo)
    py = _fake_python(tmp_path / "py", True)
    d = _design(py, render=[py, "scripts/r.py", "{worktree}", "{screen}"])
    out = _check(repo, d)
    assert any("{out}" in x for x in out)


def test_C4_gate_without_renders(repo: Path, tmp_path: Path) -> None:
    _stubs(repo)
    py = _fake_python(tmp_path / "py", True)
    d = _design(py, gate=[py, "scripts/g.py", "{worktree}", "{screen}"])
    out = _check(repo, d)
    assert any("{renders}" in x for x in out)


def test_C4_python_without_pillow(repo: Path, tmp_path: Path) -> None:
    _stubs(repo)
    py = _fake_python(tmp_path / "py", False)
    out = _check(repo, _design(py))
    assert any("PIL" in x or "Pillow" in x for x in out)
    assert all("--" in x for x in out)


# ------------------------------------------------- C5 examples pass doctor


def test_C5_examples_pass_doctor(repo: Path, tmp_path: Path) -> None:
    assert RENDER.is_file() and GATE.is_file()
    shutil.copy(RENDER, repo / "scripts" / "design_render.py")
    shutil.copy(GATE, repo / "scripts" / "design_gate.py")
    py = _fake_python(tmp_path / "py", True)
    design = {
        "render": [
            py,
            "scripts/design_render.py",
            "--worktree",
            "{worktree}",
            "--out",
            "{out}",
            "--screen",
            "{screen}",
        ],
        "gate": [
            py,
            "scripts/design_gate.py",
            "--worktree",
            "{worktree}",
            "--renders",
            "{renders}",
            "--refs",
            "{refs}",
            "--screen",
            "{screen}",
        ],
        "python": py,
    }
    assert parse_config(design) is not None
    assert _check(repo, design) == []


# --------------------------------------------------------- C3 gate example


def _pil() -> Any:
    return pytest.importorskip("PIL.Image")


def _png(
    path: Path, color: Any, size: tuple[int, int] = (60, 100), noisy: bool = True
) -> None:
    Image = _pil()
    img = Image.new("RGB", size, color)
    if noisy:
        for i in range(0, min(size), 3):
            img.putpixel((i, i), (255 - color[0], 255 - color[1], 7))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _gate(renders: Path, refs: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--worktree",
            str(renders.parent),
            "--renders",
            str(renders),
            "--refs",
            str(refs),
            "--screen",
            "home",
        ],
        capture_output=True,
        text=True,
        env={**os.environ},
    )


def _sets(tmp_path: Path) -> tuple[Path, Path]:
    refs, rend = tmp_path / "refs", tmp_path / "rend"
    for st in ("main", "empty"):
        _png(refs / f"{st}-dark.png", (10, 10, 10))
        _png(refs / f"{st}-light.png", (240, 240, 240))
        _png(rend / f"{st}-dark.png", (30, 20, 90))
        _png(rend / f"{st}-light.png", (200, 230, 120))
    return rend, refs


def _failures(p: subprocess.CompletedProcess[str]) -> list[str]:
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    return data["failures"] if isinstance(data, dict) else data


def test_C3_examples_exist_and_compile() -> None:
    for f in (RENDER, GATE):
        assert f.is_file(), f
        py_compile.compile(str(f), doraise=True)


def test_C3_gate_valid_set_passes(tmp_path: Path) -> None:
    assert GATE.is_file()
    rend, refs = _sets(tmp_path)
    assert _failures(_gate(rend, refs)) == []


def test_C3_gate_missing_state(tmp_path: Path) -> None:
    assert GATE.is_file()
    rend, refs = _sets(tmp_path)
    (rend / "empty-light.png").unlink()
    f = _failures(_gate(rend, refs))
    assert f and any("empty-light" in x for x in f)


def test_C3_gate_extra_state(tmp_path: Path) -> None:
    assert GATE.is_file()
    rend, refs = _sets(tmp_path)
    _png(rend / "bonus-dark.png", (5, 90, 5))
    f = _failures(_gate(rend, refs))
    assert any("bonus" in x for x in f)


def test_C3_gate_wrong_size(tmp_path: Path) -> None:
    assert GATE.is_file()
    rend, refs = _sets(tmp_path)
    _png(rend / "main-dark.png", (30, 20, 90), size=(80, 80))
    f = _failures(_gate(rend, refs))
    assert any("main-dark" in x for x in f)


def test_C3_gate_blank_image(tmp_path: Path) -> None:
    assert GATE.is_file()
    rend, refs = _sets(tmp_path)
    _png(rend / "main-dark.png", (255, 255, 255), noisy=False)
    f = _failures(_gate(rend, refs))
    assert any("main-dark" in x for x in f)


def test_C3_gate_dark_equals_light(tmp_path: Path) -> None:
    assert GATE.is_file()
    rend, refs = _sets(tmp_path)
    shutil.copy(rend / "main-dark.png", rend / "main-light.png")
    f = _failures(_gate(rend, refs))
    assert any("main" in x for x in f)


def test_C3_gate_identical_to_ref(tmp_path: Path) -> None:
    assert GATE.is_file()
    rend, refs = _sets(tmp_path)
    shutil.copy(refs / "empty-dark.png", rend / "empty-dark.png")
    f = _failures(_gate(rend, refs))
    assert any("empty-dark" in x for x in f)


# ---------------------------------------------------------- C7 render example


def test_C7_render_example_help_without_playwright() -> None:
    assert RENDER.is_file()
    p = subprocess.run(
        [sys.executable, str(RENDER), "--help"], capture_output=True, text=True
    )
    assert p.returncode == 0, p.stderr


# --------------------------------------------------------------- C1/C2 docs

DOC = (ROOT / "docs" / "pump.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "needle",
    [
        "design_screen",
        "designloop",
        "{worktree}",
        "{out}",
        "{screen}",
        "{renders}",
        "{refs}",
        "docs/features.yaml",
        "docs/SCREENS.md",
        "docs/UI_GUIDE.md",
        "<state>-<theme>.png",
        "design_render.example.py",
        "design_gate.example.py",
        ".screens/",
    ],
)
def test_C1_doc_mentions(needle: str) -> None:
    assert needle in DOC


def test_C2_doc_temp_db_rule_and_no_private_names() -> None:
    low = DOC.lower()
    assert "temp" in low and ("db" in low or "database" in low)
    assert "parallel" in low or "concurrent" in low
    assert "artefact" in low or "artifact" in low
    for bad in ("gymbro", "withkeys"):
        assert bad not in low
    for f in (RENDER, GATE):
        if f.is_file():
            assert "gymbro" not in f.read_text().lower()
