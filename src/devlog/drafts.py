"""Channel post drafts (dv-fc9d): file, preview, publish on the owner's tap.

The one module that talks to the channel. ``publish_draft`` is called only by
the preview's callback handler (``src/bot/orchestration/devlog_taps.py``);
no agent tool imports it (``tests/unit/test_devlog``).

A draft row's content never changes. ``update_draft`` files a new row and
marks the old one ``superseded``, so the publish button of an old preview
answers "outdated" and posts nothing. At publish the image file is read once,
hashed and compared with the stored sha256, and those same bytes are sent:
what goes out is what the owner saw. The leak check runs again at publish.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Union

import structlog

from ..storage import channel_posts as store
from ..storage.channel_posts import ensure_schema  # noqa: F401  (re-export)
from .leak import leak_hits

logger = structlog.get_logger()

CAPTION_LIMIT = 1024  # Telegram: a photo caption
TEXT_LIMIT = 4096  # Telegram: a text message
IMAGE_KINDS = ("generated", "screenshot")
CALLBACK_PREFIX = "devlog:"
SHA_IN_BUTTON = 16  # hex chars of the image sha in the publish button

Terms = Union[Path, str, None]


class DraftRejected(ValueError):
    """The draft cannot be filed as given (limit, missing image, bad kind)."""


@dataclass
class Draft:
    id: int
    profile: str
    status: str
    text: str
    headline: Optional[str]
    image_path: str
    image_sha256: str
    image_kind: str
    leak_hits: List[str] = field(default_factory=list)
    superseded_by: Optional[int] = None
    preview_chat_id: Optional[int] = None
    preview_message_id: Optional[int] = None
    channel_message_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Any) -> "Draft":
        data = dict(row)
        data["leak_hits"] = json.loads(data.get("leak_hits") or "[]")
        for key in ("created_at", "updated_at", "published_at"):
            val = data.get(key)
            if isinstance(val, str):
                data[key] = datetime.fromisoformat(val)
        return cls(**data)


@dataclass(frozen=True)
class PublishResult:
    """``status``: published | refused | missing | outdated | blocked |
    already_published | cancelled | no_channel | failed."""

    status: str
    message: str
    channel_message_id: Optional[int] = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Optional[Draft]:
    row = store.get_post(conn, draft_id)
    return Draft.from_row(row) if row is not None else None


def _validate(text: str, image_path: str, image_kind: str) -> bytes:
    if not text or not text.strip():
        raise DraftRejected("the post text is empty")
    limit = CAPTION_LIMIT if image_path else TEXT_LIMIT
    if len(text) > limit:
        raise DraftRejected(
            f"text is {len(text)} characters; a post with an image allows "
            f"{CAPTION_LIMIT} (text only: {TEXT_LIMIT}). Shorten it."
        )
    if image_kind not in IMAGE_KINDS:
        raise DraftRejected(f"image_kind must be one of {', '.join(IMAGE_KINDS)}")
    try:
        return Path(image_path).read_bytes()
    except OSError as exc:
        raise DraftRejected(f"cannot read the image {image_path}: {exc}") from exc


def create_draft(
    conn: sqlite3.Connection,
    *,
    profile: str,
    text: str,
    headline: Optional[str],
    image_path: str,
    image_kind: str,
    terms_path: Terms = None,
) -> Draft:
    """File one draft; a leak hit in the text or headline files it ``blocked``
    (preview without a publish button, hits listed)."""
    image = _validate(text, image_path, image_kind)
    hits = leak_hits(text, headline, terms_path=terms_path)
    new_id = store.insert_post(
        conn,
        profile=profile,
        status="blocked" if hits else "draft",
        text=text,
        headline=headline,
        image_path=str(Path(image_path).resolve()),
        image_sha256=_sha256(image),
        image_kind=image_kind,
        leak_hits=json.dumps(hits, ensure_ascii=False),
    )
    draft = get_draft(conn, new_id)
    assert draft is not None
    return draft


def update_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    text: Optional[str] = None,
    headline: Optional[str] = None,
    image_path: Optional[str] = None,
    image_kind: Optional[str] = None,
    terms_path: Terms = None,
) -> Draft:
    """A changed draft is a new row; the old one becomes ``superseded``."""
    old = get_draft(conn, draft_id)
    if old is None:
        raise DraftRejected(f"no draft {draft_id}")
    if old.status not in ("draft", "blocked"):
        raise DraftRejected(f"draft {draft_id} is {old.status}; file a new one")
    new = create_draft(
        conn,
        profile=old.profile,
        text=old.text if text is None else text,
        headline=old.headline if headline is None else (headline or None),
        image_path=old.image_path if image_path is None else image_path,
        image_kind=old.image_kind if image_kind is None else image_kind,
        terms_path=terms_path,
    )
    store.update_post(conn, draft_id, status="superseded", superseded_by=new.id)
    return new


def cancel_draft(conn: sqlite3.Connection, draft_id: int) -> bool:
    changed = store.update_post(
        conn, draft_id, only_if=("draft", "blocked"), status="cancelled"
    )
    return changed == 1


def preview_buttons(draft: Draft) -> List[List[Any]]:
    """The preview keyboard rows as ``(label, callback_data)``. A blocked
    draft gets no publish button."""
    rows: List[List[Any]] = []
    if draft.status == "draft":
        token = draft.image_sha256[:SHA_IN_BUTTON]
        rows.append([("✅ Опубликовать", f"{CALLBACK_PREFIX}pub:{draft.id}:{token}")])
    rows.append([("✖ Отменить", f"{CALLBACK_PREFIX}no:{draft.id}")])
    return rows


def preview_note(draft: Draft) -> str:
    if draft.status == "blocked":
        lines = "\n".join(f"• {h}" for h in draft.leak_hits)
        return (
            f"⛔ Черновик #{draft.id} не прошёл проверку на утечки, публикации "
            f"нет. Исправь текст ответом агенту.\n{lines}"
        )
    return (
        f"Черновик #{draft.id}: выше ровно то, что уйдёт в канал. "
        "Правки — ответом агенту (будет новый превью)."
    )


async def send_preview(
    bot: Any, conn: sqlite3.Connection, draft: Draft, chat_id: int
) -> Draft:
    """Send the draft as it will look (photo + caption) to the owner's chat,
    then a note with the buttons; remember the preview message."""
    from ..notifications.service import button_markup

    image = Path(draft.image_path).read_bytes()
    await bot.send_photo(chat_id=chat_id, photo=image, caption=draft.text)
    note = await bot.send_message(
        chat_id=chat_id,
        text=preview_note(draft),
        reply_markup=button_markup(
            [
                [_button(label, data) for label, data in row]
                for row in preview_buttons(draft)
            ]
        ),
    )
    store.update_post(
        conn,
        draft.id,
        preview_chat_id=chat_id,
        # The note that carries the buttons: retire_preview edits it.
        preview_message_id=getattr(note, "message_id", None),
    )
    return get_draft(conn, draft.id) or draft


async def retire_preview(bot: Any, old: Draft, new_id: int) -> None:
    """Replace a superseded draft's preview note (and its buttons) with a
    pointer to the new draft. Best effort: the old button already answers
    "outdated" and posts nothing, this only makes that visible."""
    if old.preview_chat_id is None or old.preview_message_id is None:
        return
    try:
        await bot.edit_message_text(
            chat_id=old.preview_chat_id,
            message_id=old.preview_message_id,
            text=f"↪ Черновик #{old.id} заменён на #{new_id}: смотри новое превью.",
        )
    except Exception as exc:  # message too old / deleted: nothing to retire
        logger.debug("devlog preview not retired", draft=old.id, error=str(exc))


def _button(label: str, data: str) -> Any:
    from ..bot.card_buttons import button

    return button(label, data)


def _matches(stored: str, expected: str) -> bool:
    return len(expected) >= SHA_IN_BUTTON and stored.startswith(expected)


async def publish_draft(
    bot: Any,
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    expected_sha: str,
    actor_id: int,
    owner_id: Optional[int],
    channel: Any,
    terms_path: Terms = None,
) -> PublishResult:
    """Post the stored draft to ``channel``: only for the owner's tap, only
    the content the preview showed, only once, only if still leak-clean."""
    if owner_id is None or actor_id != owner_id:
        logger.warning("devlog publish refused: not the owner", draft=draft_id)
        return PublishResult("refused", "Только владелец.")
    draft = get_draft(conn, draft_id)
    if draft is None:
        return PublishResult("missing", "Черновик не найден.")
    if draft.status in ("published", "publishing"):
        return PublishResult("already_published", "Уже опубликовано.")
    if draft.status == "cancelled":
        return PublishResult("cancelled", "Черновик отменён.")
    if draft.status == "superseded" or not _matches(draft.image_sha256, expected_sha):
        return PublishResult("outdated", "Устарело: есть новый превью.")
    try:
        image = Path(draft.image_path).read_bytes()
    except OSError:
        return PublishResult("outdated", "Картинка пропала: нужен новый черновик.")
    if _sha256(image) != draft.image_sha256:
        return PublishResult("outdated", "Картинка изменилась после превью.")
    hits = leak_hits(draft.text, draft.headline, terms_path=terms_path)
    if hits or draft.status == "blocked":
        store.update_post(
            conn,
            draft_id,
            status="blocked",
            leak_hits=json.dumps(hits or draft.leak_hits, ensure_ascii=False),
        )
        return PublishResult("blocked", "Проверка на утечки: " + "; ".join(hits))
    if not channel:
        return PublishResult("no_channel", "Канал не задан (publish_channel).")
    # Claim the row first: a second tap while this one sends finds 'publishing'.
    claimed = store.update_post(conn, draft_id, only_if=("draft",), status="publishing")
    if claimed != 1:
        return PublishResult("already_published", "Уже опубликовано.")
    try:
        msg = await bot.send_photo(chat_id=channel, photo=image, caption=draft.text)
    except Exception as exc:
        store.update_post(conn, draft_id, status="draft")
        logger.warning("devlog publish failed", draft=draft_id, error=str(exc))
        return PublishResult("failed", f"Telegram не принял пост: {exc}")
    message_id = getattr(msg, "message_id", None)
    store.update_post(
        conn,
        draft_id,
        status="published",
        channel_message_id=message_id,
        published_at=store.now_iso(),
    )
    logger.info("devlog post published", draft=draft_id, message_id=message_id)
    return PublishResult("published", "Опубликовано.", message_id)
