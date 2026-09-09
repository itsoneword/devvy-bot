"""The bridge over real HTTP, against real SQLite: waves 1 and 2, and the
isolation property that makes per-turn tokens worth the machinery.

Nothing is mocked below the tool handler: a real uvicorn server on loopback,
the real MCP streamable-HTTP client, a real memory DB and a real data.db. The
question these answer is the one that matters — can a token minted for user A
reach user B's rows.
"""

import json
import socket
from pathlib import Path
from typing import Any, AsyncIterator, Dict
from unittest.mock import AsyncMock

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from src.agent_bridge import AgentBridge
from src.claude.tool_context import AgentServices, TurnContext
from src.evaluator.writer import MemoryWriter
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository
from src.storage.user_data import UserDataManager, UserDataRepository

USER_A = 111
USER_B = 222
SCOPE_A = "coach#u111"
SCOPE_B = "coach#u222"
DATE = "2026-08-15"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def memory_repo(tmp_path: Path) -> AsyncIterator[MemoryRepository]:
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield MemoryRepository(db)
    await db.close()


@pytest.fixture
async def user_data(tmp_path: Path) -> AsyncIterator[UserDataRepository]:
    db = UserDataManager(str(tmp_path / "data.db"))
    await db.initialize()
    repo = UserDataRepository(db)
    await repo.log_food(USER_A, DATE, "обед A", kcal=650)
    await repo.log_food(USER_B, DATE, "ужин B", kcal=400)
    yield repo
    await db.close()


@pytest.fixture
def registry() -> ProfileRegistry:
    return ProfileRegistry(
        [
            ProfileDefinition(
                slug="coach",
                name="coach",
                working_dir=Path("/tmp/coach"),
                memory_dir=Path("/tmp/coach/memory"),
                persona=Path("/tmp/coach/persona.md"),
                default=True,
                user_data_tools=frozenset({"logs", "products", "portions"}),
            )
        ]
    )


@pytest.fixture
def services(
    memory_repo: MemoryRepository,
    user_data: UserDataRepository,
    registry: ProfileRegistry,
) -> AgentServices:
    return AgentServices(
        event_bus=AsyncMock(),
        task_repo=AsyncMock(),
        profile_registry=registry,
        memory_writer=MemoryWriter(memory_repo, source="agent_tool"),
        memory_repo=memory_repo,
        audit_memory_writer=MemoryWriter(memory_repo, source="user_request"),
        message_repo=AsyncMock(),
        user_data=user_data,
    )


@pytest.fixture
async def bridge(services: AgentServices) -> AsyncIterator[AgentBridge]:
    bridge = AgentBridge(services, port=_free_port())
    await bridge.start()
    yield bridge
    await bridge.stop()


def _ctx(user_id: int, scope: str) -> TurnContext:
    return TurnContext(
        profile_slug="coach",
        working_dir=Path("/tmp/coach"),
        memory_agent_id=scope,
        user_id=user_id,
        role="owner",
    )


class Caller:
    """One turn's view of the bridge: its token, its tools."""

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def tools(self) -> set:
        listing = await self.session.list_tools()
        return {tool.name for tool in listing.tools}

    async def call(self, name: str, **arguments: Any) -> Dict[str, Any]:
        result = await self.session.call_tool(name, arguments)
        text = "".join(block.text for block in result.content if block.type == "text")
        return {"is_error": bool(result.isError), "text": text}

    async def payload(self, name: str, **arguments: Any) -> Any:
        out = await self.call(name, **arguments)
        assert not out["is_error"], out["text"]
        return json.loads(out["text"])


async def _connect(bridge: AgentBridge, token: str) -> Any:
    return streamablehttp_client(
        bridge.url, headers={"Authorization": f"Bearer {token}"}
    )


class TestWaveOneAndTwoOverHttp:
    async def test_memory_and_data_tools_work_end_to_end(
        self, bridge: AgentBridge, memory_repo: MemoryRepository
    ) -> None:
        token = bridge.mint(_ctx(USER_A, SCOPE_A))
        async with await _connect(bridge, token) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                caller = Caller(session)

                assert {"remember", "recall", "log_train", "day_summary"} <= (
                    await caller.tools()
                )

                # wave 1: a fact written over HTTP lands in this turn's scope
                saved = await caller.payload(
                    "remember", fact="Anna is allergic to walnuts"
                )
                assert saved["status"] == "saved"
                hits = await caller.payload("recall", query="walnuts")
                assert [hit["content"] for hit in hits] == [
                    "Anna is allergic to walnuts"
                ]

                # wave 2: a training write lands in this user's data.db
                await caller.payload(
                    "log_train",
                    date=DATE,
                    session="pull",
                    sets=[{"exercise": "подтягивания", "reps": "8"}],
                )
                day = await caller.payload("day_summary", date=DATE)

        assert [row["exercise"] for row in day["train"]] == ["подтягивания"]
        assert [meal["meal"] for meal in day["food"]] == ["обед A"]
        facts = await memory_repo.get_facts(SCOPE_A)
        assert [(f.content, f.source) for f in facts] == [
            ("Anna is allergic to walnuts", "agent_tool")
        ]

    async def test_a_denylisted_tool_is_refused_by_name(
        self, services: AgentServices
    ) -> None:
        """Not listed AND not callable — a withheld tool is off, not hidden.

        ``NOT_EXPORTED`` is empty today, so the property is exercised over
        real HTTP against a bridge built with a one-entry denylist. That is
        exactly the code path a future entry takes.
        """
        narrow = AgentBridge(services, port=_free_port(), denied={"log_food"})
        await narrow.start()
        try:
            token = narrow.mint(_ctx(USER_A, SCOPE_A))
            async with await _connect(narrow, token) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    caller = Caller(session)
                    listed = await caller.tools()
                    out = await caller.call("log_food", date=DATE, meal="x")
        finally:
            await narrow.stop()
        assert "log_food" not in listed
        assert "remember" in listed  # nothing else was withheld
        assert out["is_error"]
        assert "not available in this session" in out["text"]


class TestCrossUserIsolation:
    """A token is a turn's whole identity. It must not reach anyone else."""

    async def test_user_a_cannot_read_user_b_memory_or_food(
        self, bridge: AgentBridge, services: AgentServices
    ) -> None:
        await services.memory_writer.apply_ops(
            SCOPE_B,
            [
                {
                    "op": "add",
                    "kind": "user_fact",
                    "tier": "general",
                    "content": "Boris keeps his passport in the desk",
                }
            ],
        )
        token_a = bridge.mint(_ctx(USER_A, SCOPE_A))
        async with await _connect(bridge, token_a) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                caller = Caller(session)
                assert await caller.payload("recall", query="passport") == []
                assert await caller.payload("memory_list") == []
                day = await caller.payload("day_summary", date=DATE)
        assert [meal["meal"] for meal in day["food"]] == ["обед A"]

    async def test_user_a_cannot_write_into_user_b_food_log(
        self, bridge: AgentBridge, user_data: UserDataRepository
    ) -> None:
        b_day = await user_data.day_summary(USER_B, DATE)
        b_entry_id = b_day["food"][0]["id"]

        token_a = bridge.mint(_ctx(USER_A, SCOPE_A))
        async with await _connect(bridge, token_a) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                caller = Caller(session)
                edit = await caller.call(
                    "food_edit", edits=[{"entry_id": b_entry_id, "kcal": 9999}]
                )
                delete = await caller.call(
                    "log_delete",
                    entries=[{"domain": "food", "entry_id": b_entry_id}],
                )

        assert edit["is_error"] and delete["is_error"]
        after = await user_data.day_summary(USER_B, DATE)
        assert [(m["meal"], m["kcal"]) for m in after["food"]] == [("ужин B", 400)]

    async def test_user_a_cannot_close_user_b_fact(
        self, bridge: AgentBridge, services: AgentServices, memory_repo
    ) -> None:
        await services.memory_writer.apply_ops(
            SCOPE_B,
            [
                {
                    "op": "add",
                    "kind": "user_fact",
                    "tier": "core",
                    "content": "Boris trains on Tuesdays",
                }
            ],
        )
        (fact_b,) = await memory_repo.get_facts(SCOPE_B)

        token_a = bridge.mint(_ctx(USER_A, SCOPE_A))
        async with await _connect(bridge, token_a) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                caller = Caller(session)
                forget = await caller.call("memory_forget", fact_id=fact_b.id)
                correct = await caller.call(
                    "memory_correct",
                    fact_id=fact_b.id,
                    new_fact="Boris trains on Fridays",
                )

        assert forget["is_error"] and correct["is_error"]
        assert "no fact with that id" in forget["text"]
        still = await memory_repo.get_facts(SCOPE_B)
        assert [f.content for f in still] == ["Boris trains on Tuesdays"]

    async def test_two_tokens_on_one_bridge_stay_apart(
        self, bridge: AgentBridge, memory_repo: MemoryRepository
    ) -> None:
        """Both turns live at once, as they would with two users chatting."""
        token_a = bridge.mint(_ctx(USER_A, SCOPE_A))
        token_b = bridge.mint(_ctx(USER_B, SCOPE_B))
        async with await _connect(bridge, token_a) as (ra, wa, _):
            async with ClientSession(ra, wa) as session_a:
                await session_a.initialize()
                await Caller(session_a).payload("remember", fact="A likes espresso")
                async with await _connect(bridge, token_b) as (rb, wb, _):
                    async with ClientSession(rb, wb) as session_b:
                        await session_b.initialize()
                        caller_b = Caller(session_b)
                        await caller_b.payload("remember", fact="B likes filter coffee")
                        b_view = await caller_b.payload("memory_list")

        assert [f["content"] for f in b_view] == ["B likes filter coffee"]
        assert [f.content for f in await memory_repo.get_facts(SCOPE_A)] == [
            "A likes espresso"
        ]

    async def test_a_revoked_turn_cannot_come_back(self, bridge: AgentBridge) -> None:
        """The turn ended: its token is dead even mid-conversation."""
        token = bridge.mint(_ctx(USER_A, SCOPE_A))
        bridge.revoke(token)
        with pytest.raises(BaseException):
            async with await _connect(bridge, token) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()


class TestStartup:
    async def test_a_taken_port_raises_instead_of_taking_the_bot_down(
        self, services: AgentServices
    ) -> None:
        """uvicorn's own bind failure calls sys.exit(1) from inside the task."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            bridge = AgentBridge(services, port=taken.getsockname()[1])
            with pytest.raises(OSError):
                await bridge.start()
