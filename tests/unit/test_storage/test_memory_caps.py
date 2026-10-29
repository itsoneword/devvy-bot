"""dv-cf07: the per-turn fallback retriever honours the same fact caps as the
window bundle (MEMORY_SNAPSHOT_GENERAL_CAP / MEMORY_POINTER_CAP)."""

import pytest

from src.storage.database import DatabaseManager
from src.storage.memory_repository import (
    POINTER_CAP,
    MemoryRepository,
    MemoryRetriever,
)


@pytest.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield MemoryRepository(db)
    await db.close()


async def _fill(repo: MemoryRepository, n: int) -> None:
    for i in range(n):
        await repo.add_fact("devops", "general", f"general fact {i}")
        await repo.add_fact(
            "devops",
            "general",
            f"result {i}",
            label=f"result label {i}",
            kind="conversation_result",
        )


async def test_retriever_honours_passed_caps(repo):
    await _fill(repo, 70)
    await repo.add_fact("devops", "pinned", "never push to main")

    pinned, general, pointers = await MemoryRetriever(
        repo, general_cap=60, pointer_cap=60
    ).select("devops", "")
    assert len(pinned) == 1
    assert len(general) == 60
    assert len(pointers) == 60

    _, small_general, no_pointers = await MemoryRetriever(
        repo, general_cap=3, pointer_cap=0
    ).select("devops", "")
    assert len(small_general) == 3
    assert no_pointers == []


async def test_retriever_default_pointer_cap_unchanged(repo):
    await _fill(repo, 30)
    _, _, pointers = await MemoryRetriever(repo).select("devops", "")
    assert len(pointers) == POINTER_CAP


async def test_main_wires_caps_from_settings():
    """src/main.py passes both settings to the bundle and the retriever."""
    from pathlib import Path

    main = (Path(__file__).parents[3] / "src" / "main.py").read_text()
    assert main.count("general_cap=config.memory_snapshot_general_cap") == 2
    assert main.count("pointer_cap=config.memory_pointer_cap") == 2
