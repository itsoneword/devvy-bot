"""dv-65dd: the sweep also closes general user facts whose named cards are all
``done`` and whose card text / board DECISIONS.md holds the fact's substance.

Pinned interfaces for the build:
- ``ConsolidationService(..., card_boards={board: (cards_dir, repo_dir)})``:
  cards at ``cards_dir/<id>.md`` (front matter ``status: done``), decisions at
  ``repo_dir/docs/DECISIONS.md``. Default (None) = read boards.json.
- The card substance check is a second ``complete_text`` call per scope (its
  prompt does not start with the stale-pair prompt's first line); the reply is
  a JSON array ``[{"id": <fact id>, "reason": "..."}]``.
- ``DecisionRefChecker(agent_repos, card_dirs={board: cards_dir})`` resolves a
  card ref on any board.
"""

import json
from datetime import UTC, datetime  # noqa: F401
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from src.consolidation.service import ConsolidationService
from src.evaluator.decision_refs import DecisionRefChecker
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository

PATCH = "src.consolidation.service.complete_text"
PAIR_HEAD = "Below are the active memory facts of one agent"

F_DONE = "dv-a1b2: the digest job runs from digest.py and reads DIGEST_HOUR=7."
CARD_DONE = (
    "---\nid: dv-a1b2\nstatus: done\n---\nShipped: digest.py reads DIGEST_HOUR.\n"
)


def _card(cid, status, body="digest.py reads DIGEST_HOUR."):
    return f"---\nid: {cid}\nstatus: {status}\n---\n{body}\n"


def _fake(card_reply, pair_reply="[]"):
    """complete_text fake: stale-pair call vs card call by prompt head."""
    calls = []

    async def fake(prompt, **kw):
        calls.append(prompt)
        if prompt.lstrip().startswith(PAIR_HEAD):
            return pair_reply
        return card_reply() if callable(card_reply) else card_reply

    fake.calls = calls
    return fake


def _ids(*ids):
    return json.dumps([{"id": i, "reason": "card holds it"} for i in ids])


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
def boards(tmp_path):
    out = {}
    for name in ("devvy", "gymbro"):
        cards = tmp_path / name / "cards"
        cards.mkdir(parents=True)
        (tmp_path / name / "docs").mkdir()
        (tmp_path / name / "docs" / "DECISIONS.md").write_text("# Decisions\n")
        out[name] = (cards, tmp_path / name)
    return out


def _svc(repo, db, tmp_path, boards, **kw):
    return ConsolidationService(
        memory_repo=repo,
        db=db,
        report_dir=tmp_path / "consolidation",
        card_boards=boards,
        **kw,
    )


async def _fact(repo, text, scope="devops", **kw):
    return await repo.add_fact(scope, "general", text, source="evaluator", **kw)


async def _active(repo, scope="devops"):
    return {f.id for f in await repo.get_facts(scope)}


async def test_C1_only_done_card_facts_are_closed(repo, db, tmp_path, boards):
    cards = boards["devvy"][0]
    (cards / "dv-a1b2.md").write_text(CARD_DONE)
    (cards / "dv-c3d4.md").write_text(_card("dv-c3d4", "todo"))
    (cards / "dv-e5f6.md").write_text(_card("dv-e5f6", "in_progress"))
    ok = await _fact(repo, F_DONE)
    todo = await _fact(repo, "dv-c3d4: digest.py reads DIGEST_HOUR=7.")
    prog = await _fact(repo, "dv-e5f6: digest.py reads DIGEST_HOUR=7.")
    missing = await _fact(repo, "dv-9999: digest.py reads DIGEST_HOUR=7.")
    pinned = await _fact(repo, "dv-a1b2: pinned digest.py DIGEST_HOUR rule.")
    core = await repo.add_fact(
        "devops", "core", "dv-a1b2: core digest.py DIGEST_HOUR.", source="evaluator"
    )
    conflict = await _fact(
        repo, "dv-a1b2: conflict digest.py DIGEST_HOUR.", conflict_group="g1"
    )
    await repo.add_fact(  # a second done-card fact via source_ref only
        "devops", "general", "unrelated", source="evaluator"
    )
    svc = _svc(repo, db, tmp_path, boards)
    reply = _ids(ok, todo, prog, missing, pinned, core, conflict)
    with capture_logs() as logs, patch(PATCH, _fake(reply)):
        await svc.sweep("devops")
    act = await _active(repo)
    assert ok not in act
    assert {todo, prog, missing, core, conflict} <= act
    rejected = [e for e in logs if e.get("event") == "memory_sweep_rejected"]
    assert len(rejected) >= 5
    f = await repo.get_fact(ok, "devops")
    assert f.invalidated_by == "sweep"


async def test_C1_every_named_card_must_be_done(repo, db, tmp_path, boards):
    cards = boards["devvy"][0]
    (cards / "dv-a1b2.md").write_text(CARD_DONE)
    (cards / "dv-c3d4.md").write_text(_card("dv-c3d4", "todo"))
    fid = await _fact(repo, "dv-a1b2 and dv-c3d4: digest.py reads DIGEST_HOUR.")
    svc = _svc(repo, db, tmp_path, boards)
    with patch(PATCH, _fake(_ids(fid))):
        await svc.sweep("devops")
    assert fid in await _active(repo)


async def test_C1_no_card_call_without_candidates(repo, db, tmp_path, boards):
    await _fact(repo, "A plain owner preference about short answers.")
    await _fact(repo, "Another plain owner preference about tone.")
    svc = _svc(repo, db, tmp_path, boards)
    fake = _fake("[]")
    with patch(PATCH, fake):
        await svc.sweep("devops")
    assert len(fake.calls) == 1  # only the stale-pair call


async def test_C2_closed_only_when_model_lists_and_card_holds_it(
    repo, db, tmp_path, boards
):
    cards = boards["devvy"][0]
    (cards / "dv-a1b2.md").write_text(CARD_DONE)
    (cards / "dv-c3d4.md").write_text(_card("dv-c3d4", "done", "nothing relevant"))
    held = await _fact(repo, F_DONE)
    not_listed = await _fact(repo, "dv-a1b2: digest.py reads DIGEST_HOUR too.")
    not_held = await _fact(repo, "dv-c3d4: digest.py reads DIGEST_HOUR=7.")
    svc = _svc(repo, db, tmp_path, boards)
    with capture_logs() as logs, patch(PATCH, _fake(_ids(held, not_held))):
        await svc.sweep("devops")
    act = await _active(repo)
    assert held not in act
    assert {not_listed, not_held} <= act
    assert any(e.get("event") == "memory_sweep_rejected" for e in logs)


async def test_C2_decisions_md_paragraph_counts_as_evidence(repo, db, tmp_path, boards):
    cards, repo_dir = boards["devvy"]
    (cards / "dv-a1b2.md").write_text(_card("dv-a1b2", "done", "short card"))
    (repo_dir / "docs" / "DECISIONS.md").write_text(
        "# D\n\n## dv-a1b2 digest\ndigest.py reads DIGEST_HOUR.\n"
    )
    fid = await _fact(repo, F_DONE)
    svc = _svc(repo, db, tmp_path, boards)
    with patch(PATCH, _fake(_ids(fid))):
        await svc.sweep("devops")
    assert fid not in await _active(repo)


@pytest.mark.parametrize("reply", ["not json", '{"id": 1}', ""])
async def test_C2_bad_reply_closes_nothing(repo, db, tmp_path, boards, reply):
    (boards["devvy"][0] / "dv-a1b2.md").write_text(CARD_DONE)
    fid = await _fact(repo, F_DONE)
    svc = _svc(repo, db, tmp_path, boards)
    with patch(PATCH, _fake(reply)):
        await svc.sweep("devops")
    assert fid in await _active(repo)


async def test_C2_call_error_closes_nothing(repo, db, tmp_path, boards):
    (boards["devvy"][0] / "dv-a1b2.md").write_text(CARD_DONE)
    fid = await _fact(repo, F_DONE)
    svc = _svc(repo, db, tmp_path, boards)

    async def fake(prompt, **kw):
        if prompt.lstrip().startswith(PAIR_HEAD):
            return "[]"
        raise RuntimeError("boom")

    with patch(PATCH, fake):
        await svc.sweep("devops")
    assert fid in await _active(repo)


async def test_C2_card_closures_share_the_fuse(repo, db, tmp_path, boards):
    cards = boards["devvy"][0]
    old = await _fact(repo, "The jobs limit is 3.")
    await _fact(repo, "The jobs limit was raised to 5.")
    cand = []
    for i in range(6):
        cid = f"dv-a{i}b{i}"
        (cards / f"{cid}.md").write_text(_card(cid, "done"))
        cand.append(await _fact(repo, f"{cid}: digest.py reads DIGEST_HOUR."))
    svc = _svc(repo, db, tmp_path, boards)
    pairs = json.dumps([{"old_id": old, "newer_id": old + 1, "reason": "raised"}])
    before = await _active(repo)
    assert len(before) == 8  # general 8 -> fuse max(5, 2) = 5
    with patch(PATCH, _fake(_ids(*cand), pair_reply=pairs)):
        await svc.sweep("devops")
    assert len(before) - len(await _active(repo)) == 5


async def test_C4_report_undo_card_id_and_reopen(repo, db, tmp_path, boards):
    (boards["gymbro"][0] / "gb-1acb.md").write_text(_card("gb-1acb", "done"))
    fid = await _fact(repo, "gb-1acb: digest.py reads DIGEST_HOUR=7.", scope="gymbro")
    svc = _svc(repo, db, tmp_path, boards)
    with capture_logs() as logs, patch(PATCH, _fake(_ids(fid))):
        result = await svc.sweep("gymbro")
    assert result.failed is False
    assert fid not in await _active(repo, "gymbro")
    closed = [e for e in logs if e.get("event") == "memory_sweep_closed"]
    assert any(e.get("old_id", e.get("id")) == fid for e in closed)
    report = svc._report_path().read_text()
    assert f"- [ ] UNDO scope=gymbro id={fid}" in report
    assert "gymbro:gb-1acb" in report
    row = await repo.get_fact(fid, "gymbro")
    assert row.invalidated_by == "sweep" and row.superseded_by is None
    assert await repo.reopen_fact(fid, "gymbro") is True
    assert fid in await _active(repo, "gymbro")


async def test_C4_runs_in_all_scopes_run(repo, db, tmp_path, boards):
    cards = boards["devvy"][0]
    (cards / "dv-a1b2.md").write_text(CARD_DONE)
    a = await _fact(repo, F_DONE, scope="devops")
    b = await _fact(repo, F_DONE, scope="coach")
    svc = _svc(repo, db, tmp_path, boards)
    with patch(PATCH, _fake(_ids(a, b))):
        await svc.run()
    assert a not in await _active(repo, "devops")
    assert b not in await _active(repo, "coach")
    assert "UNDO scope=coach" in svc._report_path().read_text()


async def test_C4_runs_after_rotation(repo, db, tmp_path, boards):
    (boards["devvy"][0] / "dv-a1b2.md").write_text(CARD_DONE)
    fid = await _fact(repo, F_DONE)
    svc = _svc(repo, db, tmp_path, boards)
    with patch(PATCH, _fake(_ids(fid))):
        svc.after_rotation("devops", None)
        assert await svc.wait_rotation("devops", timeout=5)
    assert fid not in await _active(repo)


async def test_C5_flag_for_card_on_another_board_is_promoted(
    repo, db, tmp_path, boards
):
    own = tmp_path / "devops_repo"
    own.mkdir()
    (boards["gymbro"][0] / "gb-1acb.md").write_text(_card("gb-1acb", "todo"))
    checker = DecisionRefChecker(
        {"devops": (own,)}, card_dirs={n: c for n, (c, _) in boards.items()}
    )
    assert checker.verify("devops", "gb-1acb", "gb-1acb: anything") is True
    assert checker.verify("devops", "gb-ffff", "gb-ffff: anything") is False
    fid = await _fact(
        repo, "gb-1acb: the feed shows city only.", source_ref="missing:gb-1acb"
    )
    svc = _svc(repo, db, tmp_path, boards, decision_refs=checker)
    with patch(PATCH, _fake("[]")):
        await svc.sweep("devops")
    row = await repo.get_fact(fid, "devops")
    # dv-3e28: a card decision found on another board goes to that card as a
    # comment and leaves the prompt (kind card_decision, board-qualified ref)
    assert row.kind == "card_decision"
    assert row.source_ref == "gymbro:gb-1acb"
    card = (boards["gymbro"][0] / "gb-1acb.md").read_text()
    assert "the feed shows city only." in card


async def test_C6_owner_message_only_when_something_closed(repo, db, tmp_path, boards):
    (boards["devvy"][0] / "dv-a1b2.md").write_text(CARD_DONE)
    await _fact(repo, "A plain owner preference about short answers.")
    notify = AsyncMock()
    svc = _svc(repo, db, tmp_path, boards, notify_callback=notify)
    with patch(PATCH, _fake("[]")):
        await svc.run()
    notify.assert_not_called()
    fid = await _fact(repo, F_DONE)
    with patch(PATCH, _fake(_ids(fid))):
        await svc.run()
    notify.assert_called_once()
