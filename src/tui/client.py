"""What the Pump TUI does, without Textual (dv-fe4d phase 3).

Every action is an existing interface, called the way a person would call it:

- pump state: ``pump2 pending --json`` / ``answer`` / ``start`` / ``freeze`` /
  ``thaw`` (the text output is never parsed); a new task is a card written by
  ``DevizBoard.add_task`` (the one card writer), then ``pump2 start``;
- jobs: ``bgjob list`` / ``bgjob output <id> --tail N``;
- the pump pin and job asks: the local server's ``GET /internal/inbox`` and
  ``POST /internal/callback`` (channel=local only), authenticated with the
  TUI credential file that no job box can read.

`PumpClient` takes ``run`` (argv -> CompletedProcess) and ``api`` (``get`` /
``post``) so the app and the tests share it; the defaults are a subprocess
and :class:`InboxApi`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from json import dumps as json_dumps
from json import loads as json_loads
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol, Sequence

REPO = Path(__file__).resolve().parent.parent.parent
PUMP2 = REPO / "scripts" / "pump2"
BGJOB = REPO / "scripts" / "bgjob"

CONFIRM_PREFIX = "confirm:"  # ConfirmationManager's callback data
PUMP_PIN = "pump"  # the pump pin's --pin-as key

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class Api(Protocol):
    def get(self, path: str, **params: Any) -> Dict[str, Any]: ...

    def post(self, path: str, json: Dict[str, Any]) -> Dict[str, Any]: ...

    def stream(self, path: str, json: Dict[str, Any]) -> Iterator[Dict[str, Any]]: ...


def _run_sync(coro: Any) -> Any:
    """Run a coroutine to its result, also from inside a running event loop
    (a Textual action handler): then in a worker thread with its own loop."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def run_cli(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [str(a) for a in argv], capture_output=True, text=True, timeout=120
    )


class InboxApi:
    """The local server's /internal endpoints over HTTP, bearer = the TUI
    credential (``paths.tui_secret_path()``), never WEBHOOK_API_SECRET."""

    def __init__(self, base_url: Optional[str] = None, secret: Optional[str] = None):
        port = os.environ.get("API_SERVER_PORT") or "8080"
        self.base_url = base_url or f"http://127.0.0.1:{port}"
        self._secret = secret

    def _bearer(self) -> str:
        if self._secret is None:
            sys.path.insert(0, str(REPO))
            from src.utils.paths import tui_secret_path

            self._secret = tui_secret_path().read_text().strip()
        return self._secret

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            reply: Dict[str, Any] = json.loads(resp.read().decode())
        return reply

    def get(self, path: str, **params: Any) -> Dict[str, Any]:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self._call("GET", path + (f"?{query}" if query else ""))

    def post(self, path: str, json: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", path, json)

    def stream(self, path: str, json: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json_dumps(json).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=3600) as resp:
            for line in resp:
                if line.strip():
                    yield json_loads(line)


class PumpClient:
    def __init__(self, run: Optional[Runner] = None, api: Optional[Api] = None):
        self.run = run or run_cli
        self.api = api if api is not None else InboxApi()
        # The inbox as last seen, by message id: a poll returns only rows
        # created or changed since the cursor (plus live pins), so an edited
        # ask replaces its old copy here and loses its buttons.
        self.messages: Dict[int, Dict[str, Any]] = {}
        self.cursor = 0
        self.active_agent = "devops"
        self._chat_history: Dict[str, List[Dict[str, str]]] = {}

    def select_agent(self, slug: str) -> None:
        if not slug.strip():
            raise ValueError("Agent is required")
        self.active_agent = slug

    def agents(self) -> List[str]:
        return [str(x) for x in self.api.get("/internal/agents").get("agents", [])]

    def chat_history(self) -> List[Dict[str, str]]:
        return list(self._chat_history.get(self.active_agent, []))

    def chat(self, text: str) -> Iterator[Dict[str, Any]]:
        if not text.strip():
            raise ValueError("Chat message is empty")
        agent = self.active_agent
        history = self._chat_history.setdefault(agent, [])
        history.append({"role": "user", "text": text})
        reply: Optional[Dict[str, str]] = None
        for event in self.api.stream(
            "/internal/chat", {"profile": agent, "text": text}
        ):
            if event.get("type") == "delta":
                if reply is None:
                    reply = {"role": "assistant", "text": ""}
                    history.append(reply)
                reply["text"] += str(event.get("text") or "")
            yield event

    def deny_chat_ask(self, ask: Dict[str, Any]) -> Dict[str, Any]:
        return self.api.post("/internal/callback", {"data": ask["data"]})

    # -- pump2 ------------------------------------------------------------
    def _pump2(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return self.run([str(PUMP2), *args])

    def pending(self) -> List[Dict[str, Any]]:
        """Tasks waiting on the owner, from ``pending --json`` only."""
        r = self._pump2("pending", "--json")
        try:
            rows = json.loads(r.stdout or "[]")
        except ValueError:
            return []
        return (
            [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []
        )

    def answer(self, task_id: str, text: str) -> "subprocess.CompletedProcess[str]":
        # One argv element, unedited: the next stage reads the owner's words.
        return self._pump2("answer", task_id, "--", text)

    def new_task(
        self, board: str, title: str, body: str, start: bool
    ) -> "subprocess.CompletedProcess[str]":
        """A todo card on ``board`` (``DevizBoard.add_task``, as intake); with
        ``start`` it is then handed to pump2 (``pump2 start board:id``)."""
        args = ["new_task", board, title]

        def failed(message: str) -> "subprocess.CompletedProcess[str]":
            return subprocess.CompletedProcess(args, 1, "", message)

        from src.pump2.state import load_boards
        from src.storage.deviz_board import DevizBoard
        from src.utils.boards import BOARDS_FILE_DEFAULT

        try:
            boards = load_boards(BOARDS_FILE_DEFAULT, guard=True)
        except (OSError, ValueError) as exc:
            return failed(f"error: boards file unreadable: {exc}")
        if board not in boards:
            names = ", ".join(sorted(boards))
            return failed(f"error: no board named {board} (boards: {names})")
        tasks_dir = boards[board].repo / ".deviz" / "tasks"
        tid = _run_sync(
            DevizBoard(tasks_dir).add_task(
                title=title[:120], body=body or None, created_by="owner"
            )
        )
        if not tid:
            return failed(f"error: could not create the task on {board}")
        full = f"{board}:{tid}"
        if not start:
            return subprocess.CompletedProcess(args, 0, f"{full}\n", "")
        r = self.start(full)
        return subprocess.CompletedProcess(
            args, r.returncode, f"{full}\n{r.stdout or ''}", r.stderr or ""
        )

    def start(self, task_id: str) -> "subprocess.CompletedProcess[str]":
        return self._pump2("start", task_id)

    def freeze(self) -> "subprocess.CompletedProcess[str]":
        return self._pump2("freeze")

    def thaw(self) -> "subprocess.CompletedProcess[str]":
        return self._pump2("thaw")

    # -- bgjob ------------------------------------------------------------
    def jobs(self) -> str:
        return self.run([str(BGJOB), "list", "--compact"]).stdout

    def job_output(self, job_id: str, tail: int = 40) -> str:
        return self.run([str(BGJOB), "output", job_id, "--tail", str(tail)]).stdout

    # -- inbox (local server) ---------------------------------------------
    def poll(self) -> List[Dict[str, Any]]:
        """Fetch what changed since the last poll; returns the whole inbox."""
        data = self.api.get("/internal/inbox", since=self.cursor)
        for m in data.get("messages") or []:
            self.messages[int(m["id"])] = m
        self.cursor = max(self.cursor, int(data.get("cursor") or 0))
        return [self.messages[k] for k in sorted(self.messages)]

    def pump_pin(self) -> str:
        """The pump's status pin: the pin keyed "pump", else the latest pin
        that names no key (a server older than pin_as in the inbox)."""
        pins = [m for m in self.poll() if m.get("pinned")]
        keyed = [m for m in pins if m.get("pin_as") == PUMP_PIN]
        unkeyed = [m for m in pins if not m.get("pin_as")]
        best = (keyed or unkeyed)[-1:] or [{}]
        return str(best[0].get("text") or "")

    def asks(self) -> List[Dict[str, Any]]:
        """Open asks: inbox rows still carrying confirm buttons. A timed-out
        or answered ask comes back edited without buttons and drops out."""
        return [
            m
            for m in self.poll()
            if any(
                len(b) > 1 and str(b[1]).startswith(CONFIRM_PREFIX)
                for b in m.get("buttons") or []
            )
        ]

    def _press(self, ask: Dict[str, Any], verdict: str) -> Dict[str, Any]:
        data = next(
            str(b[1])
            for b in ask.get("buttons") or []
            if str(b[1]).startswith(CONFIRM_PREFIX) and str(b[1]).endswith(verdict)
        )
        return self.api.post("/internal/callback", {"data": data})

    def approve(self, ask: Dict[str, Any]) -> Dict[str, Any]:
        return self._press(ask, ":y")

    def deny(self, ask: Dict[str, Any]) -> Dict[str, Any]:
        return self._press(ask, ":n")
