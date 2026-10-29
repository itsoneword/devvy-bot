"""/limits shows live Claude subscription usage (dv-55ad).

The CLI emits a ``rate_limit_event`` on every turn (probe 2026-10-26, CLI
2.1.310) whose ``rate_limit_info["unifiedWindows"]`` carries utilization and
reset per window. The latest one of a turn is recorded once, after the stream
(in the ``finally`` of the function owning it), into the shared record
(``DataLayout.model_quota``, key of quota.py's choosing, via
``quota.update_record``). agentjob cannot write (read-only box): it prints a
usage line on stderr and ``bgjob summarize`` records it, whatever the exit.

Contract pinned here (docs/research/dv-55ad/analysis.md, Revision 2):

* ``quota.record_usage(info, source, observed_at)`` — ``info`` is the raw
  ``rate_limit_info`` dict; newest ``observed_at`` wins; own keys only.
* ``quota.claude_limits_text(now=None) -> str`` — the panel's Claude block,
  no bot context, no LLM (dv-6104 reuses it). ``/limits`` shows it.
* Rendering: "N%" per window, its reset HH:MM, "на HH:MM"; no snapshot ->
  "… ещё нет …"; a window whose reset passed -> "сброшено"; a snapshot older
  than 1 h -> "устарели".

The data root is a tmp dir (``root`` fixture of test_limits_panel, autouse
there, re-exported here).
"""

import asyncio
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import ResultMessage

from src.bot.orchestrator import MessageOrchestrator
from tests.unit.test_bgjob import bgjob  # noqa: F401  (fixture)
from tests.unit.test_limits_panel import (  # noqa: F401  (fixtures)
    _backends,
    _command_update,
    _context,
    _record_limit,
    _sent,
    _settings,
    root,
    spawned,
)
from tests.unit.test_model_router_job import _multi_turn_job, _result

STALE_WORDS = ("устар", "stale")
RESET_WORDS = ("сброшен", "reset passed")


# --- builders -----------------------------------------------------------------


def _epoch(at: datetime) -> int:
    return int(at.timestamp())


def _info(
    five: float = 0.22,
    week: float = 0.14,
    five_reset: Optional[datetime] = None,
    week_reset: Optional[datetime] = None,
    opus: Optional[float] = None,
) -> Dict[str, Any]:
    """A raw ``rate_limit_info`` shaped like the 2026-10-26 probe."""
    now = datetime.now(UTC)
    five_reset = five_reset or (now + timedelta(hours=2, minutes=7)).replace(
        second=0, microsecond=0
    )
    week_reset = week_reset or (now + timedelta(days=4, hours=3, minutes=41)).replace(
        second=0, microsecond=0
    )
    windows: Dict[str, Any] = {
        "five_hour": {
            "utilization": five,
            "resetsAt": _epoch(five_reset),
            "status": "allowed",
        },
        "seven_day": {
            "utilization": week,
            "resetsAt": _epoch(week_reset),
            "status": "allowed",
        },
    }
    if opus is not None:
        windows["seven_day_opus"] = {
            "utilization": opus,
            "resetsAt": _epoch(week_reset),
            "status": "allowed",
        }
    return {
        "status": "allowed",
        "resetsAt": _epoch(five_reset),
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": windows,
    }


def _raw_event(info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": info,
        "uuid": "rl-1",
        "session_id": "sess-usage",
    }


def _sdk_event(info: Dict[str, Any]) -> Any:
    from claude_agent_sdk._internal.message_parser import parse_message

    return parse_message(_raw_event(info))


def _ok_result(session_id: str = "sess-usage") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=50,
        duration_api_ms=40,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=0.01,
        usage={"input_tokens": 12, "output_tokens": 7},
        result="ok",
    )


def _parse(x: Any) -> Any:
    """Real parsing for raw dicts (the CLI's wire form), objects as they are."""
    from claude_agent_sdk._internal.message_parser import parse_message

    return parse_message(x) if isinstance(x, dict) else x


def _client_factory(stream):
    """A fake ClaudeSDKClient whose raw stream is the async generator
    ``stream()`` (the chat turn reads ``client._query.receive_messages``)."""

    def factory(options: Any) -> AsyncMock:
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        client._query = AsyncMock()
        client._query.receive_messages = stream
        return client

    return factory


async def _chat_turn(tmp_path: Path, stream) -> Any:
    """One real ClaudeSDKManager turn on the subscription, SDK client faked."""
    from src.claude.sdk_integration import ClaudeSDKManager
    from src.config.settings import Settings

    config = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_timeout_seconds=5,
        enable_mcp=False,
    )
    manager = ClaudeSDKManager(config)
    with (
        patch("src.claude.sdk_integration.parse_message", side_effect=_parse),
        patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_client_factory(stream),
        ),
    ):
        return await manager.execute_command(prompt="hello", working_directory=tmp_path)


async def _wait_recorded(needle: str = "0.22", seconds: float = 5.0) -> str:
    """The usage write is handed to a worker thread and not awaited: wait
    until the record file carries ``needle``. Returns the file text."""
    from src.utils.paths import data_layout

    path = data_layout().model_quota
    deadline = time.monotonic() + seconds
    text = ""
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if needle in text:
            return text
        await asyncio.sleep(0.05)
    return text


async def _panel(tmp_path: Path) -> str:
    settings = _settings(tmp_path)
    backends = _backends()
    orch = MessageOrchestrator(settings, {})
    update = _command_update()
    await orch.agentic_limits(update, _context(settings, {"backends": backends}))
    # The panel never calls a model.
    for backend in backends.values():
        backend.run_command.assert_not_awaited()
    backends["claude"].read_account_limits.assert_not_awaited()
    text, _ = _sent(update)
    return text


def _claude_block(text: str) -> str:
    """The panel without the Codex block (which has its own "N%" lines)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "codex" in line.lower() and i > 0:
            return "\n".join(lines[:i])
    return text


def _hhmm(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%H:%M")


# --- scenario 1: after a Claude turn ------------------------------------------


async def test_after_a_claude_turn_limits_shows_5h_and_weekly_percent_resets_and_as_of(
    tmp_path, spawned
):
    """Scenario: after a Claude chat turn /limits shows the 5-hour and weekly
    utilization %, each window's reset time and "на HH:MM"; an Opus weekly
    window is shown when the CLI sends one."""
    now = datetime.now(UTC)
    five_reset = (now + timedelta(hours=2, minutes=7)).replace(second=0, microsecond=0)
    week_reset = (now + timedelta(days=4, hours=3, minutes=41)).replace(
        second=0, microsecond=0
    )
    info = _info(0.22, 0.14, five_reset, week_reset, opus=0.37)

    async def stream():
        yield _raw_event(info)
        yield _ok_result()

    response = await _chat_turn(tmp_path, stream)
    assert not response.is_error
    assert "0.22" in await _wait_recorded(), "the turn recorded no usage snapshot"

    block = _claude_block(await _panel(tmp_path))
    assert "22%" in block, block
    assert "14%" in block, block
    assert "37%" in block and "opus" in block.lower(), block
    assert _hhmm(five_reset) in block, block
    assert _hhmm(week_reset) in block, block
    assert re.search(r"на \d{1,2}:\d{2}", block), block
    # Fresh data is not labelled stale.
    assert not any(w in block.lower() for w in STALE_WORDS), block


# --- scenario 2: agentjob run (pump / bgjob) ----------------------------------


def _summarize_job(bgjob, jobs: Path, name: str, stderr: str, rc: int) -> None:
    jid = f"20261027-0900{len(list(jobs.glob('*'))):02d}-{name}"
    d = jobs / jid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": jid, "name": name}))
    (d / "output.md").write_text("done")
    (d / "stderr.log").write_text(stderr)
    (d / "exit_code").write_text(f"{rc}\n")
    assert bgjob.cmd_summarize(type("A", (), {"id": jid})()) == 0


async def test_agentjob_run_on_claude_updates_the_percent_in_limits_also_on_exit_0(
    bgjob, tmp_path, monkeypatch, capsys, spawned
):
    """Scenario: a pump/bgjob agentjob run on Claude updates the % /limits
    shows once bgjob summarizes it — a job that exits 0, and a later one
    replaces an earlier one's numbers."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)

    # First job: 50 % / 30 %, exits 0.
    run, _, _, _ = _multi_turn_job(
        monkeypatch, [[_sdk_event(_info(0.50, 0.30)), _result("all done")]]
    )
    assert await run() == 0
    err = capsys.readouterr().err
    _summarize_job(bgjob, jobs, "pump-dv-1111-build", err, 0)
    block = _claude_block(await _panel(tmp_path))
    assert "50%" in block and "30%" in block, (block, err)

    # Second, later job: 22 % / 14 %, also exit 0 -> the panel moves.
    run, _, _, _ = _multi_turn_job(
        monkeypatch, [[_sdk_event(_info(0.22, 0.14)), _result("all done")]]
    )
    assert await run() == 0
    err = capsys.readouterr().err
    _summarize_job(bgjob, jobs, "nightly-review", err, 0)
    block = _claude_block(await _panel(tmp_path))
    assert "22%" in block and "14%" in block, (block, err)
    assert "50%" not in block, block


# --- scenario 3: no snapshot yet ----------------------------------------------


async def test_no_snapshot_panel_says_no_usage_data_yet_and_still_shows_limit_hit(
    tmp_path, spawned
):
    """Scenario: no usage snapshot recorded yet -> the Claude block says there
    is no usage data yet; an active limit-hit record is still shown with its
    reset time. No LLM call (``_panel`` asserts it)."""
    from src.model_router import quota

    until = _record_limit()

    block = _claude_block(await _panel(tmp_path))
    low = block.lower()
    assert "ещё нет" in low or "еще нет" in low, block
    assert _hhmm(until) in block, block
    # The renderer alone, with no bot context (dv-6104 calls it this way).
    alone = quota.claude_limits_text()
    assert isinstance(alone, str)
    assert "ещё нет" in alone.lower() or "еще нет" in alone.lower(), alone
    assert _hhmm(until) in alone, alone


# --- scenario 4: stale data ---------------------------------------------------


async def test_snapshot_older_than_one_hour_is_marked_stale_in_limits(
    tmp_path, spawned
):
    """Scenario: a snapshot observed more than 1 h ago is labelled stale in
    /limits, not shown as current."""
    from src.model_router import quota

    observed = datetime.now(UTC) - timedelta(hours=2)
    quota.record_usage(_info(0.22, 0.14), "claude-turn", observed)

    block = _claude_block(await _panel(tmp_path))
    assert "22%" in block, block
    assert any(w in block.lower() for w in STALE_WORDS), block


def test_window_whose_reset_passed_is_marked_reset_not_current(root):
    """Scenario: the 5-hour window's reset time has passed since the
    snapshot -> that window is labelled reset/stale; the weekly window whose
    reset is still ahead is shown as before."""
    from src.model_router import quota

    observed = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
    five_reset = datetime(2026, 10, 27, 8, 30, tzinfo=UTC)
    week_reset = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    quota.record_usage(
        _info(0.22, 0.14, five_reset, week_reset), "claude-turn", observed
    )

    now = datetime(2026, 10, 27, 8, 50, tzinfo=UTC)  # 50 min later: < 1 h
    text = quota.claude_limits_text(now=now)
    low = text.lower()
    assert any(w in low for w in RESET_WORDS), text
    assert "14%" in text, text
    reset_lines = [
        ln for ln in text.splitlines() if any(w in ln.lower() for w in RESET_WORDS)
    ]
    assert all("14%" not in ln for ln in reset_lines), text


# --- scenario 5: interrupted / failed turn, no write inside the loop ----------


async def test_failed_turn_still_records_usage_once_and_never_inside_the_stream(
    tmp_path, spawned
):
    """Scenario: a turn whose stream dies after the rate_limit_event still
    records the snapshot (from the finally); no record write happens while the
    stream is still being read."""
    from src.model_router import quota

    real_update = quota.update_record
    writes: List[bool] = []  # True = written while the stream was still open
    stream_open = [True]

    def spy(change):
        writes.append(stream_open[0])
        return real_update(change)

    async def stream():
        yield _raw_event(_info(0.22, 0.14))
        # Give any eager writer a chance to run while the stream is open.
        await asyncio.sleep(0.3)
        yield {"type": "system", "subtype": "status", "status": "working"}
        await asyncio.sleep(0.1)
        stream_open[0] = False
        raise RuntimeError("CLI stdout closed mid-turn")

    with patch.object(quota, "update_record", side_effect=spy):
        try:
            await _chat_turn(tmp_path, stream)
        except Exception:
            pass  # the failure itself is not under test
        text = await _wait_recorded()

    assert "0.22" in text, "a failed turn lost its usage snapshot"
    assert writes, "no record write at all"
    assert not any(writes), "the record was written inside the stream loop"
    assert writes.count(False) == 1, writes
    assert "22%" in quota.claude_limits_text()


async def test_timed_out_plain_claude_call_still_records_usage(root):
    """Scenario (plain calls, claude_plain): a call cancelled by its timeout
    after the rate_limit_event still records the snapshot."""
    from src.model_router import quota
    from src.model_router.claude_plain import ClaudePlainCaller
    from src.model_router.types import CallRequest, ModelCallError

    event = _sdk_event(_info(0.22, 0.14))

    class FakeClient:
        def __init__(self, *_: object) -> None: ...

        async def connect(self) -> None: ...

        async def disconnect(self) -> None: ...

        async def query(self, *_: object, **__: object) -> None: ...

        async def receive_response(self):
            yield event
            await asyncio.sleep(30)  # the CLI goes quiet; the timeout fires
            yield _ok_result()

    request = CallRequest(
        purpose="test",
        prompt="hi",
        system_prompt="",
        model="haiku",
        timeout_seconds=0.3,
    )
    with patch("src.model_router.claude_plain.ClaudeSDKClient", FakeClient):
        with pytest.raises(ModelCallError):
            await ClaudePlainCaller()(request)
    text = await _wait_recorded()
    assert "0.22" in text, "a timed-out plain call lost its usage snapshot"
    assert "22%" in quota.claude_limits_text()


# --- added --------------------------------------------------------------------


def test_older_snapshot_never_overwrites_newer_and_other_keys_are_kept(root):
    """added: newest observation wins whatever order the writes land in; the
    job switch and the limit-hit record in the same file are kept; an event
    without unifiedWindows (third-party route) records nothing."""
    from src.model_router import quota

    t_new = datetime.now(UTC) - timedelta(minutes=5)
    t_old = t_new - timedelta(minutes=30)
    until = _record_limit()
    quota.set_job_route(quota.JOB_SWITCH_HARNESSES[1], by="owner")
    route_before = quota.read_record()[quota.JOB_ROUTE_KEY]

    quota.record_usage(_info(0.22, 0.14), "claude-turn", t_new)
    quota.record_usage(_info(0.90, 0.80), "bgjob:late-reaper", t_old)
    no_windows = {k: v for k, v in _info(0.99, 0.99).items() if k != "unifiedWindows"}
    quota.record_usage(no_windows, "openrouter", datetime.now(UTC))

    text = quota.claude_limits_text()
    assert "22%" in text and "14%" in text, text
    assert "90%" not in text and "99%" not in text, text
    assert quota.read_record()[quota.JOB_ROUTE_KEY] == route_before
    got = quota.active()
    assert got is not None and got.until == until
