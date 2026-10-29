"""Per-rotation stale-fact sweep (dv-cf07 phase 2).

A window rotation (``refresh_window``, ``/new`` = ``reset_window``) hands its
final evaluator run to ``ConsolidationService.after_rotation(scope,
final_eval)``, which awaits the eval and then sweeps the scope. The new
window's bundle (``WindowBundleProvider._materialize``) waits for that, bounded
by ``MEMORY_SWEEP_WAIT_SECONDS`` (default 60), then proceeds without it.

Interfaces fixed here for the build:
- ``ConsolidationService.after_rotation(scope, final_eval)`` is a plain
  (non-async) fire-and-forget method, like ``request_run``.
- ``await ConsolidationService.wait_rotation(scope, timeout)`` returns at once
  when no rotation sweep is pending for the scope.
- ``WindowBundleProvider(..., sweep_wait_seconds=...)`` + the existing
  ``attach_consolidation(service)``.
- ``refresh_window(..., consolidation=svc)`` / ``reset_window(...,
  consolidation=svc)``.
- Setting ``memory_sweep_wait_seconds`` (``MEMORY_SWEEP_WAIT_SECONDS``),
  default 60.
"""

import asyncio
import json
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest

from src.claude.prompt_composer import PromptComposer
from src.claude.window_bundle import WindowBundleProvider
from src.consolidation.service import ConsolidationService
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository
from src.storage.window_bundle_repository import WindowBundleRepository
from src.storage.window_repository import WindowRepository

PATCH = "src.consolidation.service.complete_text"


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "rot.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
async def memory_repo(db):
    return MemoryRepository(db)


@pytest.fixture
def service(memory_repo, db, tmp_path):
    return ConsolidationService(
        memory_repo=memory_repo, db=db, report_dir=tmp_path / "consolidation"
    )


@pytest.fixture
def composer(tmp_path):
    persona = tmp_path / "agent.md"
    persona.write_text("You are the test agent persona.", encoding="utf-8")
    return PromptComposer(memory_path=tmp_path, agent_md_path=persona)


def _provider(db, memory_repo, composer, **kwargs):
    return WindowBundleProvider(
        window_repo=WindowRepository(db),
        bundle_repo=WindowBundleRepository(db),
        memory_repo=memory_repo,
        profile_registry=None,
        default_composer=composer,
        **kwargs,
    )


async def _stale_pair(db):
    """1295 'concurrency 3' made stale by 1298 'raised to 5' (devops)."""
    rows = [
        (1295, "Pump concurrency is 3 parallel jobs.", "2026-10-24T09:00:00+00:00"),
        (
            1298,
            "Pump concurrency raised to 5 parallel jobs.",
            "2026-10-24T15:00:00+00:00",
        ),
    ]
    async with db.get_connection() as conn:
        for fid, content, ts in rows:
            await conn.execute(
                "INSERT INTO memory_facts (id, agent_id, tier, content, status, "
                "source, kind, valid_from, recorded_at) "
                "VALUES (?, 'devops', 'general', ?, 'active', 'agent_tool', "
                "'user_fact', ?, ?)",
                (fid, content, ts, ts),
            )
        await conn.commit()


PROPOSAL = json.dumps([{"old_id": 1295, "newer_id": 1298, "reason": "raised"}])


# --- Scenario 4: the new window's bundle waits for the sweep -------------


async def test_new_window_bundle_waits_for_rotation_sweep(
    db, memory_repo, service, composer, tmp_path
):
    await _stale_pair(db)
    provider = _provider(db, memory_repo, composer, sweep_wait_seconds=5)
    provider.attach_consolidation(service)

    async def final_eval() -> bool:
        await asyncio.sleep(0.2)  # the outgoing window's extraction
        return True

    with patch(PATCH, AsyncMock(return_value=PROPOSAL)):
        service.after_rotation("devops", final_eval())
        bundle = await provider.get_or_materialize(tmp_path / "ws", user_id=1)

    assert bundle is not None
    assert "raised to 5" in bundle.text
    assert "concurrency is 3" not in bundle.text
    assert (await memory_repo.get_fact(1295, "devops")).superseded_by == 1298


async def test_bundle_stops_waiting_at_the_timeout_and_proceeds(
    db, memory_repo, service, composer, tmp_path
):
    await _stale_pair(db)
    provider = _provider(db, memory_repo, composer, sweep_wait_seconds=0.3)
    provider.attach_consolidation(service)
    release = asyncio.Event()

    async def stuck_final_eval() -> bool:
        await release.wait()
        return True

    model = AsyncMock(return_value=PROPOSAL)
    with patch(PATCH, model):
        service.after_rotation("devops", stuck_final_eval())
        started = time.monotonic()
        bundle = await provider.get_or_materialize(tmp_path / "ws", user_id=1)
        elapsed = time.monotonic() - started

        assert bundle is not None
        assert elapsed < 3.0
        # Went on without the sweep: both facts are in this window's bundle.
        assert "concurrency is 3" in bundle.text
        assert "raised to 5" in bundle.text
        assert model.await_count == 0

        # The sweep is not cancelled: it lands for the next window.
        release.set()
        await service.wait_rotation("devops", timeout=5)
    assert (await memory_repo.get_fact(1295, "devops")).superseded_by == 1298


def test_sweep_wait_setting_defaults_to_60_seconds(monkeypatch):
    from src.config.settings import Settings

    monkeypatch.delenv("MEMORY_SWEEP_WAIT_SECONDS", raising=False)
    settings = Settings(
        telegram_bot_token="t", telegram_bot_username="b", _env_file=None
    )
    assert settings.memory_sweep_wait_seconds == 60
    monkeypatch.setenv("MEMORY_SWEEP_WAIT_SECONDS", "7")
    settings = Settings(
        telegram_bot_token="t", telegram_bot_username="b", _env_file=None
    )
    assert settings.memory_sweep_wait_seconds == 7


async def test_first_turn_without_rotation_is_not_delayed(
    db, memory_repo, service, composer, tmp_path
):
    """added: no rotation pending -> no wait (cold start, first ever window)."""
    await _stale_pair(db)
    provider = _provider(db, memory_repo, composer, sweep_wait_seconds=30)
    provider.attach_consolidation(service)
    model = AsyncMock(return_value=PROPOSAL)
    with patch(PATCH, model):
        started = time.monotonic()
        bundle = await provider.get_or_materialize(tmp_path / "ws", user_id=1)
        assert time.monotonic() - started < 2.0
    assert bundle is not None
    assert model.await_count == 0  # no sweep without a rotation


async def test_rotation_sweep_runs_after_the_final_eval(service, db, memory_repo):
    order = []

    async def final_eval() -> bool:
        await asyncio.sleep(0.05)
        order.append("eval")
        return True

    async def model(*args, **kwargs):
        order.append("sweep")
        return PROPOSAL

    await _stale_pair(db)
    with patch(PATCH, AsyncMock(side_effect=model)):
        service.after_rotation("devops", final_eval())
        await service.wait_rotation("devops", timeout=5)
    assert order == ["eval", "sweep"]


async def test_failed_final_eval_still_sweeps(service, db, memory_repo):
    """added: an extraction failure must not cost the window its sweep."""

    async def final_eval() -> bool:
        raise RuntimeError("extraction failed")

    await _stale_pair(db)
    with patch(PATCH, AsyncMock(return_value=PROPOSAL)):
        service.after_rotation("devops", final_eval())
        await service.wait_rotation("devops", timeout=5)
    assert (await memory_repo.get_fact(1295, "devops")).superseded_by == 1298


# --- rotation call sites hand the final eval to after_rotation ----------


class _FakeConsolidation:
    def __init__(self):
        self.calls = []

    def after_rotation(self, scope, final_eval):
        self.calls.append(scope)
        self._pending = asyncio.ensure_future(final_eval)


class _FakeEvaluator:
    def __init__(self):
        self.runs = []

    async def run(self, **kwargs):
        self.runs.append(kwargs)
        return True


@pytest.fixture
async def window_repo():
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (7)")
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES ('s', 7, '/p')"
        )
        await conn.commit()
    yield WindowRepository(db)
    await db.close()


async def test_refresh_window_hands_final_eval_to_after_rotation(window_repo):
    from src.bot.orchestration.windowing import refresh_window

    consolidation = _FakeConsolidation()
    evaluator = _FakeEvaluator()
    await refresh_window(
        window_repo,
        evaluator,
        {},
        user_id=7,
        working_dir="/p",
        backend="claude",
        conversation_text="User: hi\n\nAssistant: hello",
        agent_id="devops",
        consolidation=consolidation,
    )
    assert consolidation.calls == ["devops"]
    await consolidation._pending
    assert len(evaluator.runs) == 1
    assert evaluator.runs[0]["agent_id"] == "devops"
    assert (await window_repo.get_or_create(7, "/p")).window_id == 2


async def test_reset_window_hands_final_eval_to_after_rotation(window_repo):
    from src.bot.orchestration.windowing import reset_window

    consolidation = _FakeConsolidation()
    evaluator = _FakeEvaluator()
    await reset_window(
        window_repo,
        evaluator,
        {},
        user_id=7,
        working_dir="/p",
        backend="claude",
        conversation_text="User: hi\n\nAssistant: hello",
        agent_id="coach",
        consolidation=consolidation,
    )
    assert consolidation.calls == ["coach"]
    await consolidation._pending
    assert len(evaluator.runs) == 1
    assert evaluator.runs[0]["session_id"].endswith(":reset")
