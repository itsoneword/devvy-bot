"""dv-586f: turns serialize per (user, agent); /stop is scoped to the chat's
agent; a turn whose CLI never delivers a ResultMessage is abandoned."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from src.bot.orchestration.commands.info import InfoCommandsMixin
from src.claude import sdk_integration
from src.claude.sdk_integration import ClaudeSDKManager, _ActiveRun
from src.config.settings import Settings
from src.utils.turn_locks import TurnLocks

# --- Part A: per-(user, agent) turn locks --------------------------------


async def test_different_profiles_run_concurrently_same_profile_serializes():
    locks = TurnLocks()
    running: list = []
    peak = {"n": 0}

    async def turn(profile: str) -> None:
        async with locks.hold(1, f"turn {profile}", profile):
            running.append(profile)
            peak["n"] = max(peak["n"], len(running))
            await asyncio.sleep(0.05)
            running.remove(profile)

    await asyncio.gather(turn("markets"), turn("devops"))
    assert peak["n"] == 2  # different agents overlap

    peak["n"] = 0
    await asyncio.gather(turn("markets"), turn("markets"))
    assert peak["n"] == 1  # same agent serializes


async def test_other_users_and_default_key_are_separate():
    locks = TurnLocks()
    assert locks.get(1, "a") is locks.get(1, "a")
    assert locks.get(1, "a") is not locks.get(1, "b")
    assert locks.get(1, "a") is not locks.get(2, "a")
    assert locks.get(1) is locks.get(1, None)


async def test_waiter_listed_per_profile_for_shutdown():
    locks = TurnLocks()
    await locks.get(1, "markets").acquire()
    locks.enqueue(1, 99, "text", "markets")
    assert [w.chat_id for w in locks.pending()] == [99]


# --- Part B: scoped /stop -------------------------------------------------


def _manager(tmp_path: Path) -> ClaudeSDKManager:
    return ClaudeSDKManager(
        Settings(
            telegram_bot_token="t:t",
            telegram_bot_username="b",
            approved_directory=tmp_path,
            claude_timeout_seconds=30,
            enable_mcp=False,
        )
    )


async def test_stop_turns_hits_only_that_user_and_profile(tmp_path):
    m = _manager(tmp_path)
    mk = _ActiveRun(client=AsyncMock(), user_id=1, profile="markets")
    dv = _ActiveRun(client=AsyncMock(), user_id=1, profile="devops")
    other = _ActiveRun(client=AsyncMock(), user_id=2, profile="markets")
    for r in (mk, dv, other):
        m._active_runs.setdefault((r.user_id, r.profile), []).append(r)

    assert await m.stop_turns(1, "markets") == 1
    mk.client.interrupt.assert_awaited_once()
    dv.client.interrupt.assert_not_called()
    other.client.interrupt.assert_not_called()
    assert m.is_stopped is False  # no killswitch

    assert await m.stop_turns(1, "nothing") == 0


async def test_stop_all_still_interrupts_everything_and_sets_killswitch(tmp_path):
    m = _manager(tmp_path)
    runs = [
        _ActiveRun(client=AsyncMock(), user_id=1, profile="markets"),
        _ActiveRun(client=AsyncMock(), user_id=2, profile="devops"),
    ]
    for r in runs:
        m._active_runs.setdefault((r.user_id, r.profile), []).append(r)
    assert await m.stop_all() == 2
    assert m.is_stopped is True


class _Cmds(InfoCommandsMixin):
    def __init__(self, slug):
        self._slug = slug

    async def _turn_profile_slug(self, update, context, text=None):
        return self._slug


def _update(text: str):
    u = MagicMock()
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.effective_user.id = 7
    return u


async def test_stop_command_scoped_to_chat_agent():
    backend = MagicMock()
    backend.stop_turns = AsyncMock(return_value=1)
    sdk = MagicMock()
    sdk.stop_all = AsyncMock(return_value=3)
    ctx = MagicMock()
    ctx.bot_data = {
        "backends": {"claude": backend},
        "claude_integration": MagicMock(sdk_manager=sdk),
    }
    update = _update("/stop")
    await _Cmds("markets").agentic_stop(update, ctx)
    backend.stop_turns.assert_awaited_once_with(7, "markets")
    sdk.stop_all.assert_not_called()
    assert "markets" in update.message.reply_text.call_args.args[0]


async def test_stop_command_nothing_running_and_all():
    backend = MagicMock()
    backend.stop_turns = AsyncMock(return_value=0)
    sdk = MagicMock()
    sdk.stop_all = AsyncMock(return_value=3)
    ctx = MagicMock()
    ctx.bot_data = {
        "backends": {"claude": backend},
        "claude_integration": MagicMock(sdk_manager=sdk),
    }
    update = _update("/stop")
    await _Cmds("devops").agentic_stop(update, ctx)
    assert "Nothing is running" in update.message.reply_text.call_args.args[0]

    update = _update("/stop all")
    await _Cmds("devops").agentic_stop(update, ctx)
    sdk.stop_all.assert_awaited_once()
    assert "Stopped all" in update.message.reply_text.call_args.args[0]


# --- Part C: bot-side bounds on the turn's tail ---------------------------


def _hanging_client(first=()):
    client = AsyncMock()

    async def receive():
        for m in first:
            yield m
        await asyncio.sleep(60)
        yield  # pragma: no cover

    client._query = AsyncMock()
    client._query.receive_messages = receive
    return client


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(sdk_integration, "_POLL_SECONDS", 0.02)
    monkeypatch.setattr(sdk_integration, "_INTERRUPT_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(sdk_integration, "_FINAL_RESULT_GRACE_SECONDS", 0.1)
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        yield


async def test_interrupted_run_is_abandoned_without_result(tmp_path, fast):
    m = _manager(tmp_path)
    client = _hanging_client()
    with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
        task = asyncio.create_task(
            m.execute_command(prompt="x", working_directory=tmp_path, user_id=5)
        )
        for _ in range(100):
            if m._active_runs:
                break
            await asyncio.sleep(0.01)
        (key,) = m._active_runs
        assert await m.stop_turns(*key) == 1
        resp = await asyncio.wait_for(task, 5)
    assert resp.error_type == "interrupted"
    assert m._active_runs == {}
    client.disconnect.assert_awaited()


async def test_missing_result_after_final_answer_is_abandoned(tmp_path, fast):
    m = _manager(tmp_path)
    final = AssistantMessage(
        content=[TextBlock(text="the answer")],
        model="m",
        stop_reason="end_turn",
    )
    client = _hanging_client([final])
    with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
        resp = await asyncio.wait_for(
            m.execute_command(prompt="x", working_directory=tmp_path, user_id=5), 5
        )
    assert "the answer" in resp.content
    assert m._active_runs == {}


async def test_hung_stream_callback_does_not_block_the_stream(
    tmp_path, fast, monkeypatch
):
    monkeypatch.setattr(sdk_integration, "_STREAM_CALLBACK_TIMEOUT_SECONDS", 0.05)
    from tests.unit.test_claude.test_sdk_integration import _make_result_message

    m = _manager(tmp_path)
    client = AsyncMock()

    async def receive():
        yield AssistantMessage(content=[TextBlock(text="hi")], model="m")
        yield _make_result_message()

    client._query = AsyncMock()
    client._query.receive_messages = receive

    async def hung(_update):
        await asyncio.sleep(60)

    with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
        resp = await asyncio.wait_for(
            m.execute_command(
                prompt="x",
                working_directory=tmp_path,
                user_id=5,
                stream_callback=hung,
            ),
            5,
        )
    assert resp.content == "Success"


async def test_turn_profile_slug_topic_then_active_profile():
    from src.bot.orchestration.base import OrchestratorBase

    base = OrchestratorBase.__new__(OrchestratorBase)
    registry = MagicMock()
    registry.get_default.return_value = MagicMock(slug="devops")
    registry.enabled_profiles = [MagicMock(slug="devops"), MagicMock(slug="markets")]
    router = MagicMock()
    router.profile_for_message = AsyncMock(return_value="markets")
    ctx = MagicMock()
    ctx.bot_data = {"profile_registry": registry, "reports_router": router}
    ctx.user_data = {"active_profile": "devops"}
    update = MagicMock()
    update.message.is_topic_message = True

    assert await base._turn_profile_slug(update, ctx) == "markets"  # bound topic

    router.profile_for_message.return_value = None  # DM / unbound
    assert await base._turn_profile_slug(update, ctx) == "devops"
    assert await base._turn_profile_slug(update, ctx, "@markets hi") == "markets"
