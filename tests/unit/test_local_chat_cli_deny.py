"""dv-fe4d p5 review: a TUI deny blocks a real backend's tool call in local chat.

The real entry point (``src.main``, CHANNEL=local) boots with the real
backend wiring (Claude SDK manager + its PreToolUse hook, Codex CLI
integration + the agent bridge's policy gate). Only the model is replaced: a
fake ``claude`` / ``codex`` executable speaks each CLI's real protocol (the
SDK control protocol's ``hook_callback`` for Claude, the ``hooks.PreToolUse``
command for Codex), attempts one destructive Bash call, and runs it only if
the hook allows it. The owner answers through ``/internal/inbox`` +
``/internal/callback`` with the TUI credential, exactly as the TUI does.

So the test fails if main.py stops attaching either gate to the backend that
``/internal/chat`` uses: the fake then runs the tool without an ask.
"""

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
INTERNAL = "internal-secret-cli"
TUI = "tui-only-secret-cli"
COMMAND = "git push origin main"
MARKER = "tool-executed.txt"

_FAKE_CLAUDE = r'''
import json, os, sys

if "-v" in sys.argv or "--version" in sys.argv:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if "--input-format" not in sys.argv:
    print("{}")
    sys.exit(0)

SESSION = "fake-claude-session"
COMMAND = %(command)r
MARKER = %(marker)r
hooks = []
pending = {}
seq = [0]


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def ask_hooks(tool_input):
    """Send one hook_callback per PreToolUse hook; True if none denied."""
    allowed = True
    for callback_id in hooks:
        seq[0] += 1
        rid = "cli_%%d" %% seq[0]
        out({"type": "control_request", "request_id": rid, "request": {
            "subtype": "hook_callback", "callback_id": callback_id,
            "tool_use_id": "t1",
            "input": {"hook_event_name": "PreToolUse", "session_id": SESSION,
                      "cwd": os.getcwd(), "tool_name": "Bash",
                      "tool_input": tool_input, "tool_use_id": "t1"}}})
        while True:
            line = sys.stdin.readline()
            if not line:
                sys.exit(1)
            msg = json.loads(line)
            if msg.get("type") == "control_response" and \
                    msg["response"].get("request_id") == rid:
                break
            handle(msg)
        body = msg["response"].get("response") or {}
        spec = body.get("hookSpecificOutput") or {}
        if spec.get("permissionDecision") == "deny" or body.get("decision") in (
            "block", "deny"
        ) or msg["response"].get("subtype") == "error":
            allowed = False
    return allowed


def handle(msg):
    if msg.get("type") == "control_request":
        req = msg["request"]
        if req.get("subtype") == "initialize":
            for matcher in (req.get("hooks") or {}).get("PreToolUse") or []:
                if matcher.get("matcher") in (None, "", "Bash", "*") or \
                        "Bash" in str(matcher.get("matcher")):
                    hooks.extend(matcher.get("hookCallbackIds") or [])
        out({"type": "control_response", "response": {
            "subtype": "success", "request_id": msg["request_id"],
            "response": {}}})
    elif msg.get("type") == "user":
        turn()


def turn():
    out({"type": "system", "subtype": "init", "session_id": SESSION,
         "cwd": os.getcwd(), "tools": ["Bash"], "model": "fake"})
    tool_input = {"command": COMMAND}
    out({"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "assistant", "model": "fake", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": tool_input}]}})
    if ask_hooks(tool_input):
        with open(os.path.join(os.getcwd(), MARKER), "a") as f:
            f.write("claude ran: " + COMMAND + "\n")
        result, text = "ran", "pushed"
    else:
        result, text = "denied by hook", "not pushed"
    out({"type": "user", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": result,
              "is_error": result != "ran"}]}})
    out({"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "assistant", "model": "fake", "content": [
             {"type": "text", "text": text}]}})
    out({"type": "result", "subtype": "success", "duration_ms": 1,
         "duration_api_ms": 1, "is_error": False, "num_turns": 1,
         "session_id": SESSION, "total_cost_usd": 0.0, "result": text,
         "usage": {"input_tokens": 10, "output_tokens": 2}})


for line in sys.stdin:
    line = line.strip()
    if line:
        handle(json.loads(line))
'''

_FAKE_CODEX = r"""
import json, os, re, shlex, subprocess, sys

argv = sys.argv[1:]
if not argv or argv[0] != "exec":
    sys.exit(0)
COMMAND = %(command)r
MARKER = %(marker)r
cwd = argv[argv.index("-C") + 1] if "-C" in argv else os.getcwd()
out_file = argv[argv.index("-o") + 1] if "-o" in argv else None
hook_cmd = None
for value in argv:
    if value.startswith("hooks.PreToolUse="):
        m = re.search(r'command=("(?:[^"\\]|\\.)*")', value)
        hook_cmd = json.loads(m.group(1)) if m else None


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


out({"type": "thread.started", "thread_id": "fake-codex-thread"})
item = {"id": "i1", "type": "command_execution", "command": COMMAND}
out({"type": "item.started", "item": {**item, "status": "in_progress"}})
allowed = True  # no hook mounted = codex runs the tool unasked
if hook_cmd:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": COMMAND}, "tool_use_id": "i1",
               "cwd": cwd}
    proc = subprocess.run(shlex.split(hook_cmd), input=json.dumps(payload),
                          capture_output=True, text=True, timeout=600)
    decision = {}
    if proc.stdout.strip():
        decision = json.loads(proc.stdout).get("hookSpecificOutput") or {}
    allowed = proc.returncode == 0 and decision.get("permissionDecision") != "deny"
if allowed:
    with open(os.path.join(cwd, MARKER), "a") as f:
        f.write("codex ran: " + COMMAND + "\n")
    text = "pushed"
    out({"type": "item.completed", "item": {**item, "status": "completed",
         "exit_code": 0, "aggregated_output": ""}})
else:
    text = "not pushed"
    out({"type": "item.completed", "item": {**item, "status": "declined",
         "aggregated_output": "blocked by hook"}})
out({"type": "item.completed", "item": {"id": "i2", "type": "agent_message",
     "text": text}})
if out_file:
    open(out_file, "w").write(text)
out({"type": "turn.completed",
     "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}})
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _req(port: int, path: str, *, token: str, body: object = None) -> dict:
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


def _write_exe(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o755)
    return path


def _boot(tmp_path: Path, backend: str) -> tuple:
    port = _free_port()
    work = tmp_path / "work"
    work.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fill = {"command": COMMAND, "marker": MARKER}
    claude = _write_exe(bindir / "claude", _FAKE_CLAUDE % fill)
    _write_exe(bindir / "codex", _FAKE_CODEX % fill)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("TELEGRAM", "WEBHOOK", "CHANNEL", "PROFILES", "AGENT_", "CLAUDE_")
        )
    }
    env.update(
        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        CHANNEL="local",
        DEVVY_DATA_ROOT=str(tmp_path / "data"),
        APPROVED_DIRECTORY=str(work),
        DATABASE_URL=f"sqlite:///{tmp_path / 'bot.db'}",
        ENABLE_API_SERVER="true",
        API_SERVER_PORT=str(port),
        AGENT_BRIDGE_PORT=str(_free_port()),
        ENABLE_AGENT_BRIDGE="true",
        WEBHOOK_API_SECRET=INTERNAL,
        LOCAL_TUI_SECRET=TUI,
        DEVELOPMENT_MODE="true",
        ALLOWED_USERS="1",
        AGENT_BACKEND=backend,
        # The full-access cell (the devops chat): a sandboxed turn boxes the
        # command instead of asking, so it would never reach the owner.
        SANDBOX_ENABLED="false",
        CLAUDE_CLI_PATH=str(claude),
        CONFIRMATION_TIMEOUT_SECONDS="60",
        CODEX_HOME=str(tmp_path / "codex_home"),
    )
    log = open(tmp_path / "server.log", "w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import runpy; runpy.run_module('src.main', run_name='__main__')",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 40
    while time.time() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            ) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.3)
    return port, proc, log, work


def _chat(port: int, text: str, out: dict) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/internal/chat",
        data=json.dumps({"profile": "devops", "text": text}).encode(),
        headers={"Authorization": f"Bearer {TUI}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            out["events"] = [
                json.loads(line) for line in r.read().decode().splitlines() if line
            ]
    except Exception as exc:  # surfaced by the assertion below
        out["error"] = repr(exc)


def _wait_for_ask(port: int, chat: threading.Thread) -> str:
    deadline = time.time() + 45
    while time.time() < deadline and chat.is_alive():
        for message in _req(port, "/internal/inbox?since=0", token=TUI)["messages"]:
            for button in message.get("buttons") or []:
                data = button[1] if isinstance(button, (list, tuple)) else button
                data = data.get("data", "") if isinstance(data, dict) else str(data)
                if data.startswith("confirm:"):
                    return data.rsplit(":", 1)[0]
        time.sleep(0.2)
    return ""


@pytest.mark.parametrize("backend", ["claude", "openai_codex"])
@pytest.mark.parametrize("verdict", ["n", "y"])
def test_tui_verdict_gates_the_real_backend_tool_call(
    tmp_path: Path, backend: str, verdict: str
) -> None:
    port, proc, log, work = _boot(tmp_path, backend)
    logp = tmp_path / "server.log"
    try:
        assert proc.poll() is None, logp.read_text()[-3000:]
        out: dict = {}
        chat = threading.Thread(target=_chat, args=(port, "push it", out))
        chat.start()
        ask = _wait_for_ask(port, chat)
        assert ask, (
            f"no ask reached the TUI inbox; chat={out} "
            f"log={logp.read_text()[-3000:]}"
        )
        # Nothing ran while the ask was pending.
        assert not (work / MARKER).exists()
        assert _req(
            port, "/internal/callback", token=TUI, body={"data": f"{ask}:{verdict}"}
        ) == {"matched": True}
        chat.join(60)
        assert not chat.is_alive()
        events = out.get("events")
        assert events, (out, logp.read_text()[-3000:])
        assert any(e["type"] == "done" for e in events), events
        reply = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        if verdict == "n":
            assert not (work / MARKER).exists(), "denied tool call still ran"
            assert "not pushed" in reply
        else:
            assert (work / MARKER).read_text().count(COMMAND) == 1
            assert "pushed" in reply and "not pushed" not in reply
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
