"""dv-3d18 phase 2: a pump job's rm inside its tree, in chains, never asks.

Rows run through ``policy.decide`` and the job hook (``policy_hook``), the
real entry points of an unattended job. ``_temp_roots`` is narrowed to a fake
dir so pytest's tmp_path cannot make a row pass by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import AsyncMock

import pytest

from src.security import destructive, policy

ALLOW, DENY, ASK = policy.ALLOW, policy.DENY, policy.ASK


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: Any) -> Dict[str, Path]:
    base = tmp_path.resolve()
    fake_tmp = base / "faketmp"
    (fake_tmp / "d").mkdir(parents=True)
    (fake_tmp / "x").mkdir()
    monkeypatch.setattr(destructive, "_temp_roots", lambda: (str(fake_tmp),))
    root = base / "pump-worktrees" / "dv-ab12"
    (root / "tests").mkdir(parents=True)
    (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/dv-ab12\n")
    (root / "tests" / "x.py").write_text("x\n")
    (root / "x.py").write_text("x\n")
    (root / "f").write_text("f\n")
    (base / "pump-worktrees" / "dv-other").mkdir()
    (base / "pump-worktrees" / "dv-other" / "x").write_text("x\n")
    return {"root": root, "tmp": fake_tmp, "home": base}


def _turn(tree: Dict[str, Path]) -> policy.TurnPolicy:
    return policy.TurnPolicy(
        working_directory=tree["root"],
        full_access=True,
        unrestricted=True,
        free_root=tree["root"],
    )


def _fmt(command: str, tree: Dict[str, Path]) -> str:
    return command.format(**{k: str(v) for k, v in tree.items()})


async def _hook(turn: policy.TurnPolicy, command: str) -> Tuple[Dict[str, Any], int]:
    from src.model_router.job import BotLink, policy_hook

    link = BotLink("http://127.0.0.1:1", "s")
    link.ask = AsyncMock(return_value=False)  # type: ignore[method-assign]
    hook = policy_hook(turn, link, source="pump-dv-ab12-build")
    result = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}}, "t1", None
    )
    return result, link.ask.await_count


FREE = [
    "cd {root}; rm -f tests/x.py; git status",
    "cd {tmp} && rm -rf d",
    "P={tmp}/x; rm -rf $P",
    "cat > f <<EOF\nhello\nEOF\nrm f",
]


@pytest.mark.parametrize("command", FREE)
def test_chained_delete_in_own_tree_is_allowed(command, tree) -> None:
    d = policy.decide("Bash", {"command": _fmt(command, tree)}, _turn(tree))
    assert d.action == ALLOW, d


@pytest.mark.parametrize("command", FREE)
async def test_chained_delete_in_own_tree_never_asks_the_owner(command, tree) -> None:
    result, asks = await _hook(_turn(tree), _fmt(command, tree))
    assert result == {}
    assert asks == 0


@pytest.mark.parametrize("command", FREE)
def test_same_chain_in_a_chat_turn_still_asks(command, tree) -> None:
    turn = policy.TurnPolicy(
        working_directory=tree["root"], full_access=True, unrestricted=True
    )
    d = policy.decide("Bash", {"command": _fmt(command, tree)}, turn)
    # "cd {root}; rm tests/x.py" has a delete outside temp: chat asks.
    if "rm -f tests/x.py" in command or "rm f" in command:
        assert d.action == ASK, d


NEVER_ALLOW = [
    "ln -s /etc x && rm -rf x/y",
    "ln -s {home} x; rm -rf x/y",
    "rm -rf ../dv-other/x",
    "rm -rf x/y `ln -s ~ x`",
    "rm -rf x/y <(ln -s ~ x)",
    "rm -rf x/y | ln -s ~ x",
    "ln -s ~ x & rm -rf x/y",
    "for i in 1 2; do rm -rf x/y; ln -s ~ x; done",
    "f() {{ ln -s ~ x; }}; f; rm -rf x/y",
]


@pytest.mark.parametrize("command", NEVER_ALLOW)
def test_symlink_and_escape_chains_are_never_allowed(command, tree) -> None:
    d = policy.decide("Bash", {"command": _fmt(command, tree)}, _turn(tree))
    assert d.action != ALLOW, d


@pytest.mark.parametrize("command", NEVER_ALLOW)
async def test_symlink_and_escape_chains_never_run_unasked(command, tree) -> None:
    result, asks = await _hook(_turn(tree), _fmt(command, tree))
    assert result != {}  # denied (owner said no / instant deny), never run


def test_rm_outside_the_tree_still_asks(tree) -> None:
    d = policy.decide("Bash", {"command": "rm ~/.codex/.probe"}, _turn(tree))
    assert d.action == ASK, d


def test_interpreter_then_rm_is_denied_at_once_with_a_hint(tree) -> None:
    d = policy.decide("Bash", {"command": "python3 x.py && rm x.py"}, _turn(tree))
    assert d.action == DENY, d
    assert "run rm alone" in d.reason


async def test_interpreter_then_rm_never_reaches_the_owner(tree) -> None:
    result, asks = await _hook(_turn(tree), "python3 x.py && rm x.py")
    assert asks == 0
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "run rm alone" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_interpreter_then_rm_in_a_chat_turn_keeps_asking(tree) -> None:
    turn = policy.TurnPolicy(
        working_directory=tree["root"], full_access=True, unrestricted=True
    )
    d = policy.decide("Bash", {"command": "python3 x.py && rm x.py"}, turn)
    assert d.action == ASK, d
