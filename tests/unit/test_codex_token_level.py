"""Codex reports its context LEVEL from the rollout file, its FLOW as a delta.

`codex exec --json` puts the thread's running `total_token_usage` on
`turn.completed`. Reading that as the turn's context rolled the 150k window on
the first tool-using turn of every fresh window (2026-09-05, general profile:
turn.completed said 175,970 while the last model call saw 35,604). The level
lives only in `$CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl`.
"""

import json
import stat
from pathlib import Path

import pytest

from src.agent_backends.codex_cli import (
    CodexCliIntegration,
    InMemoryCodexSessionStore,
    read_rollout_token_info,
)
from src.config.settings import Settings

THREAD = "01a071c6-63d2-7160-b549-98065a37da42"


def _token_count(total_in: int, last_in: int) -> str:
    return json.dumps(
        {
            "timestamp": "2026-09-05T13:35:26.877Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": total_in, "output_tokens": 9},
                    "last_token_usage": {"input_tokens": last_in, "output_tokens": 3},
                    "model_context_window": 258400,
                },
            },
        }
    )


def _write_rollout(home: Path, thread: str, lines: list[str]) -> Path:
    day = home / "sessions" / "2026" / "09" / "05"
    day.mkdir(parents=True)
    path = day / f"rollout-2026-09-05T13-33-42-{thread}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_reader_returns_the_last_token_count_payload(tmp_path):
    _write_rollout(
        tmp_path,
        THREAD,
        [
            json.dumps({"type": "session_meta", "payload": {"id": THREAD}}),
            _token_count(19_685, 19_685),
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}),
            "not json {",
            _token_count(175_970, 35_604),
        ],
    )
    info = read_rollout_token_info(THREAD, home=tmp_path)
    assert info["last_token_usage"]["input_tokens"] == 35_604
    assert info["total_token_usage"]["input_tokens"] == 175_970
    assert info["model_context_window"] == 258400


def test_reader_is_none_without_a_rollout(tmp_path):
    assert read_rollout_token_info(THREAD, home=tmp_path) is None
    (tmp_path / "sessions").mkdir()
    assert read_rollout_token_info("other-thread", home=tmp_path) is None


@pytest.fixture
def settings():
    return Settings(telegram_bot_token="x", telegram_bot_username="x")


def _fake_codex(tmp_path: Path, total_in: int) -> Path:
    """`codex` stand-in: one thread id, turn.completed carries the running total."""
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'echo \'{"type":"thread.started","thread_id":"' + THREAD + "\"}'\n"
        'echo \'{"type":"turn.completed","usage":{"input_tokens":'
        + str(total_in)
        + ',"cached_input_tokens":136960,"output_tokens":4575}}\'\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


async def test_turn_reports_rollout_level_and_delta_flow(
    tmp_path, settings, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    _write_rollout(tmp_path / "home", THREAD, [_token_count(175_970, 35_604)])
    store = InMemoryCodexSessionStore()
    # Previous turn on the same thread ended at these totals.
    await store.record(
        7, tmp_path, THREAD, 0, 0, thread_total_input=100_000, thread_total_output=4_000
    )
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(_fake_codex(tmp_path, 175_970)),
        session_store=store,
    )
    resp = await integ.run_command(prompt="hi", working_directory=tmp_path, user_id=7)
    assert resp.context_tokens == 35_604  # the level, not 175,970
    assert resp.input_tokens == 75_970  # this turn's flow: total minus previous
    assert resp.output_tokens == 575
    info = await store.info_for(7, tmp_path)
    assert info["thread_total_input"] == 175_970
    assert info["cumulative_tokens"] == 75_970 + 575


async def test_turn_without_a_rollout_reports_unknown_level(
    tmp_path, settings, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(_fake_codex(tmp_path, 175_970)),
        session_store=InMemoryCodexSessionStore(),
    )
    resp = await integ.run_command(prompt="hi", working_directory=tmp_path, user_id=7)
    assert resp.context_tokens == 0  # unknown -> the window holds its last level
    assert resp.input_tokens == 175_970  # fresh thread: the total IS the delta
