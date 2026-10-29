"""Dependency set guards (dv-2776: pip-audit 2026-10-25).

What the lock pins must be free of known vulnerabilities, stay inside the
majors the code is written against (mcp 1.x, claude-agent-sdk 0.2.123), and
still start every server the bot ships. mypy and lint are checked on the
installed toolchain, so a dev-tool bump (black 26) that reformats the tree is
caught here too.
"""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from packaging.version import Version
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "poetry.lock"
PYPROJECT = REPO / "pyproject.toml"

# Lowest version that closes every advisory pip-audit reported on 2026-10-25
# for the packages it flagged (max of each package's fix_versions; mcp takes
# the 1.x fix, 2.x is a separate migration). Offline floor: holds without
# network, the live audit below is the authoritative one.
FIX_FLOORS = {
    "anyio": "4.14.2",
    "black": "26.10.1",
    "certifi": "2026.10.19",
    "cryptography": "50.0.0",
    "filelock": "4.2.1",
    "fsspec": "2026.6.0",
    "idna": "3.15",
    "mcp": "1.30.0",
    "opentelemetry-api": "1.45.1",
    "pydantic-settings": "2.14.2",
    "pygments": "2.20.0",
    "pyjwt": "2.15.0",
    "pytest": "9.0.3",
    "python-dotenv": "1.2.2",
    "python-multipart": "0.0.31",
    "requests": "2.33.0",
    "setuptools": "84.1.0",
    "starlette": "1.3.1",
    "urllib3": "2.8.0",
}

# Majors a plain `poetry update` would have bumped (analysis): none of them
# is part of a security fix, so none may move here.
UNCHANGED_MAJORS = {
    "huggingface-hub": 1,
    "protobuf": 6,
    "websockets": 16,
    "python-dateutil": 2,
    "python-telegram-bot": 22,
    "pydantic": 2,
}

MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "dv-2776-test", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
# FastMCP validates the Host header for localhost-bound servers.
LOCAL_BASE = "http://127.0.0.1:8000"


def _locked() -> dict[str, str]:
    data = tomllib.loads(LOCK.read_text())
    return {p["name"].lower(): p["version"] for p in data["package"]}


def _tmp_tool_env(tmp_path: Path) -> dict[str, str]:
    """uv/poetry write caches under ~/.cache, which is read-only in the box."""
    env = dict(os.environ)
    for key in (
        "UV_CACHE_DIR",
        "UV_TOOL_DIR",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "POETRY_CACHE_DIR",
    ):
        env[key] = str(tmp_path / key.lower())
    return env


# ---------------------------------------------------------------------------
# 1. pip-audit of the lock's pinned set
# ---------------------------------------------------------------------------


def test_pip_audit_of_locked_set_reports_no_vulnerabilities(tmp_path: Path) -> None:
    uvx = shutil.which("uvx")
    if uvx is None:
        pytest.skip("uvx not installed; test_locked_versions_meet_fix_floors holds")
    req = tmp_path / "req.txt"
    req.write_text(
        "".join(f"{name}=={ver}\n" for name, ver in sorted(_locked().items()))
    )
    out = tmp_path / "audit.json"
    proc = subprocess.run(
        [uvx, "pip-audit", "-r", str(req), "--no-deps", "--disable-pip"]
        + ["-f", "json", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
        env=_tmp_tool_env(tmp_path),
    )
    if not out.exists():
        pytest.skip(f"pip-audit could not run (network?): {proc.stderr[-500:]}")
    report = json.loads(out.read_text())
    vulnerable = {
        f"{d['name']} {d['version']}": sorted(v["id"] for v in d["vulns"])
        for d in report["dependencies"]
        if d.get("vulns") and d["name"].lower() != "pip"
    }
    assert vulnerable == {}


def test_locked_versions_meet_fix_floors() -> None:
    locked = _locked()
    below = {
        name: f"{locked[name]} < {floor}"
        for name, floor in FIX_FLOORS.items()
        if name in locked and Version(locked[name]) < Version(floor)
    }
    assert below == {}


# ---------------------------------------------------------------------------
# 2. The suite runs on exactly the locked set
# ---------------------------------------------------------------------------


def test_lock_is_in_sync_with_pyproject(tmp_path: Path) -> None:
    """A stale lock means `poetry install` would not give the tested set."""
    poetry = shutil.which("poetry") or str(Path.home() / ".local/bin/poetry")
    if not Path(poetry).exists():
        pytest.skip("poetry not installed")
    proc = subprocess.run(
        [poetry, "check", "--lock"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        env=_tmp_tool_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# 3. Every shipped server starts and answers
# ---------------------------------------------------------------------------


async def test_channel_intel_server_starts_and_answers_initialize(
    tmp_path: Path,
) -> None:
    from src.channel_intel.server import build_app
    from src.channel_intel.store import Store

    store = await Store.open(tmp_path / "kb.db")
    try:
        app = build_app(store, token="secret")
        with TestClient(app, base_url=LOCAL_BASE) as client:
            r = client.post(
                "/mcp",
                json=MCP_INIT,
                headers={**MCP_HEADERS, "Authorization": "Bearer secret"},
                follow_redirects=True,
            )
        assert r.status_code == 200, r.text
        assert "serverInfo" in r.text
    finally:
        await store.close()


def test_agent_bridge_server_starts_and_answers_initialize() -> None:
    from src.agent_bridge.server import build_app
    from src.agent_bridge.tokens import TokenRegistry
    from src.claude.tool_context import AgentServices, TurnContext
    from src.profiles.registry import ProfileRegistry

    services = AgentServices(
        event_bus=AsyncMock(),
        task_repo=AsyncMock(),
        profile_registry=ProfileRegistry([]),
        memory_writer=AsyncMock(),
        memory_repo=AsyncMock(),
        message_repo=AsyncMock(),
        audit_memory_writer=AsyncMock(),
        user_data=AsyncMock(),
        items=AsyncMock(),
    )
    tokens = TokenRegistry()
    token = tokens.mint(
        TurnContext(
            profile_slug="devops",
            working_dir=Path("/tmp/devops"),
            memory_agent_id="devops#u111",
            user_id=111,
            role="owner",
        )
    )
    with TestClient(build_app(services, tokens), base_url=LOCAL_BASE) as client:
        r = client.post(
            "/mcp",
            json=MCP_INIT,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
    assert r.status_code == 200, r.text
    assert "serverInfo" in r.text


def test_llm_analytics_server_starts_and_answers_health(tmp_path: Path) -> None:
    from src.llm_analytics.server import build_app
    from tests.unit.test_llm_analytics.test_server import _build_fixture

    config = _build_fixture(tmp_path)
    with TestClient(build_app(config)) as client:
        r = client.get("/api/health")
    assert r.status_code == 200, r.text


def test_telegram_mcp_server_starts_over_stdio() -> None:
    """Run the module the way the bot mounts it and complete the handshake."""
    proc = subprocess.run(
        [sys.executable, "-m", "src.mcp.telegram_server"],
        cwd=REPO,
        input=json.dumps(MCP_INIT) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, proc.stderr[-2000:]
    reply: dict[str, Any] = json.loads(lines[0])
    assert reply.get("id") == 1, reply
    assert reply["result"]["serverInfo"]["name"] == "telegram"


# ---------------------------------------------------------------------------
# 4. No unrelated major bumps
# ---------------------------------------------------------------------------


def test_mcp_declared_and_locked_at_1x_fix_and_sdk_unchanged() -> None:
    deps = tomllib.loads(PYPROJECT.read_text())["tool"]["poetry"]["dependencies"]
    mcp_spec = deps.get("mcp")
    spec = mcp_spec if isinstance(mcp_spec, str) else (mcp_spec or {}).get("version")
    assert spec is not None, "mcp is imported directly: declare it in pyproject"
    assert "<2" in spec.replace(" ", ""), spec

    locked = _locked()
    mcp = Version(locked["mcp"])
    assert Version("1.30.0") <= mcp < Version("2"), mcp
    assert locked["claude-agent-sdk"] == "0.2.123"


def test_no_unrelated_major_bumps() -> None:
    locked = _locked()
    moved = {
        name: locked[name]
        for name, major in UNCHANGED_MAJORS.items()
        if Version(locked[name]).major != major
    }
    assert moved == {}


# ---------------------------------------------------------------------------
# 5. mypy baseline and lint
# ---------------------------------------------------------------------------


def test_mypy_error_count_not_above_baseline() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "src"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
    )
    tail = proc.stdout.strip().splitlines()[-1]
    if tail.startswith("Success"):
        return
    assert tail.startswith("Found "), proc.stdout[-2000:] + proc.stderr[-2000:]
    assert int(tail.split()[1]) <= 378, tail


@pytest.mark.parametrize(
    "cmd",
    [
        ["scripts/gen_env_example.py", "--check"],
        ["-m", "black", "--check", "src", "tests"],
        ["-m", "isort", "--check-only", "src", "tests"],
        ["-m", "flake8", "src", "tests"],
    ],
    ids=["env-example", "black", "isort", "flake8"],
)
def test_make_lint_step_passes(cmd: list[str]) -> None:
    """The steps of `make lint`, on the installed toolchain."""
    proc = subprocess.run(
        [sys.executable, *cmd],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
