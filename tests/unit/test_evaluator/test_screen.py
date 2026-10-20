"""The Jev screen (dv-7e11 step c): pure helpers, selection, and the shadow
run inside EvaluatorService (writes no facts, Jev failure = chunk skipped)."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.settings import Settings
from src.evaluator import screen
from src.evaluator.screen import (
    JevScores,
    ScreenConfig,
    chunk_turns,
    content_questions,
    fact_questions,
    pack_calls,
    parse_turns,
    select_facts,
)
from src.evaluator.service import EvaluatorService
from src.evaluator.writer import MemoryWriter
from src.model_router import Answer, DecideResult, ModelCallError
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository


def _fact(fid: int, tier: str = "general", content: str = "x") -> Any:
    return SimpleNamespace(id=fid, tier=tier, content=content, kind="user_fact")


def _scores(**by_id: Any) -> JevScores:
    """_scores(t1=0.9, m2=0.4) -> answers {'t1': .9, 'm2': .4}."""
    return JevScores(answers={k: float(v) for k, v in by_id.items()})


# ---------------------------------------------------------------- helpers


def test_parse_turns_splits_user_and_assistant():
    turns = parse_turns("User: hello\n\nAssistant: hi there\n\nUser: bench 110")
    assert [t["from"] for t in turns] == ["user", "assistant", "user"]
    assert turns[2]["text"] == "bench 110"


def test_chunks_cut_before_user_turns_and_cover_everything():
    turns = parse_turns(
        "\n\n".join(f"User: {'word ' * 400}{i}\n\nAssistant: ok" for i in range(6))
    )
    chunks = chunk_turns(turns, target_tokens=1_500)
    assert len(chunks) > 1
    assert all(c[0]["from"] == "user" for c in chunks)
    assert [t["id"] for c in chunks for t in c] == [t["id"] for t in turns]


def test_long_code_blocks_are_collapsed():
    code = "```py\n" + "x = 1\n" * 40 + "```"
    (turn,) = parse_turns(f"User: look\n{code}")
    assert "[code block, 40 lines omitted]" in turn["text"]


def test_fact_questions_are_two_per_fact_and_content_questions_per_agent():
    qs = fact_questions([_fact(7, content="bench 100kg")])
    assert set(qs) == {"t7", "m7"}
    assert qs["t7"].instructions["saved_fact"] == "bench 100kg"
    assert "targets_or_program_change" in content_questions("coach")
    assert "taste_reaction" in content_questions("chef")
    assert "taste_reaction" not in content_questions("coach")
    # the weak questions are asked (and logged), never dropped
    assert {"change_or_end", "result_to_keep", "only_transient"} <= set(
        content_questions("general")
    )


def test_agent_key_maps_scope_ids():
    assert screen.agent_key("coach#u1000001") == "coach"
    assert screen.agent_key("f1:chef") == "chef"
    assert screen.agent_key("brand-new-agent") == "general"


def test_pack_calls_covers_every_question_once_gate_in_first_call():
    facts = [_fact(i, content="fact text " * 60) for i in range(120)]
    state = {"assistant_role": "r", "conversation": [{"id": "a", "text": "hi"}]}
    gate = content_questions("devops")
    fq = fact_questions(facts)
    calls = pack_calls(state, gate, fq, limit=6_000)
    assert len(calls) > 1
    keys = [k for call in calls for k in call]
    assert sorted(keys) == sorted(list(gate) + list(fq))
    assert set(gate) <= set(calls[0])


# -------------------------------------------------------------- selection


def test_selection_threshold_is_on_either_question():
    cfg = ScreenConfig(mode="shadow")
    facts = [_fact(i) for i in (1, 2, 3, 4)]
    sel = select_facts(facts, _scores(t1=0.31, m2=0.30, t3=0.29, m3=0.1), cfg)
    assert sel.ids == {1, 2}  # 0.30 counts (>=); 0.29 does not
    assert {f.id: sel.reasons[f.id] for f in sel.facts} == {1: "score", 2: "score"}


def test_selection_caps_at_top_n_by_score_but_keeps_pinned_and_remember():
    cfg = ScreenConfig(mode="shadow", fact_send_max=2)
    facts = [_fact(1), _fact(2), _fact(3), _fact(4, tier="pinned"), _fact(5)]
    sel = select_facts(
        facts, _scores(t1=0.5, t2=0.9, t3=0.7, t4=0.0, t5=0.0), cfg, remember_ids={5}
    )
    assert sel.ids == {2, 3, 4, 5}
    assert sel.reasons == {2: "score", 3: "score", 4: "pinned", 5: "remember"}
    assert sel.capped and sel.over_threshold == 3


def test_selection_empty_scores_still_sends_pinned():
    sel = select_facts(
        [_fact(1), _fact(2, tier="pinned")], _scores(), ScreenConfig(mode="shadow")
    )
    assert sel.ids == {2} and not sel.capped


# --------------------------------------------------------------- settings


def _settings(**kw: Any) -> Settings:
    return Settings(
        telegram_bot_token="123:abc", telegram_bot_username="b", _env_file=None, **kw
    )


def test_settings_default_is_shadow_and_validated(monkeypatch):
    monkeypatch.delenv("MEMORY_SCREEN_MODE", raising=False)
    s = _settings()
    assert s.memory_screen_mode == "shadow"
    assert (s.memory_screen_chunk_tokens, s.memory_screen_fact_send_max) == (12000, 40)
    assert s.memory_screen_fact_send_min == 0.3
    assert _settings(memory_screen_mode="off").memory_screen_mode == "off"
    with pytest.raises(ValueError):
        _settings(memory_screen_mode="maybe")


def test_screen_config_enabled_flag():
    assert not ScreenConfig(mode="off").enabled
    assert ScreenConfig(mode="shadow").enabled and ScreenConfig(mode="on").enabled


# ------------------------------------------------------ shadow in service


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


def _service(repo, db, mode: str = "shadow") -> EvaluatorService:
    svc = EvaluatorService(
        memory_repo=repo,
        writer=MemoryWriter(repo),
        token_tracker=MagicMock(),
        db=db,
        notify_callback=AsyncMock(),
        screen=ScreenConfig(mode=mode),
    )
    svc._invoke_reconcile_cli = AsyncMock(return_value="[]")
    return svc


def _router(score: Callable[[str], float], fail: bool = False) -> Any:
    seen: List[Any] = []

    async def decide(request: Any) -> DecideResult:
        seen.append(request)
        if fail:
            raise ModelCallError("jev down")
        return DecideResult(
            answers={
                qid: Answer(kind="noul", value=score(qid)) for qid in request.questions
            },
            harness="typesafe",
            input_tokens=1000,
            cost_usd=0.00004,
        )

    router = MagicMock()
    router.decide = decide
    router.seen = seen
    return router


async def _active(repo, agent="chef") -> List[str]:
    return sorted(f.content for f in await repo.get_facts(agent))


async def _screen_stats(db) -> Dict[str, Any]:
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT op_stats FROM memory_extractions ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
    return json.loads(row[0]).get("screen", {})


async def _run(svc, router, classic: str, shadow: str, agent="chef") -> AsyncMock:
    """Classic extraction returns ``classic``; the shadow call returns ``shadow``."""

    async def cli(prompt: str, backend: str = "claude", purpose: str = "extraction"):
        return shadow if purpose == "screen_extraction" else classic

    mock = AsyncMock(side_effect=cli)
    with patch.object(svc, "_invoke_cli", new=mock):
        with patch("src.evaluator.service.get_router", return_value=router):
            assert await svc.run(
                session_id="s",
                user_id=123,
                conversation_text="User: bench is 110 now\n\nAssistant: noted",
                cumulative_tokens=55000,
                agent_id=agent,
            )
            await asyncio.gather(*svc._shadow_tasks)
    return mock


async def test_shadow_writes_nothing_to_facts_and_records_proposed_ops(repo, db):
    bench = await repo.add_fact("chef", "general", "bench 100kg")
    await repo.add_fact("chef", "general", "likes spicy food")
    await repo.add_fact("chef", "pinned", "no peanuts")
    svc = _service(repo, db)
    classic = json.dumps(
        [
            {
                "op": "add",
                "tier": "general",
                "content": "bench 110kg",
                "supersedes_id": bench,
            }
        ]
    )
    shadow = json.dumps(
        [
            {
                "op": "add",
                "tier": "general",
                "content": "bench 110kg",
                "supersedes_id": bench,
            },
            {"op": "add", "tier": "general", "content": "brand new thing"},
        ]
    )
    router = _router(
        lambda qid: 0.9 if qid in (f"t{bench}", "remember_request") else 0.01
    )

    cli = await _run(svc, router, classic, shadow)

    # classic path wrote exactly what it always does; the shadow wrote nothing
    # (the stub reconciler says "new", so the classic add sits beside the old row)
    assert await _active(repo) == [
        "bench 100kg",
        "bench 110kg",
        "likes spicy food",
        "no peanuts",
    ]
    stats = await _screen_stats(db)
    assert stats["status"] == "ok" and stats["mode"] == "shadow"
    (chunk,) = stats["chunks"]
    assert chunk["status"] == "ok"
    assert {s["id"]: s["why"] for s in chunk["selected"]}[bench] == "score"
    assert "pinned" in {s["why"] for s in chunk["selected"]}
    assert chunk["facts"]["sent"] == 2 < chunk["facts"]["total"] + 1
    assert chunk["gate"]["remember_request"] == 0.9
    ops = {(o["op"], o["content"]): o for o in chunk["ops"]}
    assert ops[("supersede", "bench 110kg")]["supersedes_id"] == bench
    assert ("add", "brand new thing") in ops
    # exactly one classic + one shadow model call, shadow tagged for cost
    assert [c.kwargs.get("purpose", "extraction") for c in cli.await_args_list] == [
        "extraction",
        "screen_extraction",
    ]
    assert {r.purpose for r in router.seen} == {"screen"}
    assert all(r.fallback_model is None for r in router.seen)


async def test_shadow_prompt_lists_only_selected_facts(repo, db):
    hit = await repo.add_fact("chef", "general", "alpha fact")
    await repo.add_fact("chef", "general", "beta fact")
    svc = _service(repo, db)
    prompts: List[str] = []

    async def cli(prompt: str, backend: str = "claude", purpose: str = "extraction"):
        if purpose == "screen_extraction":
            prompts.append(prompt)
        return "[]"

    router = _router(lambda qid: 0.8 if qid == f"m{hit}" else 0.0)
    with patch.object(svc, "_invoke_cli", new=AsyncMock(side_effect=cli)):
        with patch("src.evaluator.service.get_router", return_value=router):
            await svc.run("s", 123, "User: hi", 1, agent_id="chef")
            await asyncio.gather(*svc._shadow_tasks)

    (prompt,) = prompts
    assert "alpha fact" in prompt and "beta fact" not in prompt
    assert "NOT the whole memory" in prompt


async def test_jev_failure_skips_the_chunk_and_leaves_the_classic_path_alone(repo, db):
    svc = _service(repo, db)
    classic = json.dumps([{"op": "add", "tier": "general", "content": "kept"}])

    cli = await _run(svc, _router(lambda q: 0.0, fail=True), classic, "[]")

    assert await _active(repo) == ["kept"]
    stats = await _screen_stats(db)
    (chunk,) = stats["chunks"]
    assert chunk["status"] == "jev_failed" and "jev down" in chunk["error"]
    assert "ops" not in chunk
    # no targeted Sonnet call was made for the failed chunk
    assert [c.kwargs.get("purpose", "extraction") for c in cli.await_args_list] == [
        "extraction"
    ]


async def test_shadow_sonnet_failure_is_recorded_not_raised(repo, db):
    svc = _service(repo, db)

    async def cli(prompt: str, backend: str = "claude", purpose: str = "extraction"):
        if purpose == "screen_extraction":
            raise RuntimeError("cli exploded")
        return "[]"

    with patch.object(svc, "_invoke_cli", new=AsyncMock(side_effect=cli)):
        with patch(
            "src.evaluator.service.get_router", return_value=_router(lambda q: 0.5)
        ):
            assert await svc.run("s", 123, "User: hi", 1, agent_id="chef")
            await asyncio.gather(*svc._shadow_tasks)

    (chunk,) = (await _screen_stats(db))["chunks"]
    assert chunk["status"] == "extract_failed" and "cli exploded" in chunk["error"]


async def test_unexpected_shadow_error_is_swallowed(repo, db):
    svc = _service(repo, db)
    classic = json.dumps([{"op": "add", "tier": "general", "content": "kept"}])
    with patch.object(
        svc, "_screen_run", new=AsyncMock(side_effect=ValueError("boom"))
    ):
        await _run(svc, _router(lambda q: 0.0), classic, "[]")
    assert await _active(repo) == ["kept"]
    assert (await _screen_stats(db))["status"] == "error"


async def test_mode_off_runs_no_shadow(repo, db):
    svc = _service(repo, db, mode="off")
    router = _router(lambda q: 0.9)
    cli = await _run(svc, router, "[]", "[]")
    assert not svc._shadow_tasks and router.seen == []
    assert cli.await_count == 1
    assert await _screen_stats(db) == {}


async def test_invalidate_outside_the_sent_list_is_dropped(repo, db):
    shown = await repo.add_fact("chef", "general", "shown fact")
    hidden = await repo.add_fact("chef", "general", "hidden fact")
    svc = _service(repo, db)
    shadow = json.dumps(
        [
            {"op": "invalidate", "target_id": shown},
            {"op": "invalidate", "target_id": hidden},
        ]
    )
    router = _router(lambda qid: 0.9 if qid == f"t{shown}" else 0.0)

    await _run(svc, router, "[]", shadow)

    (chunk,) = (await _screen_stats(db))["chunks"]
    assert [o["target_id"] for o in chunk["ops"]] == [shown]
    assert chunk["dropped_invalidations"] == [hidden]
    assert hidden in chunk["referenced_not_sent"]
    assert sorted(await _active(repo)) == [
        "hidden fact",
        "shown fact",
    ]  # nothing closed


async def test_safety_conflict_and_revocation_map_like_the_classic_path(repo, db):
    nuts = await repo.add_fact("chef", "pinned", "allergic to peanuts")
    svc = _service(repo, db)
    shadow = json.dumps(
        [
            {
                "op": "add",
                "tier": "pinned",
                "content": "allergic to walnuts",
                "conflicts_with_id": nuts,
            },
            {
                "op": "add",
                "tier": "general",
                "content": "peanuts are fine now",
                "supersedes_id": nuts,
                "revokes": True,
            },
        ]
    )
    await _run(svc, _router(lambda q: 0.9), "[]", shadow)
    (chunk,) = (await _screen_stats(db))["chunks"]
    by_content = {o["content"]: o for o in chunk["ops"]}
    assert by_content["allergic to walnuts"]["op"] == "add"  # keep both
    assert chunk["safety_pairs"] == [[nuts, 0]]
    assert by_content["peanuts are fine now"]["op"] == "supersede"


# ------------------------------------------- b2 fixes (new path only)

COMPOUND = (
    "Hyperliquid account holds 120 USDC. Leverage cap is 3x. "
    "Funding is paid every 8 hours."
)


def _propose(svc, ops, texts):
    ids = set(texts)
    return svc._propose_ops(ops, "chef", ids, set(), texts)


def test_chunk_date_note_single_day_and_span():
    from datetime import datetime

    from src.evaluator.screen import chunk_date_note

    d1, d2 = datetime(2026, 9, 3, 10), datetime(2026, 9, 5, 9)
    assert "03.09.26 -" in chunk_date_note(d1, d2) and "05.09.26" in chunk_date_note(
        d1, d2
    )
    note = chunk_date_note(d1, datetime(2026, 9, 3, 22))
    assert "03.09.26 (DD.MM.YY)" in note and "never from today" in note
    assert chunk_date_note(None, None) == ""


async def test_shadow_prompt_carries_the_slice_date_not_run_date(repo, db):
    await repo.add_fact("chef", "general", "alpha fact")
    svc = _service(repo, db)
    prompts: List[str] = []

    async def cli(prompt: str, backend: str = "claude", purpose: str = "extraction"):
        if purpose == "screen_extraction":
            prompts.append(prompt)
        return "[]"

    with patch.object(svc, "_invoke_cli", new=AsyncMock(side_effect=cli)):
        with patch.object(
            svc, "_message_timestamp", AsyncMock(return_value=screen_dt(2026, 3, 4))
        ):
            with patch(
                "src.evaluator.service.get_router", return_value=_router(lambda q: 0.9)
            ):
                await svc.run(
                    session_id="s",
                    user_id=123,
                    conversation_text="User: hi\n\nAssistant: yo",
                    cumulative_tokens=55000,
                    agent_id="chef",
                    since_message_id=7,
                )
                await asyncio.gather(*svc._shadow_tasks)
    assert "CONVERSATION DATE: 04.03.26 -" in prompts[0]


def screen_dt(y, m, d):
    from datetime import datetime

    return datetime(y, m, d, 12)


def test_split_claims_and_lost_claims():
    from src.evaluator.screen import closes_compound_safely, lost_claims, split_claims

    assert len(split_claims(COMPOUND)) == 3
    assert len(split_claims("Bench is 100 kg")) == 1
    # one claim lost (the changed one) -> fine
    assert closes_compound_safely(
        COMPOUND,
        [
            "Hyperliquid account holds 90 USDC",
            "Leverage cap is 3x",
            "Funding is paid every 8 hours",
        ],
    )
    # nothing re-added -> two untouched claims lost
    assert len(lost_claims(COMPOUND, ["Hyperliquid account holds 90 USDC"])) == 3
    assert not closes_compound_safely(COMPOUND, ["Hyperliquid account holds 90 USDC"])
    # a single-claim fact is never compound
    assert closes_compound_safely("Bench is 100 kg", ["Bench is 110 kg"])


def test_supersede_of_compound_fact_without_remainder_is_downgraded(repo, db):
    svc = _service(repo, db)
    ops = [
        {
            "op": "add",
            "tier": "general",
            "kind": "user_fact",
            "scope": "private",
            "content": "Hyperliquid account holds 90 USDC",
            "supersedes_id": 5,
        }
    ]
    out = _propose(svc, ops, {5: COMPOUND})
    assert [o["op"] for o in out["ops"]] == ["add"]
    assert "supersedes_id" not in out["ops"][0]
    assert out["compound_downgraded"] == [5]


def test_supersede_of_compound_fact_with_remainder_is_kept(repo, db):
    svc = _service(repo, db)
    base = {"op": "add", "tier": "general", "kind": "user_fact", "scope": "private"}
    ops = [
        {**base, "content": "Hyperliquid account holds 90 USDC", "supersedes_id": 5},
        {**base, "content": "Leverage cap is 3x"},
        {**base, "content": "Funding is paid every 8 hours"},
    ]
    out = _propose(svc, ops, {5: COMPOUND})
    assert out["ops"][0]["op"] == "supersede" and out["compound_downgraded"] == []


def test_invalidate_of_compound_fact_without_remainder_is_rejected(repo, db):
    svc = _service(repo, db)
    out = _propose(svc, [{"op": "invalidate", "target_id": 5}], {5: COMPOUND})
    assert out["ops"] == [] and out["compound_rejected_invalidations"] == [5]
    ok = _propose(svc, [{"op": "invalidate", "target_id": 6}], {6: "single claim here"})
    assert [o["op"] for o in ok["ops"]] == ["invalidate"]


def test_invalidate_dropped_when_same_id_is_superseded(repo, db):
    svc = _service(repo, db)
    ops = [
        {
            "op": "add",
            "tier": "general",
            "kind": "user_fact",
            "scope": "private",
            "content": "bench 110kg",
            "supersedes_id": 5,
        },
        {"op": "invalidate", "target_id": 5},
        {"op": "invalidate", "target_id": 6},
    ]
    out = _propose(svc, ops, {5: "bench 100kg", 6: "old status"})
    assert [(o["op"], o.get("target_id")) for o in out["ops"]] == [
        ("invalidate", 6),
        ("supersede", None),
    ]
    assert out["redundant_invalidations"] == [5]


def test_ticket_status_candidates_are_rejected_on_the_screen_path(repo, db):
    svc = _service(repo, db)
    base = {"op": "add", "tier": "general", "kind": "user_fact", "scope": "private"}
    ops = [
        {**base, "content": "dv-7e11 is in progress, step c shipped"},
        {**base, "content": "Commit 126465c landed on main"},
        {**base, "content": "bgjob for the digest started at 10:00"},
        {**base, "content": "Owner decided: tickets use the dv-xxxx ids always"},
        {**base, "content": "Bench press target is 110 kg"},
        {**base, "tier": "pinned", "content": "dv-1234 done: never publish keys"},
    ]
    out = _propose(svc, ops, {})
    kept = {o["content"] for o in out["ops"]}
    assert kept == {
        "Owner decided: tickets use the dv-xxxx ids always",
        "Bench press target is 110 kg",
        "dv-1234 done: never publish keys",
    }
    assert len(out["rejected_ticket_status"]) == 3


def test_classic_split_ops_still_keeps_ticket_status(repo, db):
    # the classic path (shared _split_ops) is untouched by the new guard
    svc = _service(repo, db)
    cands, _, _ = svc._split_ops(
        [
            {
                "op": "add",
                "tier": "general",
                "kind": "user_fact",
                "scope": "private",
                "content": "dv-7e11 is in progress",
            }
        ],
        "chef",
    )
    assert len(cands) == 1


def test_header_names_ticket_rule_and_compound_rule():
    from src.evaluator.screen import SELECTED_FACTS_HEADER

    assert "Ticket and job status is NOT a fact" in SELECTED_FACTS_HEADER
    assert "is rejected" in SELECTED_FACTS_HEADER
