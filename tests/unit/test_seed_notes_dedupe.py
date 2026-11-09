"""dv-b50f: the roll seed's <session_notes> must not restate what the window
bundle (<constraints>, <user_facts>, <memory_pointers>) already carries."""

import re
import tempfile
from pathlib import Path

import pytest

from src.bot.orchestration.windowing import (
    _session_notes_for_seed,
    consume_pending_seed,
)
from src.bot.window_seed import SeedTail, build_seed_preamble
from src.claude.prompt_composer import PromptComposer
from src.storage.database import DatabaseManager
from src.storage.memory_repository import (
    MemoryRepository,
    render_fact_line,
    render_pointer_line,
)
from src.storage.window_repository import WindowRepository

LONG = 40


@pytest.fixture
async def repos():
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    return WindowRepository(db), MemoryRepository(db)


async def _parents(db):
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (7)")
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES ('s', 7, '/p')"
        )
        await conn.commit()


async def _arm(wrepo):
    await wrepo.get_or_create(7, "/p")
    await wrepo.roll_window(7, "/p", new_backend="claude")
    await wrepo.set_pending_transition(7, "/p", seed_window=1, force_new_session=True)


def _section(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1) if m else ""


def _long_lines(block: str) -> set:
    return {ln.strip() for ln in block.splitlines() if len(ln.strip()) > LONG}


async def test_seed_notes_empty_when_bundle_carries_everything(repos):
    _w, mem = repos
    await mem.add_fact("devops", "pinned", "Never push to main without asking first")
    for i in range(5):
        await mem.add_fact("devops", "general", f"General fact number {i} is here ok")
    await mem.add_fact(
        "devops", "general", "body", label="old conclusion", kind="conversation_result"
    )
    assert await _session_notes_for_seed(mem, "devops") == ""


def test_preamble_omits_notes_block_when_empty():
    tail = SeedTail(text="User: hi\nAssistant: yo", shown_turns=1, total_turns=1)
    out = build_seed_preamble(tail=tail, user_message="next", session_notes="")
    assert "<session_notes>" not in out
    assert "<recent_conversation>" in out
    assert (
        build_seed_preamble(
            tail=SeedTail(text="", shown_turns=0, total_turns=0),
            user_message="next",
            session_notes="",
        )
        == "next"
    )


async def test_notes_keep_overflow_facts_and_exclude_conflicts(repos):
    """Facts the bundle does not render (beyond its caps) still reach the
    seed; a conflict-pair member never does."""
    _w, mem = repos
    for i in range(130):
        await mem.add_fact("devops", "general", f"Overflow fact {i:03d} of the run")
    notes = await _session_notes_for_seed(mem, "devops")
    assert "Overflow fact 000" in notes
    assert "Overflow fact 129" not in notes

    snap = await mem.facts_snapshot("devops")
    conflict_ids = {f.id for pair in snap.conflicts for f in pair}
    assert not any(
        render_fact_line(f) in notes for f in snap.general if f.id in conflict_ids
    )


async def test_no_long_line_in_two_seed_or_bundle_sections(repos):
    wrepo, mem = repos
    await _parents(wrepo._db)
    await mem.add_fact("devops", "pinned", "Always run make test-core before restart")
    for i in range(6):
        await mem.add_fact("devops", "general", f"The user prefers option {i} always")
    await mem.add_fact(
        "devops",
        "general",
        "body",
        label="earlier conclusion",
        kind="conversation_result",
    )
    snap = await mem.facts_snapshot("devops")
    persona = Path(tempfile.mkdtemp()) / "agent.md"
    persona.write_text("You are a test agent.")
    bundle = PromptComposer(Path(tempfile.mkdtemp()), persona).compose(
        Path(tempfile.mkdtemp()),
        pinned_facts=[render_fact_line(f) for f in snap.pinned],
        general_facts=[render_fact_line(f) for f in snap.general],
        result_pointers=[render_pointer_line(p) for p in snap.pointers],
    )
    async with wrepo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,'Always run make test-core before restart?',"
            "'The user prefers option 1 always, so yes',1)"
        )
        await conn.commit()
    await _arm(wrepo)
    seed = await consume_pending_seed(
        wrepo,
        {},
        user_id=7,
        working_dir="/p",
        user_message="please continue the earlier work",
        memory_repo=mem,
        agent_id="devops",
    )
    sections = {
        "constraints": _section(bundle, "constraints"),
        "user_facts": _section(bundle, "user_facts"),
        "memory_pointers": _section(bundle, "memory_pointers"),
        "session_notes": _section(seed, "session_notes"),
    }
    names = list(sections)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            dup = _long_lines(sections[a]) & _long_lines(sections[b])
            # Section header prose may legitimately differ; facts must not.
            assert not dup, (a, b, dup)
    assert "<session_notes>" not in seed


async def test_recent_conversation_does_not_repeat_the_new_request(repos):
    wrepo, mem = repos
    await _parents(wrepo._db)
    request = "Please check why the nightly backup job keeps failing on the NAS"
    async with wrepo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,?,'On it, looking at the logs now',1)",
            (request,),
        )
        await conn.commit()
    await _arm(wrepo)
    seed = await consume_pending_seed(
        wrepo,
        {},
        user_id=7,
        working_dir="/p",
        user_message=request,
        memory_repo=mem,
        agent_id="devops",
    )
    assert seed.count(request) == 1
