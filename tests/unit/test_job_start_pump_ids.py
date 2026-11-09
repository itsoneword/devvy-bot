"""dv-80b8 C4: job_start(task_id) takes board ids (gb-xxxx) and runs
`pump2 start <board>:<id> --by <profile>` for the gymbro agent."""

import json
import shlex
import sys
from pathlib import Path

import pytest

from tests.unit.test_claude import test_agent_tools as base

captured_tools = base.captured_tools
event_bus = base.event_bus
scheduler = base.scheduler
services = base.services


@pytest.fixture(autouse=True)
def _gymbro_board(tmp_path_factory, monkeypatch) -> None:
    """The gymbro board in a private boards.json: the live one is not shipped."""
    from src.utils import boards as boards_mod

    f = tmp_path_factory.mktemp("boards") / "boards.json"
    f.write_text(
        json.dumps({"boards": [{"name": "gymbro", "profile": "gymbro", "repo": "/x"}]})
    )
    monkeypatch.setattr(boards_mod, "BOARDS_FILE_DEFAULT", f)


_fake = base.TestJobStartBoardTask._fake
_tools = base.TestJobStartBoardTask._tools


@pytest.mark.parametrize("tid", ["gb-1af9", "gymbro:gb-1af9"])
async def test_C4_gymbro_job_start_runs_pump2_start(
    services, captured_tools, monkeypatch, tid
) -> None:
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "gymbro")
    r = await tools["job_start"].handler({"task_id": tid})
    assert r.get("isError") is not True, r
    starts = [a for a in spawned if "start" in a]
    assert len(starts) == 1 and Path(starts[0][0]).name == "pump2"
    argv = list(starts[0])
    assert "gymbro:gb-1af9" in argv
    assert argv[argv.index("--by") + 1] == "gymbro"


@pytest.mark.parametrize(
    "tid", ["--cwd=/", "dv-dfb1; rm", "../dv-dfb1", "pl:gb-1af9", "other:gb-1af9"]
)
async def test_C4_job_start_rejects_injection_and_other_boards(
    services, captured_tools, monkeypatch, tid
) -> None:
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "gymbro")
    r = await tools["job_start"].handler({"task_id": tid})
    assert r.get("isError") is True
    assert not any("start" in a for a in spawned)


# ---- dv-06e6: job_start(task_id) on a designloop card runs `pump2 design run`

from src.claude import agent_tools as _at  # noqa: E402
from src.pump2 import state as _state  # noqa: E402


def _design_board(monkeypatch, tmp_path, cards: dict) -> None:
    """A gymbro board in tmp_path holding `cards` {id: frontmatter extras}."""
    tasks = tmp_path / ".deviz" / "tasks"
    tasks.mkdir(parents=True)
    for cid, extra in cards.items():
        (tasks / f"{cid}.md").write_text(
            f"---\nid: {cid}\ntitle: t\nstatus: in_progress\n{extra}---\n\nbody\n"
        )
    board = _state._board({"profile": "gymbro"}, "gymbro", tmp_path)

    def fake_load(path, *, guard=False):
        return {"gymbro": board}

    monkeypatch.setattr(_state, "load_boards", fake_load)
    monkeypatch.setattr(_at, "load_boards", fake_load, raising=False)


def _bgjob_runs(spawned: list) -> list:
    return [a for a in spawned if Path(a[0]).name == "bgjob" and "run" in a]


async def test_C1_designloop_card_runs_design_run_unboxed(
    services, captured_tools, monkeypatch, tmp_path
) -> None:
    _design_board(monkeypatch, tmp_path, {"gb-d001": "labels: [designloop]\n"})
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "gymbro")
    r = await tools["job_start"].handler({"task_id": "gb-d001"})
    assert r.get("isError") is not True, r
    runs = _bgjob_runs(spawned)
    assert len(runs) == 1, spawned
    argv = list(runs[0])
    cmd = argv[argv.index("--cmd") + 1]
    assert "pump2 design run gymbro:gb-d001" in cmd
    # the venv python: the design path needs structlog (system python3 lacks it)
    assert shlex.split(cmd)[0] == sys.executable
    assert "bwrap" not in " ".join(argv)
    assert not any("start" in a and Path(a[0]).name == "pump2" for a in spawned)


async def test_C2_non_designloop_card_still_runs_pump2_start(
    services, captured_tools, monkeypatch, tmp_path
) -> None:
    _design_board(monkeypatch, tmp_path, {"gb-a002": "labels: [bug]\n"})
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "gymbro")
    r = await tools["job_start"].handler({"task_id": "gb-a002"})
    assert r.get("isError") is not True, r
    starts = [a for a in spawned if Path(a[0]).name == "pump2" and "start" in a]
    assert len(starts) == 1 and "gymbro:gb-a002" in starts[0]
    assert not _bgjob_runs(spawned)


@pytest.mark.parametrize("tid", ["gb-0000", "other:gb-d001", "gb-d001; rm"])
async def test_C3_designloop_branch_keeps_guards(
    services, captured_tools, monkeypatch, tmp_path, tid
) -> None:
    _design_board(monkeypatch, tmp_path, {"gb-d001": "labels: [designloop]\n"})
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "gymbro")
    r = await tools["job_start"].handler({"task_id": tid})
    assert r.get("isError") is True, r
    assert not _bgjob_runs(spawned)


async def test_C3_designloop_branch_owner_only(
    services, captured_tools, monkeypatch, tmp_path
) -> None:
    _design_board(monkeypatch, tmp_path, {"gb-d001": "labels: [designloop]\n"})
    spawned = _fake(monkeypatch)
    _at.build_agent_mcp_server(
        services, "gymbro", Path("/tmp/ws"), requester_chat_id=555, role="member"
    )
    tool = captured_tools.get("job_start")
    if tool is not None:  # members may not get the tool at all
        r = await tool.handler({"task_id": "gb-d001"})
        assert r.get("isError") is True, r
    assert not _bgjob_runs(spawned)


@pytest.mark.parametrize("state", ["running", "awaiting_pick", "picked"])
async def test_C4_designloop_in_flight_or_past_is_refused(
    services, captured_tools, monkeypatch, tmp_path, state
) -> None:
    _design_board(
        monkeypatch,
        tmp_path,
        {"gb-d001": f"labels: [designloop]\ndesign_state: {state}\n"},
    )
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "gymbro")
    r = await tools["job_start"].handler({"task_id": "gb-d001"})
    assert r.get("isError") is True, r
    assert not _bgjob_runs(spawned)
