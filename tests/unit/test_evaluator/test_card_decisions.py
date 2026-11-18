"""dv-3e28: a per-task decision naming a card goes to the card, not the prompt.

Pinned interface (the build must provide it):
- ``DecisionRefChecker(agent_repos, boards_path=Path)``: ``boards_path`` is a
  boards.json; card lookup order = board-qualified ref, the speaker's own
  boards, then every other board (``all_tasks_dirs``, incl. archive).
- ``MemoryWriter(repo, decision_refs=checker)``: an ``add`` op whose
  ``source_ref`` is an existing card id appends one comment (full text) to that
  card and stores the row with ``kind='card_decision'``,
  ``source_ref='<board>:<id>'`` (off-prompt, still searchable).
- ``ConsolidationService(..., decision_refs=checker)`` routes flagged rows of
  existing cards the same way.
- ``scripts/card_decision_replay.py --db DB --boards FILE --out FILE [--days 7]``
  read-only replay report.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.evaluator.decision_refs import DecisionRefChecker
from src.evaluator.prompts import build_evaluator_prompt
from src.evaluator.writer import MemoryWriter
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository

ROOT = Path(__file__).resolve().parents[3]
DECISION = "gb-1ab3: the feed header shows the city only, because the owner found the long form cluttered."
RULE = (
    "Owner rule: never ship a feature without a test first, e.g. as on gb-1ab3 "
    "where the missing test cost a day."
)


@pytest.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield MemoryRepository(db)
    await db.close()


def _card(d: Path, cid: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{cid}.md"
    p.write_text(
        f"---\nid: {cid}\ntitle: t\nstatus: in_progress\n---\n\n## Description\n\nx\n"
    )
    return p


@pytest.fixture
def world(tmp_path):
    """devvy (devops), gymbro, pl boards, each with its own cards dir."""
    cards = {
        n: tmp_path / "boards" / n / ".deviz" / "tasks"
        for n in ("devvy", "gymbro", "pl", "budgetbot")
    }
    repos = {n: tmp_path / "repos" / n for n in cards}
    for r in repos.values():
        r.mkdir(parents=True)
    boards = tmp_path / "boards.json"
    boards.write_text(
        json.dumps(
            {
                "pump2_home": {"cards_dir": str(cards["devvy"])},
                "boards": [
                    {
                        "name": n,
                        "repo": str(repos[n]),
                        "profile": p,
                        "cards_dir": str(cards[n]),
                    }
                    for n, p in (
                        ("gymbro", "gymbro"),
                        ("pl", "pl"),
                        ("budgetbot", "budgetbot"),
                    )
                ],
            }
        )
    )
    return {"cards": cards, "repos": repos, "boards": boards}


def _checker(world) -> DecisionRefChecker:
    return DecisionRefChecker(
        {
            "devops": (world["repos"]["devvy"],),
            "gymbro": (world["repos"]["gymbro"],),
            "pl": (world["repos"]["pl"],),
        },
        boards_path=world["boards"],
    )


def _op(content, ref, kind="conversation_result", tier="general", supersedes=None):
    return {
        "op": "add",
        "kind": kind,
        "tier": tier,
        "content": content,
        "label": None,
        "source_ref": ref,
        "supersedes_id": supersedes,
        "scope": "private",
    }


# --- C1 ----------------------------------------------------------------


async def test_C1_card_decision_goes_to_card_and_off_prompt(repo, world):
    card = _card(world["cards"]["gymbro"], "gb-1ab3")
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    res = await writer.apply_ops("gymbro", [_op(DECISION, "gb-1ab3")])
    assert res.added == 1

    text = card.read_text()
    assert text.count("city only") == 1
    assert "## Comments" in text

    snap = await repo.facts_snapshot("gymbro")
    assert snap.general == [] and snap.pinned == [] and snap.pointers == []
    assert await repo.get_pointers("gymbro") == []
    assert await repo.search_general("gymbro", "city") == []
    assert not any(
        f.content == DECISION
        for f in await repo.search_general("gymbro", "feed", limit=50)
    )

    found = await repo.search("gymbro", "city")
    assert any("city only" in f.content for f in found)  # recall path
    assert not any("missing:" in (f.source_ref or "") for f in found)


# --- C2 ----------------------------------------------------------------


async def test_C2_cross_board_ref_lands_on_owning_board_unflagged(repo, world):
    gb = _card(world["cards"]["gymbro"], "gb-77aa")
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    res = await writer.apply_ops(
        "devops", [_op("gb-77aa: quiet mode starts at 22:00.", "gb-77aa")]
    )
    assert res.added == 1
    assert "quiet mode starts at 22:00" in gb.read_text()
    facts = await repo.get_facts("devops")
    assert len(facts) == 1
    assert "missing:" not in (facts[0].source_ref or "")
    assert (await repo.facts_snapshot("devops")).general == []


async def test_C2_board_qualified_ref_goes_to_that_board(repo, world):
    pl = _card(world["cards"]["pl"], "dv-1a2b")
    other = _card(world["cards"]["devvy"], "dv-1a2b")
    before = other.read_text()
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    await writer.apply_ops(
        "devops", [_op("dv-1a2b: use weekly digests.", "pl:dv-1a2b")]
    )
    assert "use weekly digests" in pl.read_text()
    assert other.read_text() == before


async def test_C2_ambiguous_id_on_two_foreign_boards_is_written_to_neither(repo, world):
    a = _card(world["cards"]["pl"], "dv-5e5e")
    b = _card(world["cards"]["budgetbot"], "dv-5e5e")
    ta, tb = a.read_text(), b.read_text()
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    res = await writer.apply_ops("gymbro", [_op("dv-5e5e: cap is 50.", "dv-5e5e")])
    assert res.added == 1
    assert a.read_text() == ta and b.read_text() == tb
    facts = await repo.get_facts("gymbro")
    assert facts[0].kind == "user_fact"
    assert (facts[0].source_ref or "").startswith("missing:")


# --- C3 ----------------------------------------------------------------


async def test_C3_rule_citing_card_as_example_stays_injected(repo, world):
    card = _card(world["cards"]["gymbro"], "gb-1ab3")
    before = card.read_text()
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    res = await writer.apply_ops(
        "gymbro", [_op(RULE, None, kind="user_fact", tier="pinned")]
    )
    assert res.added == 1
    snap = await repo.facts_snapshot("gymbro")
    assert [f.content for f in snap.pinned] == [RULE]
    assert card.read_text() == before


def test_C3_repo_agent_prompt_states_the_distinction():
    prompt = build_evaluator_prompt(
        "User: hi",
        "(none)",
        agent_id="devops",
        repo_agents=frozenset({"devops", "gymbro"}),
    )
    low = prompt.lower()
    assert "card" in low and "comment" in low
    assert "example" in low  # a rule citing a card id as an example stays a fact


# --- C5 ----------------------------------------------------------------


async def test_C5_same_decision_twice_adds_one_comment(repo, world):
    card = _card(world["cards"]["gymbro"], "gb-1ab3")
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    await writer.apply_ops("gymbro", [_op(DECISION, "gb-1ab3")])
    res = await writer.apply_ops("gymbro", [_op(DECISION, "gb-1ab3")])
    assert res.added == 0
    assert card.read_text().count("city only") == 1
    assert len(await repo.get_facts("gymbro")) == 1


async def test_C5_missing_card_keeps_flagged_full_fact(repo, world):
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    res = await writer.apply_ops("gymbro", [_op(DECISION, "gb-0000")])
    assert res.added == 1
    facts = await repo.get_facts("gymbro")
    assert facts[0].kind == "user_fact"
    assert facts[0].source_ref == "missing:gb-0000"
    assert [f.content for f in (await repo.facts_snapshot("gymbro")).general] == [
        DECISION
    ]


async def test_C5_unwritable_card_keeps_flagged_full_fact(repo, world, monkeypatch):
    _card(world["cards"]["gymbro"], "gb-1ab3")
    from src.storage import deviz_board

    async def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(deviz_board.DevizBoard, "append_comment", boom)
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    res = await writer.apply_ops("gymbro", [_op(DECISION, "gb-1ab3")])
    assert res.added == 1
    facts = await repo.get_facts("gymbro")
    assert facts[0].kind == "user_fact"
    assert (facts[0].source_ref or "").startswith("missing:")


async def test_C5_card_decision_still_supersedes_old_fact(repo, world):
    _card(world["cards"]["gymbro"], "gb-1ab3")
    old = await repo.add_fact(
        "gymbro", "general", "gb-1ab3: header shows city and country"
    )
    writer = MemoryWriter(repo, decision_refs=_checker(world))
    op = _op(DECISION, "gb-1ab3", supersedes=old)
    op["op"] = "supersede"
    await writer.apply_ops("gymbro", [op])
    closed = await repo.get_facts("gymbro", status="superseded")
    assert [f.id for f in closed] == [old]


# --- C6 ----------------------------------------------------------------


async def test_C6_sweep_routes_flagged_card_refs_to_the_card(world, tmp_path):
    from src.consolidation.service import ConsolidationService

    db = DatabaseManager(str(tmp_path / "c.db"))
    await db.initialize()
    mem = MemoryRepository(db)
    try:
        card = _card(world["cards"]["gymbro"], "gb-77aa")
        fid = await mem.add_fact(
            "devops",
            "general",
            "gb-77aa: quiet mode starts at 22:00.",
            kind="user_fact",
            source_ref="missing:gb-77aa",
        )
        svc = ConsolidationService(
            mem, db, report_dir=tmp_path / "rep", decision_refs=_checker(world)
        )
        await svc._promote_decisions("devops")
        assert "quiet mode starts at 22:00" in card.read_text()
        assert await mem.flagged_decisions("devops") == []
        assert (await mem.facts_snapshot("devops")).general == []
        assert any(f.id == fid for f in await mem.get_facts("devops"))
    finally:
        await db.close()


# --- C4 ----------------------------------------------------------------


async def test_C4_replay_report(world, tmp_path):
    db_path = tmp_path / "r.db"
    db = DatabaseManager(str(db_path))
    await db.initialize()
    mem = MemoryRepository(db)
    _card(world["cards"]["gymbro"], "gb-1ab3")
    _card(world["cards"]["devvy"], "dv-aaaa")
    await mem.add_fact(
        "gymbro", "general", DECISION, kind="conversation_result", source_ref="gb-1ab3"
    )
    await mem.add_fact(
        "devops",
        "general",
        "gb-1ab3: devops note on gymbro card",
        kind="conversation_result",
        source_ref="gb-1ab3",
    )
    await mem.add_fact(
        "devops",
        "general",
        "dv-aaaa: ship it",
        kind="user_fact",
        source_ref="missing:dv-aaaa",
    )
    await mem.add_fact(
        "devops",
        "general",
        "gb-dead: gone card",
        kind="user_fact",
        source_ref="missing:gb-dead",
    )
    await mem.add_fact("devops", "pinned", RULE, kind="user_fact")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE memory_facts SET recorded_at=? WHERE content LIKE 'gb-dead%'",
            (old,),
        )
        await conn.commit()
    await db.close()

    cards_before = {
        p: p.read_text()
        for d in world["cards"].values()
        if d.exists()
        for p in d.glob("*.md")
    }
    db_before = db_path.read_bytes()
    out = tmp_path / "out" / "replay.md"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "card_decision_replay.py"),
            "--db",
            str(db_path),
            "--boards",
            str(world["boards"]),
            "--out",
            str(out),
            "--days",
            "7",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    text = out.read_text()
    assert "devops" in text and "gymbro" in text
    assert "gb-dead" not in text  # older than 7 days: not replayed
    assert "flagged" in text.lower()
    assert {p: p.read_text() for p in cards_before} == cards_before  # no card writes
    assert db_path.read_bytes() == db_before  # read-only
