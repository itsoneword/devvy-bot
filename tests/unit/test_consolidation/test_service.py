"""Tests for the stale-fact sweep (dv-cf07 phase 2; replaces dv-4c6f's
report-only similarity pass).

The sweep makes one ``complete_text(purpose="consolidation")`` call per scope
that returns ``[{old_id, newer_id, reason}]``. Every proposal is validated
deterministically before anything is closed:

- only ``tier='general'`` rows are closable (pinned / core never), and never a
  row with a ``conflict_group`` (safety keep-both, review R1);
- both ids must be active rows of the scope being swept;
- the newer row must really be newer (``recorded_at`` later, or equal with a
  higher id);
- at most ``max(5, 25 % of the scope's active general rows)`` closures per run;
- bad model output closes nothing.

Closure is soft: ``superseded_by`` + ``invalidated_by='sweep'``, one
``memory_sweep_closed`` log line per row, and a ``- [ ] UNDO scope=… id=…``
line in the report that ``scripts/consolidation_apply.py`` turns back into an
active row.

Interfaces fixed here for the build:
- ``MemoryRepository.close_superseded(old_id, by_id, agent_id, source="sweep")``
  -> bool
- ``MemoryRepository.reopen_fact(fact_id, agent_id)`` -> bool (only rows closed
  by ``sweep`` / ``consolidation``)
- ``ConsolidationService.sweep(scope)`` -> SweepResult with ``.closed`` = list
  of ``(old_id, newer_id)``
- ``ConsolidationService.run(scope=None)`` -> report Path (weekly: all scopes)
"""

import asyncio
import importlib.util
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from src.consolidation.service import ConsolidationService
from src.storage.database import DatabaseManager
from src.storage.deviz_board import DevizBoard
from src.storage.memory_repository import MemoryRepository

REPO_ROOT = Path(__file__).resolve().parents[3]
PATCH = "src.consolidation.service.complete_text"


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
def board(tmp_path):
    return DevizBoard(tmp_path / "tasks")


@pytest.fixture
def report_dir(tmp_path):
    return tmp_path / "consolidation"


@pytest.fixture
def service(repo, db, report_dir, board):
    return ConsolidationService(
        memory_repo=repo,
        db=db,
        report_dir=report_dir,
        board=board,
    )


# --- helpers ----------------------------------------------------------------


def _ts(day: int, hour: int = 12) -> str:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC).isoformat()


async def _put(
    db,
    fid: int,
    agent_id: str,
    tier: str,
    content: str,
    recorded_at: str,
    kind: str = "user_fact",
    source: str = "agent_tool",
    conflict_group: Optional[str] = None,
) -> int:
    """Insert a row with an explicit id/date (the replay of real ids)."""
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO memory_facts (id, agent_id, tier, content, status, "
            "source, kind, valid_from, recorded_at, conflict_group) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                fid,
                agent_id,
                tier,
                content,
                source,
                kind,
                recorded_at,
                recorded_at,
                conflict_group,
            ),
        )
        await conn.commit()
    return fid


def _proposals(pairs: Iterable[Tuple[int, int]]) -> str:
    return json.dumps(
        [
            {"old_id": old, "newer_id": new, "reason": "newer value, same subject"}
            for old, new in pairs
        ]
    )


def _closed_pairs(result) -> set:
    return {(int(old), int(new)) for old, new in result.closed}


async def _active_ids(repo, scope: str) -> set:
    return {f.id for f in await repo.get_facts(scope)}


def _report_text(report_dir: Path) -> str:
    files = sorted(report_dir.rglob("*.md")) if report_dir.exists() else []
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def _load_apply_script():
    path = REPO_ROOT / "scripts" / "consolidation_apply.py"
    spec = importlib.util.spec_from_file_location("consolidation_apply_t", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _devops_scope_today(db) -> None:
    """A copy of the devops scope on 26.10.26, rows closed that day reopened.

    Synthetic paraphrases of the real rows; ids and order of the two named
    stale pairs are the real ones (1295 -> 1298, 1261 -> 1315).
    """
    # Pinned owner rules (never closable).
    await _put(
        db,
        1001,
        "devops",
        "pinned",
        "Owner rule: never restart the bot " "from a turn; use restart_bot.",
        _ts(1),
        source="manual",
    )
    await _put(
        db,
        1002,
        "devops",
        "pinned",
        "Owner rule: pump concurrency is " "the owner's call, ask before changing it.",
        _ts(2),
        source="manual",
    )
    # Core identity (never closable).
    await _put(
        db,
        1003,
        "devops",
        "core",
        "Owner writes in Russian, wants " "replies in Russian.",
        _ts(3),
        source="manual",
    )
    # General facts, including the two stale status pairs.
    await _put(
        db, 1261, "devops", "general", "The public repo is at version 1.3.", _ts(20)
    )
    await _put(
        db,
        1270,
        "devops",
        "general",
        "Owner prefers short Telegram " "replies with the result first.",
        _ts(21),
    )
    await _put(
        db,
        1295,
        "devops",
        "general",
        "Pump concurrency is 3 parallel " "jobs.",
        _ts(24, 9),
    )
    await _put(
        db,
        1298,
        "devops",
        "general",
        "Pump concurrency raised to 5 " "parallel jobs.",
        _ts(24, 15),
    )
    await _put(
        db,
        1300,
        "devops",
        "general",
        "Finding: the remember tool " "bypasses the reconciler.",
        _ts(25),
    )
    await _put(
        db, 1315, "devops", "general", "The public repo is at version 1.5.", _ts(26, 10)
    )
    # A few pointers.
    for i, fid in enumerate((1280, 1290, 1310)):
        await _put(
            db,
            fid,
            "devops",
            "general",
            f"Result {i}: investigated window roll timing.",
            _ts(22 + i),
            kind="conversation_result",
            source="evaluator",
        )


# --- Scenario 1: replay on today's devops scope ---------------------------


async def test_replay_devops_scope_closes_known_stale_pairs(service, repo, db):
    await _devops_scope_today(db)
    model = AsyncMock(return_value=_proposals([(1295, 1298), (1261, 1315)]))

    with patch(PATCH, model):
        result = await service.sweep("devops")

    assert _closed_pairs(result) == {(1295, 1298), (1261, 1315)}
    old_pump = await repo.get_fact(1295, "devops")
    old_repo = await repo.get_fact(1261, "devops")
    assert old_pump.invalid_at is not None and old_pump.superseded_by == 1298
    assert old_repo.invalid_at is not None and old_repo.superseded_by == 1315

    # One model call for the scope, on the consolidation route, showing the
    # facts with their ids.
    assert model.await_count == 1
    prompt = model.await_args.args[0] if model.await_args.args else ""
    prompt = prompt or model.await_args.kwargs.get("prompt", "")
    assert model.await_args.kwargs.get("purpose") == "consolidation"
    for fid in (1261, 1295, 1298, 1315):
        assert str(fid) in prompt

    # Everything else untouched.
    active = await _active_ids(repo, "devops")
    assert {1001, 1002, 1003, 1270, 1298, 1300, 1315} <= active
    assert {1280, 1290, 1310} <= active


async def test_replay_snapshot_has_no_contradictory_status_pair(service, repo, db):
    await _devops_scope_today(db)
    with patch(PATCH, AsyncMock(return_value=_proposals([(1295, 1298), (1261, 1315)]))):
        await service.sweep("devops")

    snap = await repo.facts_snapshot("devops", general_cap=60, pointer_cap=60)
    ids = {f.id for f in snap.pinned} | {f.id for f in snap.general}
    assert 1298 in ids and 1315 in ids
    assert 1295 not in ids and 1261 not in ids
    texts = " ".join(f.content for f in snap.general)
    assert "version 1.3" not in texts
    assert "concurrency is 3" not in texts


# --- Scenario 2: never pinned / core / conflict / foreign ids -------------


async def test_sweep_never_closes_pinned_core_or_conflict_facts(service, repo, db):
    await _devops_scope_today(db)
    await _put(
        db,
        1320,
        "devops",
        "general",
        "Allergic to nothing.",
        _ts(26, 11),
        conflict_group="1320",
    )
    await _put(
        db,
        1321,
        "devops",
        "general",
        "Allergic to nuts.",
        _ts(26, 12),
        conflict_group="1320",
    )
    proposals = [
        (1001, 1298),  # pinned owner rule
        (1002, 1298),  # pinned owner rule
        (1003, 1270),  # core identity
        (1320, 1321),  # flagged safety conflict
    ]
    with patch(PATCH, AsyncMock(return_value=_proposals(proposals))):
        result = await service.sweep("devops")

    assert _closed_pairs(result) == set()
    active = await _active_ids(repo, "devops")
    assert {1001, 1002, 1003, 1320, 1321} <= active


async def test_sweep_ignores_foreign_unknown_and_inverted_ids(service, repo, db):
    await _devops_scope_today(db)
    await _put(db, 2001, "chef", "general", "Likes peaches.", _ts(20))
    await _put(db, 2002, "chef", "general", "Likes bananas.", _ts(26, 20))
    proposals = [
        (2001, 2002),  # both rows of another scope
        (1270, 2002),  # newer row of another scope
        (2001, 1315),  # old row of another scope
        (1270, 99999),  # newer does not exist
        (1315, 1261),  # "old" is newer than its "newer"
    ]
    with patch(PATCH, AsyncMock(return_value=_proposals(proposals))):
        result = await service.sweep("devops")

    assert _closed_pairs(result) == set()
    assert {1261, 1270, 1315} <= await _active_ids(repo, "devops")
    assert await _active_ids(repo, "chef") == {2001, 2002}


async def test_sweep_does_not_close_in_favour_of_an_inactive_row(service, repo, db):
    await _devops_scope_today(db)
    await repo.invalidate_fact(1315, "devops", source="user_request")
    with patch(PATCH, AsyncMock(return_value=_proposals([(1261, 1315)]))):
        result = await service.sweep("devops")
    assert _closed_pairs(result) == set()
    assert 1261 in await _active_ids(repo, "devops")


@pytest.mark.parametrize(
    "model",
    [
        AsyncMock(return_value="not json at all"),
        AsyncMock(return_value='{"old_id": 1295}'),
        AsyncMock(return_value='[{"old_id": "1295", "newer_id": null}]'),
        AsyncMock(return_value='[{"old_id": 1295, "newer_id": 1298}'),  # cut off
        AsyncMock(side_effect=RuntimeError("CLI down")),
    ],
    ids=["prose", "object", "wrong-types", "truncated", "model-error"],
)
async def test_bad_model_output_closes_nothing(service, repo, db, model):
    await _devops_scope_today(db)
    before = await _active_ids(repo, "devops")
    with patch(PATCH, model):
        result = await service.sweep("devops")
    assert _closed_pairs(result) == set()
    assert await _active_ids(repo, "devops") == before


async def test_fuse_caps_closures_per_run(service, repo, db):
    """40 active general rows -> max(5, 25 % of 40) = 10 closures at most."""
    ids = []
    for i in range(40):
        ids.append(
            await _put(
                db,
                3000 + i,
                "devops",
                "general",
                f"setting {i} value",
                _ts(1 + i % 25, i % 24),
            )
        )
    # Order by recorded_at so every proposal is old -> genuinely newer.
    rows = sorted(await repo.get_facts("devops"), key=lambda f: (f.recorded_at, f.id))
    pairs = [(rows[2 * k].id, rows[2 * k + 1].id) for k in range(20)]
    with patch(PATCH, AsyncMock(return_value=_proposals(pairs))):
        result = await service.sweep("devops")

    assert len(result.closed) == 10
    assert len(await _active_ids(repo, "devops")) == 30


async def test_fuse_floor_allows_five_in_a_small_scope(service, repo, db):
    """8 general rows: 25 % is 2, the floor of 5 still allows 4 proposals."""
    for i in range(8):
        await _put(db, 4000 + i, "chef", "general", f"chef note {i}", _ts(1 + i))
    pairs = [(4000, 4001), (4002, 4003), (4004, 4005), (4006, 4007)]
    with patch(PATCH, AsyncMock(return_value=_proposals(pairs))):
        result = await service.sweep("chef")
    assert _closed_pairs(result) == set(pairs)


# --- Scenario 3: closure is soft, logged, UNDO reopens -------------------


async def test_closure_is_soft_logged_and_listed_with_undo(
    service, repo, db, report_dir
):
    await _devops_scope_today(db)
    with capture_logs() as logs:
        with patch(PATCH, AsyncMock(return_value=_proposals([(1295, 1298)]))):
            await service.sweep("devops")

    row = await repo.get_fact(1295, "devops")
    assert row is not None  # never deleted
    assert row.content == "Pump concurrency is 3 parallel jobs."  # never rewritten
    assert row.superseded_by == 1298
    assert row.invalid_at is not None
    assert row.invalidated_by == "sweep"

    closed = [e for e in logs if e.get("event") == "memory_sweep_closed"]
    assert len(closed) == 1
    assert 1295 in closed[0].values() and 1298 in closed[0].values()

    text = _report_text(report_dir)
    assert re.search(r"- \[ \] UNDO scope=devops id=1295\b", text), text


async def test_undo_line_in_report_reopens_the_fact(service, repo, db, report_dir):
    await _devops_scope_today(db)
    with patch(PATCH, AsyncMock(return_value=_proposals([(1295, 1298)]))):
        await service.sweep("devops")

    report = next(
        p
        for p in report_dir.rglob("*.md")
        if "UNDO scope=devops id=1295" in p.read_text(encoding="utf-8")
    )
    report.write_text(
        report.read_text(encoding="utf-8").replace(
            "- [ ] UNDO scope=devops id=1295", "- [x] UNDO scope=devops id=1295"
        ),
        encoding="utf-8",
    )
    apply = _load_apply_script()
    conn = sqlite3.connect(str(Path(report_dir).parent / "mem.db"))
    try:
        apply.apply_report(conn, report)
    finally:
        conn.close()

    row = await repo.get_fact(1295, "devops")
    assert row.status == "active"
    assert row.invalid_at is None
    assert row.superseded_by is None
    assert row.invalidated_by is None
    assert 1295 in await _active_ids(repo, "devops")


async def test_close_superseded_and_reopen_fact_roundtrip(repo, db):
    await _devops_scope_today(db)
    assert await repo.close_superseded(1295, 1298, "devops", source="sweep") is True
    row = await repo.get_fact(1295, "devops")
    assert row.superseded_by == 1298 and row.invalidated_by == "sweep"
    # Already closed: a second close is a no-op.
    assert await repo.close_superseded(1295, 1298, "devops", source="sweep") is False
    # Wrong scope can neither close nor reopen.
    assert await repo.close_superseded(1261, 1315, "chef", source="sweep") is False
    assert await repo.reopen_fact(1295, "chef") is False

    assert await repo.reopen_fact(1295, "devops") is True
    row = await repo.get_fact(1295, "devops")
    assert row.status == "active" and row.invalid_at is None
    assert row.superseded_by is None and row.invalidated_by is None


async def test_reopen_refuses_rows_the_owner_forgot(repo, db):
    """UNDO is for sweep/consolidation closures only: a ``memory_forget``
    (invalidated_by='user_request') stays forgotten."""
    await _devops_scope_today(db)
    await repo.invalidate_fact(1270, "devops", source="user_request")
    assert await repo.reopen_fact(1270, "devops") is False
    assert 1270 not in await _active_ids(repo, "devops")


# --- Scenario 5: weekly job sweeps all scopes; similarity merges gone -----


def _scope_aware_model(by_scope_ids: dict):
    """Fake model: answer with the proposals of whichever scope's ids appear
    in the prompt."""

    async def _complete(prompt=None, *args, **kwargs):
        text = prompt if prompt is not None else kwargs.get("prompt", "")
        for marker, pairs in by_scope_ids.items():
            if str(marker) in text:
                return _proposals(pairs)
        return "[]"

    return AsyncMock(side_effect=_complete)


async def test_weekly_run_sweeps_every_scope(repo, db, report_dir):
    notify = AsyncMock()
    service = ConsolidationService(
        memory_repo=repo, db=db, notify_callback=notify, report_dir=report_dir
    )
    await _devops_scope_today(db)
    await _put(db, 5001, "gymbro", "general", "Bench press max is 80 kg.", _ts(10))
    await _put(db, 5002, "gymbro", "general", "Bench press max is 90 kg now.", _ts(25))
    model = _scope_aware_model({1295: [(1295, 1298)], 5001: [(5001, 5002)]})

    with patch(PATCH, model):
        path = await service.run()

    assert model.await_count == 2  # one call per active scope
    assert 1295 not in await _active_ids(repo, "devops")
    assert 5001 not in await _active_ids(repo, "gymbro")
    text = path.read_text(encoding="utf-8")
    assert "UNDO scope=devops id=1295" in text
    assert "UNDO scope=gymbro id=5001" in text
    notify.assert_awaited_once()


async def test_no_similarity_merges_or_expire_list_remain(service, repo, db):
    """Banana/peach, two mozzarellas and lexical near-duplicates are NOT
    staleness: with the model proposing nothing, nothing is closed and the
    report carries no MERGE or EXPIRE actions."""
    await repo.add_fact("coach", "general", "likes banana in the morning smoothie")
    await repo.add_fact("coach", "general", "likes peach in the morning smoothie")
    await repo.add_fact("coach", "general", "buys mozzarella di bufala")
    await repo.add_fact("coach", "general", "buys mozzarella fior di latte")
    await repo.add_fact("coach", "general", "likes spicy food")
    await repo.add_fact("coach", "general", "likes spicy foods!")
    dead = await repo.add_fact("coach", "general", "long dead fact")
    await repo.invalidate_fact(dead, "coach")
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE memory_facts SET invalid_at=? WHERE id=?", (old, dead)
        )
        await conn.commit()

    with patch(PATCH, AsyncMock(return_value="[]")):
        path = await service.run("coach")

    assert len(await repo.get_facts("coach")) == 6
    text = path.read_text(encoding="utf-8")
    assert "MERGE" not in text
    assert "EXPIRE" not in text


def test_similarity_machinery_is_retired():
    import src.consolidation.service as svc

    for name in ("CANDIDATE_RATIO", "SEMANTIC_PROMPT", "SEMANTIC_SYSTEM_PROMPT"):
        assert not hasattr(svc, name), name
    for name in ("_lexical_clusters", "_semantic_clusters", "_expiry_candidates"):
        assert not hasattr(ConsolidationService, name), name


# --- valve path (kept: debounce + one board task per scope) --------------


async def test_request_run_debounces_and_records_board_task(service, board, repo):
    await repo.add_fact("devops", "general", "some fact")
    with patch(PATCH, AsyncMock(return_value="[]")):
        service.request_run("devops", "snapshot over budget", snapshot_chars=13000)
        service.request_run("devops", "snapshot over budget", snapshot_chars=13000)
        # Let the fire-and-forget task complete.
        await asyncio.sleep(0.2)

    tasks = await board.list_tasks(status="open")
    # Debounce: two requests, one board task.
    assert len(tasks) == 1
    assert tasks[0].title == "Consolidation needed: devops"
    assert tasks[0].created_by == "consolidation"
    assert tasks[0].assignee == "user"
    assert tasks[0].status == "todo"


async def test_repeat_trip_updates_the_open_task_instead_of_filing_another(
    service, board, repo
):
    """dv-1327: the debounce is 24h and in-memory, so a scope that stays over
    budget trips daily. Eight duplicate rows on the old board came from this."""
    await repo.add_fact("devops", "general", "some fact")
    with patch(PATCH, AsyncMock(return_value="[]")):
        service.request_run("devops", "over budget", snapshot_chars=13000)
        await asyncio.sleep(0.2)
        # Simulate the next day: debounce expired, still over budget.
        service._last_run.clear()
        service.request_run("devops", "over budget", snapshot_chars=14000)
        await asyncio.sleep(0.2)

    tasks = await board.list_tasks(status="open")
    assert len(tasks) == 1
    body = (board.tasks_dir / f"{tasks[0].id}.md").read_text()
    assert "snapshot_chars: 14000" in body
    assert "still over budget" in body


async def test_valve_sweeps_only_that_scope(service, repo, db):
    await _devops_scope_today(db)
    await _put(db, 5001, "gymbro", "general", "Bench press max is 80 kg.", _ts(10))
    await _put(db, 5002, "gymbro", "general", "Bench press max is 90 kg now.", _ts(25))
    model = _scope_aware_model({1295: [(1295, 1298)], 5001: [(5001, 5002)]})
    with patch(PATCH, model):
        service.request_run("devops", "snapshot over budget", snapshot_chars=30000)
        await asyncio.sleep(0.3)

    assert 1295 not in await _active_ids(repo, "devops")
    assert 5001 in await _active_ids(repo, "gymbro")
