"""dv-535b: what agents may do on the task board.

Owner decisions 2026-10-18: devops alone changes status; every other agent
creates and comments only. A task for devops goes straight to the pump
(in_progress, label `agent`); per-agent limits and title dedupe apply.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from src.claude import agent_tools
from src.claude.agent_tools import build_agent_mcp_server
from src.claude.tool_context import AgentServices
from src.storage import board_policy
from src.storage.deviz_board import DevizBoard


@pytest.fixture
def board(tmp_path: Path) -> DevizBoard:
    return DevizBoard(tmp_path / "tasks")


@pytest.fixture
def services(board: DevizBoard) -> AgentServices:
    return AgentServices(event_bus=MagicMock(), task_repo=board)


@pytest.fixture
def mount(monkeypatch: pytest.MonkeyPatch, services: AgentServices) -> Any:
    def _mount(slug: str) -> Dict[str, Any]:
        captured: Dict[str, Any] = {}

        def fake_create(name: str, version: str, tools: list) -> Dict[str, str]:
            captured.update({t.name: t for t in tools})
            return {"name": name}

        monkeypatch.setattr(agent_tools, "create_sdk_mcp_server", fake_create)
        build_agent_mcp_server(services, slug, Path("/tmp/ws"))
        return captured

    return _mount


async def _call(tools: Dict[str, Any], name: str, **args: Any) -> Dict[str, Any]:
    out = await tools[name].handler(args)
    return {"error": bool(out.get("isError")), **_payload(out)}


def _payload(out: Dict[str, Any]) -> Dict[str, Any]:
    text = out["content"][0]["text"]
    try:
        data = json.loads(text)
    except ValueError:
        return {"text": text}
    return data if isinstance(data, dict) else {"items": data}


# ---------------------------------------------------------------- rights


async def test_task_done_is_mounted_for_devops_only(mount: Any) -> None:
    assert "task_done" in mount("devops")
    for slug in ("markets", "coach#u1", "devops#u1", "general"):
        tools = mount(slug)
        assert "task_done" not in tools, slug
        assert {"task_add", "task_list", "task_comment"} <= set(tools), slug


async def test_task_done_handler_refuses_a_non_devops_caller(
    mount: Any, board: DevizBoard
) -> None:
    task_id = await board.add_task(title="x", created_by="markets")
    devops_tools = mount("devops")
    # Reach the handler with another agent's context, as a stray caller would.
    handler = next(
        s.handler for s in agent_tools.AGENT_TOOL_SPECS if s.name == "task_done"
    )
    from src.claude.tool_context import TurnContext

    out = await handler(
        AgentServices(event_bus=MagicMock(), task_repo=board),
        TurnContext(profile_slug="markets"),
        {"task_id": task_id},
    )
    assert out.get("isError")
    assert (await board.get_task(task_id)).status == "todo"  # type: ignore[union-attr]
    done = await _call(devops_tools, "task_done", task_id=task_id)
    assert done["status"] == "updated"


async def test_agents_comment_on_any_open_task_marked_as_agent(
    mount: Any, board: DevizBoard
) -> None:
    other = await board.add_task(title="other", created_by="coach")
    tools = mount("markets")
    assert (await _call(tools, "task_comment", task_id=other, text="c"))[
        "status"
    ] == "commented"
    text = (await board.get_task(other)).path.read_text()  # type: ignore[union-attr]
    assert "from markets (agent): c" in text
    assert board_policy.has_agent_comments(text)
    # devops keeps its own marker and is not "an agent"
    await _call(mount("devops"), "task_comment", task_id=other, text="d")
    text = (await board.get_task(other)).path.read_text()  # type: ignore[union-attr]
    assert "@devops: d" in text and "from devops" not in text
    await board.set_status(other, "done")
    assert (await _call(tools, "task_comment", task_id=other, text="e"))["error"]


async def test_comment_daily_limit_is_per_agent(mount: Any, board: DevizBoard) -> None:
    task_id = await board.add_task(title="t", created_by="coach")
    tools = mount("markets")
    for i in range(board_policy.MAX_COMMENTS_PER_DAY):
        out = await _call(tools, "task_comment", task_id=task_id, text=f"n{i}")
        assert out["status"] == "commented"
    assert (await _call(tools, "task_comment", task_id=task_id, text="more"))["error"]
    assert (await _call(mount("coach"), "task_comment", task_id=task_id, text="x"))[
        "status"
    ] == "commented"


async def test_comment_is_append_only_and_capped(mount: Any, board: DevizBoard) -> None:
    task_id = await board.add_task(title="t", body="original", created_by="markets")
    tools = mount("markets")
    long = "x" * (board_policy.MAX_COMMENT_CHARS + 1)
    assert (await _call(tools, "task_comment", task_id=task_id, text=long))["error"]
    await _call(tools, "task_comment", task_id=task_id, text="line1\nline2")
    task = await board.get_task(task_id)
    assert task is not None and task.body == "original"
    assert "from markets (agent): line1 line2" in task.path.read_text()  # type: ignore[union-attr]
    missing = await _call(tools, "task_comment", task_id="dv-0000", text="x")
    assert missing["status"] == "not_found"


# ---------------------------------------------------------------- create


async def test_agent_task_for_devops_goes_to_the_pump(
    mount: Any, board: DevizBoard
) -> None:
    out = await _call(
        mount("markets"), "task_add", title="fetch script", assignee="devops"
    )
    assert out["status"] == "created" and "note" in out
    task = await board.get_task(out["task_id"])
    assert task is not None
    assert task.status == "in_progress"
    assert task.labels == ["markets", "agent"]
    assert task.created_by == "markets"
    assert task.assignee == "devops"
    # No pump key: the pump's eligibility rule picks it up next tick.
    assert "pump:" not in task.path.read_text()  # type: ignore[union-attr]


async def test_agent_task_for_itself_stays_todo(mount: Any, board: DevizBoard) -> None:
    out = await _call(mount("coach#u7"), "task_add", title="weekly review")
    task = await board.get_task(out["task_id"])
    assert task is not None
    assert task.status == "todo" and task.labels == ["coach"]
    assert task.created_by == "coach#u7"


async def test_devops_task_add_is_unchanged(mount: Any, board: DevizBoard) -> None:
    out = await _call(mount("devops"), "task_add", title="t", assignee="devops")
    task = await board.get_task(out["task_id"])
    assert task is not None and task.status == "todo" and task.labels == ["devops"]


async def test_duplicate_open_title_returns_the_existing_task(
    mount: Any, board: DevizBoard
) -> None:
    first = await _call(mount("markets"), "task_add", title="Same  title")
    again = await _call(mount("markets"), "task_add", title="Same title")
    assert again["status"] == "duplicate" and again["task_id"] == first["task_id"]
    await board.set_status(first["task_id"], "done")
    third = await _call(mount("markets"), "task_add", title="Same title")
    assert third["status"] == "created"


async def test_open_devops_cap(mount: Any, board: DevizBoard) -> None:
    tools = mount("markets")
    ids = []
    for i in range(board_policy.MAX_OPEN_AGENT_TASKS):
        out = await _call(tools, "task_add", title=f"d{i}", assignee="devops")
        assert out["status"] == "created"
        ids.append(out["task_id"])
    over = await _call(tools, "task_add", title="d-over", assignee="devops")
    assert over["status"] == "limit" and ids[0] in over["message"]
    # Its own (non-devops) items are not held by the devops cap.
    assert (await _call(tools, "task_add", title="own"))["status"] == "created"
    # Another agent has its own cap.
    other = await _call(mount("coach"), "task_add", title="c", assignee="devops")
    assert other["status"] == "created"
    # Closing one frees a slot.
    await board.set_status(ids[0], "done")
    again = await _call(tools, "task_add", title="d-again", assignee="devops")
    assert again["status"] == "created"


async def test_daily_cap_counts_today_only(
    mount: Any, board: DevizBoard, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = mount("markets")
    for i in range(board_policy.MAX_AGENT_TASKS_PER_DAY):
        assert (await _call(tools, "task_add", title=f"t{i}"))["status"] == "created"
    over = await _call(tools, "task_add", title="one more")
    assert over["status"] == "limit"

    # Tomorrow the count starts again.
    tomorrow = datetime.now(UTC) + timedelta(days=1)

    class _Tomorrow(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "datetime":  # type: ignore[override]
            return tomorrow

    monkeypatch.setattr(board_policy, "datetime", _Tomorrow)
    assert (await _call(tools, "task_add", title="next day"))["status"] == "created"


# ------------------------------------------------------------- pump flag


def test_privileged_paths() -> None:
    flagged = board_policy.privileged_paths(
        [
            "config/profiles.yaml",
            "src/security/policy.py",
            "src/claude/agent_tools.py",
            "deploy/claude-bot.service",
            "scripts/taskpump",
            ".env",
            "src/security/keystore.py",
            "./config/mcp.json",
            "src/markets/fetch.py",
            "docs/notes.md",
            "tests/unit/test_x.py",
        ]
    )
    assert flagged == [
        "config/profiles.yaml",
        "src/security/policy.py",
        "src/claude/agent_tools.py",
        "deploy/claude-bot.service",
        "scripts/taskpump",
        ".env",
        "src/security/keystore.py",
        "config/mcp.json",
    ]


def _flagged_task(tmp_path: Path, task_id: str, value: str) -> Path:
    tasks = tmp_path / ".deviz" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    extra = f"pump_privileged: {value}\n" if value else ""
    (tasks / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\ntitle: t\nstatus: review\n{extra}---\n\nbody\n"
    )
    return tasks


def test_merge_of_a_flagged_branch_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(board_policy, "TASK_DIRS", [])
    _flagged_task(tmp_path, "dv-ab12", "config/profiles.yaml")
    _flagged_task(tmp_path, "dv-cd34", "")
    reason = board_policy.privileged_merge_reason
    for cmd in (
        "git merge pump/dv-ab12",
        "git merge --no-ff pump/dv-ab12 -m x",
        "git -C /repo cherry-pick pump/dv-ab12~2..pump/dv-ab12",
        "cd x; git checkout pump/dv-ab12 -- config/profiles.yaml",
    ):
        assert reason(cmd, tmp_path), cmd
    assert "config/profiles.yaml" in (reason("git merge pump/dv-ab12", tmp_path) or "")
    for cmd in (
        "git merge pump/dv-cd34",  # not flagged
        "git log pump/dv-ab12",  # read-only
        "git diff main..pump/dv-ab12",
        "git merge feature/x",
    ):
        assert reason(cmd, tmp_path) is None, cmd


def test_destructive_classify_asks_on_a_flagged_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.security.destructive import classify

    tasks = _flagged_task(tmp_path / "home", "dv-ab12", "src/security/policy.py")
    monkeypatch.setattr(board_policy, "TASK_DIRS", [tasks])
    assert classify("Bash", {"command": "git merge pump/dv-ab12"}, tmp_path)
    assert classify("Bash", {"command": "git log pump/dv-ab12"}, tmp_path) is None


# --------------------------------------------------------- HTTP path


async def test_http_agent_rules(tmp_path: Path) -> None:
    """POST from a profile gets the agent rules; PATCH by a profile is refused."""
    from fastapi.testclient import TestClient

    from src.api.server import create_api_app

    settings = MagicMock()
    settings.deviz_tasks_dir = tmp_path / "tasks"
    settings.webhook_api_secret = "s3cret"
    registry = MagicMock()
    registry.get_by_slug.side_effect = lambda s: (
        object() if s in ("markets", "devops") else None
    )
    app = create_api_app(MagicMock(), settings, profile_registry=registry)
    client = TestClient(app)
    auth = {"Authorization": "Bearer s3cret"}

    r = client.post(
        "/internal/tasks",
        json={"title": "need x", "created_by": "markets", "assignee": "devops"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    task = await DevizBoard(tmp_path / "tasks").get_task(task_id)
    assert task is not None and task.status == "in_progress"
    assert "agent" in task.labels

    dup = client.post(
        "/internal/tasks",
        json={"title": "need x", "created_by": "markets"},
        headers=auth,
    )
    assert dup.json()["status"] == "duplicate"

    refused = client.patch(
        f"/internal/tasks/{task_id}",
        json={"status": "done", "by": "markets"},
        headers=auth,
    )
    assert refused.status_code == 403
    ok = client.patch(
        f"/internal/tasks/{task_id}", json={"status": "done"}, headers=auth
    )
    assert ok.status_code == 200

    owner = client.post("/internal/tasks", json={"title": "owner thing"}, headers=auth)
    owner_task = await DevizBoard(tmp_path / "tasks").get_task(owner.json()["task_id"])
    assert owner_task is not None and owner_task.status == "todo"
