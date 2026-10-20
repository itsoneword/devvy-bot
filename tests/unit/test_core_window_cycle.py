"""dv-5206 phase 2: core window-roll + evaluator mechanics, end to end, at
small thresholds.

Every turn goes through the real ``MessageOrchestrator.agentic_text`` (and
``/new`` through ``agentic_new``) against a real SQLite ``WindowRepository``
built with ``refresh_threshold_tokens=2_000`` and ``eval_interval_tokens=0``
(the live default since 0382a6c). The backend is a fake ``run_command`` that
reports a chosen context level per turn; ``save_claude_interaction`` writes a
real ``messages`` row so conversation text and the checkpoint cursor see each
turn. The only double is the recording evaluator.

The point is the WIRING in ``turns.py::_account_window`` — context level from
the response -> ``account_turn_and_maybe_eval(force_eval=...)`` ->
``should_refresh`` -> ``refresh_window`` — which the helper-level tests in
``test_orchestrator_window_accounting.py`` do not exercise. Each test here
fails if one of those triggers regresses.

Settings cannot carry a 2k threshold (``ge=20_000``), so the repo is built
directly, as ``main.py`` does from Settings.
"""

from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from tests.unit.window_harness import (
    USER_ID,
    _drain,
    _Eval,
    _harness,
    _make_repo,
    _mock_response,
    _real_message_saver,
    _seed_conversation,
)

REFRESH = 2_000


class _Cycle:
    """A user's conversation driven turn by turn through the real pipeline."""

    def __init__(self, tmp_path: Path, repo: Any, ev: _Eval, settings: Any) -> None:
        self.repo = repo
        self.ev = ev
        self.wd = str(Path(tmp_path))
        self.settings = settings
        self.seen: List[dict] = []
        self._next_tokens = 0

        async def run_command(**kw: Any) -> MagicMock:
            self.seen.append(
                {
                    "prompt": kw["prompt"],
                    "force_new": kw["force_new"],
                    "window_id": (await repo.get_or_create(USER_ID, self.wd)).window_id,
                }
            )
            return _mock_response(context_tokens=self._next_tokens)

        self.orchestrator = MessageOrchestrator(
            settings, {"claude_integration": MagicMock()}
        )
        self.update, self.context = _harness(
            settings,
            repo,
            ev,
            AsyncMock(side_effect=run_command),
            save_interaction=_real_message_saver(repo),
        )

    async def turn(self, text: str, context_tokens: int) -> dict:
        """One user message; the backend reports ``context_tokens`` after it."""
        self._next_tokens = context_tokens
        self.update.message.text = text
        self.update.message.message_id += 1
        await self.orchestrator.agentic_text(self.update, self.context)
        await _drain()
        return self.seen[-1]

    async def new(self) -> None:
        """/new through the real command handler, same user_data/bot_data."""
        new_update = MagicMock()
        new_update.effective_user.id = USER_ID
        new_update.message.reply_text = AsyncMock()
        await self.orchestrator.agentic_new(new_update, self.context)
        await _drain()

    async def state(self) -> Any:
        return await self.repo.get_or_create(USER_ID, self.wd)

    async def max_message_id(self) -> int:
        async with self.repo._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(message_id), 0) FROM messages WHERE user_id=?",
                (USER_ID,),
            )
            row = await cur.fetchone()
        return int(row[0])

    def runs(self, suffix: Optional[str] = None) -> List[dict]:
        calls = self.ev.calls
        if suffix is None:
            return list(calls)
        return [c for c in calls if c["session_id"].endswith(suffix)]


async def _cycle(
    tmp_path: Path, *, eval_interval: int = 0, result: bool = True
) -> _Cycle:
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    repo = await _make_repo(eval_interval=eval_interval, refresh=REFRESH)
    ev = _Eval(result=result)
    c = _Cycle(tmp_path, repo, ev, settings)
    await _seed_conversation(repo, USER_ID, c.wd)
    return c


# --- 1. interval 0 (live default): below the roll, nothing fires -------------


async def test_turns_below_threshold_never_run_evaluator_or_roll(tmp_path):
    c = await _cycle(tmp_path)
    assert c.repo.eval_interval_tokens == 0

    for tokens in (500, 1_000, 1_500):
        seen = await c.turn("how is the build going?", tokens)
        assert seen["force_new"] is False
        assert seen["prompt"] == "how is the build going?"  # no seed

    assert c.ev.calls == [], "interval 0 must never fire a checkpoint run"
    st = await c.state()
    assert st.window_id == 1
    assert st.last_context_tokens == 1_500
    assert st.force_new_session is False
    assert st.pending_seed_window is None
    assert st.last_checkpoint_message_id == 0


# --- 2. crossing 2k: one roll, one :refresh run, seed on the next turn -------


async def test_crossing_threshold_rolls_once_with_one_refresh_run_and_seeds(
    tmp_path,
):
    c = await _cycle(tmp_path)
    await c.turn("step one", 500)
    await c.turn("step two", 1_500)
    crossing = await c.turn("step three", 2_100)
    assert crossing["window_id"] == 1 and crossing["force_new"] is False

    # Exactly one roll, armed durably for the next turn.
    st = await c.state()
    assert st.window_id == 2
    assert st.force_new_session is True
    assert st.pending_seed_window == 1

    # Exactly one evaluator run, and it is the :refresh one over window 1.
    assert len(c.ev.calls) == 1
    run = c.ev.calls[0]
    assert run["session_id"].endswith(":refresh")
    assert run["since_message_id"] == 0
    assert "weather at caparica" in run["conversation_text"]
    assert "step three" in run["conversation_text"]

    # Next turn (fresh, small session): consumes the seed, no further run,
    # no second roll.
    nxt = await c.turn("and now?", 400)
    assert nxt["force_new"] is True
    assert nxt["window_id"] == 2
    assert "<conversation_context>" in nxt["prompt"]
    assert "step three" in nxt["prompt"]
    assert nxt["prompt"].rstrip().endswith("and now?")

    assert len(c.ev.calls) == 1
    after = await c.state()
    assert after.window_id == 2
    assert after.force_new_session is False
    assert after.pending_seed_window is None
    assert after.last_context_tokens == 400

    # And the turn after that resumes normally: no seed, no fresh session.
    plain = await c.turn("thanks", 600)
    assert plain["force_new"] is False
    assert plain["prompt"] == "thanks"
    assert len(c.ev.calls) == 1
    assert (await c.state()).window_id == 2


# --- 3. "remember" below 2k: one forced run; cursor only on success ----------


@pytest.mark.parametrize("text", ["remember: my bike is blue", "запомни: байк синий"])
async def test_remember_below_threshold_forces_one_run_and_advances_cursor(
    tmp_path, text
):
    c = await _cycle(tmp_path, result=True)
    await c.turn("hello", 500)
    assert c.ev.calls == []

    await c.turn(text, 600)

    assert len(c.ev.calls) == 1
    run = c.ev.calls[0]
    assert run["session_id"] == f"{USER_ID}:{c.wd}:1"  # window run, not :refresh
    assert run["since_message_id"] == 0
    assert text in run["conversation_text"]

    st = await c.state()
    assert st.window_id == 1, "a forced run must not roll the window"
    assert st.force_new_session is False
    assert st.last_checkpoint_message_id == await c.max_message_id()
    assert st.last_checkpoint_message_id > 0

    # The next forced run starts after the cursor: the old slice is not
    # re-sent.
    await c.turn("remember: the van is red", 700)
    assert len(c.ev.calls) == 2
    second = c.ev.calls[1]
    assert second["since_message_id"] == st.last_checkpoint_message_id
    assert "the van is red" in second["conversation_text"]
    assert text not in second["conversation_text"]


async def test_remember_failed_run_leaves_cursor_in_place(tmp_path):
    c = await _cycle(tmp_path, result=False)
    await c.turn("hello", 500)
    await c.turn("remember: my bike is blue", 600)

    assert len(c.ev.calls) == 1
    st = await c.state()
    assert st.last_checkpoint_message_id == 0, "failed run must not advance"
    assert st.window_id == 1

    # The un-extracted slice is offered again on the next forced run.
    await c.turn("remember: the van is red", 700)
    assert len(c.ev.calls) == 2
    retry = c.ev.calls[1]
    assert retry["since_message_id"] == 0
    assert "my bike is blue" in retry["conversation_text"]
    assert "the van is red" in retry["conversation_text"]
    assert (await c.state()).last_checkpoint_message_id == 0


# --- 4. "remember" on the crossing turn: still exactly one run --------------


async def test_remember_on_crossing_turn_runs_only_the_refresh(tmp_path):
    c = await _cycle(tmp_path)
    await c.turn("step one", 1_000)
    await c.turn("remember: deploy on fridays is banned", 2_100)

    assert len(c.ev.calls) == 1, "crossing + remember must not extract twice"
    run = c.ev.calls[0]
    assert run["session_id"].endswith(":refresh")
    assert "deploy on fridays is banned" in run["conversation_text"]

    st = await c.state()
    assert st.window_id == 2
    assert st.force_new_session is True
    assert st.pending_seed_window == 1


# --- 5. /new mid-sequence: one :reset run, roll, next turn unseeded ----------


async def test_new_mid_sequence_resets_once_and_next_turn_is_unseeded(tmp_path):
    c = await _cycle(tmp_path)
    await c.turn("step one", 500)
    await c.turn("step two", 1_000)
    assert c.ev.calls == []

    await c.new()

    assert len(c.ev.calls) == 1
    run = c.ev.calls[0]
    assert run["session_id"].endswith(":reset")
    assert "step two" in run["conversation_text"]
    st = await c.state()
    assert st.window_id == 2
    assert st.force_new_session is True
    assert st.pending_seed_window is None

    nxt = await c.turn("fresh topic", 300)
    assert nxt["force_new"] is True
    assert nxt["window_id"] == 2
    assert nxt["prompt"] == "fresh topic", "/new must not carry a seed"
    assert "<conversation_context>" not in nxt["prompt"]

    # No further run and no second roll from the post-/new turn.
    assert len(c.ev.calls) == 1
    after = await c.state()
    assert after.window_id == 2
    assert after.force_new_session is False


# --- added ----------------------------------------------------------------


async def test_nonzero_interval_fires_once_per_bucket(tmp_path):
    """eval_interval=500: crossing 500 and 1k fire one run each; staying in
    the same bucket (1.2k after 1.1k) fires nothing more."""
    c = await _cycle(tmp_path, eval_interval=500)
    await c.turn("a", 400)
    assert c.ev.calls == []
    await c.turn("b", 600)
    assert len(c.ev.calls) == 1
    await c.turn("c", 1_100)
    assert len(c.ev.calls) == 2
    await c.turn("d", 1_200)
    assert len(c.ev.calls) == 2
    assert all(call["session_id"] == f"{USER_ID}:{c.wd}:1" for call in c.ev.calls)
    st = await c.state()
    assert st.window_id == 1
    assert st.last_eval_checkpoint == 2


async def test_remember_after_roll_reads_only_the_new_window(tmp_path):
    """After a roll, a forced run is scoped to the new window: the old
    window's turns are not re-extracted under the new window id."""
    c = await _cycle(tmp_path)
    await c.turn("old window talk", 2_100)
    assert len(c.ev.calls) == 1  # the :refresh run
    await c.turn("first in new window", 400)
    await c.turn("remember: the cat is called Miso", 500)

    assert len(c.ev.calls) == 2
    run = c.ev.calls[1]
    assert run["session_id"] == f"{USER_ID}:{c.wd}:2"
    assert "the cat is called Miso" in run["conversation_text"]
    assert "old window talk" not in run["conversation_text"]
    assert "weather at caparica" not in run["conversation_text"]
