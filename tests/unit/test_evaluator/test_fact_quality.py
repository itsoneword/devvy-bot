"""Tests for the daily fact-quality check (dv-404f): report-only, never writes."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.evaluator.fact_quality import (
    FactQualityCheck,
    register_daily_check,
)
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository

MEAL = "12.08: ужин — паста 650 ккал, итого за день 2100 ккал"
DURABLE = "Аллергия на грецкие орехи, реакция немедленная"


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "mem.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
async def repo(db):
    return MemoryRepository(db)


@pytest.fixture
def service(db):
    return FactQualityCheck(db=db, notify_callback=AsyncMock())


def _judge(*entries) -> str:
    return json.dumps(list(entries))


async def _age_fact(db, fact_id: int, hours: int) -> None:
    """Backdate created_at to simulate a fact saved outside the window."""
    old = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE memory_facts SET created_at=? WHERE id=?",
            (old.isoformat(sep=" "), fact_id),
        )
        await conn.commit()


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list = []

    def add_native_job(self, name, cron_expression, coroutine) -> None:
        self.jobs.append((name, cron_expression, coroutine))


# --- no new facts ----------------------------------------------------------


async def test_no_new_facts_no_report(service):
    notify = service._notify

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value="[]"),
    ) as cli:
        reports = await service.run()

    assert reports == []
    notify.assert_not_awaited()
    # And no model call: an empty window must cost nothing.
    cli.assert_not_awaited()


async def test_facts_older_than_the_window_are_not_new(service, repo, db):
    fid = await repo.add_fact("coach#u1", "general", DURABLE)
    await _age_fact(db, fid, hours=48)

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value="[]"),
    ):
        assert await service.run() == []
    service._notify.assert_not_awaited()


# --- guard + judge aggregation --------------------------------------------


async def test_guard_and_judge_results_aggregate_per_scope(service, repo):
    meal = await repo.add_fact("coach#u111111111", "general", MEAL)
    blob = await repo.add_fact("coach#u111111111", "general", "и на этом всё, потом")
    good = await repo.add_fact("coach#u111111111", "general", DURABLE)

    judge = _judge(
        {"id": meal, "verdict": "episodic", "why": "day total"},
        {"id": blob, "verdict": "malformed", "why": "truncated fragment"},
        {"id": good, "verdict": "durable", "why": "allergy"},
    )
    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value=judge),
    ):
        reports = await service.run()

    assert len(reports) == 1
    report = reports[0]
    assert report.scope == "coach#u111111111"
    assert report.total == 3
    assert report.judge_status == "ok"
    assert [v.fact_id for v in report.suspects] == [meal, blob]

    # The guard caught the meal on its own; the judge alone caught the blob.
    by_id = {v.fact_id: v for v in report.verdicts}
    assert by_id[meal].guard_reason is not None
    assert by_id[blob].guard_reason is None
    assert by_id[blob].judge_verdict == "malformed"
    assert by_id[good].is_suspect is False

    message = service._notify.await_args.args[0]
    assert (
        f"coach#u111111111: 3 new facts, 2 look like trash (ids {meal}, {blob})"
        in message
    )
    assert "Report only" in message


async def test_scopes_are_reported_separately(service, repo):
    await repo.add_fact("coach#u1", "general", MEAL)
    await repo.add_fact("chef", "general", DURABLE)

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value="[]"),
    ):
        reports = await service.run()

    assert [r.scope for r in reports] == ["chef", "coach#u1"]
    message = service._notify.await_args.args[0]
    assert "chef: 1 new fact, all look durable" in message
    assert "coach#u1: 1 new fact, 1 look like trash" in message


async def test_core_and_pinned_facts_are_never_guard_flagged(service, repo):
    """Same exemption as the write path: an allergy phrased with a date must
    not be reported as junk."""
    pinned = await repo.add_fact("coach#u1", "pinned", MEAL)
    core = await repo.add_fact("coach#u1", "core", MEAL)

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value="[]"),
    ):
        reports = await service.run()

    by_id = {v.fact_id: v for v in reports[0].verdicts}
    assert by_id[pinned].guard_reason is None
    assert by_id[core].guard_reason is None
    assert reports[0].suspects == []


async def test_judge_sees_only_this_scopes_facts(service, repo):
    a = await repo.add_fact("coach#u1", "general", DURABLE)
    b = await repo.add_fact("chef", "general", "prefers cast iron pans")

    cli = AsyncMock(return_value="[]")
    with patch("src.evaluator.fact_quality.invoke_claude_cli", cli):
        await service.run()

    assert cli.await_count == 2
    prompts = [call.args[0] for call in cli.await_args_list]
    chef_prompt = next(p for p in prompts if "cast iron" in p)
    assert str(a) not in chef_prompt.split("## Facts", 1)[1]
    assert f"{b} | user_fact | prefers cast iron pans" in chef_prompt


async def test_long_facts_are_cut_with_an_explained_marker(service, repo):
    """Measured on live data 2026-08-15: an unmarked cut made the judge call
    two good facts 'malformed — truncated mid-sentence'."""
    long_fact = "детали протокола " * 60  # >600 chars
    fid = await repo.add_fact("chef", "general", long_fact, kind="conversation_result")

    cli = AsyncMock(return_value="[]")
    with patch("src.evaluator.fact_quality.invoke_claude_cli", cli):
        await service.run()

    prompt = cli.await_args.args[0]
    listing = prompt.split("## Facts", 1)[1]
    assert f"{fid} | conversation_result | " in listing
    assert "shortened for this review" in listing
    # The marker is explained in the instructions, not just appended.
    assert "never call it malformed for stopping mid-sentence" in prompt


async def test_unknown_ids_and_verdicts_are_ignored(service, repo):
    good = await repo.add_fact("chef", "general", DURABLE)

    judge = _judge(
        {"id": 99999, "verdict": "episodic", "why": "not our fact"},
        {"id": good, "verdict": "nonsense", "why": "bad enum"},
    )
    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value=judge),
    ):
        reports = await service.run()

    assert reports[0].suspects == []
    assert reports[0].verdicts[0].judge_verdict is None


# --- LLM failure -----------------------------------------------------------


async def test_llm_failure_warns_and_still_reports(service, repo, capsys):
    meal = await repo.add_fact("coach#u1", "general", MEAL)

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(side_effect=RuntimeError("CLI down")),
    ):
        reports = await service.run()

    assert reports[0].judge_status.startswith("failed: CLI down")
    # The deterministic guard still stands, so the meal is still reported.
    assert [v.fact_id for v in reports[0].suspects] == [meal]
    message = service._notify.await_args.args[0]
    assert "judge unavailable" in message
    # structlog renders to stdout in tests, not through the logging module.
    assert "Fact quality judge failed" in capsys.readouterr().out


async def test_unparseable_judge_output_does_not_crash(service, repo):
    await repo.add_fact("chef", "general", DURABLE)

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value="I think they all look fine, honestly"),
    ):
        reports = await service.run()

    assert reports[0].judge_status.startswith("failed:")
    assert reports[0].suspects == []


async def test_fenced_json_is_parsed(service, repo):
    meal = await repo.add_fact("chef", "general", "any old fact")
    fenced = "```json\n" + _judge({"id": meal, "verdict": "episodic"}) + "\n```"

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value=fenced),
    ):
        reports = await service.run()

    assert reports[0].judge_status == "ok"
    assert [v.fact_id for v in reports[0].suspects] == [meal]


async def test_notification_failure_does_not_crash_the_run(db, repo):
    service = FactQualityCheck(db=db, notify_callback=AsyncMock(side_effect=OSError()))
    await repo.add_fact("chef", "general", DURABLE)

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value="[]"),
    ):
        reports = await service.run()

    assert reports[0].total == 1


# --- report-only guarantee -------------------------------------------------


async def test_run_writes_nothing(service, repo):
    meal = await repo.add_fact("coach#u1", "general", MEAL)
    judge = _judge({"id": meal, "verdict": "episodic", "why": "meal log"})

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value=judge),
    ):
        await service.run()

    facts = await repo.get_facts("coach#u1")
    assert [f.id for f in facts] == [meal]
    assert facts[0].status == "active"
    assert facts[0].invalid_at is None
    assert facts[0].content == MEAL


# --- registration ----------------------------------------------------------


async def test_flag_off_never_registers_the_job(db):
    scheduler = FakeScheduler()

    service = register_daily_check(
        scheduler=scheduler,
        db=db,
        enabled=False,
        cron_expression="0 7 * * *",
    )

    assert service is None
    assert scheduler.jobs == []


async def test_flag_on_registers_a_daily_job(db):
    scheduler = FakeScheduler()

    service = register_daily_check(
        scheduler=scheduler,
        db=db,
        enabled=True,
        cron_expression="0 7 * * *",
        model="haiku",
    )

    assert isinstance(service, FactQualityCheck)
    assert len(scheduler.jobs) == 1
    name, cron, job = scheduler.jobs[0]
    assert name == "fact-quality-check"
    assert cron == "0 7 * * *"

    # The registered callable swallows failures: one bad night must not kill
    # the scheduler thread.
    with patch.object(service, "run", AsyncMock(side_effect=RuntimeError("boom"))):
        await job()


async def test_registered_job_runs_the_check(db, repo):
    scheduler = FakeScheduler()
    notify = AsyncMock()
    register_daily_check(
        scheduler=scheduler,
        db=db,
        enabled=True,
        cron_expression="0 7 * * *",
        notify_callback=notify,
    )
    await repo.add_fact("coach#u1", "general", MEAL)

    with patch(
        "src.evaluator.fact_quality.invoke_claude_cli",
        AsyncMock(return_value="[]"),
    ):
        await scheduler.jobs[0][2]()

    assert "look like trash" in notify.await_args.args[0]
