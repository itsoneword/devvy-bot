"""Profile-aware memory base: evaluator writes to the active profile's dir."""

import asyncio

from src.evaluator.service import EvaluatorService
from src.evaluator.writer import MemoryWriter


async def test_memory_writer_honors_per_call_base(tmp_path):
    default_dir = tmp_path / "default"
    profile_dir = tmp_path / "channel"
    default_dir.mkdir()
    profile_dir.mkdir()

    writer = MemoryWriter(memory_base=default_dir, write_lock=asyncio.Lock())
    written = await writer.apply_extractions(
        [{"file": "notes.md", "action": "update", "content": "hi"}],
        base=profile_dir,
    )

    assert written == ["notes.md"]
    assert (profile_dir / "notes.md").read_text() == "hi"
    assert not (default_dir / "notes.md").exists()


async def test_memory_writer_defaults_to_construction_base(tmp_path):
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    writer = MemoryWriter(memory_base=default_dir, write_lock=asyncio.Lock())
    await writer.apply_extractions(
        [{"file": "notes.md", "action": "update", "content": "hi"}]
    )
    assert (default_dir / "notes.md").read_text() == "hi"


def test_build_memory_map_uses_passed_base(tmp_path):
    base = tmp_path / "mem"
    base.mkdir()
    (base / "index.md").write_text("x")
    (base / "decisions.md").write_text("y")
    service = EvaluatorService(
        memory_base=tmp_path / "other",
        writer=MemoryWriter(memory_base=tmp_path / "other", write_lock=asyncio.Lock()),
        token_tracker=None,
        db=None,
    )
    out = service._build_memory_map(base)
    assert "index.md" in out
    assert "decisions.md" in out
