"""dv-55b7: ``pump2 doctor`` and the readable errors for a missing boards
file / missing .deviz/tasks/."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.pump2 import cli
from src.pump2.state import Board


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    (r / "f").write_text("x")
    _git(r, "add", "f")
    _git(r, "commit", "-m", "init")
    (r / ".deviz" / "tasks").mkdir(parents=True)
    return r


class Reg:
    def __init__(self, slug: str, working_dir: Path, extra: list[Path] | None = None):
        self.p = SimpleNamespace(
            slug=slug, working_dir=working_dir, extra_dirs=list(extra or [])
        )

    def get_by_slug(self, slug: str) -> Any:
        return self.p if slug == self.p.slug else None


def board(repo: Path, **kw: Any) -> Board:
    kw.setdefault("test", "true")
    kw.setdefault("profile", "devops")
    return Board(name="b", repo=repo, **kw)


def run(b: Board, reg: Any = None) -> list[str]:
    from src.pump2.doctor import check_board

    return check_board(b, reg)


def test_C1_green(repo: Path) -> None:
    reg = Reg("devops", repo)
    assert run(board(repo), reg) == []


def test_C1_repo_missing(tmp_path: Path) -> None:
    out = run(board(tmp_path / "nope"), None)
    assert out and all("\n" not in x for x in out)
    assert any("nope" in x for x in out)


def test_C1_not_git(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    (d / ".deviz" / "tasks").mkdir(parents=True)
    out = run(board(d), None)
    assert any("git" in x.lower() for x in out)


def test_C1_no_commits(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    _git(d, "init", "-b", "main")
    (d / ".deviz" / "tasks").mkdir(parents=True)
    out = run(board(d), None)
    assert any("commit" in x.lower() for x in out)


def test_C1_missing_branch(repo: Path) -> None:
    out = run(board(repo, branch="trunk"), None)
    assert len(out) == 1 and "trunk" in out[0]


def test_C1_no_deviz_tasks(repo: Path) -> None:
    (repo / ".deviz" / "tasks").rmdir()
    out = run(board(repo), None)
    assert len(out) == 1 and ".deviz/tasks" in out[0]


def test_C1_unknown_profile(repo: Path) -> None:
    out = run(board(repo, profile="ghost"), Reg("devops", repo))
    assert len(out) == 1 and "ghost" in out[0]


def test_C1_repo_outside_profile(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    out = run(board(repo), Reg("devops", other))
    assert len(out) == 1 and "extra_dirs" in out[0]


def test_C1_repo_in_extra_dirs_or_under_working_dir(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert run(board(repo), Reg("devops", other, [repo])) == []
    assert run(board(repo), Reg("devops", tmp_path)) == []


def test_C1_test_command_unresolvable(repo: Path) -> None:
    out = run(board(repo, test="no-such-binary-xyz -q"), None)
    assert len(out) == 1 and "no-such-binary-xyz" in out[0]
    assert run(board(repo, test=""), None)  # missing test command is a problem


def test_C1_test_command_absolute_path(repo: Path) -> None:
    assert run(board(repo, test=f"{sys.executable} -m pytest"), None) == []
    f = repo / "notexec"
    f.write_text("x")
    assert run(board(repo, test=f"{f} a"), None)


def test_C1_bad_pump2_python(repo: Path) -> None:
    out = run(board(repo, python="/no/such/python"), None)
    assert len(out) == 1 and "pump2_python" in out[0]
    assert run(board(repo, python=sys.executable), None) == []


def _boards_file(tmp: Path, entries: list[dict]) -> Path:
    p = tmp / "boards.json"
    p.write_text(json.dumps({"boards": entries}))
    return p


def test_C1_cli_exit_codes(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _boards_file(
        tmp_path, [{"name": "ok", "repo": str(repo), "pump2_test": "true"}]
    )
    # single-profile mode: no profiles.yaml, profile check skipped
    assert cli.main(["--boards", str(good), "doctor"]) == 0
    (repo / ".deviz" / "tasks").rmdir()
    assert cli.main(["--boards", str(good), "doctor", "ok"]) == 1
    assert ".deviz/tasks" in capsys.readouterr().out + ""


def test_C1_unknown_board_name(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _boards_file(tmp_path, [{"name": "ok", "repo": str(repo)}])
    assert cli.main(["--boards", str(good), "doctor", "zzz"]) == 2
    cap = capsys.readouterr()
    assert "ok" in cap.out + cap.err


def test_C2_red_on_temp_repo_without_deviz(repo: Path, tmp_path: Path) -> None:
    from src.pump2.doctor import check_boards

    (repo / ".deviz" / "tasks").rmdir()
    (repo / ".deviz").rmdir()
    f = _boards_file(tmp_path, [{"name": "t", "repo": str(repo), "pump2_test": "true"}])
    out = check_boards(f, None)
    assert out and any(".deviz" in x for x in out)


@pytest.mark.parametrize("cmd", ["tick", "status", "doctor"])
def test_C3_missing_boards_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], cmd: str
) -> None:
    missing = tmp_path / "none.json"
    rc = cli.main(["--boards", str(missing), cmd])
    cap = capsys.readouterr()
    assert rc != 0
    msg = cap.out + cap.err
    assert str(missing) in msg and "boards.example.json" in msg
    assert "Traceback" not in msg


def test_C3_missing_default_boards_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "default.json"
    monkeypatch.setattr(cli, "DEFAULT_BOARDS", missing)
    assert cli.main(["status"]) != 0
    cap = capsys.readouterr()
    assert str(missing) in cap.out + cap.err


def test_C4_board_without_tasks_dir_shows_error(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".deviz" / "tasks").rmdir()
    f = _boards_file(tmp_path, [{"name": "nb", "repo": str(repo)}])
    assert cli.main(["--boards", str(f), "status"]) == 0
    text = capsys.readouterr().out
    assert "ERROR" in text and ".deviz/tasks" in text
    assert cli.main(["--boards", str(f), "status", "--json"]) == 0
    rows = capsys.readouterr().out
    assert ".deviz/tasks" in rows and "ERROR" in rows.upper()


def test_C4_list_cards_reports_missing_dir(repo: Path) -> None:
    from src.pump2.state import list_cards

    (repo / ".deviz" / "tasks").rmdir()
    cards, bad = list_cards(board(repo))
    assert cards == [] and len(bad) == 1
    assert ".deviz/tasks" in str(bad[0][0]) + bad[0][1]


def _env(tmp: Path) -> Path:
    e = tmp / "t.env"
    e.write_text(
        "TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_BOT_USERNAME=t_bot\n"
        "ALLOWED_USERS=1\nAPPROVED_DIRECTORY=/tmp\n"
    )
    return e


def _verify(
    tmp: Path, mp: pytest.MonkeyPatch, preflight: bool = False
) -> list[tuple[bool, str, str]]:
    from src.boot.check import verify

    mp.delenv("PROFILES_CONFIG_PATH", raising=False)
    return asyncio.run(verify(skip_db=True, env_file=_env(tmp), preflight=preflight))


def test_C5_check_install_step_ok_without_boards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "DEFAULT_BOARDS", tmp_path / "absent.json")
    res = _verify(tmp_path, monkeypatch)
    labels = [r[1] for r in res]
    assert "pump2 boards" in labels
    assert labels.index("pump2 boards") == labels.index("profiles") + 1
    assert res[labels.index("pump2 boards")][0] is True


def test_C5_check_install_step_fails_with_first_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "boards.json"
    f.write_text("{}")
    monkeypatch.setattr(cli, "DEFAULT_BOARDS", f)
    calls: list[Any] = []

    def fake(path: Path, only: Any = None, *a: Any, **k: Any) -> list[str]:
        calls.append(path)
        return ["b: first problem -- fix it", "b: second"]

    monkeypatch.setattr("src.pump2.doctor.check_boards", fake)
    res = {r[1]: r for r in _verify(tmp_path, monkeypatch)}
    ok, _, detail = res["pump2 boards"]
    assert ok is False and "first problem" in detail and "second" not in detail
    assert calls


def test_C5_preflight_skips_boards_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "boards.json"
    f.write_text("{}")
    monkeypatch.setattr(cli, "DEFAULT_BOARDS", f)
    monkeypatch.setattr(
        "src.pump2.doctor.check_boards", lambda *a, **k: ["x: red -- fix"]
    )
    res = _verify(tmp_path, monkeypatch, True)
    assert "pump2 boards" not in [r[1] for r in res]


def test_C6_doctor_does_not_run_full_test_command(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    script = tmp_path / "suite.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    assert run(board(repo, test=f"{script} -q"), None) == []
    assert not marker.exists()


def test_C6_subprocess_timeouts(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = subprocess.run

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(k.get("timeout"))
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    run(board(repo, python=sys.executable), None)
    assert seen and all(t is not None and t <= 30 for t in seen)
