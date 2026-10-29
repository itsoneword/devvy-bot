"""dv-58db: edge cases of a pump job's free root in ``destructive.classify``.

The approved scenarios live in ``test_policy_parity.py``; these are the
parser corners around them: wrappers, odd spellings, globs, symlinks.
"""

from pathlib import Path
from typing import Any, Optional

import pytest

from src.security import destructive
from src.security.destructive import classify


@pytest.fixture
def root(tmp_path: Path, monkeypatch: Any) -> Path:
    base = tmp_path.resolve()
    monkeypatch.setattr(destructive, "_temp_roots", lambda: (str(base / "faketmp"),))
    root = base / "pump-worktrees" / "dv-ab12"
    (root / ".pump" / "d").mkdir(parents=True)
    (root / ".git").write_text("gitdir: /x/.git/worktrees/dv-ab12\n")
    (root / ".pump" / "a.jsonl").write_text("{}\n")
    (root / "-x").write_text("dash\n")
    (base / "outside").mkdir()
    (base / "outside" / "f").write_text("f\n")
    return root


def _ask(command: str, root: Path, free_root: Optional[Path] = None) -> Any:
    return classify("Bash", {"command": command}, root, free_root or root)


@pytest.mark.parametrize(
    "command",
    [
        "rm .pump/a.jsonl",
        "rm ./.pump/a.jsonl",
        "rm -rf .pump/d",
        "rm -f .pump/missing_*.log",  # no match: rm sees the literal, inside
        "rm -rf .pump/*",
        "rm tests/../.pump/a.jsonl",
        "rm .pump/a.jsonl && echo done",
    ],
)
def test_free_inside_the_worktree(command: str, root: Path) -> None:
    assert _ask(command, root) is None


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf ./*",
        "rm -rf ./",
        "rm -rf ../dv-ab12",
        "rm .pump/../../dv-ab12x/f",
        "rm ~user/x",
        "rm .pump/{a,b}.jsonl",
        "rm $(cat list)",
        "rm `cat list`",
        "rm ./.git/index",
        "env rm .pump/a.jsonl",
        "timeout 5 rm .pump/a.jsonl",
        "nohup rm .pump/a.jsonl",
        "pushd /tmp && rm .pump/a.jsonl",
        "bash -c 'cd / && rm .pump/a.jsonl'",
        "rm -- -x",  # target hidden behind `--`: fail closed
        "git clean -fdx",
        "shred .pump/a.jsonl",
    ],
)
def test_still_asks_in_the_worktree(command: str, root: Path) -> None:
    assert _ask(command, root) is not None


def test_symlink_made_by_the_job_then_deleted_asks(root: Path) -> None:
    (root / ".pump" / "out").symlink_to(root.parent.parent / "outside")
    assert _ask("rm -rf .pump/out/f", root) is not None
    assert _ask("rm .pump/out", root) is not None


def test_glob_match_through_a_symlink_out_asks(root: Path) -> None:
    (root / ".pump" / "d" / "z").symlink_to(root.parent.parent / "outside" / "f")
    assert _ask("rm .pump/d/*", root) is not None


def test_no_free_root_keeps_todays_rule(root: Path) -> None:
    assert classify("Bash", {"command": "rm .pump/a.jsonl"}, root) is not None


# Review of dv-58db: realpath runs before the line does, so a link (or move,
# copy, interpreter) made earlier in the same command is invisible to it. Any
# command beyond a deletion and a few read-only ones voids the free root.
@pytest.mark.parametrize(
    "command",
    [
        "ln -s /etc x && rm -rf x/y",
        "ln -s {home} lnk; rm lnk/important",
        "mv {outside} .pump/m && rm -rf .pump/m",
        "cp -as {outside} .pump/c && rm -rf .pump/c",
        "tar xf a.tar && rm -rf .pump/d",
        'python3 -c \'import os; os.symlink("/etc", "x")\' && rm -rf x/y',
        "bash -c 'ln -s /etc x' && rm -rf x/y",
        "git checkout other && rm -rf .pump/d",
        "rm .pump/a.jsonl; ln -s /etc .pump/a.jsonl",
    ],
)
def test_path_making_command_on_the_line_asks(command: str, root: Path) -> None:
    command = command.format(
        home=root.parent.parent, outside=root.parent.parent / "outside"
    )
    assert _ask(command, root) is not None


@pytest.mark.parametrize(
    "command",
    [
        "ln -s /etc x && rm -rf x/y",
        "mv ../x .pump/m && rm -rf .pump/m",
    ],
)
def test_path_making_command_asks_through_decide(command: str, root: Path) -> None:
    from src.security import policy

    turn = policy.TurnPolicy(
        working_directory=root, full_access=True, unrestricted=True, free_root=root
    )
    assert policy.decide("Bash", {"command": command}, turn).action == policy.ASK


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p .pump/d && rm -rf .pump/d",
        "ls .pump && rm .pump/a.jsonl && echo ok",
        "test -f .pump/a.jsonl && rm -f .pump/a.jsonl",
    ],
)
def test_read_only_companions_keep_the_free_root(command: str, root: Path) -> None:
    assert _ask(command, root) is None
