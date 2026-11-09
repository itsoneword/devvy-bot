"""dv-32fe phase 2: a flagged ("missing:<ref>") decision fact becomes a pointer
at the next sweep once the repo holds it.

Pinned interface: ``ConsolidationService(..., decision_refs=checker)``; the
promotion runs inside ``sweep(scope)`` before the model call.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.consolidation.service import ConsolidationService
from src.evaluator.decision_refs import DecisionRefChecker
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository

PATCH = "src.consolidation.service.complete_text"
DECISION = "gb-1ab3: the feed header shows the city name only, no country."
OTHER = "gb-9f2c: quiet mode mutes push notifications between 22:00 and 08:00."
PREF = "The owner wants answers short and without emoji."


@pytest.fixture
async def db(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
async def repo(db):
    return MemoryRepository(db)


@pytest.fixture
def gymbro_dir(tmp_path):
    root = tmp_path / "gymbro"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "DECISIONS.md").write_text("# Decisions\n")
    return root


@pytest.fixture
def service(repo, db, tmp_path, gymbro_dir):
    return ConsolidationService(
        memory_repo=repo,
        db=db,
        report_dir=tmp_path / "consolidation",
        decision_refs=DecisionRefChecker({"gymbro": (gymbro_dir,)}),
    )


async def _seed(repo):
    ids = {}
    for key, text, ref in (
        ("a", DECISION, "missing:docs/DECISIONS.md"),
        ("b", OTHER, "missing:docs/DECISIONS.md"),
        ("c", PREF, None),
    ):
        ids[key] = await repo.add_fact(
            "gymbro", "general", text, source="evaluator", source_ref=ref
        )
    return ids


async def test_flag_clears_to_pointer_once_repo_has_the_id(service, repo, gymbro_dir):
    ids = await _seed(repo)
    with patch(PATCH, new=AsyncMock(return_value="[]")):
        await service.sweep("gymbro")
    # Repo still lacks both ids: nothing promoted.
    assert (await repo.get_fact(ids["a"], "gymbro")).kind == "user_fact"

    (gymbro_dir / "docs" / "DECISIONS.md").write_text("## gb-1ab3 feed header\n")
    with patch(PATCH, new=AsyncMock(return_value="[]")):
        await service.sweep("gymbro")

    a = await repo.get_fact(ids["a"], "gymbro")
    assert a.kind == "conversation_result"
    assert a.source_ref == "docs/DECISIONS.md"  # prefix stripped
    assert a.content == DECISION  # full text untouched
    assert a.status == "active"
    b = await repo.get_fact(ids["b"], "gymbro")
    assert b.kind == "user_fact" and b.source_ref == "missing:docs/DECISIONS.md"
    c = await repo.get_fact(ids["c"], "gymbro")
    assert c.kind == "user_fact" and c.source_ref is None


async def test_promotion_skips_agents_without_a_repo(repo, db, tmp_path, gymbro_dir):
    (gymbro_dir / "docs" / "DECISIONS.md").write_text("## gb-1ab3\n")
    svc = ConsolidationService(
        memory_repo=repo,
        db=db,
        report_dir=tmp_path / "c2",
        decision_refs=DecisionRefChecker({"gymbro": (gymbro_dir,)}),
    )
    fid = await repo.add_fact(
        "chef", "general", DECISION, source_ref="missing:docs/DECISIONS.md"
    )
    await repo.add_fact("chef", "general", "likes spicy soup")
    with patch(PATCH, new=AsyncMock(return_value="[]")):
        await svc.sweep("chef")
    assert (await repo.get_fact(fid, "chef")).kind == "user_fact"
