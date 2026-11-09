"""The owner's feature-feedback answers in the Feedback topic (dv-a42c).

The questions come from the pump2 tick (``src/pump2/feedback.py``). The
Feedback topic is bound to no profile: ``reports_chat_gate`` hands an
owner's text or voice message here and stops the update, so an answer never
reaches an agent turn, a session or any profile's history.

- a score button (``fb:<feature_id>:<n|nt|skip>``) -> ``record_feedback``;
- text / voice (transcribed by the configured ``VoiceHandler``) -> the raw
  text in ``comment`` (keyword bug rule), then ONE stateless
  ``complete_text(purpose="feedback_extract")`` call (JSON only, no session,
  no history) fills score / tested / pros / cons / bugs / aspects. A bad
  JSON or a model error keeps the raw text and the keyword rule.

The answer goes to the feature of the question replied to, else the open
question, else the feature asked last; it closes the question.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import structlog

from ...pump2 import feedback as survey
from ...pump2.feedback import (
    NOT_TESTED,
    SKIP,
)
from ...pump2.feedback import SOURCE as FEEDBACK_SOURCE
from ...pump2.feedback import (
    close_open,
    feature_links,
    parse_callback,
    target_feature,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "FEEDBACK_SOURCE",
    "extract",
    "handle_feedback_callback",
    "handle_feedback_message",
]

PURPOSE_EXTRACT = "feedback_extract"
EXTRACT_SYSTEM = (
    "You turn the owner's spoken or written feedback about a software "
    "feature into JSON. Output ONLY one JSON object, no prose, with keys: "
    '"score" (integer 1-10 if the owner gave or clearly implied a rating, '
    'else null), "tested" (1 if they used the feature, 0 if they say they '
    'did not test it, else null), "pros" (short summary of what they liked, '
    'or ""), "cons" (short summary of what they disliked, or ""), '
    '"has_bugs" (1 if they report a bug, else 0), "bugs_text" (the bugs in '
    'a few words, or ""), "aspects" (list of any of "ui", "function", '
    '"speed", "other" they talk about). Keep their language.'
)
_JSON = re.compile(r"\{.*\}", re.S)

Complete = Callable[..., Awaitable[str]]


async def _default_complete(prompt: str, **kw: Any) -> str:
    from ...model_router import complete_text

    return await complete_text(prompt, **kw)


async def extract(
    text: str, complete: Optional[Complete] = None
) -> Optional[dict[str, Any]]:
    """The structured fields of ``text``, or None (model error / bad JSON)."""
    call = complete or _default_complete
    try:
        raw = await call(
            text,
            purpose=PURPOSE_EXTRACT,
            system_prompt=EXTRACT_SYSTEM,
            timeout=120.0,
        )
    except Exception as e:
        logger.warning("feedback extraction failed", error=str(e))
        return None
    m = _JSON.search(str(raw or ""))
    try:
        data = json.loads(m.group(0)) if m else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        logger.warning("feedback extraction: no JSON object")
        return None
    return data


def _replied_feature(message: Any) -> Optional[str]:
    """The feature of the question message being replied to (its buttons)."""
    quoted = getattr(message, "reply_to_message", None)
    markup = getattr(quoted, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", None) or ():
        for b in row:
            parsed = parse_callback(str(getattr(b, "callback_data", "") or ""))
            if parsed:
                return parsed[0]
    return None


async def _transcribe(message: Any, context: Any) -> Optional[str]:
    features = context.bot_data.get("features")
    vh = features.get_voice_handler() if features else None
    if vh is None:
        await message.reply_text("🔇 Голос недоступен: ответь текстом.")
        return None
    try:
        done = await vh.process_voice_message(message.voice, None)
    except Exception as e:
        logger.warning("feedback voice transcription failed", error=str(e))
        await message.reply_text("Не удалось распознать голос: ответь текстом.")
        return None
    return str(done.transcription)


async def _save_links(storage: Any, fid: str, state_path: Path) -> None:
    links = feature_links(state_path, fid)
    if links:
        await storage.reactions.save_feature_cards(fid, links)


def _ack(row: Optional[dict[str, Any]]) -> str:
    if not row:
        return "Записал."
    parts = []
    if row.get("score") is not None:
        parts.append(f"оценка {row['score']}")
    if row.get("has_bugs"):
        parts.append(f"баги: {row.get('bugs_text') or 'да'}")
    if row.get("pros"):
        parts.append(f"+ {row['pros']}")
    if row.get("cons"):
        parts.append(f"− {row['cons']}")
    return "Записал" + (": " + "; ".join(parts) if parts else ".")


async def handle_feedback_message(
    update: Any,
    context: Any,
    *,
    state_path: Optional[Path] = None,
    complete: Optional[Complete] = None,
) -> None:
    """An owner's text / voice answer in the Feedback topic."""
    state_path = state_path or survey.STATE
    message = update.effective_message
    user = update.effective_user
    storage = context.bot_data.get("storage")
    if message is None or user is None:
        return
    if storage is None:
        logger.warning("feedback answer dropped: no storage")
        await message.reply_text("Ответ не сохранён: хранилище недоступно.")
        return
    fid = _replied_feature(message) or target_feature(state_path)
    if not fid:
        await message.reply_text("Сейчас нет вопроса, к которому отнести ответ.")
        return
    if getattr(message, "voice", None):
        text = await _transcribe(message, context)
    else:
        text = message.text
    if not text or not text.strip():
        return
    reactions = storage.reactions
    await reactions.record_feedback(
        fid,
        score=None,
        chat_id=message.chat_id,
        message_id=message.message_id,
        user_id=user.id,
    )
    before = await reactions.get_feedback(fid) or {}
    full = "\n".join(x for x in (before.get("comment"), text.strip()) if x)
    await reactions.set_comment(fid, full)
    fields = await extract(full, complete)
    if fields:
        await reactions.set_extracted(fid, fields)
    await _save_links(storage, fid, state_path)
    close_open(state_path, fid)
    row = await reactions.get_feedback(fid)
    logger.info(
        "feature feedback answer stored",
        feature_id=fid,
        voice=bool(getattr(message, "voice", None)),
        extracted=bool(fields),
    )
    await message.reply_text(_ack(row))


async def handle_feedback_callback(
    update: Any, context: Any, *, state_path: Optional[Path] = None
) -> None:
    """A score / "не тестил" / "пропустить" tap (owner-gated by the caller)."""
    state_path = state_path or survey.STATE
    query = update.callback_query
    parsed = parse_callback(str(getattr(query, "data", "") or ""))
    storage = context.bot_data.get("storage")
    if parsed is None or storage is None:
        await query.answer()
        return
    fid, value = parsed
    msg = query.message
    score = int(value) if value.isdigit() else None
    await storage.reactions.record_feedback(
        fid,
        score=score,
        chat_id=msg.chat_id if msg is not None else 0,
        message_id=msg.message_id if msg is not None else 0,
        user_id=query.from_user.id,
        skipped=value == SKIP,
        not_tested=value == NOT_TESTED,
    )
    await _save_links(storage, fid, state_path)
    close_open(state_path, fid, retry=value == NOT_TESTED)
    label = {SKIP: "пропущено", NOT_TESTED: "не тестил, спрошу позже"}.get(
        value, f"оценка {value}"
    )
    await query.answer(f"Записал: {label}")
    if msg is not None and value != NOT_TESTED and value != SKIP:
        # keep the buttons' feature id reachable for a reply: only re-mark
        try:
            await msg.reply_text(
                f"Записал {label}. Можешь добавить голосом/текстом ответом на "
                "вопрос: что хорошо, что плохо, баги."
            )
        except Exception:
            logger.debug("feedback ack failed", exc_info=True)
