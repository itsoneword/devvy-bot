"""Where a notification physically goes: Telegram or the local inbox (dv-fe4d).

NotificationService owns routing, pins, the ledger and rate limiting; a
transport only performs the calls. Two of them:

* :class:`TelegramTransport` wraps ``telegram.Bot`` with the exact calls the
  service made before (``send_message``, ``edit_message_text``, pin/unpin,
  ``delete_message``, photos/documents). ``telegram`` is imported only here,
  and only when one is built.
* :class:`LocalTransport` writes rows to ``local_messages`` in bot.db, the
  inbox a local install's TUI reads over ``/internal/inbox``. Its message id
  is the row id, so the pin store and the ledger key on it unchanged and
  "one pump pin, the latest" is the service's own edit-in-place rule.

Buttons are ``[(label, callback_data), ...]``, one row, or a list of such
rows (merge cards, dv-ca8b). Telegram renders them as an inline keyboard; the local inbox stores them and ``/internal/callback``
takes the same ``callback_data`` back. ``edit(..., buttons=None)`` removes
them (as an edit without ``reply_markup`` does in Telegram).

Each transport names the exceptions a failed call raises (``Error``, and the
parse-rejection subclass ``BadRequest``), so the service catches them without
importing ``telegram``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

import aiosqlite
import structlog

if TYPE_CHECKING:
    from ..storage.database import DatabaseManager

logger = structlog.get_logger()

# A local install has one owner and one address (settings fills
# notification_chat_ids with it), so ConfirmationManager's chat_ids[0] and
# NotificationService's default chat are this.
LOCAL_CHAT_ID = 1

ButtonRow = Sequence[Tuple[str, str]]
# One row of (label, callback_data), or several rows of them (dv-ca8b).
Buttons = Union[ButtonRow, Sequence[ButtonRow]]


def button_rows(buttons: Buttons) -> List[List[Tuple[str, str]]]:
    """*buttons* as rows: a flat list of pairs is one row."""
    items = list(buttons)
    if not items:
        return []
    if isinstance(items[0][0], str):
        return [[(str(a), str(b)) for a, b in items]]  # type: ignore[misc]
    return [[(str(a), str(b)) for a, b in row] for row in items]  # type: ignore[union-attr]


class Transport(Protocol):
    """The calls NotificationService and the owner prompts make."""

    Error: type[Exception]
    BadRequest: type[Exception]

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        thread_id: Optional[int] = None,
        buttons: Optional[Buttons] = None,
    ) -> int:
        """Send *text*; return the new message id."""
        ...

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        buttons: Optional[Buttons] = None,
    ) -> None: ...

    async def pin(self, chat_id: int, message_id: int) -> None: ...

    async def unpin(self, chat_id: int, message_id: int) -> None: ...

    async def unpin_all(self, chat_id: int, thread_id: int) -> None: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...

    async def send_files(
        self,
        chat_id: int,
        photos: Sequence[Path],
        documents: Sequence[Path],
        thread_id: Optional[int] = None,
    ) -> None: ...


class TelegramTransport:
    """``telegram.Bot`` behind the transport calls (moved code, same calls)."""

    def __init__(self, bot: Any) -> None:
        from telegram.error import BadRequest, TelegramError

        self.bot = bot
        self.Error: type[Exception] = TelegramError
        self.BadRequest: type[Exception] = BadRequest

    @staticmethod
    def _keyboard(buttons: Buttons) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(label, callback_data=data) for label, data in row]
                for row in button_rows(buttons)
            ]
        )

    def _kwargs(
        self,
        parse_mode: Optional[str],
        thread_id: Optional[int],
        buttons: Optional[Buttons],
    ) -> Dict[str, Any]:
        kw: Dict[str, Any] = {}
        if parse_mode is not None:
            from telegram.constants import ParseMode

            kw["parse_mode"] = ParseMode.HTML if parse_mode == "HTML" else parse_mode
        if thread_id is not None:
            kw["message_thread_id"] = thread_id
        if buttons:
            kw["reply_markup"] = self._keyboard(buttons)
        return kw

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        thread_id: Optional[int] = None,
        buttons: Optional[Buttons] = None,
    ) -> int:
        msg = await self.bot.send_message(
            chat_id=chat_id, text=text, **self._kwargs(parse_mode, thread_id, buttons)
        )
        return msg.message_id  # type: ignore[no-any-return]

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        buttons: Optional[Buttons] = None,
    ) -> None:
        await self.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            **self._kwargs(parse_mode, None, buttons),
        )

    async def pin(self, chat_id: int, message_id: int) -> None:
        await self.bot.pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True
        )

    async def unpin(self, chat_id: int, message_id: int) -> None:
        await self.bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)

    async def unpin_all(self, chat_id: int, thread_id: int) -> None:
        await self.bot.unpin_all_forum_topic_messages(
            chat_id=chat_id, message_thread_id=thread_id
        )

    async def delete(self, chat_id: int, message_id: int) -> None:
        await self.bot.delete_message(chat_id=chat_id, message_id=message_id)

    async def send_files(
        self,
        chat_id: int,
        photos: Sequence[Path],
        documents: Sequence[Path],
        thread_id: Optional[int] = None,
    ) -> None:
        """Raster photos as one album (one photo: a plain photo), the rest as
        documents (dv-f90c)."""
        from telegram import InputMediaPhoto

        topic_kw = self._kwargs(None, thread_id, None)
        if len(photos) == 1:
            with open(photos[0], "rb") as f:
                await self.bot.send_photo(chat_id=chat_id, photo=f, **topic_kw)
        elif photos:
            handles = [open(p, "rb") for p in photos[:10]]  # noqa: SIM115
            try:
                await self.bot.send_media_group(
                    chat_id=chat_id,
                    media=[InputMediaPhoto(media=h) for h in handles],
                    **topic_kw,
                )
            finally:
                for h in handles:
                    h.close()
        for doc in documents:
            with open(doc, "rb") as f:
                await self.bot.send_document(
                    chat_id=chat_id, document=f, filename=doc.name, **topic_kw
                )


class LocalTransportError(Exception):
    """A local inbox write failed (or targeted a row that does not exist)."""


class LocalBadRequest(LocalTransportError):
    """Never raised for formatting (the inbox stores text as sent); exists so
    the service's parse-error fallback has a type to name."""


@asynccontextmanager
async def _inbox_db(db: "DatabaseManager") -> AsyncIterator[aiosqlite.Connection]:
    """A short connection of its own to bot.db for one inbox read or write.

    Not the pool: the inbox is written by the notification consumer and by
    owner prompts sent from API request handlers, and read by the TUI's poll.
    A pooled connection is tied to the event loop that first used the pool,
    so a caller on another loop (an API request served on its own loop, as
    under TestClient) can wait on it forever. A plain connection has no such
    tie, and the inbox's traffic is a few rows a minute."""
    conn = await aiosqlite.connect(
        db.database_path, detect_types=sqlite3.PARSE_DECLTYPES
    )
    try:
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        await conn.close()


def _now() -> datetime:
    return datetime.now(UTC)


# The next value of the inbox's change cursor. Evaluated inside the writing
# statement, so SQLite's write lock makes it unique across connections.
_NEXT_SEQ = "(SELECT COALESCE(MAX(seq), 0) + 1 FROM local_messages)"


def _iso(value: Any) -> Optional[str]:
    """A TIMESTAMP column as ISO text (bot.db converts it to a datetime)."""
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _buttons_json(buttons: Optional[Buttons]) -> Optional[str]:
    if not buttons:
        return None
    # The inbox keeps one flat row: the TUI lays the buttons out itself.
    flat = [[label, data] for row in button_rows(buttons) for label, data in row]
    return json.dumps(flat, ensure_ascii=False)


class LocalTransport:
    """The local inbox: one ``local_messages`` row per message (dv-fe4d)."""

    Error: type[Exception] = LocalTransportError
    BadRequest: type[Exception] = LocalBadRequest
    # No per-chat flood limit on a local table (Telegram's is 1.1 s).
    send_interval: float = 0.0

    def __init__(self, db: "DatabaseManager", desktop_notify: bool = False) -> None:
        self.db = db
        # Off by default (tests, CI); main turns it on for a real install.
        self.desktop_notify = desktop_notify

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        thread_id: Optional[int] = None,
        buttons: Optional[Buttons] = None,
    ) -> int:
        async with _inbox_db(self.db) as conn:
            cur = await conn.execute(
                "INSERT INTO local_messages"
                " (chat_id, text, parse_mode, buttons_json, pinned, created_at, seq)"
                f" VALUES (?, ?, ?, ?, 0, ?, {_NEXT_SEQ})",
                (chat_id, text, parse_mode, _buttons_json(buttons), _now()),
            )
            message_id = cur.lastrowid
            await conn.commit()
        if message_id is None:
            raise LocalTransportError("inbox insert returned no row id")
        self._notify_desktop(text)
        return int(message_id)

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        buttons: Optional[Buttons] = None,
    ) -> None:
        async with _inbox_db(self.db) as conn:
            cur = await conn.execute(
                "UPDATE local_messages SET text = ?, parse_mode = ?,"
                f" buttons_json = ?, edited_at = ?, seq = {_NEXT_SEQ}"
                " WHERE id = ? AND chat_id = ?",
                (
                    text,
                    parse_mode,
                    _buttons_json(buttons),
                    _now(),
                    message_id,
                    chat_id,
                ),
            )
            await conn.commit()
        if cur.rowcount == 0:
            # The same answer Telegram gives, so the pin logic re-creates it.
            raise LocalTransportError("message to edit not found")

    async def _set_pinned(self, chat_id: int, message_id: int, pinned: int) -> None:
        async with _inbox_db(self.db) as conn:
            await conn.execute(
                f"UPDATE local_messages SET pinned = ?, seq = {_NEXT_SEQ}"
                " WHERE id = ? AND chat_id = ?",
                (pinned, message_id, chat_id),
            )
            await conn.commit()

    async def pin(self, chat_id: int, message_id: int) -> None:
        await self._set_pinned(chat_id, message_id, 1)

    async def unpin(self, chat_id: int, message_id: int) -> None:
        await self._set_pinned(chat_id, message_id, 0)

    async def unpin_all(self, chat_id: int, thread_id: int) -> None:
        # No topics locally; the service only calls this for a topic pin.
        return None

    async def delete(self, chat_id: int, message_id: int) -> None:
        async with _inbox_db(self.db) as conn:
            await conn.execute(
                "DELETE FROM local_messages WHERE id = ? AND chat_id = ?",
                (message_id, chat_id),
            )
            await conn.commit()

    async def send_files(
        self,
        chat_id: int,
        photos: Sequence[Path],
        documents: Sequence[Path],
        thread_id: Optional[int] = None,
    ) -> None:
        """The files stay where they are; the inbox gets their paths."""
        paths = [*photos, *documents]
        if paths:
            await self.send(chat_id, "\n".join(f"📎 {p}" for p in paths))

    def _notify_desktop(self, text: str) -> None:
        """Fire-and-forget desktop banner (osascript / notify-send); never
        awaited, so a slow or missing notifier cannot hold the send queue."""
        if not self.desktop_notify:
            return
        first = (text.strip().splitlines() or [""])[0][:200]
        if sys.platform == "darwin":
            script = 'display notification {} with title "Devvy"'.format(
                json.dumps(first)
            )
            argv = ["osascript", "-e", script]
        elif shutil.which("notify-send"):
            argv = ["notify-send", "Devvy", first]
        else:
            return
        try:
            asyncio.get_running_loop().create_task(_spawn_quietly(argv))
        except RuntimeError:
            return


async def _spawn_quietly(argv: List[str]) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception as e:  # noqa: BLE001 - a banner is best effort
        logger.debug("desktop notification failed", error=str(e))


# --- the inbox, read side (the API's /internal/inbox and /internal/callback) --


def _row_dict(row: Any) -> Dict[str, Any]:
    buttons = json.loads(row["buttons_json"]) if row["buttons_json"] else []
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "text": row["text"],
        "parse_mode": row["parse_mode"],
        "buttons": buttons,
        "pinned": bool(row["pinned"]),
        "created_at": _iso(row["created_at"]),
        "edited_at": _iso(row["edited_at"]),
        "seq": row["seq"],
        # Which pin this is ("pump", "jobs"), so the TUI tells them apart.
        "pin_as": row["pin_as"],
    }


async def inbox_since(db: "DatabaseManager", since: int = 0) -> List[Dict[str, Any]]:
    """Rows created *or changed* after the cursor *since*, plus every live
    pin. The cursor is ``seq``, bumped on insert, edit and pin change, so an
    ask card edited after the client's last poll (timed out, answered,
    explained) comes back with its buttons gone. A client passes the largest
    ``seq`` it has seen (see :func:`inbox_cursor`). One query, oldest first."""
    async with _inbox_db(db) as conn:
        cur = await conn.execute(
            "SELECT id, chat_id, text, parse_mode, buttons_json, pinned,"
            " created_at, edited_at, seq,"
            " (SELECT p.pin_as FROM pinned_messages p WHERE p.chat_id ="
            " m.chat_id AND p.message_id = m.id LIMIT 1) AS pin_as"
            " FROM local_messages m"
            " WHERE seq > ? OR pinned = 1 ORDER BY id",
            (since,),
        )
        rows = await cur.fetchall()
    return [_row_dict(r) for r in rows]


def inbox_cursor(messages: List[Dict[str, Any]], since: int = 0) -> int:
    """The ``since`` for a client's next poll after receiving *messages*."""
    return max([since, *(int(m["seq"]) for m in messages)])


async def close_buttons(db: "DatabaseManager", data: str, verdict: str) -> bool:
    """After a tap: the card holding the button *data* loses its buttons and
    gets *verdict* appended (what the Telegram callback's edit does)."""
    async with _inbox_db(db) as conn:
        cur = await conn.execute(
            "SELECT id, buttons_json FROM local_messages"
            " WHERE buttons_json IS NOT NULL ORDER BY id DESC LIMIT 200"
        )
        rows = await cur.fetchall()
        target = next(
            (
                r["id"]
                for r in rows
                if any(b[1] == data for b in json.loads(r["buttons_json"]))
            ),
            None,
        )
        if target is None:
            return False
        await conn.execute(
            "UPDATE local_messages SET text = text || ?, buttons_json = NULL,"
            f" edited_at = ?, seq = {_NEXT_SEQ} WHERE id = ?",
            (f"\n\n<b>{verdict}</b>", _now(), target),
        )
        await conn.commit()
    return True
