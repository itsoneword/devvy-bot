"""dv-28f2 edges beyond tests/unit/test_codex_sandbox_keys.py.

The redaction wrapper (no bwrap, so no skip), the gate's rewrite scope, the
JSONL backstop and the key-store loader the Codex backend shares with the
launcher.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, _scrubber
from src.agent_bridge.policy_gate import PolicyGate
from src.claude.tool_context import TurnContext
from src.security import policy
from src.security.bash_box import BashBox, KeyEnvError, load_key_env

CWD = Path("/home/testuser/Devvybot/data/workspaces/keyed")
LONG = "sk-long-0123456789abcdef"
SHORT = "sk-long-0123"  # a prefix of LONG: must not leave LONG's tail behind


def _run(command: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **env},
    )


def test_redact_env_merges_streams_keeps_exit_code_longest_first() -> None:
    wrapped = BashBox.wrap_env(
        'echo "out=$A"; echo "err=$B" >&2; exit 3', ("A", "B")
    )
    res = _run(wrapped, {"A": LONG, "B": SHORT})
    assert res.returncode == 3
    assert "out=<redacted>" in res.stdout
    assert "err=<redacted>" in res.stdout  # stderr merged into stdout
    assert LONG not in res.stdout and SHORT not in res.stdout
    assert "0123456789" not in res.stdout


def test_redact_env_refuses_box_options() -> None:
    wrapped = BashBox.wrap_env("echo ran", ("A",)).replace(
        "--redact-env", "--rw /tmp --redact-env", 1
    )
    res = _run(wrapped, {"A": LONG})
    assert res.returncode == 126
    assert "ran" not in res.stdout


async def test_gate_rewrites_only_bash_on_a_keyed_turn() -> None:
    gate = PolicyGate(
        lambda c: policy.TurnPolicy(working_directory=CWD, full_access=False)
    )
    keyed = TurnContext(profile_slug="keyed", working_dir=CWD, redact_env=("A",))
    plain = TurnContext(profile_slug="coach", working_dir=CWD)
    body = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert "updated_input" not in await gate.check("k", plain, body)
    assert "updated_input" in await gate.check("k", keyed, body)
    read = {"tool_name": "Read", "tool_input": {"file_path": str(CWD / "x")}}
    out = await gate.check("k", keyed, read)
    assert out["decision"] == policy.ALLOW and "updated_input" not in out
    empty = {"tool_name": "Bash", "tool_input": {"command": "  "}}
    assert "updated_input" not in await gate.check("k", keyed, empty)


def test_scrubber_covers_the_json_escaped_form() -> None:
    value = 'ab"cd\\efgh'
    scrub = _scrubber([value])
    import json

    line = json.dumps({"out": f"K={value}"})
    assert value not in json.loads(scrub(line))["out"]
    assert "<redacted>" in json.loads(scrub(line))["out"]
    assert _scrubber([])("unchanged") == "unchanged"


def test_load_key_env_raises_instead_of_exiting(tmp_path, monkeypatch) -> None:
    root = tmp_path / "data"
    (root / "secrets" / "tools").mkdir(parents=True)
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(root))
    with pytest.raises(KeyEnvError, match="no keys stored for 'nope'"):
        load_key_env(("nope",))
    (root / "secrets" / "tools" / "svc.env").write_text(f"K={LONG}\n")
    env, values = load_key_env(("svc",))
    assert env == {"K": LONG} and values == [LONG]


def test_shell_env_args_empty_without_names() -> None:
    assert CodexCliIntegration._shell_env_args(()) == []
    args = " ".join(CodexCliIntegration._shell_env_args(("K",)))
    assert "features.shell_snapshot=false" in args
    assert '"K"' in args and "AGENT_BRIDGE_TOKEN" not in args
