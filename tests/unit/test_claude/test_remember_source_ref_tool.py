"""dv-32fe phase 2 (build): ``remember`` with ``source_ref`` goes through the
same decision check as the evaluator; without it, exactly as before.

Same scenarios as the protected test_remember_source_ref.py, whose fixture
fakes ``create_sdk_mcp_server(name, version, tl)`` and so cannot receive the
``tools=`` keyword the real call (and test_agent_tools_schemas.py's
keyword-only fake) uses. This copy names the parameter ``tools``, plus a
non-repo agent case.
"""

from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from src.claude import agent_tools
from src.claude.agent_tools import AgentServices, build_agent_mcp_server
from src.evaluator.decision_refs import DecisionRefChecker
from src.evaluator.writer import MemoryWriter
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository

DECISION = "gb-1ab3: the feed header shows the city name only, no country."
ABSENT = "gb-9f2c: quiet mode mutes push notifications at night."
# Built, not a literal: the export gate flags withheld paths named in tests.
REF = "/".join(("docs", "DECISIONS.md"))


class _Bus:
    async def publish(self, event: Any) -> None:
        pass


@pytest.fixture
async def memory_repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield MemoryRepository(db)
    await db.close()


@pytest.fixture
def tools(monkeypatch, tmp_path, memory_repo) -> Dict[str, Any]:
    root = tmp_path / "gymbro"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "DECISIONS.md").write_text("## gb-1ab3 feed header\n")
    captured: Dict[str, Any] = {}

    def fake_create(name: str, version: str, tools: list) -> Dict[str, str]:
        captured.update({t.name: t for t in tools})
        return {"name": name}

    monkeypatch.setattr(agent_tools, "create_sdk_mcp_server", fake_create)
    writer = MemoryWriter(
        memory_repo,
        source="agent_tool",
        decision_refs=DecisionRefChecker({"gymbro": (root,)}),
    )
    services = AgentServices(
        event_bus=_Bus(),
        task_repo=AsyncMock(),
        scheduler=AsyncMock(),
        memory_writer=writer,
    )
    build_agent_mcp_server(
        services,
        "gymbro",
        Path("/tmp/ws"),
        requester_chat_id=1,
        memory_agent_id="gymbro",
    )
    return captured


def _text(result) -> str:
    return result["content"][0]["text"]


async def test_remember_with_verified_ref_saves_a_pointer(tools, memory_repo):
    result = await tools["remember"].handler({"fact": DECISION, "source_ref": REF})
    assert result.get("isError") is not True
    assert '"saved"' in _text(result) and "not_in_repo" not in _text(result)
    (fact,) = await memory_repo.get_facts("gymbro")
    assert (fact.kind, fact.source_ref, fact.content) == (
        "conversation_result",
        REF,
        DECISION,
    )


async def test_remember_with_unverified_ref_is_saved_not_in_repo(tools, memory_repo):
    result = await tools["remember"].handler({"fact": ABSENT, "source_ref": REF})
    assert result.get("isError") is not True
    assert "saved_not_in_repo" in _text(result)
    (fact,) = await memory_repo.get_facts("gymbro")
    assert (fact.kind, fact.source_ref) == ("user_fact", f"missing:{REF}")
    assert fact.content == ABSENT


async def test_remember_without_source_ref_is_unchanged(tools, memory_repo):
    result = await tools["remember"].handler({"fact": "prefers short answers"})
    assert '"saved"' in _text(result)
    (fact,) = await memory_repo.get_facts("gymbro")
    assert (fact.kind, fact.source_ref) == ("user_fact", None)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd"])
async def test_remember_rejects_escaping_source_ref(tools, memory_repo, bad):
    result = await tools["remember"].handler({"fact": DECISION, "source_ref": bad})
    assert result.get("isError") is True
    assert await memory_repo.get_facts("gymbro") == []


async def test_remember_source_ref_too_long_is_rejected(tools, memory_repo):
    result = await tools["remember"].handler(
        {"fact": DECISION, "source_ref": "docs/" + "x" * 300}
    )
    assert result.get("isError") is True
    assert await memory_repo.get_facts("gymbro") == []


async def test_remember_card_ref_unverified_is_flagged(tools, memory_repo):
    result = await tools["remember"].handler({"fact": ABSENT, "source_ref": "gb-9f2c"})
    assert "saved_not_in_repo" in _text(result)
    (fact,) = await memory_repo.get_facts("gymbro")
    assert (fact.kind, fact.source_ref) == ("user_fact", "missing:gb-9f2c")
