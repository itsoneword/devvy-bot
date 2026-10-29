"""``/setup`` and the first-run gate: the onboarding interview (dv-bf1e, dv-b022).

While the default persona still carries the "not configured" marker, every
owner message (text or voice) lands in :func:`handle_text` instead of an agent
turn. The interview is a short conversation of at most 8 skippable questions,
answerable one by one (buttons/text) or all at once in free form; free form is
parsed by one model-router call (``purpose="onboarding"``), then only the
missing essentials are asked. Step 0 is the voice setup (``/voice``), chained
by sending its prompt first -- not re-implemented here.

State lives in ``bot_data["onboarding"][user_id]`` (process memory: a restart
mid-interview just starts over). Outputs and the "configured" check are in
``src/onboarding/core.py``; finishing rewrites the persona, which flips the gate.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ....onboarding import core
from ....onboarding.core import Answers
from ....profiles.registry import agent_id_for_working_dir
from ...utils.html_format import escape_html

logger = structlog.get_logger()

STATE_KEY = "onboarding"

#: Asked again after a free-form answer if still missing; the rest take defaults.
ESSENTIALS = ("name", "language", "timezone", "agents")

_LANG_BY_CODE = {
    "ru": "Russian",
    "en": "English",
    "uk": "Ukrainian",
    "pt": "Portuguese",
}

Send = Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable[Any]]


@dataclass
class Session:
    answers: Answers = field(default_factory=Answers)
    phase: str = "intro"  # intro | ask | followup | confirm
    current: Optional[str] = None
    lang_code: Optional[str] = None


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _sessions(context: ContextTypes.DEFAULT_TYPE) -> Dict[int, Session]:
    store: Dict[int, Session] = context.bot_data.setdefault(STATE_KEY, {})
    return store


def active(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return user_id in context.bot_data.get(STATE_KEY, {})


def unconfigured(
    context: ContextTypes.DEFAULT_TYPE, settings: Optional[Any] = None
) -> bool:
    """`settings` is for callers that hold their own (the orchestrator);
    handlers wrapped by _inject_deps find it in bot_data."""
    return core.is_unconfigured(
        {"profile_registry": context.bot_data.get("profile_registry")},
        settings if settings is not None else context.bot_data["settings"],
    )


def _sender(update: Update) -> Send:
    message = update.callback_query.message if update.callback_query else None
    message = message or update.message

    async def send(text: str, markup: Optional[InlineKeyboardMarkup] = None) -> Any:
        return await message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    return send


# ------------------------------------------------------------------ prompts

_INTRO = (
    "👋 <b>Давай настроим меня под тебя.</b> Это быстро, любой вопрос можно "
    "пропустить.\n\n"
    "Мне нужно знать:\n"
    "1. как к тебе обращаться\n2. язык ответов\n3. часовой пояс\n"
    "4. как называть меня\n5. тон общения\n6. спорить с тобой, если ты неправ\n"
    "7. какие агенты нужны\n8. что важно знать о тебе\n\n"
    "Можно <b>ответить на всё сразу</b> — текстом или голосом, как удобно, "
    "я разберу сам. Или выбери готовый сценарий / жми «Далее» — спрошу по шагам."
)


def _intro_markup() -> InlineKeyboardMarkup:
    rows = [[_btn(p["label"], f"onb:preset:{k}")] for k, p in core.PRESETS.items()]
    rows.append([_btn("➡️ Далее", "onb:next")])
    return InlineKeyboardMarkup(rows)


def _skip_row() -> List[InlineKeyboardButton]:
    return [_btn("⏭ Пропустить", "onb:skip")]


def _question(sess: Session) -> tuple:
    """(text, markup) for the current question."""
    f = sess.current
    skip = _skip_row()
    if f == "name":
        return "Как к тебе обращаться?", InlineKeyboardMarkup([skip])
    if f == "language":
        return (
            "На каком языке отвечать?",
            InlineKeyboardMarkup(
                [
                    [
                        _btn("Русский", "onb:lang:Russian"),
                        _btn("English", "onb:lang:English"),
                    ],
                    skip,
                ]
            ),
        )
    if f == "timezone":
        return (
            "Твой часовой пояс? Напиши в формате <code>Europe/Lisbon</code> "
            "(или город, если знаешь зону).",
            InlineKeyboardMarkup([skip]),
        )
    if f == "assistant_name":
        return (
            f"Как меня называть? (по умолчанию — {core.DEFAULT_ASSISTANT_NAME})",
            InlineKeyboardMarkup([skip]),
        )
    if f == "tone":
        return (
            "Какой тон общения?",
            InlineKeyboardMarkup(
                [
                    [_btn("Коротко и по делу", "onb:tone:concise")],
                    [_btn("Дружелюбно", "onb:tone:friendly")],
                    [_btn("Формально", "onb:tone:formal")],
                    skip,
                ]
            ),
        )
    if f == "push_back":
        return (
            "Спорить, если я вижу, что ты неправ?",
            InlineKeyboardMarkup(
                [[_btn("Да, прямо", "onb:pb:y"), _btn("Нет", "onb:pb:n")], skip]
            ),
        )
    if f == "agents":
        return _agents_question(sess)
    return (
        "Что важно знать о тебе (работа, семья, привычки)? Одним сообщением.",
        InlineKeyboardMarkup([skip]),
    )


def _agents_question(sess: Session) -> tuple:
    chosen = set(sess.answers.agents or [])
    rows = [
        [_btn(("✅ " if s in chosen else "▫️ ") + label, f"onb:ag:{s}")]
        for s, label in core.OPTIONAL_AGENTS.items()
    ]
    rows.append([_btn("Готово", "onb:agdone"), _btn("⏭ Пропустить", "onb:skip")])
    return (
        "Какие ещё агенты нужны? DevOps есть всегда. Отметь и жми «Готово».",
        InlineKeyboardMarkup(rows),
    )


def with_defaults(sess: Session) -> Answers:
    """Copy of the answers with sane defaults filled in for the unanswered."""
    a = sess.answers
    return Answers(
        name=a.name,
        language=a.language
        or _LANG_BY_CODE.get((sess.lang_code or "").split("-")[0].lower()),
        timezone=a.timezone,
        assistant_name=a.assistant_name,
        tone=a.tone or "concise",
        push_back=True if a.push_back is None else a.push_back,
        agents=list(a.agents or []),
        facts=a.facts,
    )


def _summary(answers: Answers) -> str:
    def show(v: Any) -> str:
        return escape_html(str(v)) if v else "— (по умолчанию)"

    agents = ", ".join(answers.agents or []) or "только DevOps"
    return (
        "📋 <b>Проверь:</b>\n"
        f"• Имя: {show(answers.name)}\n"
        f"• Язык: {show(answers.language)}\n"
        f"• Часовой пояс: {show(answers.timezone)}\n"
        f"• Я: {show(answers.assistant_name or core.DEFAULT_ASSISTANT_NAME)}\n"
        f"• Тон: {show(answers.tone)}\n"
        f"• Спорить: {'да' if answers.push_back else 'нет'}\n"
        f"• Агенты: {escape_html(agents)}\n"
        f"• О тебе: {show(answers.facts)}"
    )


# -------------------------------------------------------------------- flow


async def _ask_next(sess: Session, send: Send) -> None:
    missing = sess.answers.missing()
    if not missing:
        await _confirm(sess, send)
        return
    sess.phase = "ask"
    sess.current = missing[0]
    text, markup = _question(sess)
    await send(text, markup)


async def _confirm(sess: Session, send: Send) -> None:
    sess.phase = "confirm"
    sess.current = None
    await send(
        _summary(with_defaults(sess)),
        InlineKeyboardMarkup(
            [[_btn("✅ Сохранить", "onb:ok"), _btn("🔄 Заново", "onb:redo")]]
        ),
    )


async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 0 (voice) then the intro. Used by /setup, /start's button, the gate."""
    from .voice import voice_command

    user = update.effective_user
    _sessions(context)[user.id] = Session(
        lang_code=getattr(user, "language_code", None)
    )
    if update.callback_query is None:
        await voice_command(update, context)
    else:
        # voice_command replies to update.message, which a button press lacks.
        await update.callback_query.message.reply_text(
            "🎙 Голос: /voice — можно настроить в любой момент."
        )
    await _sender(update)(_INTRO, _intro_markup())


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/setup`` — (re)run onboarding any time. Owner-only via the table."""
    if not unconfigured(context):
        await update.message.reply_text(
            "⚙️ Настройка заново: блок identity в персоне будет переписан "
            "(копия старой сохранится рядом)."
        )
    await start_onboarding(update, context)


async def handle_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """One owner message (typed or transcribed) while onboarding is active."""
    user = update.effective_user
    sessions = _sessions(context)
    sess = sessions.get(user.id)
    send = _sender(update)
    if sess is None:  # first message of an unconfigured bot
        await start_onboarding(update, context)
        return
    if text.strip().lower() in ("cancel", "отмена") and not unconfigured(context):
        sessions.pop(user.id, None)
        await send("Настройка отменена. Вернуться: /setup.", None)
        return
    if sess.phase == "confirm":
        await send("Нажми «✅ Сохранить» или «🔄 Заново».", None)
        return
    if sess.phase == "ask" and sess.current in (
        "name",
        "assistant_name",
        "facts",
        "timezone",
        "language",
    ):
        if await _answer_field(sess, text, send):
            await _ask_next(sess, send)
        return

    # Free form (intro), an answer the buttons cannot parse, or a follow-up.
    await core.extract_answers(text, sess.answers)
    if sess.phase == "followup":
        await _confirm(sess, send)
        return
    if sess.phase == "ask":
        await _ask_next(sess, send)
        return
    missing = [f for f in ESSENTIALS if getattr(sess.answers, f) is None]
    if not missing:
        await _confirm(sess, send)
        return
    sess.phase = "followup"
    names = {
        "name": "как к тебе обращаться",
        "language": "язык ответов",
        "timezone": "часовой пояс",
        "agents": "какие агенты нужны",
    }
    await send(
        "Понял. Не хватает: " + ", ".join(names[f] for f in missing) + ".\n"
        "Допиши одним сообщением или пропусти — возьму значения по умолчанию.",
        InlineKeyboardMarkup([[_btn("⏭ Пропустить", "onb:skip")]]),
    )


async def _answer_field(sess: Session, text: str, send: Send) -> bool:
    """Store a typed answer for the current question; False = ask again."""
    f = sess.current
    value = text.strip()
    if f == "timezone":
        tz = core.valid_timezone(value)
        if tz is None:
            await send(
                "Не узнаю такую зону. Формат <code>Europe/Lisbon</code>, "
                "или пропусти.",
                InlineKeyboardMarkup([_skip_row()]),
            )
            return False
        sess.answers.timezone = tz
    elif f == "language":
        lang = core.clean_language(value)
        if lang is None:
            await send("Напиши язык одним словом, например English.", None)
            return False
        sess.answers.language = lang
    elif f == "name":
        sess.answers.name = value[:60]
    elif f == "assistant_name":
        sess.answers.assistant_name = value[:40]
    else:
        sess.answers.facts = value[:600]
    return True


async def handle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    """``onb:*`` buttons (owner-gated by the dispatcher)."""
    query = update.callback_query
    user = query.from_user
    send = _sender(update)
    parts = data.split(":")
    action = parts[1]
    if action == "go":
        await start_onboarding(update, context)
        return
    sess = _sessions(context).get(user.id)
    if sess is None:
        await query.edit_message_text("Сессия настройки закончилась. Запусти /setup.")
        return
    a = sess.answers
    arg = parts[2] if len(parts) > 2 else ""

    if action == "preset":
        preset = core.PRESETS.get(arg)
        if preset is None:
            return
        a.tone = preset["tone"]
        a.agents = list(preset["agents"])
        await _ask_next(sess, send)
    elif action == "next":
        await _ask_next(sess, send)
    elif action == "skip":
        if sess.phase == "followup":
            await _confirm(sess, send)
            return
        if sess.current:
            a.skipped.append(sess.current)
        await _ask_next(sess, send)
    elif action == "lang":
        a.language = arg
        await _ask_next(sess, send)
    elif action == "tone" and arg in core.TONES:
        a.tone = arg
        await _ask_next(sess, send)
    elif action == "pb":
        a.push_back = arg == "y"
        await _ask_next(sess, send)
    elif action == "ag" and arg in core.OPTIONAL_AGENTS:
        chosen = list(a.agents or [])
        chosen.remove(arg) if arg in chosen else chosen.append(arg)
        a.agents = chosen
        text, markup = _agents_question(sess)
        await query.edit_message_reply_markup(reply_markup=markup)
    elif action == "agdone":
        a.agents = list(a.agents or [])
        await _ask_next(sess, send)
    elif action == "redo":
        _sessions(context)[user.id] = Session(lang_code=sess.lang_code)
        await send(_INTRO, _intro_markup())
    elif action == "ok":
        await _finish(update, context, sess, send)


async def _finish(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sess: Session, send: Send
) -> None:
    user = update.callback_query.from_user
    registry = context.bot_data.get("profile_registry")
    settings = context.bot_data["settings"]
    persona = core.default_persona_path({"profile_registry": registry}, settings)
    working_dir = registry.get_default().working_dir if registry else None
    agent_id = agent_id_for_working_dir(registry, working_dir, user.id)
    try:
        report = await core.apply_answers(
            with_defaults(sess),
            persona_path=persona,
            settings=settings,
            env_file=_env_file(context),
            memory_writer=context.bot_data.get("agent_memory_writer"),
            memory_agent_id=agent_id,
            profiles_config=settings.profiles_config_path if registry else None,
        )
    except Exception as exc:
        logger.error("Onboarding apply failed", error=str(exc))
        await send(
            f"❌ Не получилось сохранить: {escape_html(str(exc))}. "
            "Попробуй ещё раз: «✅ Сохранить».",
            InlineKeyboardMarkup([[_btn("✅ Сохранить", "onb:ok")]]),
        )
        return
    _sessions(context).pop(user.id, None)
    lines = ["✅ <b>Готово.</b> Дальше — обычный разговор."]
    markup = None
    if report.agents_added:
        lines.append(
            f"Добавлены агенты: {', '.join(report.agents_added)} — "
            "заработают после перезапуска."
        )
        # Same owner-gated restart as /agents → Manage (one mechanism).
        markup = InlineKeyboardMarkup([[_btn("🔄 Перезапустить", "agents:restart")]])
    if report.agents_failed:
        lines.append(f"⚠️ Не добавились: {', '.join(report.agents_failed)}.")
    lines.append("Изменить всё можно через /setup.")
    await send("\n".join(lines), markup)
    audit = context.bot_data.get("audit_logger")
    if audit:
        await audit.log_command(user.id, "setup", [], True)


def _env_file(context: ContextTypes.DEFAULT_TYPE) -> Any:
    from pathlib import Path

    return Path(context.bot_data.get("env_file") or ".env")


# ---------------------------------------------------------------- the gate


async def gate_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, role: str
) -> bool:
    """True = the message was consumed by onboarding (caller returns)."""
    if role != "owner":
        return False
    user_id = update.effective_user.id
    if not (active(context, user_id) or unconfigured(context)):
        return False
    await handle_text(update, context, text)
    return True


async def gate_media(
    update: Update, context: ContextTypes.DEFAULT_TYPE, role: str
) -> bool:
    """Files/photos can't be answers: point at /setup, consume the message."""
    if role != "owner" or not unconfigured(context):
        return False
    await update.message.reply_text("Сначала настройка — отправь текст или /setup.")
    return True
