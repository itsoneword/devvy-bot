"""dv-fe4d phase 2: the real entry point boots with CHANNEL=local, no token."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTERNAL = "internal-secret-xyz"
TUI = "tui-only-secret-xyz"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _req(port, path, *, token, body=None):  # type: ignore[no-untyped-def]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


@pytest.fixture
def server(tmp_path):  # type: ignore[no-untyped-def]
    port = _free_port()
    work = tmp_path / "work"
    work.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TELEGRAM", "WEBHOOK", "CHANNEL", "PROFILES"))
    }
    env.update(
        CHANNEL="local",
        DEVVY_DATA_ROOT=str(tmp_path / "data"),
        APPROVED_DIRECTORY=str(work),
        DATABASE_URL=f"sqlite:///{tmp_path / 'bot.db'}",
        ENABLE_API_SERVER="true",
        API_SERVER_PORT=str(port),
        WEBHOOK_API_SECRET=INTERNAL,
        LOCAL_TUI_SECRET=TUI,
        DEVELOPMENT_MODE="true",
        ALLOWED_USERS="1",
    )
    log = open(tmp_path / "server.log", "w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "importtime",
            "-c",
            "import sys, runpy; runpy.run_module('src.main', run_name='__main__')",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1
                ) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.3)
        yield port, proc, tmp_path / "server.log"
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _up(server):  # type: ignore[no-untyped-def]
    port, proc, logp = server
    assert proc.poll() is None, logp.read_text()[-1500:]
    return port


def test_server_starts_without_token_health_ok(server) -> None:  # type: ignore[no-untyped-def]
    port = _up(server)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
        assert r.status == 200


def test_telegram_is_never_imported(server) -> None:  # type: ignore[no-untyped-def]
    """The server runs with -X importtime (stderr -> log): no ``telegram``
    package import line may appear while it boots and serves."""
    port, proc, logp = server
    _up(server)
    imported = [
        ln
        for ln in logp.read_text().splitlines()
        if ln.startswith("import time:")
        and ln.rstrip().endswith(("| telegram", "| telegram.ext", "| telegram.error"))
    ]
    assert imported == []


def test_notify_pin_twice_and_ask_approved_end_to_end(server) -> None:  # type: ignore[no-untyped-def]
    port = _up(server)
    for t in ("pump a", "pump b"):
        _req(
            port, "/internal/notify", token=INTERNAL, body={"text": t, "pin_as": "pump"}
        )
        time.sleep(0.5)

    def inbox():  # type: ignore[no-untyped-def]
        return _req(port, "/internal/inbox?since=0", token=TUI)["messages"]

    deadline = time.time() + 10
    while time.time() < deadline and not any(m["text"] == "pump b" for m in inbox()):
        time.sleep(0.2)
    pins = [m for m in inbox() if m["pinned"]]
    assert [m["text"] for m in pins] == ["pump b"]

    out: dict = {}

    def ask() -> None:
        out["r"] = _req(
            port,
            "/internal/policy/ask",
            token=INTERNAL,
            body={"reason": "danger", "preview": "rm x"},
        )

    th = threading.Thread(target=ask)
    th.start()
    token = ""
    deadline = time.time() + 10
    while time.time() < deadline and not token:
        for m in inbox():
            for b in m.get("buttons") or []:
                data = b[1] if isinstance(b, (list, tuple)) else b.get("data", "")
                if str(data).endswith(":y"):
                    token = str(data).split(":")[1]
        time.sleep(0.2)
    assert token, "ask card never reached the inbox"
    _req(port, "/internal/callback", token=TUI, body={"data": f"confirm:{token}:y"})
    th.join(10)
    assert out["r"] == {"approved": True}
