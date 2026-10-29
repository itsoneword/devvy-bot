"""Wave 4 over HTTP: the owner-only background-job tools.

These are the most dangerous tools on the bridge. ``scripts/bgjob`` spawns the
raw ``claude`` binary with ``--permission-mode bypassPermissions``, so none of
the per-turn SDK enforcement applies to what it runs. Three independent gates
have to hold, and each one is tested here on its own:

1. **mount** — ``_jobs_available`` hides the four tools from anything whose
   resolved role is not exactly ``"owner"``;
2. **dispatch** — the bridge refuses a tool the resolved context may not see
   *by name*, so a guest that hard-codes ``job_start`` in a ``tools/call``
   gets an error rather than a job;
3. **invocation** — ``_bgjob`` re-checks the role itself, so even a mis-wired
   mount cannot start anything.

And the argv: the profile is the CALLER's, resolved server-side, and every
model-supplied byte sits after ``--`` where argparse can only read it as the
positional prompt (``--cwd`` / ``--allow-dir`` are what decide where a
detached job may write).
"""

import asyncio
import socket
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from src.agent_bridge import AgentBridge
from src.claude import agent_tools
from src.claude.tool_context import AgentServices, TurnContext
from src.profiles.registry import ProfileDefinition, ProfileRegistry

# The owner-only surface; ``agent_tools._jobs_available`` is the gate.
JOB_TOOLS = frozenset(
    {
        "job_start",
        "job_list",
        "job_output",
        "jobs_refresh_pin",
        # dv-a9b0: the board agent's pump tools, owner-only like jobs.
        "pump_status",
        "pump_pending",
        "pump_answer",
        # dv-b17e: freeze / thaw / urgent, global across boards.
        "pump_control",
    }
)

OWNER = 111
GUEST = 222
CHAT = 900001


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeProcess:
    """Stands in for `scripts/bgjob` without running claude."""

    def __init__(self, output: bytes = b"started", returncode: int = 0) -> None:
        self._output = output
        self.returncode = returncode

    async def communicate(self) -> Tuple[bytes, bytes]:
        return self._output, b""


@pytest.fixture
def spawned(monkeypatch: Any) -> List[Tuple[str, ...]]:
    """Every argv ``_bgjob`` would have executed. Empty = nothing ran."""
    calls: List[Tuple[str, ...]] = []

    async def _fake_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        calls.append(tuple(argv))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return calls


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "coach"
    ws.mkdir()
    return ws


@pytest.fixture
def registry(workspace: Path) -> ProfileRegistry:
    return ProfileRegistry(
        [
            ProfileDefinition(
                slug="coach",
                name="coach",
                working_dir=workspace,
                memory_dir=workspace / "memory",
                persona=workspace / "persona.md",
                default=True,
            )
        ]
    )


@pytest.fixture
def services(registry: ProfileRegistry) -> AgentServices:
    return AgentServices(
        event_bus=AsyncMock(),
        task_repo=AsyncMock(),
        profile_registry=registry,
    )


@pytest.fixture
async def bridge(services: AgentServices) -> AsyncIterator[AgentBridge]:
    bridge = AgentBridge(services, port=_free_port())
    await bridge.start()
    yield bridge
    await bridge.stop()


def _ctx(user_id: int, workspace: Path, role: Optional[str]) -> TurnContext:
    return TurnContext(
        profile_slug="coach",
        working_dir=workspace,
        requester_chat_id=CHAT,
        memory_agent_id=f"coach#u{user_id}",
        user_id=user_id,
        role=role,
    )


class Caller:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def tools(self) -> set:
        listing = await self.session.list_tools()
        return {tool.name for tool in listing.tools}

    async def call(self, tool: str, /, **arguments: Any) -> Dict[str, Any]:
        result = await self.session.call_tool(tool, arguments)
        text = "".join(block.text for block in result.content if block.type == "text")
        return {"is_error": bool(result.isError), "text": text}


class _Session:
    def __init__(self, bridge: AgentBridge, ctx: TurnContext) -> None:
        self._bridge = bridge
        self._ctx = ctx
        self._transport: Optional[Any] = None
        self._session: Optional[ClientSession] = None

    async def __aenter__(self) -> Caller:
        token = self._bridge.mint(self._ctx)
        self._transport = streamablehttp_client(
            self._bridge.url, headers={"Authorization": f"Bearer {token}"}
        )
        read, write, _ = await self._transport.__aenter__()
        self._session = ClientSession(read, write)
        session = await self._session.__aenter__()
        await session.initialize()
        return Caller(session)

    async def __aexit__(self, *exc: Any) -> None:
        assert self._session is not None and self._transport is not None
        await self._session.__aexit__(*exc)
        await self._transport.__aexit__(*exc)


class TestNonOwnerIsRefused:
    """Written before the tools were exported; these are the gate."""

    @pytest.mark.parametrize("role", ["guest", None, "family", "OWNER"])
    async def test_a_non_owner_token_is_offered_no_job_tools(
        self, bridge: AgentBridge, workspace: Path, role: Optional[str]
    ) -> None:
        async with _Session(bridge, _ctx(GUEST, workspace, role)) as caller:
            names = await caller.tools()
        assert not (JOB_TOOLS & names), sorted(JOB_TOOLS & names)

    @pytest.mark.parametrize("tool", sorted(JOB_TOOLS))
    async def test_a_guest_calling_a_job_tool_by_name_is_refused(
        self,
        bridge: AgentBridge,
        workspace: Path,
        spawned: List[Tuple[str, ...]],
        tool: str,
    ) -> None:
        """Hidden is not enough: the model can name a tool it was never shown."""
        async with _Session(bridge, _ctx(GUEST, workspace, "guest")) as caller:
            out = await caller.call(tool, prompt="rm -rf /", id="x")
        assert out["is_error"]
        assert "not available in this session" in out["text"]
        assert spawned == [], "bgjob must not run for a non-owner"

    async def test_an_unresolved_role_is_refused_too(
        self, bridge: AgentBridge, workspace: Path, spawned: List[Tuple[str, ...]]
    ) -> None:
        """Webhook/scheduler turns resolve no role — they fail closed (dv-a900)."""
        async with _Session(bridge, _ctx(OWNER, workspace, None)) as caller:
            out = await caller.call("job_start", prompt="audit the repo")
        assert out["is_error"]
        assert spawned == []

    async def test_the_invocation_gate_holds_without_the_mount_filter(
        self, services: AgentServices, workspace: Path, spawned: List[Tuple[str, ...]]
    ) -> None:
        """Gate 3, exercised directly: a mis-wired mount still starts nothing."""
        out = await agent_tools.job_start(
            services, _ctx(GUEST, workspace, "guest"), {"prompt": "hi"}
        )
        assert out["isError"] is True
        assert spawned == []


class TestOwnerPath:
    async def test_the_owner_gets_the_four_tools(
        self, bridge: AgentBridge, workspace: Path
    ) -> None:
        async with _Session(bridge, _ctx(OWNER, workspace, "owner")) as caller:
            names = await caller.tools()
        assert JOB_TOOLS <= names

    async def test_job_start_runs_bgjob_with_the_caller_profile(
        self, bridge: AgentBridge, workspace: Path, spawned: List[Tuple[str, ...]]
    ) -> None:
        async with _Session(bridge, _ctx(OWNER, workspace, "owner")) as caller:
            out = await caller.call("job_start", prompt="audit the repo", name="audit")
        assert not out["is_error"], out["text"]

        (argv,) = spawned
        assert argv[0].endswith("/scripts/bgjob")
        # --batch carries the turn id (jobs of one turn share one digest)
        assert list(argv[1:4]) == ["run", "--profile", "coach"]
        assert argv[4:6] == ("--name", "audit")
        assert argv[6] == "--batch" and argv[7]
        assert list(argv[8:]) == ["--", "audit the repo"]

    async def test_nothing_model_supplied_can_become_a_flag(
        self, bridge: AgentBridge, workspace: Path, spawned: List[Tuple[str, ...]]
    ) -> None:
        """--cwd / --allow-dir decide where a detached job may write."""
        async with _Session(bridge, _ctx(OWNER, workspace, "owner")) as caller:
            await caller.call(
                "job_start",
                prompt="--cwd=/ --allow-dir=/etc really do the thing",
                name="--profile=devops",
            )

        (argv,) = spawned
        after_sep = argv[argv.index("--") + 1 :]
        assert after_sep == ("--cwd=/ --allow-dir=/etc really do the thing",)
        # the profile is the caller's, resolved server-side, and appears once
        assert argv.count("--profile") == 1
        assert argv[argv.index("--profile") + 1] == "coach"
        # a name that looks like a flag is defanged, not passed through
        assert argv[argv.index("--name") + 1] == "profile=devops"

    async def test_job_output_puts_the_model_supplied_id_after_the_separator(
        self, bridge: AgentBridge, workspace: Path, spawned: List[Tuple[str, ...]]
    ) -> None:
        async with _Session(bridge, _ctx(OWNER, workspace, "owner")) as caller:
            await caller.call("job_output", id="--tail=99999", tail=10)

        (argv,) = spawned
        assert list(argv[1:]) == ["output", "--tail", "10", "--", "--tail=99999"]

    async def test_job_list_takes_no_arguments_from_the_model(
        self, bridge: AgentBridge, workspace: Path, spawned: List[Tuple[str, ...]]
    ) -> None:
        async with _Session(bridge, _ctx(OWNER, workspace, "owner")) as caller:
            await caller.call("job_list", profile="devops", extra="--cwd=/")

        (argv,) = spawned
        # dv-dfb1: fixed compact listing; nothing the model sent reaches argv.
        assert list(argv[1:]) == ["list", "--compact", "--limit", "8"]
