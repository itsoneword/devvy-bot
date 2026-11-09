"""The Pump TUI (dv-fe4d phase 3): Textual screens over :class:`PumpClient`.

Panes: the pump pin, tasks waiting on the owner (with an answer box), job
asks (Approve / Deny), jobs and the selected job's output tail, and a New
task form. Keys: n new task, a answer, s start, f freeze, t thaw,
y / d approve / deny the selected ask, g refresh, q quit. Everything is a
call on the client; nothing here knows how the pump works.

Textual is optional (``pip install textual``); run with ``scripts/devvy-tui``.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

try:
    # Optional dependency, no stubs: pip install textual.
    from textual.app import App, ComposeResult  # type: ignore[import-not-found]
    from textual.containers import (  # type: ignore[import-not-found]
        Horizontal,
        Vertical,
    )
    from textual.screen import ModalScreen  # type: ignore[import-not-found]
    from textual.widgets import (  # type: ignore[import-not-found]
        Button,
        Checkbox,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        Static,
        Tab,
        Tabs,
        TextArea,
    )
except ImportError:  # pragma: no cover - exercised only without textual
    App = None  # type: ignore[assignment,misc]

from src.tui.client import PumpClient

REFRESH_SECONDS = 5.0
DEFAULT_BOARD = "devvy"  # src/utils/boards.py HOME_BOARD
OUTPUT_TAIL = 40


def missing_textual() -> str:
    return (
        "The Pump TUI needs Textual: `pip install textual` in the bot's venv "
        "(or `poetry run pip install textual`), then run scripts/devvy-tui again."
    )


if App is not None:

    class NewTaskScreen(ModalScreen):  # type: ignore[type-arg]
        """Board, title, body, start now -> a new card (+ ``pump2 start``)."""

        def __init__(self, board: str) -> None:
            super().__init__()
            self.board = board

        def compose(self) -> ComposeResult:
            with Vertical(id="new-task"):
                yield Label("New task (an unknown board is refused with the list)")
                yield Input(value=self.board, placeholder="Board", id="board")
                yield Input(placeholder="Title", id="title")
                yield TextArea(id="body")
                yield Checkbox("Start now (analyse on the next tick)", id="start")
                with Horizontal():
                    yield Button("Create", variant="primary", id="create")
                    yield Button("Cancel", id="cancel")

        def on_button_pressed(self, event: Any) -> None:
            if event.button.id != "create":
                self.dismiss(None)
                return
            title = self.query_one("#title", Input).value.strip()
            if not title:
                self.app.notify("A task needs a title", severity="warning")
                return
            self.dismiss(
                {
                    "board": self.query_one("#board", Input).value.strip(),
                    "title": title,
                    "body": self.query_one("#body", TextArea).text,
                    "start": self.query_one("#start", Checkbox).value,
                }
            )

    class AnswerScreen(ModalScreen):  # type: ignore[type-arg]
        """The owner's reply to one pending task, sent verbatim."""

        def __init__(self, row: Dict[str, Any]) -> None:
            super().__init__()
            self.row = row

        def compose(self) -> ComposeResult:
            with Vertical(id="answer"):
                yield Label(f"{self.row['id']} — {self.row.get('title', '')}")
                yield Static(self.row.get("block") or "", id="block")
                yield TextArea(id="reply")
                with Horizontal():
                    yield Button("Send", variant="primary", id="send")
                    yield Button("Cancel", id="cancel")

        def on_button_pressed(self, event: Any) -> None:
            text = self.query_one("#reply", TextArea).text
            self.dismiss(text if event.button.id == "send" and text.strip() else None)

    class TaskIdScreen(ModalScreen):  # type: ignore[type-arg]
        """A task id for start, prefilled with the selected one."""

        def __init__(self, action: str, default: str) -> None:
            super().__init__()
            self.action_name, self.default = action, default

        def compose(self) -> ComposeResult:
            with Vertical(id="answer"):
                yield Label(f"{self.action_name}: task id (e.g. devvy:dv-ab12)")
                yield Input(value=self.default, id="task")

        def on_input_submitted(self, event: Any) -> None:
            self.dismiss(event.value.strip() or None)

        def key_escape(self) -> None:
            self.dismiss(None)

    class PumpApp(App):  # type: ignore[type-arg]
        TITLE = "Devvy Pump"
        CSS = """
        #pin { height: auto; max-height: 12; border: round $accent; }
        #chat { height: 14; border: round $accent; }
        #chat-history { height: 1fr; overflow-y: auto; }
        #chat-input { height: 4; }
        #left, #right { width: 1fr; }
        ListView { height: 1fr; border: round $panel; }
        #output { height: 1fr; border: round $panel; overflow-y: auto; }
        #new-task, #answer { width: 80; height: auto; border: thick $accent;
                             background: $surface; padding: 1; }
        """
        BINDINGS = [
            ("n", "new_task", "New task"),
            ("a", "answer", "Answer"),
            ("s", "start", "Start"),
            ("f", "freeze", "Freeze"),
            ("t", "thaw", "Thaw"),
            ("y", "approve", "Approve ask"),
            ("d", "deny", "Deny ask"),
            ("g", "refresh", "Refresh"),
            ("q", "quit", "Quit"),
        ]

        def __init__(self, client: Optional[PumpClient] = None) -> None:
            super().__init__()
            self.client = client or PumpClient()
            self.pending_rows: List[Dict[str, Any]] = []
            self.ask_rows: List[Dict[str, Any]] = []
            self.job_ids: List[str] = []

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("", id="pin")
            with Vertical(id="chat"):
                yield Tabs(Tab("devops", id="agent-devops"), id="agent-tabs")
                yield Static("", id="chat-history")
                yield TextArea(id="chat-input")
                yield Button("Send chat", variant="primary", id="send-chat")
            with Horizontal():
                with Vertical(id="left"):
                    yield Label("Waiting for you")
                    yield ListView(id="pending")
                    yield Label("Job asks")
                    yield ListView(id="asks")
                with Vertical(id="right"):
                    yield Label("Jobs")
                    yield ListView(id="jobs")
                    yield Static("", id="output")
            yield Footer()

        async def on_mount(self) -> None:
            if hasattr(self.client, "agents"):
                try:
                    slugs = self.client.agents()
                    tabs = self.query_one("#agent-tabs", Tabs)
                    for slug in slugs:
                        if slug != "devops":
                            tabs.add_tab(Tab(slug, id=f"agent-{slug}"))
                except Exception:
                    pass
            await self.action_refresh()
            self.set_interval(REFRESH_SECONDS, self.action_refresh)

        # -- data ---------------------------------------------------------
        async def _fill(self, view_id: str, labels: List[str]) -> None:
            view = self.query_one(f"#{view_id}", ListView)
            index = view.index or 0
            await view.clear()
            await view.extend(ListItem(Label(text)) for text in labels)
            if labels:
                view.index = min(index, len(labels) - 1)

        async def action_refresh(self) -> None:
            try:
                self.query_one("#pin", Static).update(
                    self.client.pump_pin() or "(no pump pin yet)"
                )
                self.ask_rows = self.client.asks()
            except Exception as exc:  # server down: the CLI panes still work
                self.query_one("#pin", Static).update(f"local server: {exc}")
                self.ask_rows = []
            self.pending_rows = self.client.pending()
            await self._fill(
                "pending",
                [
                    f"{r['id']}  [{r.get('kind', '')}]  {r.get('title', '')}"
                    for r in self.pending_rows
                ],
            )
            await self._fill("asks", [a.get("text", "")[:200] for a in self.ask_rows])
            lines = [x for x in self.client.jobs().splitlines() if x.strip()]
            self.job_ids = [x.split()[0] for x in lines]
            await self._fill("jobs", lines)
            self._show_output()

        def _selected(self, view_id: str, rows: List[Any]) -> Optional[Any]:
            index = self.query_one(f"#{view_id}", ListView).index
            return rows[index] if index is not None and index < len(rows) else None

        def _show_output(self) -> None:
            job = self._selected("jobs", self.job_ids)
            text = self.client.job_output(job, tail=OUTPUT_TAIL) if job else ""
            self.query_one("#output", Static).update(text)

        def on_list_view_highlighted(self, event: Any) -> None:
            if event.list_view.id == "jobs":
                self._show_output()

        def on_tabs_tab_activated(self, event: Any) -> None:
            if hasattr(self.client, "select_agent"):
                self.client.select_agent(event.tab.id.removeprefix("agent-"))
                self._show_chat_history()

        def _show_chat_history(self) -> None:
            if not hasattr(self.client, "chat_history"):
                return
            lines = [
                f"{row['role']}: {row['text']}" for row in self.client.chat_history()
            ]
            self.query_one("#chat-history", Static).update("\n\n".join(lines))

        def on_button_pressed(self, event: Any) -> None:
            if event.button.id != "send-chat":
                return
            text = self.query_one("#chat-input", TextArea).text
            if not text.strip() or not hasattr(self.client, "chat"):
                return
            self.query_one("#chat-input", TextArea).clear()
            self.run_worker(lambda: self._chat_worker(text), thread=True)

        def _chat_worker(self, text: str) -> None:
            try:
                for event in self.client.chat(text):
                    if event.get("type") == "ask":
                        self.call_from_thread(
                            self.notify, event.get("text", "Approve?")
                        )
                        self.call_from_thread(self.action_refresh)
                    elif event.get("type") == "error":
                        self.call_from_thread(
                            self.notify,
                            event.get("text", "Chat failed"),
                            severity="error",
                        )
                    self.call_from_thread(self._show_chat_history)
            except Exception as exc:
                self.call_from_thread(self.notify, str(exc), severity="error")
            self.call_from_thread(self._show_chat_history)

        def _report(self, r: Any) -> None:
            out = (r.stdout or r.stderr or "").strip().splitlines()
            self.notify(
                out[-1] if out else "done",
                severity="information" if r.returncode == 0 else "error",
            )
            self.call_after_refresh(self.action_refresh)

        # -- actions ------------------------------------------------------
        def _selected_id(self) -> str:
            row = self._selected("pending", self.pending_rows)
            return str(row["id"]) if row else ""

        def action_new_task(self) -> None:
            def done(form: Optional[Dict[str, Any]]) -> None:
                if form:
                    self._report(self.client.new_task(**form))

            row = self._selected("pending", self.pending_rows)
            board = str(row.get("board") or DEFAULT_BOARD) if row else DEFAULT_BOARD
            self.push_screen(NewTaskScreen(board), done)

        def action_answer(self) -> None:
            row = self._selected("pending", self.pending_rows)
            if not row:
                return

            def done(text: Optional[str]) -> None:
                if text is not None:
                    self._report(self.client.answer(row["id"], text))

            self.push_screen(AnswerScreen(row), done)

        def _with_task_id(self, action: str) -> None:
            def done(task_id: Optional[str]) -> None:
                if task_id:
                    self._report(getattr(self.client, action)(task_id))

            self.push_screen(TaskIdScreen(action, self._selected_id()), done)

        def action_start(self) -> None:
            self._with_task_id("start")

        def action_freeze(self) -> None:
            self._report(self.client.freeze())

        def action_thaw(self) -> None:
            self._report(self.client.thaw())

        def action_approve(self) -> None:
            ask = self._selected("asks", self.ask_rows)
            if ask:
                self.client.approve(ask)
                self.call_after_refresh(self.action_refresh)

        def action_deny(self) -> None:
            ask = self._selected("asks", self.ask_rows)
            if ask:
                self.client.deny(ask)
                self.call_after_refresh(self.action_refresh)


def main() -> int:
    if App is None:
        print(missing_textual(), file=sys.stderr)
        return 2
    PumpApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
