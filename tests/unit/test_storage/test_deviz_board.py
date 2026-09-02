"""DevizBoard: the file-backed task board that replaced agent_tasks (dv-1327)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.deviz_board import DevizBoard


@pytest.fixture
def board(tmp_path: Path) -> DevizBoard:
    return DevizBoard(tmp_path / "tasks")


async def test_add_task_writes_a_file_and_returns_a_dv_id(board: DevizBoard) -> None:
    task_id = await board.add_task(
        title="Test task",
        body="body text\nsecond line",
        created_by="coach#u111111111",
        assignee="user",
        due_at=datetime(2026, 8, 18, 18, tzinfo=UTC),
        labels=["coach"],
    )

    assert task_id.startswith("dv-")
    path = board.tasks_dir / f"{task_id}.md"
    assert path.is_file()

    task = await board.get_task(task_id)
    assert task is not None
    assert task.title == "Test task"
    assert task.body is not None and task.body.startswith("body text")
    assert task.created_by == "coach#u111111111"
    assert task.assignee == "user"
    assert task.due_at == datetime(2026, 8, 18, 18, tzinfo=UTC)
    assert task.labels == ["coach"]
    assert task.status == "todo"


async def test_title_with_a_colon_round_trips(board: DevizBoard) -> None:
    """Real titles contain colons; deviz writes frontmatter unquoted, so a
    YAML parser would reject the board's own files."""
    title = "Переделать логи coach: CSV + notes + q.py"
    task_id = await board.add_task(title=title, created_by="devops")

    task = await board.get_task(task_id)
    assert task is not None and task.title == title


async def test_list_open_excludes_done_and_orders_due_first(board: DevizBoard) -> None:
    no_due = await board.add_task(title="no due", created_by="devops")
    later = await board.add_task(
        title="later", created_by="devops", due_at=datetime(2026, 9, 1, tzinfo=UTC)
    )
    sooner = await board.add_task(
        title="sooner", created_by="devops", due_at=datetime(2026, 8, 12, tzinfo=UTC)
    )
    closed = await board.add_task(title="closed", created_by="devops")
    assert await board.set_status(closed, "done") is True

    open_ids = [t.id for t in await board.list_tasks(status="open")]
    assert open_ids == [sooner, later, no_due]
    assert [t.id for t in await board.list_tasks(status="done")] == [closed]
    assert len(await board.list_tasks(status="all")) == 4


async def test_set_status_maps_open_to_todo_and_reports_missing(
    board: DevizBoard,
) -> None:
    task_id = await board.add_task(title="t", created_by="devops")
    assert await board.set_status(task_id, "done") is True
    assert await board.set_status(task_id, "open") is True

    task = await board.get_task(task_id)
    assert task is not None and task.status == "todo"
    assert await board.set_status("dv-0000", "done") is False

    with pytest.raises(ValueError):
        await board.set_status(task_id, "nonsense")


async def test_status_write_preserves_pump_frontmatter(board: DevizBoard) -> None:
    """The taskpump owns `pump*` keys in the same files. An agent marking a
    task done must not drop them."""
    task_id = await board.add_task(title="t", created_by="devops")
    path = board.tasks_dir / f"{task_id}.md"
    path.write_text(
        path.read_text().replace("created_by:", "pump: analyzing\ncreated_by:"),
        encoding="utf-8",
    )

    await board.set_status(task_id, "done")

    text = path.read_text()
    assert "pump: analyzing" in text
    assert "status: done" in text


async def test_find_open_by_title_and_append_comment(board: DevizBoard) -> None:
    """The consolidation service's dedupe path: same scope, same title, one
    task that accumulates evidence."""
    title = "Consolidation needed: devops"
    task_id = await board.add_task(title=title, created_by="consolidation")

    hit = await board.find_open_by_title(title)
    assert hit is not None and hit.id == task_id

    assert await board.append_comment(task_id, "2026-08-11: still over budget") is True
    assert "still over budget" in (board.tasks_dir / f"{task_id}.md").read_text()

    await board.set_status(task_id, "done")
    assert await board.find_open_by_title(title) is None


async def test_reads_a_hand_written_file_with_unknown_sections(
    board: DevizBoard,
) -> None:
    board.tasks_dir.mkdir(parents=True, exist_ok=True)
    (board.tasks_dir / "dv-abcd.md").write_text(
        "---\n"
        "id: dv-abcd\n"
        "title: Hand written: with colon\n"
        "status: review\n"
        "priority: high\n"
        "labels: [coach, persona]\n"
        "created: 2026-08-11T07:10:02Z\n"
        "---\n\n"
        "## Description\n\nthe description\n\n"
        "## Root cause\n\nsomething else\n",
        encoding="utf-8",
    )

    task = await board.get_task("dv-abcd")
    assert task is not None
    assert task.title == "Hand written: with colon"
    assert task.status == "review"
    assert task.priority == "high"
    assert task.labels == ["coach", "persona"]
    assert task.body == "the description"
    assert task.is_open is True
    assert task.to_dict()["id"] == "dv-abcd"
