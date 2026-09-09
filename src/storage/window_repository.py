"""Bot-owned conversation windows, keyed by (user_id, working_dir).

A window is the unit of conversation; the backend is an interchangeable
executor. Tracks token accounting and evaluator/compaction checkpoints so
memory and seeding work identically across Claude and Codex.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()

# Defaults in REAL context tokens (input + cache_read + cache_creation).
# Overridable per deployment via Settings — see window_eval_interval_tokens /
# window_refresh_threshold_tokens. The historical 50k/150k constants counted
# uncached input only, which on a cache-hitting deployment is ~7 tokens a
# call, so they actually fired at a real ~125k/~300k. These values reproduce
# that observed cadence honestly. See dv-9f31.
EVAL_INTERVAL_TOKENS = 120_000
REFRESH_THRESHOLD_TOKENS = 200_000

# Backends that can report the conversation's real size per turn, so a missing
# number from them means "not measurable this turn", not "no such thing".
# See record_turn. Codex joined on 2026-09-05: its level is read from the
# CLI's rollout file (agent_backends.codex_cli.read_rollout_token_info).
_LEVEL_REPORTING_BACKENDS = frozenset({"claude", "openai_codex"})

PathLike = Union[str, Path]

_COLUMNS = (
    "user_id, working_dir, window_id, active_backend, cumulative_tokens, "
    "last_eval_checkpoint, last_checkpoint_message_id, last_context_tokens, "
    "pending_seed_window, force_new_session"
)


@dataclass
class WindowState:
    user_id: int
    working_dir: str
    window_id: int
    active_backend: str
    # Running SUM of per-turn token flow. Useful for "how much has this
    # conversation cost/processed", useless as a size gauge — see
    # last_context_tokens for that.
    cumulative_tokens: int
    last_eval_checkpoint: int
    last_checkpoint_message_id: int
    # LEVEL: real context the model saw on the most recent turn. This is what
    # the eval and refresh thresholds compare against.
    last_context_tokens: int = 0
    # Set by a roll, consumed by the next turn. Persisted rather than kept in
    # PTB user_data so a restart between the two cannot silently cancel the
    # rotation (dv-9f31).
    pending_seed_window: Optional[int] = None
    force_new_session: bool = False

    def __post_init__(self) -> None:
        # SQLite has no bool; normalize the 0/1 INTEGER on the way out.
        self.force_new_session = bool(self.force_new_session)


class WindowRepository:
    """CRUD + threshold logic for conversation_windows."""

    def __init__(
        self,
        db: "DatabaseManager",
        eval_interval_tokens: int = EVAL_INTERVAL_TOKENS,
        refresh_threshold_tokens: int = REFRESH_THRESHOLD_TOKENS,
    ) -> None:
        self._db = db
        self.eval_interval_tokens = eval_interval_tokens
        self.refresh_threshold_tokens = refresh_threshold_tokens

    @staticmethod
    def _key(working_dir: PathLike) -> str:
        return str(Path(working_dir))

    async def get_or_create(self, user_id: int, working_dir: PathLike) -> WindowState:
        wd = self._key(working_dir)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} "
                "FROM conversation_windows WHERE user_id=? AND working_dir=?",
                (user_id, wd),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.execute(
                    "INSERT INTO conversation_windows "
                    "(user_id, working_dir, window_id, active_backend) "
                    "VALUES (?, ?, 1, 'claude')",
                    (user_id, wd),
                )
                await conn.commit()
                return WindowState(user_id, wd, 1, "claude", 0, 0, 0)
            return WindowState(*row)

    async def list_for_user(self, user_id: int) -> List[WindowState]:
        """Return all conversation windows for a user (read-only, no creation)."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM conversation_windows WHERE user_id=?",
                (user_id,),
            )
            rows = await cur.fetchall()
        return [WindowState(*row) for row in rows]

    async def record_turn(
        self,
        user_id: int,
        working_dir: PathLike,
        backend: str,
        added_tokens: int,
        context_tokens: Optional[int] = None,
    ) -> WindowState:
        """Record one turn.

        ``added_tokens`` feeds the cumulative flow sum. ``context_tokens`` is
        the real size of the conversation as of this turn and is what the
        thresholds read.

        None means the turn reported no usable size, and what that implies
        depends on the backend:

        * ``claude`` CAN measure a level (the per-call peak, dv-9f31), so None
          means "this particular turn couldn't" — aborted run, or a response
          with no per-call usage. Hold the last known level. Recomputing it
          from the flow sum, which is what this used to do unconditionally,
          would reset a 300k window to a few thousand and postpone its
          rotation indefinitely. That path became reachable on 2026-08-05 when
          measurement moved to the peak.
        * Any other backend has no level source at all, so the running flow
          sum stays its only proxy — otherwise rotation never fires there.
        """
        await self.get_or_create(user_id, working_dir)
        wd = self._key(working_dir)
        async with self._db.get_connection() as conn:
            if context_tokens is not None:
                # A resumed session can briefly report a smaller context than
                # the previous turn (e.g. the CLI dropped a tool result), and
                # a *fresh* session legitimately reports much less. Take the
                # reported value either way — it is the truth about right now,
                # which is exactly what a level should be.
                level_sql = "last_context_tokens = ?"
                level_args: Tuple[int, ...] = (int(context_tokens),)
            elif backend in _LEVEL_REPORTING_BACKENDS:
                # Unknown this turn: keep what the last measurable turn set.
                level_sql = "last_context_tokens = last_context_tokens"
                level_args = ()
            else:
                # No cache-aware number available from this backend, ever:
                # level == flow sum.
                level_sql = "last_context_tokens = cumulative_tokens + ?"
                level_args = (int(added_tokens),)
            await conn.execute(
                "UPDATE conversation_windows SET "
                "cumulative_tokens = cumulative_tokens + ?, "
                f"{level_sql}, "
                "active_backend = ?, updated_at = ? "
                "WHERE user_id=? AND working_dir=?",
                (
                    int(added_tokens),
                    *level_args,
                    backend,
                    datetime.now(UTC).isoformat(),
                    user_id,
                    wd,
                ),
            )
            await conn.commit()
        return await self.get_or_create(user_id, working_dir)

    def should_run_evaluator(self, state: WindowState) -> bool:
        # last_eval_checkpoint is the interval-quotient at the last eval OR
        # roll (see roll_window) -- comparing quotients this way means we
        # only fire once the level has crossed into a bucket *beyond* the
        # one the window already started/checkpointed in, not merely bucket
        # >= 1 in absolute terms. Without this, a window that opens above
        # zero (system prompt + persona + facts + tool schemas + seed, ~35-
        # 90k in this deployment) could have its very first turn's quotient
        # exceed a checkpoint that was wrongly reset to 0 (dv-r3c1 item 1).
        return (
            state.last_context_tokens // self.eval_interval_tokens
        ) > state.last_eval_checkpoint

    async def mark_evaluator_run(
        self, user_id: int, working_dir: PathLike, max_message_id: int
    ) -> None:
        wd = self._key(working_dir)
        st = await self.get_or_create(user_id, working_dir)
        checkpoint = st.last_context_tokens // self.eval_interval_tokens
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE conversation_windows SET last_eval_checkpoint=?, "
                "last_checkpoint_message_id=? WHERE user_id=? AND working_dir=?",
                (checkpoint, int(max_message_id), user_id, wd),
            )
            await conn.commit()

    def should_refresh(self, state: WindowState) -> bool:
        return state.last_context_tokens >= self.refresh_threshold_tokens

    async def set_pending_transition(
        self,
        user_id: int,
        working_dir: PathLike,
        *,
        seed_window: Optional[int],
        force_new_session: bool,
    ) -> None:
        """Persist "the next turn must start a fresh backend session, seeded
        from window ``seed_window``".

        Durable on purpose: this used to live in PTB ``user_data``, which is
        in-memory with no persistence configured, so a systemd restart between
        the roll and the user's next message resumed the very session the roll
        had just retired — verified on 2026-08-02, session f95398bd, which
        then ran to 418k context (dv-9f31).
        """
        wd = self._key(working_dir)
        await self.get_or_create(user_id, working_dir)
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE conversation_windows SET pending_seed_window=?, "
                "force_new_session=? WHERE user_id=? AND working_dir=?",
                (
                    None if seed_window is None else int(seed_window),
                    1 if force_new_session else 0,
                    user_id,
                    wd,
                ),
            )
            await conn.commit()

    async def clear_pending_transition(
        self, user_id: int, working_dir: PathLike
    ) -> None:
        """Drop the armed transition once a turn has actually consumed it."""
        await self.set_pending_transition(
            user_id, working_dir, seed_window=None, force_new_session=False
        )

    async def tail_messages(
        self,
        user_id: int,
        working_dir: PathLike,
        window_id: int,
        limit: int,
    ) -> List[Tuple[str, str]]:
        """Return the LAST ``limit`` [(prompt, response), …] of a window,
        message_id ascending (oldest→newest) for readable transcript order.

        Deliberately *not* bounded by the evaluator checkpoint. Memory holds
        durable facts, which is lossy compression of a conversation — it does
        not preserve "what we were just talking about". The seed needs raw
        recent turns regardless of whether they were already memorized.

        Joins to sessions and filters on project_path: window_id is only
        unique *within* a (user_id, working_dir) pair — every profile's
        counter independently starts at 1 (see get_or_create) — so filtering
        by (user_id, window_id) alone lets another profile's window with the
        same numeric id leak its messages into this tail.
        """
        wd = self._key(working_dir)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT m.prompt, m.response FROM ("
                "  SELECT m2.message_id, m2.prompt, m2.response FROM messages m2 "
                "  JOIN sessions s ON m2.session_id = s.session_id "
                "  WHERE m2.user_id=? AND s.project_path=? AND m2.window_id=? "
                "  ORDER BY m2.message_id DESC LIMIT ?"
                ") m ORDER BY m.message_id ASC",
                (user_id, wd, window_id, int(limit)),
            )
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def count_window_messages(
        self,
        user_id: int,
        working_dir: PathLike,
        window_id: int,
    ) -> int:
        """Total turns recorded in a window — the seed's honest 'last N of
        TOTAL' label (dv-e574). Same join discipline as tail_messages:
        window_id is only unique within a (user_id, working_dir) pair."""
        wd = self._key(working_dir)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM messages m "
                "JOIN sessions s ON m.session_id = s.session_id "
                "WHERE m.user_id=? AND s.project_path=? AND m.window_id=?",
                (user_id, wd, window_id),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def roll_window(
        self,
        user_id: int,
        working_dir: PathLike,
        new_backend: str,
        initial_tokens: int = 0,
    ) -> Tuple[int, int]:
        """Start a fresh window. Returns (old_window_id, new_window_id).

        ``initial_tokens``: estimated size of the continuity seed carried
        into the new window. Starting the counter there instead of at 0 keeps
        /status and the footer honest — the fresh session is NOT empty, it
        opens with the seed already in context.

        The eval checkpoint is seeded to ``initial_tokens``'s own interval
        quotient rather than hardcoded 0. A hardcoded 0 checkpoint on a
        non-empty opening meant the first turn's quotient could already
        exceed it -- firing the evaluator immediately after every rotation
        regardless of how little new conversation happened (dv-r3c1 item 1).
        Seeding the checkpoint from the baseline itself makes the trigger
        relative to the window's own starting point, not absolute 0.
        """
        wd = self._key(working_dir)
        st = await self.get_or_create(user_id, working_dir)
        old = st.window_id
        new = old + 1
        baseline_checkpoint = int(initial_tokens) // self.eval_interval_tokens
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE conversation_windows SET window_id=?, active_backend=?, "
                "cumulative_tokens=?, last_context_tokens=?, "
                "last_eval_checkpoint=?, updated_at=? "
                "WHERE user_id=? AND working_dir=?",
                (
                    new,
                    new_backend,
                    int(initial_tokens),
                    # The level must drop with the roll too, or should_refresh
                    # stays true and every subsequent turn rolls again.
                    int(initial_tokens),
                    baseline_checkpoint,
                    datetime.now(UTC).isoformat(),
                    user_id,
                    wd,
                ),
            )
            await conn.commit()
        logger.info(
            "Rolled conversation window",
            user_id=user_id,
            working_dir=wd,
            old=old,
            new=new,
            backend=new_backend,
        )
        return old, new
