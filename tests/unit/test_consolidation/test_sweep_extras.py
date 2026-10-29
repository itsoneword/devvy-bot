"""Edges of the stale-fact sweep (dv-cf07) not pinned by the protected tests."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.consolidation.service import ConsolidationService
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository

REPO_ROOT = Path(__file__).resolve().parents[3]
PATCH = "src.consolidation.service.complete_text"


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "x.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
async def repo(db):
    return MemoryRepository(db)


def _apply_module():
    path = REPO_ROOT / "scripts" / "consolidation_apply.py"
    spec = importlib.util.spec_from_file_location("consolidation_apply_x", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_undo_of_a_forgotten_row_rolls_back_everything(repo, tmp_path):
    """A ticked UNDO naming a memory_forget closure aborts the whole report:
    the owner's forget is never reopened by the sweep's undo path."""
    a = await repo.add_fact("devops", "general", "old")
    b = await repo.add_fact("devops", "general", "new")
    c = await repo.add_fact("devops", "general", "forgotten")
    assert await repo.close_superseded(a, b, "devops")
    await repo.invalidate_fact(c, "devops", source="user_request")
    report = tmp_path / "report.md"
    report.write_text(
        f"- [x] UNDO scope=devops id={a}\n- [x] UNDO scope=devops id={c}\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    try:
        with pytest.raises(RuntimeError):
            _apply_module().apply_report(conn, report)
    finally:
        conn.close()
    assert (await repo.get_fact(a, "devops")).superseded_by == b  # rolled back
    assert (await repo.get_fact(c, "devops")).invalid_at is not None


async def test_rotation_without_a_final_eval_still_sweeps(repo, db, tmp_path):
    """/new with an empty window has no final eval; the rotation still sweeps."""
    a = await repo.add_fact("devops", "general", "concurrency is 3")
    b = await repo.add_fact("devops", "general", "concurrency raised to 5")
    service = ConsolidationService(memory_repo=repo, db=db, report_dir=tmp_path / "rep")
    reply = json.dumps([{"old_id": a, "newer_id": b, "reason": "raised"}])
    with patch(PATCH, AsyncMock(return_value=reply)):
        service.after_rotation("devops", None)
        assert await service.wait_rotation("devops", timeout=5)
    assert (await repo.get_fact(a, "devops")).superseded_by == b


async def test_rotation_sweep_with_nothing_stale_writes_no_report(repo, db, tmp_path):
    """Per-rotation sweeps only append to the report when they closed
    something; a quiet rotation leaves no file behind."""
    await repo.add_fact("devops", "general", "a")
    await repo.add_fact("devops", "general", "b")
    service = ConsolidationService(memory_repo=repo, db=db, report_dir=tmp_path / "rep")
    with patch(PATCH, AsyncMock(return_value="[]")):
        service.after_rotation("devops", None)
        await service.wait_rotation("devops", timeout=5)
    assert not (tmp_path / "rep").exists()
