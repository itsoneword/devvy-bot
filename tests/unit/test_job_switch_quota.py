"""The job switch's record helpers and its refusals (dv-d63d phase 4).

Complements tests/unit/test_limits_job_switch.py: what a tap refuses (no
login, no key, no model for the harness) writes nothing; the record helpers
keep other writers' keys; ``show_config routes`` names the switch.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.commands import info
from src.model_router import quota
from src.model_router.routes import describe_call_routes, process_call_routes


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("MODEL_ROUTES_PATH", str(tmp_path / "no-models.yaml"))
    return tmp_path


def _path() -> Path:
    from src.utils.paths import data_layout

    return data_layout().model_quota


def _query() -> MagicMock:
    query = MagicMock()
    query.from_user.id = 1
    query.edit_message_text = AsyncMock()
    return query


def _reply(query: MagicMock) -> str:
    return str(query.edit_message_text.call_args.args[0])


async def test_codex_without_a_login_is_refused_and_writes_nothing():
    query = _query()
    await info.handle_limits_callback(query, MagicMock(), "limits:jobs:openai_codex")
    assert "Не переключено" in _reply(query) and "Login" in _reply(query)
    assert not _path().exists()


async def test_openrouter_without_a_key_is_refused(monkeypatch):
    monkeypatch.setattr("src.model_router.routes.openrouter_key", lambda: None)
    query = _query()
    await info.handle_limits_callback(query, MagicMock(), "limits:jobs:openrouter")
    assert "/setkey openrouter" in _reply(query)
    assert quota.job_route_override() is None


async def test_openrouter_with_no_job_model_is_refused(monkeypatch):
    """The built-in jobs table has no OpenRouter model: the tap says where to
    set one instead of sending jobs off with no model."""
    monkeypatch.setattr("src.model_router.routes.openrouter_key", lambda: "k")
    query = _query()
    await info.handle_limits_callback(query, MagicMock(), "limits:jobs:openrouter")
    assert "jobs.default.models.openrouter" in _reply(query)
    assert not _path().exists()


async def test_openrouter_with_a_job_model_switches(root, monkeypatch):
    table = root / "models.yaml"
    table.write_text(
        "jobs:\n  default: {models: {openrouter: z-ai/glm-5.3}}\n", encoding="utf-8"
    )
    monkeypatch.setenv("MODEL_ROUTES_PATH", str(table))
    monkeypatch.setattr("src.model_router.routes.openrouter_key", lambda: "k")
    query = _query()
    await info.handle_limits_callback(query, MagicMock(), "limits:jobs:openrouter")
    assert quota.job_route_override() == "openrouter"
    job = process_call_routes().resolve_job("pump_review")
    assert (job.harness, job.provider, job.model) == (
        "claude",
        "openrouter",
        "z-ai/glm-5.3",
    )
    assert "OpenRouter" in _reply(query)


async def test_claude_tap_pins_every_job_on_claude_over_a_codex_row(root, monkeypatch):
    table = root / "models.yaml"
    table.write_text(
        "jobs:\n  pump_review: {harness: openai_codex}\n", encoding="utf-8"
    )
    monkeypatch.setenv("MODEL_ROUTES_PATH", str(table))
    assert process_call_routes().resolve_job("pump_review").harness == "openai_codex"
    await info.handle_limits_callback(_query(), MagicMock(), "limits:jobs:claude")
    job = process_call_routes().resolve_job("pump_review")
    assert (job.harness, job.model) == ("claude", "sonnet")


def test_clearing_an_absent_switch_writes_no_file():
    quota.set_job_route(None, "owner")
    assert not _path().exists()


def test_set_job_route_rejects_an_unknown_harness():
    with pytest.raises(ValueError):
        quota.set_job_route("gemini", "owner")
    assert not _path().exists()


def test_a_record_update_keeps_the_job_switch():
    """Another writer of the same file (dv-7ad9's limit record) going through
    update_record keeps job_route, and vice versa."""
    quota.set_job_route("openai_codex", "owner")
    quota.update_record(lambda body: body.update(kind="weekly limit"))
    body = json.loads(_path().read_text(encoding="utf-8"))
    assert body["kind"] == "weekly limit"
    assert quota.job_route_override() == "openai_codex"
    quota.set_job_route(None, "owner")
    assert json.loads(_path().read_text(encoding="utf-8")) == {"kind": "weekly limit"}


def test_describe_names_the_switch_only_while_it_is_on():
    assert not any("switch" in ln for ln in describe_call_routes(process_call_routes()))
    quota.set_job_route("openai_codex", "owner")
    lines = describe_call_routes(process_call_routes())
    switch = [ln for ln in lines if ln.startswith("jobs switch: openai_codex ")]
    # Points at the menu switch, not the /limits buttons dv-f6af removed.
    assert switch and "🧠 Модели" in switch[0] and "/limits" not in switch[0]
    assert "job monitor: openai_codex / openai / openai-codex/gpt-6-sol" in lines
