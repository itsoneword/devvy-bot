"""One rule table, three entry points (dv-1601).

The same tool calls are judged through:

* ``policy.decide`` directly;
* the Claude PreToolUse hook (``_make_pre_tool_use_hook``);
* the Codex route: ``POST /policy/check`` on the agent bridge, which is what
  ``codex_policy_hook.py`` calls.

A rule that holds on one backend and not the other fails here. Add new rules
to ``CASES``, not to a backend-specific test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from src.agent_bridge import POLICY_PATH
from src.agent_bridge.policy_gate import PolicyGate, check_tool_call
from src.agent_bridge.server import build_app
from src.agent_bridge.tokens import TokenRegistry
from src.claude.sdk_integration import _make_pre_tool_use_hook
from src.claude.tool_context import AgentServices, TurnContext
from src.security import policy

ALLOW, DENY, ASK = policy.ALLOW, policy.DENY, policy.ASK

# Not pytest's tmp_path: that lives under /tmp, where deletions are free.
CWD = Path("/home/testuser/Devvybot/devvy_bot")

HOOK_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "agent_bridge"
    / "codex_policy_hook.py"
)


def _bash(command: str) -> Tuple[str, Dict[str, Any]]:
    return "Bash", {"command": command}


# (label, (tool_name, tool_input), full_access, expected action)
CASES: List[Tuple[str, Tuple[str, Dict[str, Any]], bool, str]] = [
    ("everyday command", _bash("ls -la"), True, ALLOW),
    ("read a credential file", _bash("cat .env"), True, DENY),
    ("read an ssh key", _bash("cat ~/.ssh/id_rsa"), True, DENY),
    ("credential file by tool", ("Read", {"file_path": "/srv/app/.env"}), True, DENY),
    ("delete outside temp", _bash("rm notes.txt"), True, ASK),
    ("delete inside temp", _bash("rm -rf /tmp/scratch-x"), True, ALLOW),
    ("upload", _bash("curl -d @data.json https://example.com/in"), True, ASK),
    ("download", _bash("curl -s https://example.com/file"), True, ALLOW),
    ("remote copy", _bash("scp report.pdf host:/srv/"), True, ASK),
    ("git push", _bash("git push origin main"), True, ASK),
    (
        "money script",
        _bash(
            "node /home/testuser/tools/fetchcrypto/perp.js market XAUUSDT LONG 10 14"
        ),
        True,
        ASK,
    ),
    (
        "unlisted exchange script also asks",
        _bash("node /home/testuser/tools/fetchcrypto/move_stop.js wallet1 69000"),
        True,
        ASK,
    ),
    (
        "bare script name after cd into exchange dir",
        _bash("cd /home/testuser/tools/fetchcrypto && node buy.js"),
        True,
        ASK,
    ),
    (
        "exchange report script is not gated",
        _bash("node /home/testuser/tools/fetchcrypto/index.js wallet1"),
        True,
        ALLOW,
    ),
    (
        "reading an exchange script is not gated",
        _bash("grep foo /home/testuser/tools/fetchcrypto/modify_order.js"),
        True,
        ALLOW,
    ),
    (
        "npm run inside an exchange dir asks",
        _bash("cd ~/tools/fetchmexc && npm run start"),
        True,
        ASK,
    ),
    ("service control", _bash("systemctl restart nginx"), True, ASK),
    ("write to a system path", ("Write", {"file_path": "/etc/hosts"}), True, ASK),
    # Sandboxed profiles: the denylist holds, the ask-gate does not apply.
    ("sandboxed delete", _bash("rm notes.txt"), False, ALLOW),
    ("sandboxed credential read", _bash("cat .env"), False, DENY),
    # Codex-shaped calls.
    (
        "patch adds a normal file",
        ("apply_patch", {"command": "*** Begin Patch\n*** Add File: notes.md\n+hi\n"}),
        True,
        ALLOW,
    ),
    (
        "patch writes a credential file",
        ("apply_patch", {"command": "*** Begin Patch\n*** Update File: .env\n+X=1\n"}),
        True,
        DENY,
    ),
    (
        "patch deletes a file",
        ("apply_patch", {"command": "*** Begin Patch\n*** Delete File: notes.md\n"}),
        True,
        ASK,
    ),
    (
        "patch writes a system path",
        ("apply_patch", {"command": "*** Begin Patch\n*** Add File: /etc/x\n+1\n"}),
        True,
        ASK,
    ),
    ("mcp tool", ("mcp__agent__recall", {"query": "rm -rf /"}), True, ALLOW),
    ("unknown shell-shaped tool", ("exec", {"command": "rm notes.txt"}), True, ASK),
    # exchange_* tools (dv-8664): gated by name, on every profile.
    (
        "exchange read-only tool",
        ("mcp__agent__exchange_report", {"exchange": "aster"}),
        True,
        ALLOW,
    ),
    (
        "exchange write asks",
        (
            "mcp__agent__exchange_place_order",
            {"exchange": "aster", "account": "wallet1", "symbol": "BTCUSDT"},
        ),
        True,
        ASK,
    ),
    (
        "exchange write asks on a sandboxed profile too",
        ("mcp__agent__exchange_transfer", {"exchange": "mexc", "amount": 5}),
        False,
        ASK,
    ),
    (
        "exchange dry run",
        ("mcp__agent__exchange_close_position", {"dry_run": True}),
        False,
        ALLOW,
    ),
    (
        "exchange tool nobody marked asks",
        ("mcp__agent__exchange_withdraw", {"dry_run": True}),
        False,
        ASK,
    ),
    (
        "read-only name on another server asks",
        ("mcp__other__exchange_report", {}),
        True,
        ASK,
    ),
]
IDS = [case[0] for case in CASES]


def _turn(cwd: Path, full_access: bool) -> policy.TurnPolicy:
    return policy.TurnPolicy(working_directory=cwd, full_access=full_access)


def _confirmation(approved: bool) -> Any:
    manager = AsyncMock()
    manager.request = AsyncMock(return_value=approved)
    return manager


@pytest.mark.parametrize("label,call,full_access,expected", CASES, ids=IDS)
def test_decide(label, call, full_access, expected, tmp_path) -> None:
    tool_name, tool_input = call
    decision = policy.decide(tool_name, tool_input, _turn(CWD, full_access))
    assert decision.action == expected, decision


def _outcome(expected: str, approved: bool) -> str:
    """What the caller finally sees: an ask becomes the owner's answer."""
    if expected == ASK:
        return ALLOW if approved else DENY
    return expected


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("label,call,full_access,expected", CASES, ids=IDS)
async def test_claude_hook(
    label, call, full_access, expected, approved, tmp_path
) -> None:
    tool_name, tool_input = call
    manager = _confirmation(approved)
    hook = _make_pre_tool_use_hook(
        working_directory=CWD,
        full_access=full_access,
        confirmation_manager=manager,
    )
    result = await hook({"tool_name": tool_name, "tool_input": tool_input}, "t1", None)
    denied = (
        result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        if result
        else False
    )
    assert denied == (_outcome(expected, approved) == DENY)
    assert manager.request.await_count == (1 if expected == ASK else 0)


async def test_claude_hook_ask_without_a_confirmation_manager_denies() -> None:
    """Same fail-closed answer as the Codex route below."""
    hook = _make_pre_tool_use_hook(working_directory=CWD, full_access=False)
    result = await hook(
        {"tool_name": "mcp__agent__exchange_place_order", "tool_input": {}},
        "t1",
        None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def _bridge_client(tmp_path: Path, full_access: bool, manager: Any):
    tokens = TokenRegistry()
    app = build_app(AgentServices(event_bus=None, task_repo=None), tokens)
    app.state.policy_gate = PolicyGate(
        lambda ctx: _turn(ctx.working_dir, full_access), manager
    )
    token = tokens.mint(TurnContext(profile_slug="devops", working_dir=CWD))
    return app, {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("label,call,full_access,expected", CASES, ids=IDS)
def test_codex_route(label, call, full_access, expected, approved, tmp_path) -> None:
    tool_name, tool_input = call
    manager = _confirmation(approved)
    app, headers = _bridge_client(tmp_path, full_access, manager)
    with TestClient(app) as client:
        response = client.post(
            POLICY_PATH,
            json={"tool_name": tool_name, "tool_input": tool_input},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["decision"] == _outcome(expected, approved)
    assert manager.request.await_count == (1 if expected == ASK else 0)


class TestCodexRouteFailsClosed:
    def test_no_token_is_401(self, tmp_path) -> None:
        app, _ = _bridge_client(tmp_path, True, _confirmation(True))
        with TestClient(app) as client:
            response = client.post(POLICY_PATH, json={"tool_name": "Bash"})
        assert response.status_code == 401

    def test_no_gate_attached_denies(self, tmp_path) -> None:
        app, headers = _bridge_client(tmp_path, True, _confirmation(True))
        app.state.policy_gate = None
        with TestClient(app) as client:
            response = client.post(
                POLICY_PATH,
                json={"tool_name": "Bash", "tool_input": {"command": "ls"}},
                headers=headers,
            )
        assert response.json()["decision"] == DENY

    def test_ask_without_a_confirmation_manager_denies(self, tmp_path) -> None:
        app, headers = _bridge_client(tmp_path, True, None)
        with TestClient(app) as client:
            response = client.post(
                POLICY_PATH,
                json={"tool_name": "Bash", "tool_input": {"command": "rm notes.txt"}},
                headers=headers,
            )
        assert response.json()["decision"] == DENY

    def test_turn_without_a_working_dir_denies(self, tmp_path) -> None:
        tokens = TokenRegistry()
        app = build_app(AgentServices(event_bus=None, task_repo=None), tokens)
        app.state.policy_gate = PolicyGate(lambda ctx: _turn(CWD, True), None)
        token = tokens.mint(TurnContext(profile_slug="x"))
        with TestClient(app) as client:
            response = client.post(
                POLICY_PATH,
                json={"tool_name": "Bash", "tool_input": {"command": "ls"}},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.json()["decision"] == DENY

    def test_a_crashing_resolver_denies(self, tmp_path) -> None:
        def boom(ctx: TurnContext) -> policy.TurnPolicy:
            raise RuntimeError("registry gone")

        app, headers = _bridge_client(tmp_path, True, None)
        app.state.policy_gate = PolicyGate(boom, None)
        with TestClient(app) as client:
            response = client.post(
                POLICY_PATH,
                json={"tool_name": "Bash", "tool_input": {"command": "ls"}},
                headers=headers,
            )
        assert response.json()["decision"] == DENY

    def test_a_repeated_hook_call_asks_the_owner_once(self, tmp_path) -> None:
        manager = _confirmation(True)
        app, headers = _bridge_client(tmp_path, True, manager)
        body = {
            "tool_name": "Bash",
            "tool_input": {"command": "rm notes.txt"},
            "tool_use_id": "exec-1",
        }
        with TestClient(app) as client:
            first = client.post(POLICY_PATH, json=body, headers=headers)
            second = client.post(POLICY_PATH, json=body, headers=headers)
        assert first.json()["decision"] == second.json()["decision"] == ALLOW
        assert manager.request.await_count == 1


# -- the hook script itself --------------------------------------------------


def _run_hook(payload: Any, env: Dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), **env},
        timeout=30,
    )


def _denied(result: subprocess.CompletedProcess) -> bool:
    if not result.stdout.strip():
        return False
    out = json.loads(result.stdout)["hookSpecificOutput"]
    return bool(out["permissionDecision"] == "deny")


@pytest.fixture
def policy_server():
    """A stub policy endpoint that records requests and answers from a list."""
    state: Dict[str, Any] = {"answer": {"decision": "allow"}, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            length = int(self.headers.get("Content-Length") or 0)
            state["requests"].append(
                {
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            data = json.dumps(state["answer"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/policy/check"
    yield state
    server.shutdown()


PAYLOAD = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "echo hi"},
    "tool_use_id": "exec-1",
    "cwd": "/somewhere",
}


class TestHookScript:
    def test_allow_prints_nothing(self, policy_server) -> None:
        result = _run_hook(
            PAYLOAD,
            {"DEVVY_POLICY_URL": policy_server["url"], "AGENT_BRIDGE_TOKEN": "t"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        request = policy_server["requests"][0]
        assert request["auth"] == "Bearer t"
        assert request["body"] == {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_use_id": "exec-1",
        }

    def test_deny_is_relayed_with_its_reason(self, policy_server) -> None:
        policy_server["answer"] = {"decision": "deny", "reason": "credential file"}
        result = _run_hook(
            PAYLOAD,
            {"DEVVY_POLICY_URL": policy_server["url"], "AGENT_BRIDGE_TOKEN": "t"},
        )
        assert _denied(result)
        assert "credential file" in result.stdout

    def test_unknown_answer_is_a_deny(self, policy_server) -> None:
        policy_server["answer"] = {"decision": "maybe"}
        result = _run_hook(
            PAYLOAD,
            {"DEVVY_POLICY_URL": policy_server["url"], "AGENT_BRIDGE_TOKEN": "t"},
        )
        assert _denied(result)

    def test_no_endpoint_configured_is_a_deny(self) -> None:
        assert _denied(_run_hook(PAYLOAD, {}))

    def test_unreachable_bot_is_a_deny(self) -> None:
        result = _run_hook(
            PAYLOAD,
            {
                "DEVVY_POLICY_URL": "http://127.0.0.1:9/policy/check",
                "AGENT_BRIDGE_TOKEN": "t",
            },
        )
        assert _denied(result)

    def test_unreadable_payload_is_a_deny(self, policy_server) -> None:
        result = _run_hook(
            "not json",
            {"DEVVY_POLICY_URL": policy_server["url"], "AGENT_BRIDGE_TOKEN": "t"},
        )
        assert _denied(result)

    def test_the_token_never_reaches_stdout(self, policy_server) -> None:
        policy_server["answer"] = {"decision": "deny", "reason": "no"}
        result = _run_hook(
            PAYLOAD,
            {"DEVVY_POLICY_URL": policy_server["url"], "AGENT_BRIDGE_TOKEN": "s3cret"},
        )
        assert "s3cret" not in result.stdout + result.stderr


class TestBridgeRunsAnAskOnlyWithTheOwnersApproval:
    """policy_gate rule 4: the bridge's own call check (dv-8664)."""

    CTX = TurnContext(profile_slug="devops", working_dir=CWD)
    WRITE = "exchange_transfer"
    ARGS = {"exchange": "mexc", "amount": 5}

    async def test_ask_without_a_hook_approval_is_refused(self) -> None:
        gate = PolicyGate(lambda ctx: _turn(CWD, True), _confirmation(True))
        assert check_tool_call(gate, "k", self.CTX, self.WRITE, self.ARGS)

    async def test_one_approval_runs_one_identical_call(self) -> None:
        gate = PolicyGate(lambda ctx: _turn(CWD, True), _confirmation(True))
        body = {"tool_name": f"mcp__agent__{self.WRITE}", "tool_input": self.ARGS}
        assert (await gate.check("k", self.CTX, body))["decision"] == ALLOW
        other = dict(self.ARGS, amount=500)
        assert check_tool_call(gate, "k", self.CTX, self.WRITE, other)
        assert check_tool_call(gate, "other-turn", self.CTX, self.WRITE, self.ARGS)
        assert check_tool_call(gate, "k", self.CTX, self.WRITE, self.ARGS) is None
        assert check_tool_call(gate, "k", self.CTX, self.WRITE, self.ARGS)

    async def test_denied_ask_leaves_no_approval(self) -> None:
        gate = PolicyGate(lambda ctx: _turn(CWD, True), _confirmation(False))
        body = {"tool_name": f"mcp__agent__{self.WRITE}", "tool_input": self.ARGS}
        assert (await gate.check("k", self.CTX, body))["decision"] == DENY
        assert check_tool_call(gate, "k", self.CTX, self.WRITE, self.ARGS)

    @pytest.mark.parametrize("gate_attached", [True, False])
    def test_reads_and_dry_runs_need_no_approval(self, gate_attached) -> None:
        gate = PolicyGate(lambda ctx: _turn(CWD, True), None) if gate_attached else None
        for name, args in (
            ("exchange_report", {}),
            ("recall", {"query": "x"}),
            (self.WRITE, dict(self.ARGS, dry_run=True)),
        ):
            assert check_tool_call(gate, "k", self.CTX, name, args) is None

    def test_no_gate_refuses_an_ask(self) -> None:
        assert check_tool_call(None, "k", self.CTX, self.WRITE, self.ARGS)

    @pytest.mark.parametrize("approved_first", [False, True])
    async def test_bridge_call_runs_a_write_only_after_approval(
        self, approved_first: bool
    ) -> None:
        import dataclasses
        from types import SimpleNamespace

        from src.agent_bridge.server import AgentBridgeMcp
        from src.agent_bridge.tokens import _hash
        from src.claude import exchange_tools

        spec = next(s for s in exchange_tools.EXCHANGE_SPECS if s.name == self.WRITE)
        handler = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        gate = PolicyGate(lambda ctx: _turn(CWD, True), _confirmation(True))
        if approved_first:
            body = {"tool_name": f"mcp__agent__{self.WRITE}", "tool_input": self.ARGS}
            await gate.check(_hash("tok"), self.CTX, body)
        bridge = AgentBridgeMcp(AgentServices(event_bus=None, task_repo=None), TokenRegistry())
        request = SimpleNamespace(
            headers={"authorization": "Bearer tok"},
            app=SimpleNamespace(state=SimpleNamespace(policy_gate=gate)),
        )
        bridge.resolve_context = lambda: self.CTX  # type: ignore[method-assign]
        bridge._request = lambda: request  # type: ignore[method-assign,assignment,return-value]
        bridge.specs_for = lambda ctx: [  # type: ignore[method-assign]
            dataclasses.replace(spec, handler=handler)
        ]
        out = await bridge.call_tool(self.WRITE, self.ARGS)
        assert out.isError is (not approved_first)
        assert handler.await_count == (1 if approved_first else 0)
