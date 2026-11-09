"""Telegram reactions on bot turn replies as answer-quality labels (dv-e1a7).

One row per ``(telegram_chat_id, telegram_message_id, user_id)``: a changed
reaction updates the row, a removed one deletes it (no history). The row is
joined to the turn through ``messages_message_id`` (resolved from the
``sent_messages`` ledger by the caller). Never raises: a label write must not
disturb the bot.

dv-a42c: the same table holds the owner's after-testing rating of a whole
feature (``kind='feature_feedback'``, one row per ``feature_id``), with the
cards of that feature in ``feedback_feature_cards``. Only a tapped button
sets ``score_source='button'``; an extracted score never overrides it.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()

FEEDBACK = "feature_feedback"
# dv-3b99: a pump2 judge stage's 1-10 score of one plan criterion
STAGE_SCORE = "stage_score"
# A feature counts as answered by any feedback row except "не тестил"
# (tested=0): that one is asked again later (src/pump2/feedback.py).
ANSWERED_SQL = (
    "SELECT DISTINCT feature_id FROM message_reactions WHERE kind = ?"
    " AND feature_id IS NOT NULL AND COALESCE(tested, -1) != 0"
)
# The no-model bug rule: "есть баги: ...", "bugs: ...", "баг: ...".
_BUGS = re.compile(r"(?:есть\s+баги?|баги?\s*:|bugs?\s*:)\s*:?\s*(.*)", re.I | re.S)
ASPECTS = ("ui", "function", "speed", "other")
_EXTRACTED_TEXT = ("pros", "cons", "bugs_text")


def keyword_bugs(text: str) -> Optional[str]:
    """The bug text after a bug marker, or None when the text has none."""
    m = _BUGS.search(text or "")
    if not m:
        return None
    return (m.group(1) or "").strip() or text.strip()


def clean_aspects(raw: Any) -> Optional[str]:
    """A comma list of the known aspects, or None."""
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    got = [a for a in (str(x).strip().lower() for x in items) if a in ASPECTS]
    return ",".join(dict.fromkeys(got)) or None


def answered_features_sync(db_path: Path) -> set[str]:
    """``answered_features`` for a process without the bot's storage (the
    pump2 tick): a read-only sqlite3 open. Missing DB / table: empty."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return set()
    try:
        return {r[0] for r in conn.execute(ANSWERED_SQL, (FEEDBACK,))}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def record_stage_scores_sync(
    db_path: Path,
    *,
    board: str,
    card_id: str,
    stage: str,
    scores: Iterable[tuple[str, int]],
    size: Optional[str],
    kind: Optional[str],
) -> Optional[str]:
    """One ``kind='stage_score'`` row per (criterion, score), for a process
    without the bot's storage (the pump2 tick). The NOT NULL telegram triple
    is (0, the next row id, 0): unique, never a real chat. Returns None, or
    the error text (a missing DB / table is an error, never raised)."""
    rows = list(scores)
    if not rows:
        return None
    if not db_path.exists():
        return f"no database at {db_path}"
    now = datetime.now(UTC).isoformat(sep=" ")
    try:
        conn = sqlite3.connect(db_path, timeout=10)
    except sqlite3.Error as e:
        return str(e)
    try:
        with conn:
            for crit, score in rows:
                conn.execute(
                    "INSERT INTO message_reactions (telegram_chat_id, "
                    "telegram_message_id, user_id, emoji, score, reacted_at, kind, "
                    "board, card_id, stage, criterion, card_size, card_kind) "
                    "VALUES (0, (SELECT COALESCE(MAX(id), 0) + 1 FROM "
                    "message_reactions), 0, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (score, now, STAGE_SCORE, board, card_id, stage, crit, size, kind),
                )
        return None
    except sqlite3.Error as e:
        return str(e)
    finally:
        conn.close()


class ReactionRepository:
    """Upsert / delete over ``message_reactions``."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def upsert(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
        user_id: int,
        messages_message_id: int,
        profile_slug: Optional[str],
        emoji: str,
        score: int,
        reacted_at: datetime,
    ) -> None:
        try:
            async with self._db.get_connection() as conn:
                await conn.execute(
                    "INSERT INTO message_reactions (telegram_chat_id, "
                    "telegram_message_id, user_id, messages_message_id, "
                    "profile_slug, emoji, score, reacted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(telegram_chat_id, telegram_message_id, user_id) "
                    "DO UPDATE SET messages_message_id=excluded.messages_message_id, "
                    "profile_slug=excluded.profile_slug, emoji=excluded.emoji, "
                    "score=excluded.score, reacted_at=excluded.reacted_at",
                    (
                        telegram_chat_id,
                        telegram_message_id,
                        user_id,
                        messages_message_id,
                        profile_slug,
                        emoji,
                        score,
                        reacted_at,
                    ),
                )
                await conn.commit()
        except Exception as e:
            logger.warning(
                "message_reactions write failed",
                chat_id=telegram_chat_id,
                tg_message_id=telegram_message_id,
                error=str(e),
            )

    async def delete(
        self, telegram_chat_id: int, telegram_message_id: int, user_id: int
    ) -> None:
        try:
            async with self._db.get_connection() as conn:
                await conn.execute(
                    "DELETE FROM message_reactions WHERE telegram_chat_id=? "
                    "AND telegram_message_id=? AND user_id=?",
                    (telegram_chat_id, telegram_message_id, user_id),
                )
                await conn.commit()
        except Exception as e:
            logger.warning(
                "message_reactions delete failed",
                chat_id=telegram_chat_id,
                tg_message_id=telegram_message_id,
                error=str(e),
            )

    # --- feature feedback (dv-a42c) -------------------------------------

    async def record_feedback(
        self,
        feature_id: str,
        *,
        score: Optional[int],
        chat_id: int,
        message_id: int,
        user_id: int,
        skipped: bool = False,
        not_tested: bool = False,
    ) -> None:
        """A button tap (or the first row of a voice/text answer when
        ``score`` is None). One row per feature: a later tap updates it."""
        if skipped:
            emoji, tested, source = "skip", None, None
        elif not_tested:
            emoji, tested, source = "nt", 0, None
        elif score is not None:
            emoji, tested, source = str(score), 1, "button"
        else:
            emoji, tested, source = "", None, None
        now = datetime.now(UTC)
        try:
            async with self._db.get_connection() as conn:
                cur = await conn.execute(
                    "SELECT id FROM message_reactions WHERE kind=? AND feature_id=?",
                    (FEEDBACK, feature_id),
                )
                row = await cur.fetchone()
                if row is None:
                    await conn.execute(
                        "INSERT INTO message_reactions (telegram_chat_id, "
                        "telegram_message_id, user_id, emoji, score, reacted_at, "
                        "kind, feature_id, tested, score_source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            chat_id,
                            message_id,
                            user_id,
                            emoji,
                            score,
                            now,
                            FEEDBACK,
                            feature_id,
                            tested,
                            source,
                        ),
                    )
                elif emoji:  # a tap: it wins over whatever was there
                    await conn.execute(
                        "UPDATE message_reactions SET emoji=?, score=?, tested=?, "
                        "score_source=?, reacted_at=? WHERE id=?",
                        (emoji, score, tested, source, now, row[0]),
                    )
                await conn.commit()
        except Exception as e:
            logger.warning(
                "feature feedback write failed", feature_id=feature_id, error=str(e)
            )

    async def set_comment(self, feature_id: str, text: str) -> None:
        """Store the raw answer text; the keyword rule marks bugs (no model)."""
        bugs = keyword_bugs(text)
        await self._update(
            feature_id,
            {"comment": text, **({"has_bugs": 1, "bugs_text": bugs} if bugs else {})},
        )

    async def set_extracted(self, feature_id: str, fields: Mapping[str, Any]) -> None:
        """Fields from the ``feedback_extract`` call. ``score`` only fills a
        row without a tapped score (``score_source='extracted'``)."""
        upd: dict[str, Any] = {}
        for k in _EXTRACTED_TEXT:
            v = fields.get(k)
            if isinstance(v, str) and v.strip():
                upd[k] = v.strip()
        for k in ("has_bugs", "tested"):
            v = fields.get(k)
            if isinstance(v, bool) or v in (0, 1):
                upd[k] = int(v)
        aspects = clean_aspects(fields.get("aspects"))
        if aspects:
            upd["aspects"] = aspects
        score = fields.get("score")
        if isinstance(score, int) and not isinstance(score, bool) and 1 <= score <= 10:
            try:
                async with self._db.get_connection() as conn:
                    cur = await conn.execute(
                        "SELECT score_source FROM message_reactions "
                        "WHERE kind=? AND feature_id=?",
                        (FEEDBACK, feature_id),
                    )
                    row = await cur.fetchone()
                if row is not None and row[0] != "button":
                    upd["score"] = score
                    upd["score_source"] = "extracted"
                    upd.setdefault("tested", 1)
            except Exception as e:
                logger.warning("feature feedback read failed", error=str(e))
        await self._update(feature_id, upd)

    async def _update(self, feature_id: str, upd: Mapping[str, Any]) -> None:
        if not upd:
            return
        cols = ", ".join(f"{k}=?" for k in upd)
        try:
            async with self._db.get_connection() as conn:
                await conn.execute(
                    f"UPDATE message_reactions SET {cols} WHERE kind=? AND feature_id=?",
                    (*upd.values(), FEEDBACK, feature_id),
                )
                await conn.commit()
        except Exception as e:
            logger.warning(
                "feature feedback update failed", feature_id=feature_id, error=str(e)
            )

    async def get_feedback(self, feature_id: str) -> Optional[dict[str, Any]]:
        """The feature's feedback row as a dict, or None."""
        try:
            async with self._db.get_connection() as conn:
                cur = await conn.execute(
                    "SELECT * FROM message_reactions WHERE kind=? AND feature_id=?",
                    (FEEDBACK, feature_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                names = [d[0] for d in cur.description]
                return dict(zip(names, row))
        except Exception as e:
            logger.warning("feature feedback read failed", error=str(e))
            return None

    async def answered_features(self) -> set[str]:
        try:
            async with self._db.get_connection() as conn:
                cur = await conn.execute(ANSWERED_SQL, (FEEDBACK,))
                return {r[0] for r in await cur.fetchall()}
        except Exception as e:
            logger.warning("answered features read failed", error=str(e))
            return set()

    async def save_feature_cards(
        self, feature_id: str, links: Iterable[Mapping[str, Any]]
    ) -> None:
        """One row per card of the feature (replaces the feature's rows)."""
        rows = [
            (
                feature_id,
                str(link.get("board") or ""),
                str(link.get("card_id") or ""),
                link.get("title"),
                link.get("merge_sha"),
                link.get("merged_at"),
                str(link.get("exec_mode") or "chat"),
                ",".join(link.get("stages") or ()),
                int(link.get("returns") or 0),
            )
            for link in links
        ]
        try:
            async with self._db.get_connection() as conn:
                await conn.execute(
                    "DELETE FROM feedback_feature_cards WHERE feature_id=?",
                    (feature_id,),
                )
                await conn.executemany(
                    "INSERT INTO feedback_feature_cards (feature_id, board, card_id,"
                    " title, merge_sha, merged_at, exec_mode, stages, returns)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                await conn.commit()
        except Exception as e:
            logger.warning(
                "feedback_feature_cards write failed",
                feature_id=feature_id,
                error=str(e),
            )
