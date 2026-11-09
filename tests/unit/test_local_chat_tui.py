"""The Textual chat pane switches agents and renders streamed text."""

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Static, Tabs, TextArea  # noqa: E402

from src.tui.app import PumpApp  # noqa: E402


class Client:
    def __init__(self):
        self.active_agent = "devops"
        self.history = {"devops": [], "chef": []}

    def agents(self):
        return ["devops", "chef"]

    def select_agent(self, slug):
        self.active_agent = slug

    def chat_history(self):
        return self.history[self.active_agent]

    def chat(self, text):
        self.history[self.active_agent].append({"role": "user", "text": text})
        self.history[self.active_agent].append({"role": "assistant", "text": "Hi"})
        yield {"type": "delta", "text": "Hi"}
        yield {"type": "done", "session_id": "s"}

    def pump_pin(self):
        return "idle"

    def asks(self):
        return []

    def pending(self):
        return []

    def jobs(self):
        return ""

    def job_output(self, *args, **kwargs):
        return ""


async def test_chat_pane_switches_agents_and_sends_message():
    app = PumpApp(Client())
    async with app.run_test() as pilot:
        tabs = app.query_one("#agent-tabs", Tabs)
        assert len(tabs.query("Tab")) == 2
        tabs.active = "agent-chef"
        await pilot.pause()
        app.query_one("#chat-input", TextArea).insert("hello")
        app.query_one("#send-chat", Button).press()
        await pilot.pause()
        assert app.client.active_agent == "chef"
        assert "assistant: Hi" in str(app.query_one("#chat-history", Static).render())
