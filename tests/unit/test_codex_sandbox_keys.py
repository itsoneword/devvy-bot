"""sandbox_keys on the Codex backend (dv-28f2).

The design (docs/research/dv-28f2/analysis.md §4): for a sandboxed profile
with ``sandbox_keys`` the bot puts the key VALUES in the codex child's env
(names only on argv and on the TurnContext as ``redact_env``); the policy gate,
after an allow, returns ``updated_input`` wrapping the Bash command in
``bash_box.py --redact-env NAME -- CMD`` (no bwrap, no store read), which
scrubs the values from merged output; the Codex hook relays that as
``updatedInput``; the progress view shows the original command; every JSONL
line is scrubbed of the turn's values as a backstop.

Codex's own rollout/sqlite cannot be produced without the real CLI: the
wrapper is what keeps them clean (verified by the host probe, GATE_RESULT),
and ``scripts/backend_smoke.py``'s ``codex:keys`` cell checks it live.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from src.agent_backends.codex_cli import (
    CodexCliIntegration,
    InMemoryCodexSessionStore,
    map_codex_event,
)
from src.agent_bridge import POLICY_PATH, TOKEN_ENV_VAR
from src.agent_bridge.policy_gate import PolicyGate
from src.agent_bridge.server import build_app
from src.agent_bridge.tokens import TokenRegistry
from src.claude.tool_context import AgentServices, TurnContext
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.security import policy
from src.security.bash_box import BashBox

SERVICE = "app-openrouter"
KEY_NAME = "OPENROUTER_API_KEY"
SECRET = "sk-or-v1-test0123456789abcdef"
MODEL = "openai-codex/gpt-5.4-mini"
# Not pytest's tmp_path for policy calls: under /tmp deletions are free.
CWD = Path("/home/testuser/Devvybot/data/workspaces/keyed")

HOOK_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agent_bridge"
    / "codex_policy_hook.py"
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="x",
        telegram_bot_username="x",
        enable_mcp=False,
        mcp_config_path=None,
    )


@pytest.fixture
def key_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data root whose key store holds the keyed profile's OpenRouter key."""
    root = tmp_path / "data"
    store = root / "secrets" / "tools"
    store.mkdir(parents=True)
    path = store / f"{SERVICE}.env"
    path.write_text(f"{KEY_NAME}={SECRET}\n")
    path.chmod(0o600)
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(root))
    return root


def _profile(slug: str, wd: Path, **kw: Any) -> ProfileDefinition:
    wd.mkdir(parents=True, exist_ok=True)
    return ProfileDefinition(
        slug=slug,
        name=slug,
        working_dir=wd,
        memory_dir=wd / "memory",
        persona=wd / "persona.md",
        **kw,
    )


class Profiles:
    """keyed (sandboxed, keys), coach (sandboxed, no keys), devops (full, keys)."""

    def __init__(self, base: Path) -> None:
        ws = base / "data" / "workspaces"
        self.keyed = ws / "keyed"
        self.coach = ws / "coach"
        self.devops = base / "repo"
        self.registry = ProfileRegistry(
            [
                _profile(
                    "devops",
                    self.devops,
                    default=True,
                    sandbox=False,
                    unrestricted=True,
                    sandbox_keys=(SERVICE,),
                ),
                _profile("keyed", self.keyed, sandbox=True, sandbox_keys=(SERVICE,)),
                _profile("coach", self.coach, sandbox=True),
            ]
        )


@pytest.fixture
def profiles(tmp_path: Path, key_store: Path) -> Profiles:
    return Profiles(tmp_path)


class PolicyBridge:
    """A bridge that serves the policy hook and records minted contexts."""

    url = "http://127.0.0.1:8770/mcp"
    policy_url = "http://127.0.0.1:8770/policy/check"

    def __init__(self) -> None:
        self.tokens = TokenRegistry()
        self.contexts: List[TurnContext] = []

    def mint(self, context: TurnContext) -> str:
        self.contexts.append(context)
        return self.tokens.mint(context)

    def revoke(self, token: str) -> None:
        self.tokens.revoke(token)


def _fake_codex(tmp_path: Path) -> Path:
    """A stand-in ``codex`` that dumps its env and argv, then streams a turn
    whose shell output (and a code-mode exec and a subagent item) carry the
    raw value of $OPENROUTER_API_KEY, as a wrapper bypass would."""
    script = tmp_path / "codex"
    k = '"$OPENROUTER_API_KEY"'

    def emit(event: Dict[str, Any]) -> str:
        # Each literal piece is shell-quoted; the key value is spliced in by bash.
        parts = json.dumps(event).split("@KEY@")
        return "echo " + k.join(shlex.quote(p) for p in parts) + "\n"

    events = [
        {"type": "thread.started", "thread_id": "th-keys"},
        {
            "type": "item.started",
            "item": {
                "id": "i1",
                "type": "command_execution",
                "command": "/bin/bash -lc 'echo KEY=$OPENROUTER_API_KEY'",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "i1",
                "type": "command_execution",
                "command": "/bin/bash -lc 'echo KEY=$OPENROUTER_API_KEY'",
                "aggregated_output": "KEY=@KEY@\n",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.started",
            "item": {
                "id": "i2",
                "type": "collab_agent_tool_call",
                "tool": "spawn_agent",
                "status": "in_progress",
                "prompt": "run echo CHILDKEY=@KEY@",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "i2",
                "type": "collab_agent_tool_call",
                "tool": "spawn_agent",
                "status": "completed",
                "output": "CHILDKEY=@KEY@",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "the key is @KEY@"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'env -0 > "{tmp_path / "child_env.bin"}"\n'
        f'printf "%s\\0" "$@" > "{tmp_path / "child_argv.bin"}"\n'
        'outfile=""\n'
        'args=("$@")\n'
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi\n'
        "done\n"
        + "".join(emit(e) for e in events)
        + 'if [[ -n "$outfile" ]]; then echo -n done > "$outfile"; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


def _child_env(tmp_path: Path) -> Dict[str, str]:
    raw = (tmp_path / "child_env.bin").read_bytes().decode()
    return dict(e.split("=", 1) for e in raw.split("\0") if "=" in e)


def _child_argv(tmp_path: Path) -> List[str]:
    raw = (tmp_path / "child_argv.bin").read_bytes().decode()
    return raw.split("\0")[:-1]


async def _run_turn(
    tmp_path: Path, settings: Settings, profiles: Profiles, cwd: Path
) -> Tuple[CodexCliIntegration, PolicyBridge, Any, List[Any]]:
    bridge = PolicyBridge()
    integ = CodexCliIntegration(
        config=settings,
        session_store=InMemoryCodexSessionStore(),
        codex_bin=str(_fake_codex(tmp_path)),
        profile_registry=profiles.registry,
        agent_bridge=bridge,
    )
    updates: List[Any] = []

    async def on_stream(update: Any) -> None:
        updates.append(update)

    response = await integ.run_command(
        "hi",
        cwd,
        user_id=111,
        role="owner",
        model=MODEL,
        on_stream=on_stream,
        force_new=True,
    )
    return integ, bridge, response, updates


def _turn(cwd: Path, full_access: bool) -> policy.TurnPolicy:
    return policy.TurnPolicy(working_directory=cwd, full_access=full_access)


def _confirmation(approved: bool) -> Any:
    manager = AsyncMock()
    manager.request = AsyncMock(return_value=approved)
    return manager


def _keyed_ctx() -> TurnContext:
    return TurnContext(profile_slug="keyed", working_dir=CWD, redact_env=(KEY_NAME,))


def _run_wrapped(
    command: str, extra_env: Dict[str, str]
) -> subprocess.CompletedProcess:
    """Run a (rewritten) command the way Codex runs a shell call."""
    return subprocess.run(
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **extra_env},
    )


# --------------------------------------------------------------------------
# 1. keyed profile on Codex: echo $KEY shows <redacted> to the model, JSONL, Telegram
# --------------------------------------------------------------------------


async def test_pl_echo_key_shows_redacted_to_the_model() -> None:
    """The gate rewrites the keyed profile's Bash into the redaction wrapper; what the shell
    returns (= what the model reads, what Codex writes to JSONL and rollout)
    carries <redacted>, never the value."""
    gate = PolicyGate(lambda ctx: _turn(CWD, False), None)
    answer = await gate.check(
        "k",
        _keyed_ctx(),
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"echo KEY=${KEY_NAME}"},
            "tool_use_id": "c1",
        },
    )
    assert answer["decision"] == policy.ALLOW
    wrapped = answer["updated_input"]["command"]
    assert "--redact-env" in wrapped and KEY_NAME in wrapped
    assert SECRET not in wrapped  # names only, never values

    res = _run_wrapped(wrapped, {KEY_NAME: SECRET})
    assert res.returncode == 0
    assert "KEY=<redacted>" in res.stdout
    assert SECRET not in res.stdout + res.stderr


async def test_pl_key_value_never_reaches_jsonl_consumers_or_telegram(
    tmp_path, settings, profiles
) -> None:
    """Backstop: even a JSONL line that carries the raw value (a wrapper
    bypass) is scrubbed before the stream view, the trace and the reply."""
    from src.model_router.trace import TraceCollector, activate

    trace = TraceCollector()
    with activate(trace):
        _, _, response, updates = await _run_turn(
            tmp_path, settings, profiles, profiles.keyed
        )
    trace.close()

    # The key really was in the child (else this test proves nothing).
    assert _child_env(tmp_path).get(KEY_NAME) == SECRET
    seen = repr(response.content) + repr(updates) + repr(trace.rows())
    assert SECRET not in seen
    assert "KEY=<redacted>" in repr(trace.rows())


# --------------------------------------------------------------------------
# 2. keyed profile on Codex: a script using the key works
# --------------------------------------------------------------------------


async def test_pl_script_using_the_key_works(tmp_path, settings, profiles) -> None:
    integ, bridge, _, _ = await _run_turn(tmp_path, settings, profiles, profiles.keyed)

    env = _child_env(tmp_path)
    argv = _child_argv(tmp_path)
    assert env.get(KEY_NAME) == SECRET  # value in the child env
    assert not any(SECRET in a for a in argv)  # never on the command line
    joined = " ".join(argv)
    # Lockdown for key turns: no shell snapshot, an include_only allowlist
    # that has the key name but not the bridge token.
    assert "features.shell_snapshot=false" in joined
    (policy_arg,) = [a for a in argv if "include_only" in a]
    assert KEY_NAME in policy_arg
    assert TOKEN_ENV_VAR not in policy_arg
    assert "DEVVY_POLICY_URL" not in policy_arg
    (ctx,) = bridge.contexts
    assert ctx.redact_env == (KEY_NAME,)

    # A script that reads the key runs inside the wrapper the gate emits.
    script = tmp_path / "use_key.py"
    script.write_text(
        "import os, hashlib\n"
        f"k = os.environ['{KEY_NAME}']\n"
        "print('len', len(k), hashlib.sha256(k.encode()).hexdigest()[:8])\n"
    )
    wrapped = BashBox.wrap_env(f"{sys.executable} {script}", ctx.redact_env)
    res = _run_wrapped(wrapped, {KEY_NAME: env[KEY_NAME]})
    import hashlib

    digest = hashlib.sha256(SECRET.encode()).hexdigest()[:8]
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"len {len(SECRET)} {digest}" in res.stdout


# --------------------------------------------------------------------------
# 3. profile without sandbox_keys: Codex argv and env unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["coach", "devops"])
async def test_profile_without_keys_gets_unchanged_argv_and_env(
    tmp_path, settings, profiles, which
) -> None:
    """coach has no sandbox_keys; devops has them but is full access (the
    Claude path gives a full-access profile no box either)."""
    cwd = getattr(profiles, which)
    integ, bridge, _, _ = await _run_turn(tmp_path, settings, profiles, cwd)

    env = _child_env(tmp_path)
    argv = _child_argv(tmp_path)
    assert KEY_NAME not in env
    assert not any(SECRET in v for v in env.values())
    joined = " ".join(argv)
    assert "shell_snapshot" not in joined
    assert "shell_environment_policy" not in joined
    # Byte-identical to the argv builder's default output.
    out_file = Path(argv[argv.index("-o") + 1])
    # ("$@" in the stand-in: no argv[0]; the prompt is appended last.)
    assert (
        argv[:-1]
        == integ._build_argv(
            model=MODEL, cwd=cwd, out_file=out_file, resume_id=None, user_id=111
        )[1:]
    )
    (ctx,) = bridge.contexts
    assert getattr(ctx, "redact_env", ()) == ()


# --------------------------------------------------------------------------
# 4. denied or asked command is never rewritten; policy judges the original
# --------------------------------------------------------------------------


async def test_denied_or_asked_command_is_never_rewritten(monkeypatch) -> None:
    judged: List[str] = []
    real = policy.decide

    def spy(tool_name: str, tool_input: Dict[str, Any], turn: Any) -> Any:
        judged.append(str(tool_input.get("command")))
        return real(tool_name, tool_input, turn)

    monkeypatch.setattr(policy, "decide", spy)
    ctx = _keyed_ctx()

    def body(cmd: str) -> Dict[str, Any]:
        return {"tool_name": "Bash", "tool_input": {"command": cmd}}

    # denied by the denylist on the keyed profile's own (sandboxed) policy
    sandboxed = PolicyGate(lambda c: _turn(CWD, False), None)
    out = await sandboxed.check("k", ctx, body("cat .env"))
    assert out["decision"] == policy.DENY
    assert "updated_input" not in out

    # an ask the owner denies, and an ask nobody can confirm
    manager = _confirmation(False)
    asking = PolicyGate(lambda c: _turn(CWD, True), manager)
    out = await asking.check("k", ctx, body("rm notes.txt"))
    assert out["decision"] == policy.DENY
    assert "updated_input" not in out
    preview = repr(manager.request.await_args)
    assert "bash_box" not in preview and "redact-env" not in preview
    out = await PolicyGate(lambda c: _turn(CWD, True), None).check(
        "k", ctx, body("rm notes.txt")
    )
    assert out["decision"] == policy.DENY
    assert "updated_input" not in out

    # positive control: an allowed command IS rewritten ...
    out = await sandboxed.check("k", ctx, body("ls -la"))
    assert out["decision"] == policy.ALLOW
    assert "--redact-env" in out["updated_input"]["command"]
    # ... and every judgement was made on the original command
    assert judged == ["cat .env", "rm notes.txt", "rm notes.txt", "ls -la"]

    # the progress view / trace show the original, not the wrapper
    wrapped = out["updated_input"]["command"]
    shown = map_codex_event(
        {
            "type": "item.started",
            "item": {
                "id": "i1",
                "type": "command_execution",
                "command": shlex.join(["/bin/bash", "-lc", wrapped]),
                "status": "in_progress",
            },
        }
    ).stream_update.tool_calls[0]["input"]["command"]
    assert "ls -la" in shown
    assert "bash_box" not in shown and "redact-env" not in shown


# --------------------------------------------------------------------------
# 5. shell in code mode and in a Codex subagent is redacted too
# --------------------------------------------------------------------------


def test_code_mode_and_subagent_shells_are_rewritten_and_redacted() -> None:
    """Both reach the PreToolUse hook as a plain Bash call under the turn's
    token (probe §2): a nested exec_command from the JS ``exec`` tool, and a
    spawned child thread's shell. Each must come back wrapped and redacted."""
    tokens = TokenRegistry()
    app = build_app(AgentServices(event_bus=None, task_repo=None), tokens)
    app.state.policy_gate = PolicyGate(lambda c: _turn(CWD, False), None)
    token = tokens.mint(_keyed_ctx())
    headers = {"Authorization": f"Bearer {token}"}
    calls = {
        "code mode": {
            "tool_name": "Bash",
            "tool_input": {"command": f"echo KEY=${KEY_NAME}"},
            "tool_use_id": "exec-js:1:exec_command",
        },
        "subagent": {
            "tool_name": "Bash",
            "tool_input": {"command": f"echo CHILDKEY=${KEY_NAME}", "timeout": 5},
            "tool_use_id": "child-thread-c1",
        },
    }
    with TestClient(app) as client:
        for label, body in calls.items():
            answer = client.post(POLICY_PATH, json=body, headers=headers).json()
            assert answer["decision"] == policy.ALLOW, label
            updated = answer["updated_input"]
            assert updated.get("timeout") == body["tool_input"].get("timeout"), label
            res = _run_wrapped(updated["command"], {KEY_NAME: SECRET})
            assert SECRET not in res.stdout + res.stderr, label
            assert "KEY=<redacted>" in res.stdout, label


# --------------------------------------------------------------------------
# added
# --------------------------------------------------------------------------


def _run_hook(answer: Dict[str, Any]) -> subprocess.CompletedProcess:
    """Run the real hook script against a one-shot stub policy endpoint."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            data = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hi"},
                    "tool_use_id": "c1",
                }
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": os.environ.get("PATH", ""),
                "DEVVY_POLICY_URL": (
                    f"http://127.0.0.1:{server.server_port}/policy/check"
                ),
                "AGENT_BRIDGE_TOKEN": "t",
            },
        )
    finally:
        server.shutdown()


def test_hook_relays_updated_input_and_fails_closed_on_a_bad_one() -> None:
    new = {"command": "WRAPPED echo hi", "timeout": 5}
    res = _run_hook({"decision": "allow", "reason": "", "updated_input": new})
    out = json.loads(res.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"] == new

    bad = _run_hook({"decision": "allow", "reason": "", "updated_input": "nope"})
    assert json.loads(bad.stdout)["hookSpecificOutput"]["permissionDecision"] == (
        "deny"
    )


def test_redact_env_with_a_missing_var_fails_closed() -> None:
    env = {k: v for k, v in os.environ.items() if k != KEY_NAME}
    wrapped = BashBox.wrap_env("echo ran", (KEY_NAME,))
    res = subprocess.run(
        ["/bin/bash", "-c", wrapped],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert res.returncode == 126
    assert "ran" not in res.stdout
    assert KEY_NAME in res.stderr + res.stdout


@pytest.mark.parametrize(
    "command",
    [
        "echo 'a b' \"c\"; $(id) `id` && echo $X # \\ \n echo end",
        "python3 -c 'import os; print(os.environ[\"K\"])'",
    ],
)
def test_wrap_env_unwraps_to_the_original(command: str) -> None:
    wrapped = BashBox.wrap_env(command, (KEY_NAME,))
    assert shlex.split(wrapped)[-2:] == ["--", command]
    assert BashBox.unwrap(wrapped) == command
    assert BashBox.unwrap(shlex.join(["/bin/bash", "-lc", wrapped])) == command
    assert BashBox.unwrap(command) is None
    assert BashBox.unwrap("/bin/bash -lc 'echo hi'") is None
