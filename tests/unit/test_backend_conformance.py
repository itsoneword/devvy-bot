"""Backend parity gate, unit layer (dv-b7c4).

ONE matrix, every backend x profile type, asserting the launch config each
cell must have. A backend/sandbox change that breaks a cell fails here, not
in a week of live turns (dv-c7d1: confined Codex silently lost every bot tool
because the permission profile made MCP calls need approval under
``approval=never``).

Cells: ``claude`` / ``codex`` x ``confined`` (sandboxed profile, e.g. coach)
/ ``unrestricted`` (devops). The contract per cell:

* bot tools reachable: the ``agent`` server is mounted, and nothing stands
  between the model and a call (codex: every bot MCP server pre-approved;
  claude: ``can_use_tool`` allows ``mcp__agent__*``).
* confinement: confined cells never run with full access and hide the
  data-root secrets/ and db/; unrestricted cells run full access by design.
* the credential layer holds on every cell that has one.

Plus one per-backend scenario: a failed tool call reaches ``tool_calls`` as
success=0 with its error (the second dv-c7d1 bug). The live counterpart is
``scripts/backend_smoke.py``.
"""

from __future__ import annotations

import json
import shlex
import stat
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.agent_backends import codex_sandbox
from src.agent_backends.codex_cli import CodexCliIntegration, InMemoryCodexSessionStore
from src.claude.sdk_integration import ClaudeSDKManager
from src.claude.session import ClaudeSession, SessionManager
from src.claude.tool_context import AgentServices
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.security import denylist
from src.security.denylist import ProtectedDirs
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage
from tests.helpers.traced import traced

BACKENDS = ("claude", "codex")
PROFILE_TYPES = ("confined", "unrestricted")
CELLS = [(b, p) for b in BACKENDS for p in PROFILE_TYPES]
USER_ID = 111
BRIDGE_URL = "http://127.0.0.1:1/mcp"


class Tree:
    """A data root with secrets/db/archive, a repo, and two profiles."""

    def __init__(self, base: Path) -> None:
        home = base / "Devvybot"
        root = home / "data"
        self.secrets = root / "secrets"
        self.db = root / "db"
        self.archive = home / "archive"
        self.repo = home / "repo"
        ws = root / "workspaces"
        self.coach = ws / "coach"
        self.own = self.coach / "users" / str(USER_ID)
        self.other = self.coach / "users" / "222"
        for d in (self.secrets, self.db, self.archive, self.repo, self.own, self.other):
            d.mkdir(parents=True)
        self.dirs = ProtectedDirs(
            everyone=(self.secrets.resolve(), self.archive.resolve()),
            tenant=(self.db.resolve(),),
        )

        def profile(slug: str, wd: Path, **kw: Any) -> ProfileDefinition:
            return ProfileDefinition(
                slug=slug,
                name=slug,
                working_dir=wd,
                memory_dir=wd / "memory",
                persona=wd / "persona.md",
                **kw,
            )

        self.registry = ProfileRegistry(
            [
                profile(
                    "devops", self.repo, default=True, sandbox=False, unrestricted=True
                ),
                profile("coach", self.coach, sandbox=True),
            ]
        )

    def cwd(self, profile_type: str) -> Path:
        return self.own if profile_type == "confined" else self.repo


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tree:
    t = Tree(tmp_path)
    monkeypatch.setattr(denylist, "protected_dirs", lambda: t.dirs)
    return t


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(
        json.dumps({"mcpServers": {"telegram": {"command": "python3", "args": ["t"]}}})
    )
    return Settings(
        telegram_bot_token="x",
        telegram_bot_username="x",
        approved_directory=tmp_path,
        enable_mcp=True,
        mcp_config_path=mcp_json,
    )


# --------------------------------------------------------------------------
# Launch config per backend, normalised to what the cell asserts on
# --------------------------------------------------------------------------


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def codex_config(argv: List[str]) -> Dict[str, Any]:
    """Every ``-c key=value`` in ``argv``, parsed as TOML the way codex does."""
    cfg: Dict[str, Any] = {}
    for flag, value in zip(argv, argv[1:]):
        if flag == "-c":
            _deep_merge(cfg, tomllib.loads(value))
    return cfg


def _codex_launch(tree: Tree, settings: Settings, profile_type: str) -> List[str]:
    integ = CodexCliIntegration(
        config=settings,
        session_store=InMemoryCodexSessionStore(),
        profile_registry=tree.registry,
    )
    integ.attach_tools(None, type("Bridge", (), {"url": BRIDGE_URL})())
    cwd = tree.cwd(profile_type)
    return integ._build_argv(
        model="openai-codex/gpt-6-luna",
        cwd=cwd,
        out_file=cwd / "o.txt",
        resume_id=None,
        user_id=USER_ID,
    )


class _Bus:
    def __init__(self) -> None:
        self.events: List[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


def _claude_manager(tree: Tree, settings: Settings) -> ClaudeSDKManager:
    manager = ClaudeSDKManager(
        settings,
        security_validator=SecurityValidator(tree.repo.parent),
        profile_registry=tree.registry,
    )
    manager.attach_agent_services(AgentServices(event_bus=_Bus(), task_repo=None))
    return manager


def _mock_client(messages: List[Any], captured: List[Any]) -> Any:
    def factory(options: Any) -> Any:
        captured.append(options)
        client = AsyncMock()

        async def receive() -> Any:
            for m in messages:
                yield m

        client._query = AsyncMock()
        client._query.receive_messages = receive
        return client

    return factory


def _result(**kw: Any) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sess-conformance",
        total_cost_usd=0.0,
        result="ok",
    )
    base.update(kw)
    return ResultMessage(**base)


async def _claude_run(
    tree: Tree, settings: Settings, profile_type: str, messages: List[Any]
) -> Tuple[Any, Any]:
    manager = _claude_manager(tree, settings)
    captured: List[Any] = []
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_mock_client(messages, captured),
        ):
            response = await manager.execute_command(
                prompt="hi",
                working_directory=tree.cwd(profile_type),
                user_id=USER_ID,
                role="owner",
                model="haiku",
            )
    assert len(captured) == 1
    return captured[0], response


async def _claude_options(tree: Tree, settings: Settings, profile_type: str) -> Any:
    options, _ = await _claude_run(tree, settings, profile_type, [_result()])
    return options


async def _hook_decision(options: Any, tool: str, tool_input: Dict[str, Any]) -> str:
    hook = options.hooks["PreToolUse"][0].hooks[0]
    out = await hook({"tool_name": tool, "tool_input": tool_input}, None, None)
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision", "allow")


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize("backend,profile_type", CELLS)
class TestLaunchContract:
    async def test_bot_tools_mounted_and_callable(
        self, tree, settings, backend, profile_type
    ) -> None:
        """The agent server is there and a call needs no human approval."""
        if backend == "codex":
            argv = _codex_launch(tree, settings, profile_type)
            servers = codex_config(argv)["mcp_servers"]
            assert servers["agent"]["url"] == BRIDGE_URL
            assert "telegram" in servers
            # dv-c7d1: codex exec runs approval=never, so under a permission
            # profile an MCP server that still needs approval is a server
            # whose every call fails. danger-full-access asks for none.
            if "danger-full-access" not in argv:
                for name, server in servers.items():
                    mode = server.get("default_tools_approval_mode")
                    assert mode == "approve", f"{name}: approval mode {mode!r}"
        else:
            options = await _claude_options(tree, settings, profile_type)
            assert {"agent", "telegram"} <= set(options.mcp_servers)
            decision = await options.can_use_tool(
                "mcp__agent__notify",
                {"text": "x"},
                ToolPermissionContext(signal=None, suggestions=[]),
            )
            assert isinstance(decision, PermissionResultAllow)
            assert await _hook_decision(options, "mcp__agent__notify", {}) == "allow"

    async def test_confinement_matches_profile_type(
        self, tree, settings, backend, profile_type
    ) -> None:
        confined = profile_type == "confined"
        if backend == "codex":
            argv = _codex_launch(tree, settings, profile_type)
            cfg = codex_config(argv)
            if confined:
                name = codex_sandbox.PERMISSION_PROFILE
                assert "danger-full-access" not in argv
                assert "-s" not in argv
                assert cfg["default_permissions"] == name
                fs = cfg["permissions"][name]["filesystem"]
                assert fs[":root"] == "read"
                assert fs[str(tree.own.resolve())] == "write"
                for hidden in (tree.secrets, tree.db, tree.archive, tree.other):
                    assert fs[str(hidden.resolve())] == "none", hidden
            else:
                i = argv.index("danger-full-access")
                assert argv[i - 1] == "-s"
                assert "default_permissions" not in cfg
        else:
            options = await _claude_options(tree, settings, profile_type)
            assert options.permission_mode != "bypassPermissions"
            assert options.can_use_tool is not None
            assert options.sandbox["autoAllowBashIfSandboxed"] is False
            # dv-98f2: the CLI's own sandbox is dead on this host, so it is
            # off for every profile; confined bash runs in the hook's box.
            assert options.sandbox["enabled"] is False
            hook = options.hooks["PreToolUse"][0].hooks[0]
            boxed = await hook(
                {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
                None,
                None,
            )
            updated = (boxed.get("hookSpecificOutput") or {}).get("updatedInput")
            if confined:
                assert updated is not None
                assert "bash_box.py" in updated["command"]
                assert str(tree.secrets.resolve()) not in updated["command"]
                for path in (tree.secrets / "k", tree.db / "bot.db"):
                    assert (
                        await _hook_decision(options, "Read", {"file_path": str(path)})
                        == "deny"
                    ), path
                assert (
                    await _hook_decision(
                        options, "Bash", {"command": f"cat {tree.secrets}/k"}
                    )
                    == "deny"
                )
            else:
                # devops is the server admin (dv-e38e): data root reachable.
                assert updated is None

    async def test_credentials_denied(
        self, tree, settings, backend, profile_type
    ) -> None:
        """Credential files: denied on every cell that has a deny layer.
        Codex unrestricted runs danger-full-access, which has none (by design;
        the owner's server-admin profile)."""
        if backend == "codex":
            argv = _codex_launch(tree, settings, profile_type)
            if profile_type == "unrestricted":
                assert "danger-full-access" in argv
                return
            fs = codex_config(argv)["permissions"][codex_sandbox.PERMISSION_PROFILE][
                "filesystem"
            ]
            assert fs[str(Path.home() / ".ssh")] == "none"
            assert any(k.endswith("/.env") for k, v in fs.items() if v == "none")
        else:
            options = await _claude_options(tree, settings, profile_type)
            env_file = tree.cwd(profile_type) / ".env"
            assert (
                await _hook_decision(options, "Read", {"file_path": str(env_file)})
                == "deny"
            )

    async def test_bridge_token_never_on_argv(
        self, tree, settings, backend, profile_type
    ) -> None:
        if backend == "codex":
            cfg = codex_config(_codex_launch(tree, settings, profile_type))
            agent = cfg["mcp_servers"]["agent"]
            assert agent["bearer_token_env_var"] == "AGENT_BRIDGE_TOKEN"
            assert "bearer_token" not in agent
        else:
            pytest.skip("claude mounts the agent tools in-process: no token")


# --------------------------------------------------------------------------
# Tool outcome -> tool_calls (harness trace), per backend
# --------------------------------------------------------------------------

FAIL_MSG = "MCP tool call requires approval, but approval policy is never"


@pytest.fixture
async def storage(tmp_path: Path):
    st = Storage(f"sqlite:///{tmp_path / 'bot.db'}")
    await st.initialize()
    yield st
    await st.close()


def _fake_codex(tmp_path: Path) -> Path:
    """A codex turn with one failed and one successful bot tool call."""

    def mcp(item_id: str, tool: str, **done: Any) -> List[Dict[str, Any]]:
        item = {"id": item_id, "type": "mcp_tool_call", "server": "agent",
                "tool": tool, "arguments": {}}  # fmt: skip
        return [
            {"type": "item.started", "item": {**item, "status": "in_progress"}},
            {"type": "item.completed", "item": {**item, **done}},
        ]

    events = [
        {"type": "thread.started", "thread_id": "thread-conformance"},
        *mcp("i1", "memory_list", status="failed", error={"message": FAIL_MSG}),
        *mcp("i2", "notify", status="completed", result={"content": []}),
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    script = tmp_path / "fake_codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        + "".join(f"echo {shlex.quote(json.dumps(e))}\n" for e in events)
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def _run_turn(backend: str, tree: Tree, settings: Settings, storage, tmp_path):
    manager = SessionManager(settings, SQLiteSessionStorage(storage.db_manager))
    if backend == "codex":
        integ = CodexCliIntegration(
            config=settings,
            codex_bin=str(_fake_codex(tmp_path)),
            session_store=InMemoryCodexSessionStore(),
            session_manager=manager,
        )
        return await traced(
            storage,
            lambda: integ.run_command(
                prompt="p", working_directory=tree.own, user_id=USER_ID, force_new=True
            ),
        )
    messages = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="t1", name="mcp__agent__memory_list", input={}),
                ToolUseBlock(id="t2", name="mcp__agent__notify", input={}),
            ],
            model="haiku",
        ),
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="t1", content=FAIL_MSG, is_error=True),
                ToolResultBlock(tool_use_id="t2", content="sent", is_error=False),
            ]
        ),
        AssistantMessage(content=[TextBlock(text="ok")], model="haiku"),
        _result(),
    ]

    async def _claude_call() -> Any:
        _, resp = await _claude_run(tree, settings, "confined", messages)
        return resp

    response = await traced(storage, _claude_call)
    # The Claude facade owns the sessions row; mirror it for the FK.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    await manager.update_session(
        ClaudeSession(
            session_id="",
            user_id=USER_ID,
            project_path=tree.own,
            created_at=now,
            last_used=now,
            is_new_session=True,
        ),
        response,
    )
    return response


@pytest.mark.parametrize("backend", ["claude", "codex"])
async def test_failed_tool_call_recorded_as_failure(
    backend, tree, settings, storage, tmp_path
) -> None:
    response = await _run_turn(backend, tree, settings, storage, tmp_path)
    message_id = await storage.save_claude_interaction(
        user_id=USER_ID,
        session_id=response.session_id,
        prompt="p",
        response=response,
    )
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute(
            "SELECT tool_name, ok, error FROM tool_calls "
            "WHERE message_id = ? ORDER BY id",
            (message_id,),
        )
        rows = {r[0].rsplit("__", 1)[-1]: (r[1], r[2]) for r in await cur.fetchall()}
    assert rows["memory_list"] == (0, FAIL_MSG)
    assert rows["notify"] == (1, None)


ITEM_TOOLS = {
    "item_add",
    "item_list",
    "item_update",
    "item_note",
    "item_done",
    "item_cancel",
    "item_cancel_all_mine",
    "family_member_set",
}


@pytest.mark.parametrize("backend,profile_type", CELLS)
def test_item_tools_mounted_on_every_cell(
    tree, settings, backend, profile_type
) -> None:
    """dv-752a: family reminder tools reach both backends on every profile type.

    Claude mounts what build_agent_mcp_server wraps; Codex lists what the
    bridge serves for the minted context. Both must include every item_* tool.
    """
    services = AgentServices(event_bus=_Bus(), task_repo=None, items=object())
    cwd = tree.cwd(profile_type)
    if backend == "claude":
        from src.claude import agent_tools

        manager = _claude_manager(tree, settings)
        manager.attach_agent_services(services)
        mounted: List[str] = []
        real = agent_tools.sdk_tool

        def spy(spec: Any, *a: Any) -> Any:
            mounted.append(spec.name)
            return real(spec, *a)

        with patch.object(agent_tools, "sdk_tool", spy):
            manager._agent_mcp_server_for(cwd, user_id=USER_ID, role="owner")
        names = set(mounted)
    else:
        from src.agent_bridge.server import AgentBridgeMcp
        from src.agent_bridge.tokens import TokenRegistry
        from src.claude.turn_setup import resolve_turn_context

        ctx = resolve_turn_context(tree.registry, cwd, user_id=USER_ID, role="owner")
        names = {
            s.name for s in AgentBridgeMcp(services, TokenRegistry()).specs_for(ctx)
        }
    assert ITEM_TOOLS <= names
