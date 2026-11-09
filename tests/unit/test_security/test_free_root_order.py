"""dv-3d18 phase 2: the order rule of a pump job's free root, parser corners.

The approved scenarios are in ``test_job_rm_chains.py``; these pin the
pieces under them: ``cd`` and variable following, redirects on the rm,
groups and functions, and which asks outrank the instant "run rm alone" deny.
"""

from pathlib import Path
from typing import Any

import pytest

from src.security import destructive, policy
from src.security.destructive import RUN_RM_ALONE, _segments, classify


@pytest.fixture
def root(tmp_path: Path, monkeypatch: Any) -> Path:
    base = tmp_path.resolve()
    fake_tmp = base / "faketmp"
    (fake_tmp / "d").mkdir(parents=True)
    monkeypatch.setattr(destructive, "_temp_roots", lambda: (str(fake_tmp),))
    root = base / "pump-worktrees" / "dv-ab12"
    (root / "sub").mkdir(parents=True)
    (root / ".git").write_text("gitdir: /x/.git/worktrees/dv-ab12\n")
    (root / "sub" / "x").write_text("x\n")
    return root


def _job(command: str, root: Path) -> Any:
    return classify("Bash", {"command": command}, root, root)


def _chat(command: str, root: Path) -> Any:
    return classify("Bash", {"command": command}, root)


def _fmt(command: str, root: Path) -> str:
    return command.replace("TMP", str(root.parent.parent / "faketmp"))


@pytest.mark.parametrize(
    "command",
    [
        "cd sub && rm -f x",
        "cd sub; rm -f x 2>&1; git status",
        "rm -rf sub 2>&1 | tail -3",
        "if test -d sub; then rm -rf sub; fi",
        "git status --short && rm -f sub/x",
        "git -C sub log -1 && rm -f sub/x",
        "find sub -name '*.pyc' && rm -f sub/x",
        "P=sub/x; rm -f $P",
        "P=sub; rm -rf ${P}/x",
        "d=$(mktemp -d); rm -rf $d",
        'd="$(mktemp -d)" && rm -rf "$d/x"',
    ],
)
def test_free_in_a_job(command: str, root: Path) -> None:
    assert _job(command, root) is None


@pytest.mark.parametrize(
    "command",
    [
        "cd TMP && rm -rf d",
        "P=TMP/d; rm -rf $P",
        "d=$(mktemp -d); rm -rf $d",
    ],
)
def test_temp_paths_are_free_in_chat_too(command: str, root: Path) -> None:
    assert _chat(_fmt(command, root), root) is None


@pytest.mark.parametrize(
    "command",
    [
        # a variable changed or unknown before the rm
        "P=TMP/d; P=$(cat f); rm -rf $P",
        "P=TMP/d; printf -v P /etc; rm -rf $P",
        "P=TMP/d; : ${P:=/}; rm -rf $P",
        "P=TMP/d rm -rf $P",  # a prefix assignment is not seen by $P
        "d=$(mktemp -p ~ -d); rm -rf $d",
        "d=$(mktemp -d); rm -rf $d/../../etc",
        # a directory change it cannot follow, or one outside the tree
        "cd $X && rm -f x",
        "cd /etc && rm -f x",
        "(cd TMP; rm -rf d)",
        "cd - && rm -f x",
        # wrappers that change who deletes, or where
        "sudo rm sub/x",
        "env -C /etc rm x",
        "xargs rm < sub/x",
        "rm -rf $HOME",
    ],
)
def test_still_asks_in_a_job(command: str, root: Path) -> None:
    reason = _job(_fmt(command, root), root)
    assert reason is not None and reason != RUN_RM_ALONE, reason


@pytest.mark.parametrize(
    "command",
    [
        # text order is not run order: old rule, ln voids the free root
        "f() {{ ln -s ~ x; }}; f; rm -rf x/y",
        "{ ln -s ~ x; }; rm -rf x/y",
        "while true; do ln -s ~ x; done; rm -rf x/y",
        # a companion-only history is required before the rm
        "python3 x.py && rm sub/x",
        "pytest -q; rm -rf sub",
        "bash -c 'rm sub/x'",
    ],
)
def test_never_free_in_a_job(command: str, root: Path) -> None:
    assert _job(command.replace("{{", "{").replace("}}", "}"), root) is not None


def test_a_followed_cd_never_makes_a_link_target_temp(root: Path) -> None:
    """Temp paths get no realpath check: after a non-companion, `cd TMP` must
    not turn `d/x` into a free temp path."""
    command = _fmt("cd TMP && ln -s ~ d && rm -rf d/x", root)
    assert _chat(command, root) is not None
    turn = policy.TurnPolicy(
        working_directory=root, full_access=True, unrestricted=True, free_root=root
    )
    assert policy.decide("Bash", {"command": command}, turn).action != policy.ALLOW


def test_a_real_ask_outranks_run_rm_alone(root: Path) -> None:
    assert _job("python3 x.py && rm sub/x && git push", root) == (
        "git push (sends commits to a remote)"
    )
    assert _job("python3 x.py && rm sub/x; rm ~/.codex/x", root) == (
        "file deletion (rm)"
    )


def test_run_rm_alone_is_a_job_thing(root: Path) -> None:
    assert _job("python3 x.py && rm sub/x", root) == RUN_RM_ALONE
    assert _chat("python3 x.py && rm sub/x", root) == "file deletion (rm)"


def test_segments_record_items_and_plainness() -> None:
    segs = _segments("cd a && rm -f x 2>&1; ls | wc -l; echo $(date) & rm y")
    assert [s.text for s in segs] == [
        "cd a",
        "rm -f x 2>&1",
        "ls",
        "wc -l",
        "echo $",
        "date",
        "rm y",
    ]
    assert [s.item for s in segs] == [0, 1, 2, 2, 3, 3, 4]
    assert [s.plain for s in segs] == [True, True, False, False, False, False, True]
    assert [s.unordered for s in segs] == [False] * 5 + [True, False]
