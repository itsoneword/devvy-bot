"""dv-3d18 phase 2 review: a ``cd`` that fails leaves the shell where it was.

After ``;``, a newline or ``||`` the next item may run in the old directory
(or, after ``||``, only there), so a relative deletion must be free from
every directory the shell may be in. Only ``&&`` guarantees the ``cd`` ran.
Also: ``sed`` with an ``e`` command and ``git -c`` run code, so they are not
free-root companions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from src.security import destructive, policy
from src.security.destructive import _segments

ALLOW = policy.ALLOW


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: Any) -> Dict[str, Path]:
    base = tmp_path.resolve()
    fake_tmp = base / "faketmp"
    (fake_tmp / "d").mkdir(parents=True)
    monkeypatch.setattr(destructive, "_temp_roots", lambda: (str(fake_tmp),))
    home = base / "home"
    (home / "docs").mkdir(parents=True)
    root = home / "pump-worktrees" / "dv-ab12"
    (root / "sub").mkdir(parents=True)
    (root / ".git").write_text("gitdir: /elsewhere\n")
    (root / "sub" / "x").write_text("x\n")
    (root / "x").write_text("x\n")
    return {"root": root, "tmp": fake_tmp, "home": home}


def _job(tree: Dict[str, Path]) -> policy.TurnPolicy:
    return policy.TurnPolicy(
        working_directory=tree["root"],
        full_access=True,
        unrestricted=True,
        free_root=tree["root"],
    )


def _chat(tree: Dict[str, Path]) -> policy.TurnPolicy:
    return policy.TurnPolicy(
        working_directory=tree["home"], full_access=True, unrestricted=True
    )


def _fmt(command: str, tree: Dict[str, Path]) -> str:
    return command.replace("TMP", str(tree["tmp"]))


CHAT_NEVER = [
    "cd TMP/nonexist_zz; rm -rf *",
    "cd TMP/nonexist_zz\nrm -rf docs",
    "cd TMP/nonexist_zz || rm -rf docs",
    "cd TMP && true; rm -rf docs",
    "cd TMP && ls || rm -rf docs",
]


@pytest.mark.parametrize("command", CHAT_NEVER)
def test_chat_delete_after_a_cd_that_may_fail_asks(command, tree) -> None:
    d = policy.decide("Bash", {"command": _fmt(command, tree)}, _chat(tree))
    assert d.action == policy.ASK, d
    assert destructive.classify("Bash", {"command": _fmt(command, tree)}, tree["home"])


JOB_NEVER = [
    "cd TMP/nx/y || rm -rf ../../pump-worktrees",
    "cd TMP/nx/y; rm -rf ../../pump-worktrees",
    "cd TMP/nx/y\nrm -rf ../../pump-worktrees",
    "cd TMP/nx/y && ls; rm -rf ../dv-other",
    "cd TMP/nx/y || ls && rm -rf ../dv-other",
]


@pytest.mark.parametrize("command", JOB_NEVER)
def test_job_delete_after_a_cd_that_may_fail_is_never_allowed(command, tree) -> None:
    d = policy.decide("Bash", {"command": _fmt(command, tree)}, _job(tree))
    assert d.action != ALLOW, d


@pytest.mark.parametrize("command", JOB_NEVER)
async def test_job_delete_after_a_cd_that_may_fail_never_runs_unasked(
    command, tree
) -> None:
    from src.model_router.job import BotLink, policy_hook

    link = BotLink("http://127.0.0.1:1", "s")
    link.ask = AsyncMock(return_value=False)  # type: ignore[method-assign]
    hook = policy_hook(_job(tree), link, source="pump-dv-ab12-build")
    result = await hook(
        {"tool_name": "Bash", "tool_input": {"command": _fmt(command, tree)}},
        "t1",
        None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny", result


FREE = [
    # Free from both the old and the new directory.
    ("job", "cd sub; rm -f x"),
    ("job", "cd sub\nrm -f x; git status"),
    ("job", "cd TMP && rm -rf d"),
    ("job", "cd TMP && rm -rf d && rm -rf e"),
    ("chat", "cd TMP && rm -rf d"),
    ("chat", "cd TMP &&\nrm -rf d"),
    ("job", "rm -f x || true"),
]


@pytest.mark.parametrize("who,command", FREE)
def test_delete_free_on_every_way_is_allowed(who, command, tree) -> None:
    turn = _job(tree) if who == "job" else _chat(tree)
    d = policy.decide("Bash", {"command": _fmt(command, tree)}, turn)
    assert d.action == ALLOW, d


def test_a_variable_set_on_one_way_only_is_not_followed(tree) -> None:
    """``test ... && P=x; rm -rf $P/*`` deletes ``/*`` when the test fails."""
    command = _fmt("test -d TMP/d && P=TMP/d; rm -rf $P/y", tree)
    d = policy.decide("Bash", {"command": command}, _job(tree))
    assert d.action != ALLOW, d


@pytest.mark.parametrize(
    "command",
    [
        "sed -n '1e ln -s ~ x' f; rm -rf sub/y",
        "sed 's/a/b/e' f; rm -rf sub/y",
        "sed -e 's/a/b/' -e '2e id' f; rm -rf sub/y",
        "sed -f s.sed f; rm -rf sub/y",
        "git -c core.pager=ln log; rm -rf sub/y",
        "git -c core.fsmonitor=x status; rm -rf sub/y",
    ],
)
def test_companions_that_run_code_lose_the_free_root(command, tree) -> None:
    d = policy.decide("Bash", {"command": command}, _job(tree))
    assert d.action != ALLOW, d


@pytest.mark.parametrize(
    "command",
    [
        "sed -n '1,5p' f; rm -f sub/x",
        "sed -i 's/a/b/g' f; rm -f sub/x",
        "sed -n '/start/,/end/p' f; rm -f sub/x",
        "git -C . status; rm -f sub/x",
    ],
)
def test_read_only_sed_and_git_stay_companions(command, tree) -> None:
    d = policy.decide("Bash", {"command": command}, _job(tree))
    assert d.action == ALLOW, d


def test_segments_record_the_list_operator() -> None:
    segs = _segments("cd a && rm x; ls || pwd\necho $(date; id) | wc\nrm y")
    joiners = {s.text: s.joiner for s in segs}
    assert joiners["cd a"] == "&&"
    assert joiners["rm x"] == ";"
    assert joiners["ls"] == "||"
    assert joiners["pwd"] == "\n"
    assert joiners["wc"] == "\n"
    assert joiners["date"] == "" and joiners["id"] == ""  # nested, not top level
    assert joiners["rm y"] == ""
