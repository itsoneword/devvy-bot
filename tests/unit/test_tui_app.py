"""Textual pilots for the Pump TUI (dv-fe4d phase 3).

Textual is optional (not in the bot's venv), so these skip without it; the
logic they drive is covered without Textual in test_tui_client.py. They check
the screens wire keys to the client: answer verbatim, approve an ask, new
task, start by id.
"""

import subprocess

import pytest

pytest.importorskip("textual")

from src.tui.app import PumpApp  # noqa: E402

ROW = {
    "id": "pl:dv-1",
    "title": "T",
    "board": "pl",
    "kind": "question",
    "phase": "",
    "open": "",
    "block": "Q?",
}
ASK = {
    "id": 3,
    "text": "rm -rf?",
    "pinned": False,
    "buttons": [["Approve", "confirm:tok:y"], ["Deny", "confirm:tok:n"]],
}


class FakeClient:
    def __init__(self):
        self.calls = []

    def _ok(self, *call):
        self.calls.append(call)
        return subprocess.CompletedProcess([], 0, "ok\n", "")

    def pump_pin(self):
        return "pump: idle"

    def asks(self):
        return [ASK] if ("approve", ASK) not in self.calls else []

    def pending(self):
        return [ROW]

    def jobs(self):
        return "job-1 running x\n"

    def job_output(self, job_id, tail=40):
        return f"{job_id} tail"

    def answer(self, task_id, text):
        return self._ok("answer", task_id, text)

    def new_task(self, **form):
        return self._ok("new_task", form)

    def start(self, task_id):
        return self._ok("start", task_id)

    def freeze(self):
        return self._ok("freeze")

    def thaw(self):
        return self._ok("thaw")

    def approve(self, ask):
        self.calls.append(("approve", ask))

    def deny(self, ask):
        self.calls.append(("deny", ask))


async def test_answer_from_the_tui_is_sent_verbatim():
    c = FakeClient()
    app = PumpApp(client=c)
    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#reply").text = " 1 default,\n2 no "
        await pilot.click("#send")
        await pilot.pause()
    assert ("answer", "pl:dv-1", " 1 default,\n2 no ") in c.calls


async def test_approve_key_approves_the_selected_ask():
    c = FakeClient()
    app = PumpApp(client=c)
    async with app.run_test() as pilot:
        await pilot.press("y")
        await pilot.pause()
    assert ("approve", ASK) in c.calls


async def test_new_task_form_calls_new_task():
    c = FakeClient()
    app = PumpApp(client=c)
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#title").value = "Fix it"
        app.screen.query_one("#body").text = "why"
        app.screen.query_one("#start").value = True
        await pilot.click("#create")
        await pilot.pause()
    assert (
        "new_task",
        {"board": "pl", "title": "Fix it", "body": "why", "start": True},
    ) in c.calls


async def test_start_asks_for_the_id_prefilled_with_the_selected_task():
    c = FakeClient()
    app = PumpApp(client=c)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert ("start", "pl:dv-1") in c.calls
