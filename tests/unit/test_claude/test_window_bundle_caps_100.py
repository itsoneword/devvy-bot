"""dv-32fe: the 100/100 default caps and the 40k facts budget, on the scope
the test provider actually composes for (no profile registry -> devops)."""

import pytest

from src.claude.prompt_composer import PromptComposer
from src.claude.window_bundle import WindowBundleProvider
from src.config.settings import Settings
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository
from src.storage.window_bundle_repository import WindowBundleRepository
from src.storage.window_repository import WindowRepository


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "bundle.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
def settings(monkeypatch):
    for name in (
        "MEMORY_SNAPSHOT_GENERAL_CAP",
        "MEMORY_POINTER_CAP",
        "BUNDLE_FACTS_BUDGET_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)
    return Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        _env_file=None,
    )


def _provider(db, tmp_path, settings):
    persona = tmp_path / "agent.md"
    persona.write_text("You are the test agent persona.", encoding="utf-8")
    return WindowBundleProvider(
        window_repo=WindowRepository(db),
        bundle_repo=WindowBundleRepository(db),
        memory_repo=MemoryRepository(db),
        profile_registry=None,
        default_composer=PromptComposer(memory_path=tmp_path, agent_md_path=persona),
        facts_budget_chars=settings.bundle_facts_budget_chars,
        general_cap=settings.memory_snapshot_general_cap,
        pointer_cap=settings.memory_pointer_cap,
    )


def test_defaults_are_100_100_and_40k(settings):
    assert settings.memory_snapshot_general_cap == 100
    assert settings.memory_pointer_cap == 100
    assert settings.bundle_facts_budget_chars == 40_000


async def test_gymbro_sized_scope_injects_100_and_valve_stays_quiet(
    db, tmp_path, settings
):
    """107 general ~345 ch + 3 pinned (gymbro, 31.10.26): 100 general go in,
    the 7 oldest are left to recall(), and ~36k counted chars stay under 40k."""
    repo = MemoryRepository(db)
    calls = []

    class FakeConsolidation:
        def request_run(self, scope, reason, snapshot_chars=None):
            calls.append((scope, reason, snapshot_chars))

    for i in range(3):
        await repo.add_fact("devops", "pinned", f"pin {i} " + "p" * 370)
    for i in range(107):
        await repo.add_fact("devops", "general", f"g{i:03d} " + "x" * 340)

    provider = _provider(db, tmp_path, settings)
    provider.attach_consolidation(FakeConsolidation())
    bundle = await provider.get_or_materialize(tmp_path / "ws", user_id=1)
    assert bundle is not None
    assert bundle.facts_count == 3 + 100
    assert "g106 " in bundle.text and "g007 " in bundle.text
    assert "g006 " not in bundle.text
    assert calls == []


async def test_100_pointers_injected(db, tmp_path, settings):
    repo = MemoryRepository(db)
    for i in range(110):
        await repo.add_fact(
            "devops",
            "general",
            f"Decision {i:03d}: long result note",
            kind="conversation_result",
            label=f"decision label {i:03d}",
        )
    bundle = await _provider(db, tmp_path, settings).get_or_materialize(
        tmp_path / "ws", user_id=1
    )
    assert bundle is not None
    assert bundle.pointers_count == 100
    assert "decision label 109" in bundle.text
    assert "decision label 010" in bundle.text
    assert "decision label 009" not in bundle.text
