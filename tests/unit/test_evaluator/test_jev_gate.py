"""dv-7e11 phase 3: the Jev gate in front of the haiku reconciler."""

import json
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.evaluator.jev_gate import (
    NONE_OPTION,
    RELATED_Q,
    RELATION_Q,
    STATE_CHANGE_Q,
    JevGateConfig,
    build_request,
    judge,
    prefilter,
    run_gate,
)
from src.evaluator.service import EvaluatorService
from src.evaluator.writer import MemoryWriter
from src.model_router import (
    Answer,
    DecideRequest,
    DecideResult,
    ModelCallError,
    ModelRouter,
)
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository

CFG = JevGateConfig(
    min_conf_new=0.8, min_conf_duplicate=0.8, max_state_change=0.55, top_k=15
)


def _answers(
    relation: str,
    conf: Optional[float],
    related: str = NONE_OPTION,
    related_conf: Optional[float] = 0.9,
    state_change: float = 0.2,
) -> Dict[str, Answer]:
    return {
        RELATION_Q: Answer(kind="choice", value=relation, confidence=conf),
        RELATED_Q: Answer(kind="choice", value=related, confidence=related_conf),
        STATE_CHANGE_Q: Answer(kind="noul", value=state_change),
    }


def _fact(fid: int, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=fid, content=content, kind="user_fact", valid_from=None, recorded_at=None
    )


# ---- accept / escalate matrix ------------------------------------------


@pytest.mark.parametrize(
    "answers,verdict,existing,reason",
    [
        (_answers("new", 0.95), "new", None, "new"),
        (_answers("new", 0.8), "new", None, "new"),
        (_answers("new", 0.79), None, None, "low_confidence"),
        (_answers("duplicate", 0.9, "7", 0.85), "duplicate", 7, "duplicate"),
        (_answers("duplicate", 0.7, "7", 0.95), None, None, "low_confidence"),
        (_answers("duplicate", 0.9, "7", 0.5), None, None, "related_low_confidence"),
        (_answers("duplicate", 0.9, "7", None), None, None, "related_low_confidence"),
        (_answers("duplicate", 0.95, NONE_OPTION), None, None, "related_none"),
        (_answers("contradicts", 0.99, "7", 0.99), None, None, "contradicts"),
        (_answers("new", None), None, None, "no_confidence"),
    ],
)
def test_judge_matrix(answers, verdict, existing, reason):
    got, why, _ = judge(3, answers, CFG)
    assert why == reason
    if verdict is None:
        assert got is None
    else:
        assert got is not None
        assert (got.index, got.verdict, got.existing_id) == (3, verdict, existing)
        assert not got.safety and not got.revokes


@pytest.mark.parametrize("relation", ["new", "duplicate"])
def test_likely_state_change_escalates(relation):
    answers = _answers(relation, 0.97, "7", 0.97, state_change=0.6)
    verdict, reason, _ = judge(0, answers, CFG)
    assert verdict is None and reason == "state_change"
    assert judge(0, _answers(relation, 0.97, "7", 0.97, 0.55), CFG)[0] is not None


def test_default_config_never_accepts_duplicate():
    answers = _answers("duplicate", 1.0, "7", 1.0)
    assert judge(0, answers, JevGateConfig())[1] == "low_confidence"
    assert judge(0, _answers("new", 0.8), JevGateConfig())[0] is not None


def test_thresholds_are_separate_per_label():
    cfg = JevGateConfig(min_conf_new=0.95, min_conf_duplicate=0.6)
    assert judge(0, _answers("new", 0.9), cfg)[0] is None
    assert judge(0, _answers("duplicate", 0.65, "4", 0.65), cfg)[0] is not None


# ---- request shape -----------------------------------------------------


def test_prefilter_keeps_top_k_in_id_order():
    facts = [_fact(i, f"unrelated filler {i}") for i in range(1, 30)]
    facts.append(_fact(99, "allergic to peanuts and walnuts"))
    top = prefilter("peanuts allergy: walnuts too", facts, 5)
    assert len(top) == 5
    assert 99 in [f.id for f in top]
    assert [f.id for f in top] == sorted(f.id for f in top)


def test_request_puts_new_and_none_last_and_has_no_llm_fallback():
    req = build_request("cand", [_fact(1, "a"), _fact(2, "b")])
    assert req.purpose == "reconcile"
    assert req.fallback_model is None
    assert list(req.questions[RELATION_Q].criteria)[-1] == "new"
    assert list(req.questions[RELATED_Q].criteria) == ["1", "2", NONE_OPTION]
    assert STATE_CHANGE_Q in req.questions
    assert req.state["candidate"] == "cand"
    assert [f["id"] for f in req.state["saved_facts"]] == [1, 2]


# ---- run_gate ------------------------------------------------------------


def _router_with(decider: Any, monkeypatch) -> ModelRouter:
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    return ModelRouter(plain_callers={"claude": AsyncMock()}, decider=decider)


def _decider_by_candidate(table: Dict[str, Dict[str, Answer]]) -> AsyncMock:
    async def decide(request: DecideRequest) -> DecideResult:
        cand = request.state["candidate"]  # type: ignore[index]
        if table[cand] is None:
            raise ModelCallError("HTTP 503")
        return DecideResult(answers=table[cand], harness="typesafe", cost_usd=1e-5)

    return AsyncMock(side_effect=decide)


async def test_run_gate_splits_and_counts(monkeypatch):
    facts = [_fact(10, "drinks coffee"), _fact(11, "lives in Lisbon")]
    table = {
        "new thing": _answers("new", 0.9),
        "lives in Lisbon": _answers("duplicate", 0.9, "11", 0.9),
        "drinks tea now": _answers("contradicts", 0.9, "10", 0.9),
        "maybe": _answers("new", 0.5),
        "down": None,
    }
    decider = _decider_by_candidate(table)  # type: ignore[arg-type]
    router = _router_with(decider, monkeypatch)
    cands = [{"content": c, "scope": "private"} for c in table]
    cands.append({"content": "family thing", "scope": "family"})  # no family facts
    out = await run_gate(router, cands, {"private": facts, "family": []}, CFG)

    assert sorted(out.accepted) == [0, 1, 5]
    assert out.accepted[1].existing_id == 11
    assert out.escalated == [2, 3, 4]
    stats = out.stats()
    assert stats["accepted_by"] == {"new": 1, "duplicate": 1, "no_saved_facts": 1}
    assert stats["escalated_by"] == {
        "contradicts": 1,
        "low_confidence": 1,
        "jev_unavailable": 1,
    }
    assert stats["jev_calls"] == 4
    assert decider.await_count == 5  # family candidate needed no call


async def test_missing_key_escalates_everything_without_llm(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    claude = AsyncMock()
    router = ModelRouter(plain_callers={"claude": claude}, decider=AsyncMock())
    cands = [{"content": "x"}, {"content": "y"}]
    out = await run_gate(router, cands, {"private": [_fact(1, "x")]}, CFG)
    assert out.escalated == [0, 1]
    assert set(out.reasons.values()) == {"jev_unavailable"}
    claude.assert_not_called()


# ---- service integration -------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    mgr = DatabaseManager(str(tmp_path / "mem.db"))
    await mgr.initialize()
    async with mgr.get_connection() as conn:
        await conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (123,))
        await conn.commit()
    yield mgr
    await mgr.close()


@pytest.fixture
def repo(db):
    return MemoryRepository(db)


def _service(repo, db, gate: Optional[JevGateConfig]) -> EvaluatorService:
    return EvaluatorService(
        memory_repo=repo,
        writer=MemoryWriter(repo),
        token_tracker=MagicMock(),
        db=db,
        notify_callback=AsyncMock(),
        jev_gate=gate,
    )


async def _op_stats(db) -> Dict[str, Any]:
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT op_stats FROM memory_extractions ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return json.loads(row[0]) if row and row[0] else {}


def _ops(*contents: str) -> str:
    return json.dumps(
        [{"op": "add", "tier": "general", "content": c} for c in contents]
    )


async def _run(svc: EvaluatorService, extraction: str, router: Any) -> None:
    with patch.object(svc, "_invoke_cli", new=AsyncMock(return_value=extraction)):
        with patch("src.evaluator.service.get_router", return_value=router):
            assert await svc.run(
                session_id="s",
                user_id=123,
                conversation_text="User: hi",
                cumulative_tokens=55000,
                agent_id="chef",
            )


async def test_escalated_only_go_to_haiku_in_one_batch(repo, db, monkeypatch):
    coffee = await repo.add_fact("chef", "general", "drinks coffee")
    lisbon = await repo.add_fact("chef", "general", "lives in Lisbon")
    table = {
        "likes spicy food": _answers("new", 0.95),
        "lives in Lisbon": _answers("duplicate", 0.9, str(lisbon), 0.9),
        "drinks tea now": _answers("contradicts", 0.9, str(coffee), 0.9),
    }
    router = _router_with(_decider_by_candidate(table), monkeypatch)  # type: ignore[arg-type]
    svc = _service(repo, db, CFG)
    haiku = AsyncMock(
        return_value=json.dumps(
            [{"index": 0, "verdict": "contradicts", "existing_id": coffee}]
        )
    )
    svc._invoke_reconcile_cli = haiku

    await _run(svc, _ops(*table), router)

    haiku.assert_awaited_once()
    prompt = haiku.call_args[0][0]
    assert "drinks tea now" in prompt
    assert "likes spicy food" not in prompt
    contents = {f.content for f in await repo.get_facts("chef")}
    assert contents == {"lives in Lisbon", "likes spicy food", "drinks tea now"}
    stats = await _op_stats(db)
    assert stats["reconcile"] == "ok"
    assert stats["already_saved"] == 1 and stats["superseded"] == 1
    assert stats["reconcile_gate"]["accepted"] == 2
    assert stats["reconcile_gate"]["escalated"] == 1
    assert stats["reconcile_gate"]["escalated_by"] == {"contradicts": 1}
    by = {v["index"]: v["by"] for v in stats["verdicts"]}
    assert by == {0: "jev", 1: "jev", 2: "haiku"}
    # escalated verdict mapped back to its original index
    assert stats["verdicts"][2]["verdict"] == "contradicts"


async def test_all_accepted_skips_haiku(repo, db, monkeypatch):
    await repo.add_fact("chef", "general", "drinks coffee")
    router = _router_with(
        _decider_by_candidate({"likes spicy food": _answers("new", 0.9)}),
        monkeypatch,
    )
    svc = _service(repo, db, CFG)
    haiku = AsyncMock(return_value="[]")
    svc._invoke_reconcile_cli = haiku
    await _run(svc, _ops("likes spicy food"), router)
    haiku.assert_not_called()
    assert (await _op_stats(db))["added"] == 1


async def test_jev_down_falls_back_to_full_haiku_batch(repo, db, monkeypatch):
    await repo.add_fact("chef", "general", "drinks coffee")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    claude_plain = AsyncMock()
    router = ModelRouter(plain_callers={"claude": claude_plain}, decider=AsyncMock())
    svc = _service(repo, db, CFG)
    haiku = AsyncMock(return_value="[]")
    svc._invoke_reconcile_cli = haiku
    await _run(svc, _ops("a one", "b two"), router)
    haiku.assert_awaited_once()
    prompt = haiku.call_args[0][0]
    assert "a one" in prompt and "b two" in prompt
    claude_plain.assert_not_called()  # no per-candidate llm fallback
    gate = (await _op_stats(db))["reconcile_gate"]
    assert gate["escalated_by"] == {"jev_unavailable": 2}


async def test_haiku_failure_on_escalation_uses_hint_fallback(repo, db, monkeypatch):
    coffee = await repo.add_fact("chef", "general", "drinks coffee")
    router = _router_with(
        _decider_by_candidate({"drinks tea now": _answers("contradicts", 0.9)}),
        monkeypatch,
    )
    svc = _service(repo, db, CFG)
    svc._invoke_reconcile_cli = AsyncMock(side_effect=RuntimeError("haiku down"))
    extraction = json.dumps(
        [
            {
                "op": "supersede",
                "tier": "general",
                "content": "drinks tea now",
                "supersedes_id": coffee,
            }
        ]
    )
    await _run(svc, extraction, router)
    stats = await _op_stats(db)
    assert stats["reconcile"].startswith("failed:")
    assert stats["superseded"] == 1


async def test_flag_off_is_unchanged(repo, db):
    """Gate off: no decide call, one haiku call over all candidates, and no
    gate keys in op_stats."""
    await repo.add_fact("chef", "general", "drinks coffee")
    router = MagicMock()
    router.decide = AsyncMock()
    svc = _service(repo, db, None)
    haiku = AsyncMock(return_value="[]")
    svc._invoke_reconcile_cli = haiku
    await _run(svc, _ops("a one", "b two"), router)
    router.decide.assert_not_called()
    haiku.assert_awaited_once()
    stats = await _op_stats(db)
    assert "reconcile_gate" not in stats
    assert all(
        set(v) == {"index", "verdict", "existing_id", "safety"}
        for v in stats["verdicts"]
    )
    assert stats["added"] == 2
