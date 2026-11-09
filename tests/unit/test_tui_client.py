"""The Pump TUI's logic layer (dv-fe4d phase 3): no pump logic in the TUI.

`src.tui.client.PumpClient` is what the Textual app calls. It is injected with
`run(argv) -> CompletedProcess` (pump2 / bgjob CLIs) and `api` (an object
with `get(path, **params) -> dict` and `post(path, json) -> dict` for the
local server's /internal endpoints). Textual itself is optional (extra `tui`).
"""

import importlib.util
import json
import subprocess

import pytest


class _Lazy:
    """Import on use so a missing module fails each test, not collection."""

    def __getattr__(self, name):
        import importlib

        return getattr(importlib.import_module("src.tui.client"), name)


client_mod = _Lazy()


class Runner:
    def __init__(self, outputs=None):
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}

    def __call__(self, argv, **kw):
        self.calls.append([str(a) for a in argv])
        key = next((k for k in self.outputs if k in " ".join(map(str, argv))), None)
        return subprocess.CompletedProcess(argv, 0, self.outputs.get(key, ""), "")

    def tail(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]


class Api:
    def __init__(self, inbox=None):
        self.inbox = inbox or {"messages": [], "cursor": 0}
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    def get(self, path, **params):
        self.gets.append((path, params))
        return self.inbox

    def post(self, path, json):
        self.posts.append((path, json))
        return {"ok": True}


def make(outputs=None, inbox=None):
    r, a = Runner(outputs), Api(inbox)
    return client_mod.PumpClient(run=r, api=a), r, a


ROWS = [
    {
        "id": "pl:dv-1",
        "title": "T",
        "board": "pl",
        "kind": "question",
        "phase": "",
        "open": "",
        "block": "Q?",
    }
]


def test_pending_reads_only_the_json_output():
    c, r, _ = make({"pending": json.dumps(ROWS)})
    assert c.pending() == ROWS
    (call,) = r.tail("pending")
    assert "--json" in call


def test_pending_with_garbage_output_is_empty_not_a_crash():
    c, _, _ = make({"pending": "no task is waiting for an answer"})
    assert c.pending() == []


def test_answer_passes_the_text_verbatim_as_one_argument():
    c, r, _ = make()
    text = '  "quoted" 1 default, 2 no\nsecond line  '
    c.answer("pl:dv-1", text)
    (call,) = r.tail("answer")
    assert "pump2" in call[0]
    assert call[-3:] == ["pl:dv-1", "--", text]


@pytest.fixture
def board_repo(tmp_path, monkeypatch):
    """A boards file serving one pump2 board "pl" over a tmp repo."""
    repo = tmp_path / "pl"
    repo.mkdir()
    boards = tmp_path / "boards.json"
    boards.write_text(
        json.dumps({"boards": [{"name": "pl", "repo": str(repo), "pump2": True}]})
    )
    monkeypatch.setattr("src.utils.boards.BOARDS_FILE_DEFAULT", boards)
    return repo


def test_new_task_writes_a_todo_card_and_does_not_start_it(board_repo):
    c, r, _ = make()
    res = c.new_task(board="pl", title="Fix it", body="why\nand how", start=False)
    assert res.returncode == 0
    tid = res.stdout.strip()
    assert tid.startswith("pl:dv-")
    card = (board_repo / ".deviz" / "tasks" / f"{tid.split(':')[1]}.md").read_text()
    assert "title: Fix it" in card and "status: todo" in card
    assert "why\nand how" in card
    assert r.calls == []


def test_new_task_with_start_hands_the_card_to_pump2(board_repo):
    c, r, _ = make()
    res = c.new_task(board="pl", title="Fix it", body="", start=True)
    (call,) = r.tail("start")
    assert "pump2" in call[0]
    assert call[-1] == res.stdout.splitlines()[0]
    assert call[-1].startswith("pl:dv-")


def test_new_task_on_an_unknown_board_is_refused_with_the_list(board_repo):
    c, r, _ = make()
    res = c.new_task(board="nope", title="x", body="", start=True)
    assert res.returncode == 1
    assert "pl" in res.stderr and "no board named nope" in res.stderr
    assert r.calls == []


@pytest.mark.asyncio
async def test_new_task_also_works_inside_a_running_event_loop(board_repo):
    """A Textual action handler runs inside the app's loop."""
    c, _, _ = make()
    res = c.new_task(board="pl", title="In a loop", body="", start=False)
    assert res.returncode == 0


def test_start_goes_through_pump2():
    c, r, _ = make()
    c.start("pl:dv-1")
    (call,) = r.tail("start")
    assert "pump2" in call[0]
    assert call[-1] == "pl:dv-1"


def test_there_is_no_retry():
    c, _, _ = make()
    assert not hasattr(c, "retry")


@pytest.mark.parametrize("action", ["freeze", "thaw"])
def test_freeze_and_thaw_go_through_pump2(action):
    c, r, _ = make()
    getattr(c, action)()
    (call,) = r.tail(action)
    assert "pump2" in call[0]


def test_status_pin_comes_from_the_inbox_pinned_row():
    msgs = [
        {"id": 1, "text": "hello", "buttons": [], "pinned": False},
        {"id": 2, "text": "pump: 2 running", "buttons": [], "pinned": True},
    ]
    c, _, a = make(inbox={"messages": msgs, "cursor": 2})
    assert c.pump_pin() == "pump: 2 running"
    assert a.gets[0][0] == "/internal/inbox"


def test_asks_are_inbox_rows_with_buttons_and_approve_posts_the_callback():
    msgs = [
        {
            "id": 3,
            "text": "rm -rf?",
            "pinned": False,
            "buttons": [["Approve", "confirm:tok:y"], ["Deny", "confirm:tok:n"]],
        }
    ]
    c, _, a = make(inbox={"messages": msgs, "cursor": 3})
    (ask,) = c.asks()
    assert ask["text"] == "rm -rf?"
    c.approve(ask)
    c.deny(ask)
    assert a.posts == [
        ("/internal/callback", {"data": "confirm:tok:y"}),
        ("/internal/callback", {"data": "confirm:tok:n"}),
    ]


def test_an_edited_ask_without_buttons_is_not_open():
    msgs = [
        {
            "id": 3,
            "text": "expired",
            "pinned": False,
            "buttons": [],
            "edited_at": "2026-10-31T00:00:00",
        }
    ]
    c, _, _ = make(inbox={"messages": msgs, "cursor": 4})
    assert c.asks() == []


def test_jobs_and_output_tail_use_bgjob():
    c, r, _ = make({"list": "job-1 running x\n", "output": "line9\n"})
    assert "job-1" in c.jobs()
    assert c.job_output("job-1", tail=20) == "line9\n"
    out = r.tail("output")[0]
    assert "job-1" in out and out[out.index("--tail") + 1] == "20"


def test_the_textual_app_module_exists():
    assert importlib.util.find_spec("src.tui.app") is not None
