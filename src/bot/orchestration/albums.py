"""Telegram media groups (albums) -> one turn (dv-b399).

Telegram delivers each album item as its own update sharing a
``media_group_id``. The first item to arrive becomes the *leader*: its handler
waits for the album to go quiet, then runs ONE turn for everything collected.
Later items just register themselves and return. A plain text message from the
same chat/topic/user arriving while the album is still open is folded into the
prompt (some clients send the comment as a separate message).
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ALBUM_DEBOUNCE_S = 1.5
ALBUM_MAX_ITEMS = 10


@dataclass(eq=False)
class Album:
    leader: Any  # the Update whose handler runs the turn
    chat_id: int
    thread_id: Optional[int]
    user_id: int
    items: List[Any] = field(default_factory=list)  # Updates, arrival order
    texts: List[str] = field(default_factory=list)  # folded plain messages
    last_seen: float = field(default_factory=time.monotonic)
    closed: bool = False

    @property
    def full(self) -> bool:
        return len(self.items) >= ALBUM_MAX_ITEMS

    def captions(self) -> List[str]:
        out = []
        for u in self.items:
            cap = getattr(u.message, "caption", None)
            if cap and cap.strip():
                out.append(cap.strip())
        return out + self.texts


class AlbumBuffer:
    def __init__(self, debounce: float = ALBUM_DEBOUNCE_S) -> None:
        self.debounce = debounce
        self._open: Dict[Tuple[int, str], Album] = {}

    def join(self, update: Any) -> Tuple[Album, bool]:
        """Register an album item. Returns (album, is_leader). Items beyond
        the cap, or after the album closed, start a fresh album."""
        msg = update.message
        key = (msg.chat_id, str(msg.media_group_id))
        album = self._open.get(key)
        if album is not None and (album.closed or album.full):
            album = None
            key = (msg.chat_id, f"{msg.media_group_id}#{msg.message_id}")
        if album is None:
            album = Album(
                leader=update,
                chat_id=msg.chat_id,
                thread_id=getattr(msg, "message_thread_id", None),
                user_id=update.effective_user.id,
            )
            self._open[key] = album
            album.items.append(update)
            return album, True
        album.items.append(update)
        album.last_seen = time.monotonic()
        return album, False

    async def collect(self, album: Album) -> Album:
        """Leader only: wait until no item arrived for ``debounce`` s."""
        while not album.full:
            await asyncio.sleep(self.debounce)
            if time.monotonic() - album.last_seen >= self.debounce:
                break
        album.closed = True
        for k, v in list(self._open.items()):
            if v is album:
                del self._open[k]
        return album

    def absorb_text(self, update: Any) -> bool:
        """Fold a plain text message into an open album of the same
        chat/topic/user. True if absorbed (caller must not run a turn)."""
        msg = update.message
        text = getattr(msg, "text", None)
        if not text:
            return False
        thread = getattr(msg, "message_thread_id", None)
        for album in self._open.values():
            if (
                not album.closed
                and album.chat_id == msg.chat_id
                and album.thread_id == thread
                and album.user_id == update.effective_user.id
            ):
                album.texts.append(text.strip())
                album.last_seen = time.monotonic()
                return True
        return False
